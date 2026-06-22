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


_VB_CACHE: dict = {}


def _vb(samples: list[str]) -> str:
    if not samples:
        return ""
    # V1: the richer TwinVoice profile (surface + LLM-distilled tone/structure/
    # lexical traits + guardrails) followed by the ground-truth examples. The
    # profile build is an LLM call; cache per sample-set so a multi-run eval
    # doesn't re-distill it every run (mirrors the prod sync-time cache).
    key = tuple(samples)
    if key not in _VB_CACHE:
        _VB_CACHE[key] = (voice.render_voice_profile_block(voice.build_host_voice_profile(samples))
                          + voice.build_style_examples_block(samples))
    return _VB_CACHE[key]


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


def _mean(xs: list) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 2) if xs else None


def run_eval(runs: int = 1, verbose: bool = True, dump: Optional[str] = None) -> dict:
    """Run every case `runs` times; return PER-CASE means + an aggregate. More
    runs averages out the LLM's run-to-run variance for a steadier signal.
    `dump` writes a JSON the pairwise comparator can read."""
    per_case = []
    for case in _CASES:
        case_runs = [_eval_case(case) for _ in range(max(1, runs))]
        means = {k: _mean([rr["scores"].get(k) for rr in case_runs]) for k in _SCORE_KEYS}
        gate_fail = {g: sum(1 for rr in case_runs if not rr["gates"].get(g))
                     for g in ("no_em_dash", "concise", "not_generic")}
        per_case.append({"id": case["id"], "means": means, "gate_fail": gate_fail,
                         "drafts": [rr["draft"] for rr in case_runs],
                         "sample_move": case_runs[-1]["natural_move"],
                         "sample_draft": case_runs[-1]["draft"]})

    agg = {"cases": len(per_case), "runs_each": max(1, runs),
           "avg_scores": {k: _mean([c["means"][k] for c in per_case]) for k in _SCORE_KEYS},
           "gate_fails": {g: sum(c["gate_fail"][g] for c in per_case)
                          for g in ("no_em_dash", "concise", "not_generic")}}
    out = {"aggregate": agg, "per_case": per_case}
    if dump:
        with open(dump, "w") as f:
            json.dump(out, f, indent=2)
    if verbose:
        _print_report(per_case, agg)
    return out


def _print_report(per_case: list[dict], agg: dict) -> None:
    print("\n=== MESSAGING EVAL (per-case means) ===")
    print(f"{'case':22} {'voice':6}{'spec':6}{'intent':7}{'natu':6} flags")
    for c in per_case:
        m = c["means"]
        flags = ",".join(f"{g}×{n}" for g, n in c["gate_fail"].items() if n) or "-"
        print(f"{c['id']:22} {str(m['voice_match']):6}{str(m['specificity']):6}"
              f"{str(m['correct_intent']):7}{str(m['natural']):6} {flags}")
        print(f"  → {c['sample_draft']}")
    print("\n--- AGGREGATE ---")
    print("  avg judge scores:", agg["avg_scores"])
    print("  gate fails (lower=better):", agg["gate_fails"])


# ── pairwise old-vs-new (the ceiling-free signal) ────────────────────────────
_PAIR_SYSTEM = (
    "Two outreach drafts, A and B, were written for the SAME situation. Pick the "
    "better one overall, weighing: sounds like the host's own voice, references "
    "the real GIVEN facts (no invented familiarity), does the right thing for the "
    "situation, and reads natural/warm. A small real edge counts; only say tie if "
    "genuinely indistinguishable. Return ONLY JSON: "
    "{\"winner\":\"A\"|\"B\"|\"tie\",\"why\":\"<=15 words\"}"
)


def _case_ctx_text(case: dict) -> str:
    vs = case.get("voice") or []
    return (("Host voice samples: " + " | ".join(vs) + "\n" if vs else "")
            + f"Person: {case['name']}, {case['role']} at {case['company']}.\n"
            + f"Facts: {json.dumps(case.get('facts') or {})}\n"
            + f"Prior thread: {json.dumps(case.get('prior') or [])}\n"
            + f"Expected move: {case['expect']}\n")


def pairwise_compare(baseline_path: str, candidate_path: str, verbose: bool = True) -> dict:
    """Head-to-head: for each case+run, judge baseline vs candidate (A/B order
    randomized per pair to kill position bias). Reports candidate win-rate per
    case + overall. Ceiling-free, so it shows improvement the 1-5 means hide."""
    with open(baseline_path) as f:
        base = {c["id"]: c["drafts"] for c in json.load(f)["per_case"]}
    with open(candidate_path) as f:
        cand = {c["id"]: c["drafts"] for c in json.load(f)["per_case"]}
    by_id = {c["id"]: c for c in _CASES}
    rows, tot = [], {"cand": 0, "base": 0, "tie": 0}
    for cid in base:
        if cid not in cand:
            continue
        w = {"cand": 0, "base": 0, "tie": 0}
        pairs = list(zip(base[cid], cand[cid]))
        for i, (b, c) in enumerate(pairs):
            if not (b.strip() and c.strip()):
                continue
            cand_is_A = (i % 2 == 0)  # alternate position to cancel bias
            a_txt, b_txt = (c, b) if cand_is_A else (b, c)
            user = _case_ctx_text(by_id[cid]) + f"\nDRAFT A:\n{a_txt}\n\nDRAFT B:\n{b_txt}\n"
            out = _llm_json(_PAIR_SYSTEM, user, max_tokens=120) or {}
            win = str(out.get("winner", "tie")).upper()
            if win == "TIE" or win not in ("A", "B"):
                w["tie"] += 1
            elif (win == "A") == cand_is_A:
                w["cand"] += 1
            else:
                w["base"] += 1
        for k in tot:
            tot[k] += w[k]
        rows.append({"id": cid, **w})
    res = {"per_case": rows, "overall": tot}
    if verbose:
        print("\n=== PAIRWISE: candidate (new) vs baseline (old) ===")
        print(f"{'case':22} new  old  tie")
        for r in rows:
            print(f"{r['id']:22} {r['cand']:<4} {r['base']:<4} {r['tie']}")
        n = sum(tot.values()) or 1
        print(f"\n  OVERALL: new wins {tot['cand']}/{n} "
              f"({100*tot['cand']//n}%), old {tot['base']}/{n}, tie {tot['tie']}/{n}")
    return res


if __name__ == "__main__":
    import argparse
    from .. import env_loader
    env_loader.load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--dump", type=str, default=None, help="write results JSON")
    ap.add_argument("--pairwise", nargs=2, metavar=("BASELINE", "CANDIDATE"),
                    help="compare two dumped result JSONs head-to-head")
    args = ap.parse_args()
    if args.pairwise:
        pairwise_compare(args.pairwise[0], args.pairwise[1])
    else:
        run_eval(runs=args.runs, dump=args.dump)
