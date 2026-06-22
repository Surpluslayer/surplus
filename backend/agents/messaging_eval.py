"""agents/messaging_eval.py : a repeatable quality eval for the follow-up composer.

Messaging is the product crux, so we need to MEASURE it, not eyeball it. This is
a fixed-scenario harness: a curated set of realistic situations (with/without
voice, recent update, open next step, a live thread, stale, cold) is run through
the real composer (drafting.compose_from_context), then each draft is scored on:

  * deterministic GATES (hard pass/fail, no model):
      - no_em_dash  : the standing rule -- any em/en dash is an instant fail
      - concise     : <= 55 words (warm note, not an essay)
      - not_generic : no banned filler ("hope this finds you well", "just
                      checking in", "touch base", ...)
  * an LLM JUDGE (1-5) on the things that make a draft good:
      - voice_match   : sounds like the host's own samples (N/A when no voice)
      - specificity   : references THIS person's real facts vs a template
      - correct_intent: takes the right move for the situation
      - natural       : reads human + warm, not salesy/robotic

Run it before and after a prompt/context change to see if quality moved:

    python -m backend.agents.messaging_eval
    python -m backend.agents.messaging_eval --runs 3   # average out LLM variance

It needs ANTHROPIC_API_KEY (compose + judge). No DB, no network beyond the model,
bounded to the case set -> safe to run anytime; not a recurring job.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import voice
from .book import _llm_json
from .drafting import _natural_action, compose_from_context

# ── the scenario set ─────────────────────────────────────────────────────────
# Each case is a self-contained context (synthetic, so the eval is reproducible
# and doesn't depend on prod data). `expect` describes the right move for the
# judge to grade `correct_intent` against.

_CASUAL_VOICE = [
    "hey! so good chatting, lets def grab coffee soon 🙌",
    "yo huge congrats on the raise, so well deserved!!",
    "haha love that, lets make it happen",
]
_FORMAL_VOICE = [
    "Hi Sarah, it was a pleasure connecting at the summit. I'd welcome the "
    "chance to continue our conversation.",
    "Thank you for the thoughtful note. Looking forward to staying in touch.",
]


def _vb(samples: list[str]) -> str:
    if not samples:
        return ""
    return (voice.render_voice_profile_block(voice.build_host_voice_profile(samples))
            + voice.build_style_examples_block(samples))


_CASES = [
    {
        "id": "voiced_job_change",
        "voice": _CASUAL_VOICE,
        "name": "David Osei", "role": "Eng Manager", "company": "Stripe",
        "facts": {"met_at": "SaaStr", "stage": "warm",
                  "latest_update": "Promoted to Director of Engineering"},
        "prior": [], "reason": "Promoted to Director of Engineering",
        "expect": "congratulate on the promotion, lead with it, no hard ask",
    },
    {
        "id": "voiced_open_loop",
        "voice": _CASUAL_VOICE,
        "name": "Priya Nadel", "role": "Partner", "company": "Sequoia",
        "facts": {"met_at": "Milken", "next_step": "send her the diligence memo"},
        "prior": [], "reason": "following up",
        "expect": "deliver on the promised next step (the diligence memo)",
    },
    {
        "id": "novoice_update",
        "voice": [],
        "name": "Marcus Lee", "role": "Founder", "company": "Aria Labs",
        "facts": {"met_at": "NYC Tech Week", "stage": "warm",
                  "latest_update": "Raised a $6M seed round"},
        "prior": [], "reason": "Raised a $6M seed round",
        "expect": "congratulate on the raise; still specific despite no voice samples",
    },
    {
        "id": "they_spoke_last",
        "voice": _CASUAL_VOICE,
        "name": "Sofia Reyes", "role": "Design Lead", "company": "Figma",
        "facts": {"met_at": "Config"},
        "prior": [{"when": "2026-06-01", "who": "them", "channel": "linkedin",
                   "text": "Would love to find time to chat next week if you're around!"}],
        "reason": "following up",
        "expect": "reply to her message; they spoke last; propose a time",
    },
    {
        "id": "stale_reengage",
        "voice": _CASUAL_VOICE,
        "name": "Tom Becker", "role": "VP Finance", "company": "Atlas",
        "facts": {"met_at": "SALT", "stage": "stale"},
        "prior": [], "reason": "reconnecting",
        "expect": "warm re-engagement after time, natural angle, low pressure",
    },
    {
        "id": "formal_contact",
        "voice": _CASUAL_VOICE,
        "name": "Dr. Eleanor Vance", "role": "Managing Director", "company": "Lazard",
        "facts": {"met_at": "the Economic Forum"},
        "prior": [{"when": "2026-05-20", "who": "them", "channel": "email",
                   "text": "Dear host, it was a pleasure. I would welcome continuing our discussion at your convenience."}],
        "reason": "following up",
        "expect": "meet her formal register while keeping the host's identity",
    },
    {
        "id": "cold_just_met",
        "voice": _CASUAL_VOICE,
        "name": "Jordan Kim", "role": "Product Manager", "company": "Notion",
        "facts": {"met_at": "an SF founders dinner"},
        "prior": [], "reason": "just met them tonight",
        "expect": "reference the meeting; warm, no thread to continue",
    },
]


# ── deterministic gates ──────────────────────────────────────────────────────

_DASHES = re.compile(r"[—–‒―−]|(?<=\s)-(?=\s)")
_GENERIC = (
    "hope this finds you well", "hope this email finds you", "hope all is well",
    "hope you're doing well", "hope you are doing well", "just checking in",
    "touch base", "i wanted to reach out", "i hope you're well",
    "circling back", "per my last",
)


def _word_count(s: str) -> int:
    return len(re.findall(r"\b[\w']+\b", s or ""))


def _gates(draft: str) -> dict:
    low = (draft or "").lower()
    wc = _word_count(draft)
    generic = [p for p in _GENERIC if p in low]
    return {
        "no_em_dash": not bool(_DASHES.search(draft or "")),
        "concise": wc <= 55,
        "word_count": wc,
        "not_generic": not generic,
        "generic_hits": generic,
    }


# ── LLM judge ────────────────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are a strict evaluator of outreach-message quality. Score the DRAFT on a "
    "1-5 integer scale (5 = excellent) for each dimension:\n"
    "- voice_match: does it sound like the host's own past messages? If no "
    "<voice_samples> are given, return null (not a number).\n"
    "- specificity: does it reference THIS person's real, given facts (their "
    "update, where they met, the open next step) rather than a generic template?\n"
    "- correct_intent: does it do the RIGHT thing for the situation (see "
    "<expected_move>)?\n"
    "- natural: does it read like a warm human note, not salesy or robotic?\n"
    "Return ONLY JSON: {\"voice_match\":<1-5|null>,\"specificity\":<1-5>,"
    "\"correct_intent\":<1-5>,\"natural\":<1-5>,\"critique\":\"<=15 words\"}"
)


def _judge(case: dict, draft: str) -> dict:
    vs = case.get("voice") or []
    user = (
        (f"<voice_samples>\n" + "\n".join(f"- {s}" for s in vs) + "\n</voice_samples>\n"
         if vs else "No voice samples (voice_match = null).\n")
        + f"Person: {case['name']}, {case['role']} at {case['company']}.\n"
        + f"Known facts: {json.dumps(case.get('facts') or {})}\n"
        + f"Prior thread: {json.dumps(case.get('prior') or [])}\n"
        + f"<expected_move>{case['expect']}</expected_move>\n"
        + f"DRAFT:\n{draft}\n"
    )
    out = _llm_json(_JUDGE_SYSTEM, user, max_tokens=200)
    return out or {}


# ── run ──────────────────────────────────────────────────────────────────────

_SCORE_KEYS = ("voice_match", "specificity", "correct_intent", "natural")


def _eval_case(case: dict) -> dict:
    ctx = {
        "name": case["name"], "role": case["role"], "company": case["company"],
        "prior": case.get("prior") or [],
        "register": voice.detect_register(
            [m.get("text") or "" for m in (case.get("prior") or [])
             if m.get("who") == "them"]),
        "facts": case.get("facts") or {},
        "voice_block": _vb(case.get("voice") or []),
    }
    move = _natural_action(ctx)
    out = compose_from_context(ctx, case.get("reason") or "following up", "linkedin")
    draft = (out or {}).get("body") or ""
    gates = _gates(draft)
    scores = _judge(case, draft) if draft else {}
    return {"id": case["id"], "natural_move": move or "(general)",
            "draft": draft, "gates": gates, "scores": scores}


def run_eval(runs: int = 1, verbose: bool = True) -> dict:
    """Run every case `runs` times; return per-case results + an aggregate. More
    runs averages out the LLM's run-to-run variance for a steadier signal."""
    results = []
    for case in _CASES:
        case_runs = [_eval_case(case) for _ in range(max(1, runs))]
        results.append(case_runs[-1] | {"runs": case_runs})

    # aggregate: gate pass-rates + mean judge scores
    n = len(results)
    gate_pass = {g: 0 for g in ("no_em_dash", "concise", "not_generic")}
    score_sum = {k: 0.0 for k in _SCORE_KEYS}
    score_cnt = {k: 0 for k in _SCORE_KEYS}
    for r in results:
        for rr in r["runs"]:
            for g in gate_pass:
                gate_pass[g] += 1 if rr["gates"].get(g) else 0
            for k in _SCORE_KEYS:
                v = (rr.get("scores") or {}).get(k)
                if isinstance(v, (int, float)):
                    score_sum[k] += v
                    score_cnt[k] += 1
    total_runs = n * max(1, runs)
    agg = {
        "cases": n, "runs_each": max(1, runs),
        "gate_pass_rate": {g: round(gate_pass[g] / total_runs, 2) for g in gate_pass},
        "avg_scores": {k: (round(score_sum[k] / score_cnt[k], 2) if score_cnt[k] else None)
                       for k in _SCORE_KEYS},
    }
    if verbose:
        _print_report(results, agg)
    return {"aggregate": agg, "results": results}


def _print_report(results: list[dict], agg: dict) -> None:
    print("\n=== MESSAGING EVAL ===")
    for r in results:
        s = r.get("scores") or {}
        g = r["gates"]
        flags = []
        if not g["no_em_dash"]:
            flags.append("EM-DASH")
        if not g["concise"]:
            flags.append(f"LONG({g['word_count']}w)")
        if not g["not_generic"]:
            flags.append("GENERIC")
        sc = " ".join(f"{k[:4]}={s.get(k)}" for k in _SCORE_KEYS)
        print(f"\n[{r['id']}] {sc} {'  ⚠ ' + ','.join(flags) if flags else ''}")
        print(f"   move: {r['natural_move'][:60]}")
        print(f"   {r['draft']}")
        if s.get("critique"):
            print(f"   judge: {s['critique']}")
    print("\n--- AGGREGATE ---")
    print("  gate pass-rate:", agg["gate_pass_rate"])
    print("  avg judge scores:", agg["avg_scores"])


if __name__ == "__main__":
    import argparse
    from .. import env_loader
    env_loader.load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    run_eval(runs=ap.parse_args().runs)
