"""
Offline eval harness for the follow-up drafting voice + grounding stack.

The voice work (context_brief, host_voice_profile) is meant to improve two
things at once: GROUNDING (the draft only asserts facts the thread supports) and
VOICE (the draft sounds like the host). This harness makes that measurable by
A/B/C/D-ablating the two new layers and scoring the resulting drafts, so a future
change can be judged instead of eyeballed.

Variants (the two layers toggled independently):
  A  baseline      : style_examples only (pre-Step-1/2 behavior)
  B  +brief        : + the deterministic <context_brief>
  C  +profile      : + the distilled <host_voice_profile>
  D  +both         : the current production stack

Two modes:
  - deterministic (default): builds the exact prompts each variant would send and
    runs the structural scorer over a CANNED draft per case. No API key, no cost,
    CI-safe. Proves the harness wires the variants correctly and the scorer
    catches the failure modes (dash leak, hallucinated fact, wrong length/voice).
  - live (--live): actually calls the model per variant and scores the real
    drafts. Needs ANTHROPIC_API_KEY. This is the one you run to compare variants.

Run:
  python3 -m scripts.voice_eval            # deterministic, prints the scorecard
  python3 -m scripts.voice_eval --live     # real Anthropic calls (load .env first)

The scorer + variant builder are importable (tests/test_voice_eval.py exercises
them without the API), so the structural metrics stay honest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.agents import voice
from backend.agents import relationship_agent as ragent


# ── Variants ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    use_brief: bool
    use_profile: bool


VARIANTS = [
    Variant("A", "baseline", False, False),
    Variant("B", "+brief", True, False),
    Variant("C", "+profile", False, True),
    Variant("D", "+both", True, True),
]


# ── Eval cases ────────────────────────────────────────────────────────────────

@dataclass
class EvalCase:
    name: str
    host_examples: list[str]          # the host's voice exemplars
    sel: dict                         # triage selection {reason, angle}
    ctx: dict                         # {summary, events, prior_messages}
    must_reference: list[str] = field(default_factory=list)  # the real hook
    forbidden_facts: list[str] = field(default_factory=list)  # must NOT invent
    canned_draft: str = ""            # used by the deterministic scorer demo
    # The right KIND of action for this case (scored as outcome_ok):
    #   "draft"  : there's a clear owed/open action — the agent SHOULD draft+send
    #   "skip"   : the ball is in their court / too soon — the agent SHOULD hold
    #   "either" : both a grounded draft and a hold are defensible — not graded
    expect: str = "draft"


def _msg(who: str, text: str, *, days_ago: int = 0) -> dict:
    return {"when": datetime.now(timezone.utc) - timedelta(days=days_ago),
            "who": who, "channel": "linkedin", "text": text}


def cases() -> list[EvalCase]:
    return [
        EvalCase(
            name="owed_resource_casual_host",
            host_examples=[
                "Hey Sarah! so good meeting you, lets grab coffee soon 🙌",
                "hey, you free next week? would love to catch up!",
                "Hey! thanks so much for the intro, really appreciate it",
            ],
            sel={"reason": "host promised the deck", "angle": "send the deck"},
            ctx={
                "summary": {"name": "Sarah Lin", "company": "Acme",
                            "relationship_stage": "replied", "next_step": "",
                            "last_touch_at": datetime.now(timezone.utc) - timedelta(days=5)},
                "events": [{"name": "AI Founders Dinner"}],
                "prior_messages": [
                    _msg("them", "Loved the chat! would you mind sending that deck?", days_ago=6),
                    _msg("host", "Of course, I'll send it over this week", days_ago=5),
                ],
            },
            must_reference=["deck"],
            forbidden_facts=["series a", "funding", "promotion", "new job",
                             "congrats on the round"],
            expect="draft",  # host owes the deck — the agent should send it
            canned_draft="Hey Sarah! Meant to get this over sooner, "
                         "here's the deck I mentioned. Let me know what you think 🙌",
        ),
        EvalCase(
            name="their_court_should_be_brief_aware",
            host_examples=[
                "Hi Daniel, thank you for the thoughtful note. Looking forward to it.",
                "Hi there, appreciate you reaching out. Best regards.",
            ],
            sel={"reason": "recent thread", "angle": "keep momentum"},
            ctx={
                "summary": {"name": "Tom Reed", "company": "", "relationship_stage": "contacted",
                            "next_step": "", "last_touch_at": datetime.now(timezone.utc) - timedelta(days=1)},
                "events": [],
                "prior_messages": [
                    _msg("them", "Sounds great, let me check my calendar and circle back", days_ago=2),
                    _msg("host", "No rush at all, whenever works", days_ago=1),
                ],
            },
            must_reference=[],
            forbidden_facts=["raised", "launched", "acquired"],
            expect="skip",  # ball is in Tom's court, host just said "no rush" — hold
            # The canned draft simulates the FAILURE mode (an over-eager nudge),
            # so deterministic mode shows outcome_ok flagging the wrong action.
            canned_draft="Just bumping this to the top of your inbox, "
                         "any thoughts on times?",
        ),
        EvalCase(
            name="stale_reconnect_no_history_to_invent",
            host_examples=[
                "Hey! been a minute, how are things going?",
                "hey, was just thinking about our chat, hope youre well!",
            ],
            sel={"reason": "gone quiet 60d", "angle": "reconnect"},
            ctx={
                "summary": {"name": "Mia Park", "company": "Northwind",
                            "relationship_stage": "stale", "next_step": "",
                            "last_touch_at": datetime.now(timezone.utc) - timedelta(days=62)},
                "events": [{"name": "ML Infra Mixer"}],
                "prior_messages": [
                    _msg("host", "Great meeting you at the mixer!", days_ago=62),
                ],
            },
            must_reference=[],
            forbidden_facts=["series a", "new role", "congrats", "saw you raised",
                             "your launch"],
            expect="either",  # a grounded reconnect OR a hold are both defensible;
                              # the real test here is grounding (invent nothing)
            canned_draft="Hey Mia! been a minute since the mixer, "
                         "how are things going at Northwind?",
        ),
        # ── Adversarial: where the NEW stack should pull ahead of the OLD ──────
        # Distinctive lowercase/no-greeting/emoji voice. The old stack sees the
        # raw examples but tends to "clean them up" into a polished "Hey Name!";
        # the distilled <host_voice_profile> states "often starts lowercase / no
        # greeting" as an explicit rule, so D should keep the quirk and A drift.
        EvalCase(
            name="quirky_lowercase_voice",
            host_examples=[
                "yep on it, sending those notes over in a sec 🔥",
                "haha no worries at all, hit me whenever ✌️",
                "ok bet, lmk what works and we'll lock it in 🙏",
            ],
            sel={"reason": "host owes the notes", "angle": "send the notes"},
            ctx={
                "summary": {"name": "Devon", "company": "", "relationship_stage": "replied",
                            "next_step": "",
                            "last_touch_at": datetime.now(timezone.utc) - timedelta(days=3)},
                "events": [{"name": "Hack Night"}],
                "prior_messages": [
                    _msg("them", "this was awesome, can you send those notes you mentioned?", days_ago=4),
                    _msg("host", "yeah for sure, will get those to you 🔥", days_ago=3),
                ],
            },
            must_reference=["notes"],
            forbidden_facts=["funding", "raised", "new role"],
            expect="draft",
            canned_draft="yep sending those notes over now, lmk what you think 🔥",
        ),
        # Grounding temptation: triage hands an angle that the thread does NOT
        # support ("congratulate on the new role"), but nothing in prior_messages
        # mentions a role change. The old stack has only the angle pushing it to
        # assert the fact; the new <context_brief> flags the angle as uncertain
        # under facts_to_avoid, so D should reconnect WITHOUT inventing the news.
        EvalCase(
            name="angle_tempts_unsupported_fact",
            host_examples=[
                "Hi Priya, great catching up. Let's find time soon.",
                "Hi Marcus, good to reconnect. Talk soon.",
            ],
            sel={"reason": "saw they got promoted", "angle": "congratulate on the new role"},
            ctx={
                "summary": {"name": "Lena Ortiz", "company": "Vela",
                            "relationship_stage": "stale", "next_step": "",
                            "last_touch_at": datetime.now(timezone.utc) - timedelta(days=40)},
                "events": [{"name": "Founders Brunch"}],
                "prior_messages": [
                    _msg("host", "Really enjoyed meeting you at the brunch!", days_ago=40),
                    _msg("them", "Likewise! let's stay in touch", days_ago=39),
                ],
            },
            must_reference=[],
            forbidden_facts=["congrats on the new role", "congratulations on the promotion",
                             "new role", "promotion", "saw you got promoted"],
            expect="either",  # reconnect or hold both fine; the test is GROUNDING
            canned_draft="Hi Lena! been a while since the brunch, "
                         "would love to find time to catch up soon.",
        ),
    ]


# ── Prompt assembly per variant (mirrors prod, ablation-controlled) ───────────

def build_prompts(case: EvalCase, variant: Variant) -> dict:
    """Return {"system", "user"} exactly as the given variant would send them.

    Reuses the real building blocks (voice.*, ragent._context_brief) so the eval
    tracks production, but assembles them here so each layer can be toggled
    without touching prod code."""
    examples = case.host_examples
    system = ragent._DRAFT_SYSTEM
    if variant.use_profile:
        system += voice.render_voice_profile_block(
            voice.build_host_voice_profile(examples))
    system += voice.build_style_examples_block(examples)

    sel, ctx = case.sel, case.ctx
    name = (ctx.get("summary") or {}).get("name") or "them"
    user = (f"Follow up with {name}.\n\n"
            "<triage_signal>\n"
            f"Triage flagged them because: {sel.get('reason')}\n"
            f"Suggested angle: {sel.get('angle')}\n"
            "</triage_signal>\n\n")
    if variant.use_brief:
        brief = ragent._context_brief(sel, ctx)
        user += ("<context_brief>\n"
                 "Deterministic pre-read; prior_messages wins on conflict.\n"
                 + json.dumps(brief, default=str) + "\n</context_brief>\n\n")
    user += ("<full_context_json>\n" + json.dumps(ctx, default=str)
             + "\n</full_context_json>")
    return {"system": system, "user": user}


# ── Structural scorer ─────────────────────────────────────────────────────────

_DASH_RE = re.compile(r"[—–―−]|\s-\s")
_EMOJI_RE = voice._EMOJI_RE
_BAND_MAX = {"short": 35, "medium": 70, "long": 120}

# The live draft path prefixes non-draft tool outcomes; everything else is a
# real outgoing draft. Classifying this matters because a skip's "reason" is
# internal reasoning that is NEVER sent — scoring its dashes/length/greeting as
# if it were a draft (the bug the first live run exposed) penalizes the agent
# for correctly holding back.
_OUTCOME_MARKERS = {"[skip]": "skip", "[next_step]": "next_step",
                    "[no tool call]": "none"}


def _classify_outcome(text: str) -> str:
    for marker, name in _OUTCOME_MARKERS.items():
        if text.startswith(marker):
            return name
    return "draft"


def score_draft(draft: str, case: EvalCase, profile: Optional[dict]) -> dict:
    """Deterministic, API-free metrics over a single agent outcome.

    First classifies the OUTCOME (draft / skip / next_step / none), because the
    voice + structure metrics only apply to an actual outgoing draft:
      - outcome_ok: took the right KIND of action for this case (draft vs hold)
      - grounded: asserts none of the case's forbidden (un-sourced) facts —
        graded on ANY outcome, since a skip reason that invents a fact is a tell
      - dash_clean: no em/en/spaced-hyphen 'AI tell' leaked      (draft only)
      - references_hook: mentions the real open-loop hook         (draft only)
      - length_ok: within the profile's length band              (draft only)
      - greeting_match: opens in the host's greeting style        (draft only)
      - emoji_match: emoji presence matches the host's habit      (draft only)
    Non-applicable metrics are None and excluded from the 0..1 `score`."""
    text = (draft or "").strip()
    low = text.lower()
    outcome = _classify_outcome(text)
    is_draft = outcome == "draft"
    m: dict[str, Any] = {"outcome": outcome}

    # Right kind of action? "either" cases don't grade this (both are fine).
    if case.expect == "draft":
        m["outcome_ok"] = is_draft
    elif case.expect == "skip":
        m["outcome_ok"] = outcome in ("skip", "next_step")
    else:  # "either"
        m["outcome_ok"] = None

    # Grounding applies regardless of outcome.
    m["grounded"] = not any(f.lower() in low for f in case.forbidden_facts)

    if is_draft:
        m["dash_clean"] = not bool(_DASH_RE.search(text))
        m["references_hook"] = (any(h.lower() in low for h in case.must_reference)
                                if case.must_reference else None)
        words = len(re.findall(r"\b[\w']+\b", text))
        if profile:
            m["length_ok"] = words <= _BAND_MAX.get(profile["length_band"], 120)
            g = (profile.get("greeting") or "")
            m["greeting_match"] = (low.startswith(g) if g else None)
            m["emoji_match"] = bool(_EMOJI_RE.search(text)) == profile["uses_emoji"]
        else:
            m["length_ok"] = words <= 70
            m["greeting_match"] = None
            m["emoji_match"] = None
    else:
        # A held message: voice/structure metrics don't apply to the reasoning.
        m["dash_clean"] = m["references_hook"] = None
        m["length_ok"] = m["greeting_match"] = m["emoji_match"] = None

    graded = [v for k, v in m.items()
              if k != "outcome" and isinstance(v, bool)]
    m["score"] = round(sum(graded) / len(graded), 3) if graded else 0.0
    return m


# ── Live model call (only in --live) ──────────────────────────────────────────

def _draft_live(prompts: dict, client: Any) -> str:
    """One model call for a variant; returns the draft text (or a skip marker).
    Uses the same draft tools as prod so a skip is a real, scoreable outcome."""
    resp = client.messages.create(
        model=ragent._AGENT_MODEL,
        max_tokens=ragent._DRAFT_MAX_TOKENS,
        system=prompts["system"],
        tools=ragent._DRAFT_TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompts["user"]}],
    )
    for tu in ragent._tool_uses(resp):
        name = ragent._tu_name(tu)
        inp = ragent._tu_input(tu)
        if name == "draft_message":
            return ragent._strip_dashes(inp.get("message") or "")
        if name == "propose_next_step":
            return "[next_step] " + ragent._strip_dashes(inp.get("next_step") or "")
        if name == "skip_contact":
            return "[skip] " + (inp.get("reason") or "")
    return "[no tool call]"


# ── Runner / reporting ────────────────────────────────────────────────────────

def _fmt(v: Any) -> str:
    if v is True:
        return "  ok"
    if v is False:
        return "FAIL"
    if v is None:
        return "   -"
    return f"{v:>4}"


def run(live: bool = False) -> None:
    client = None
    if live:
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise SystemExit("ANTHROPIC_API_KEY not set — `set -a; . .env; set +a` first.")
        from anthropic import Anthropic
        client = Anthropic(api_key=key, max_retries=2)

    metrics_order = ["outcome", "outcome_ok", "grounded", "references_hook",
                     "dash_clean", "length_ok", "greeting_match", "emoji_match",
                     "score"]
    header = f"{'variant':<12}" + "".join(f"{m:>14}" for m in metrics_order)

    for case in cases():
        profile = voice.build_host_voice_profile(case.host_examples)
        print("\n" + "=" * 96)
        print(f"CASE: {case.name}")
        print(f"  hook={case.must_reference or '(none)'}  "
              f"forbidden={case.forbidden_facts}")
        print(f"  profile: greeting={profile['greeting']} band={profile['length_band']} "
              f"emoji={profile['uses_emoji']} tone={profile['formality']}")
        print("-" * 96)
        print(header)
        for variant in VARIANTS:
            prompts = build_prompts(case, variant)
            draft = (_draft_live(prompts, client) if live else case.canned_draft)
            sc = score_draft(draft, case, profile if variant.use_profile else None)
            row = f"{variant.key} {variant.label:<10}" + "".join(
                f"{_fmt(sc[m]):>14}" for m in metrics_order)
            print(row)
            if live:
                print(f"   draft: {draft}")
        print("-" * 96)
        if not live:
            print(f"  (deterministic mode: scored the same canned draft across "
                  f"variants to demo the scorer — run --live for real drafts)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="make real Anthropic calls instead of scoring canned drafts")
    args = ap.parse_args()
    run(live=args.live)


if __name__ == "__main__":
    main()
