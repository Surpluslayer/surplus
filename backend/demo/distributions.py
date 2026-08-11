"""backend/demo/distributions.py : the calibration layer.

BE HONEST ABOUT WHAT THIS IS. Two different kinds of number live here, and
they are not the same kind of claim:

  1. REAL, IN-CODE PRODUCT DESIGN CONSTANTS -- the cadence the product itself
     is already built around: due_contacts' VIP=daily/others=weekly tiering
     (agents/relationship/updates_engine.py), the hourly/6-hourly sweep
     intervals (updates_scheduler.py: UPDATES_SWEEP_GAP_SECONDS=3600,
     GATHERING_SWEEP_GAP_SECONDS=21600), the follow-up cadence
     (config.py: FOLLOWUP_DELAY_HOURS=12). These aren't measured behavior,
     but they ARE real -- they're the rhythm the product was designed to
     support, which is the closest honest proxy available in this
     environment for "how often does a lawyer plausibly check in".

  2. DOCUMENTED ASSUMPTIONS -- everything this file can't derive from #1,
     because the actual source (PostHog click/session telemetry; the
     agents/usage.py LLM-call ledger) is either external and unreachable
     from this environment, or -- in usage.py's case -- was found to be
     dead code (models.LlmUsage does not exist, install() is never called;
     see the session that built this package). These are reasonable,
     clearly-labeled guesses, not measurements. Replace them the moment
     real telemetry is reachable -- that's the entire point of keeping them
     in one file instead of scattered through cohort.py.

Every constant below is commented with which of the two it is.
"""
from __future__ import annotations

# ── #1: real, in-code product design constants ──────────────────────────────

# updates_engine.due_contacts(): vip -> daily, everyone else -> weekly.
VIP_CHECK_IN_DAYS = 1
STANDARD_CHECK_IN_DAYS = 7

# updates_scheduler.py defaults.
UPDATES_SWEEP_GAP_HOURS = 1
GATHERING_SWEEP_GAP_HOURS = 6

# config.py (events side; still the only in-repo follow-up cadence constant).
FOLLOWUP_DELAY_HOURS = 12

# messaging_eval._CASES' own category set -- the real, already-in-use
# taxonomy of "what kind of relationship-state situation a draft gets
# written for". Reused here as the shape of contact-state diversity in the
# generated cohort, rather than inventing a parallel taxonomy.
CONTACT_STATE_CATEGORIES = (
    "voiced_recent_update", "voiced_open_loop", "novoice_recent_update",
    "they_spoke_last", "stale_reengage", "formal_contact", "cold_just_met",
)

# updates_engine._DRAFTWORTHY_KINDS -- the real signal-kind taxonomy that
# actually triggers an autodraft.
DRAFTWORTHY_SIGNAL_KINDS = ("job_change", "new_post")

# ── #2: documented assumptions (no live telemetry reachable) ────────────────

# Fraction of a lawyer's book that's VIP-tier. No measured figure exists;
# this is a plausible "power users star a minority of contacts" assumption,
# not a derived one.
ASSUMED_VIP_FRACTION = 0.15

# Sessions per active week, log-normal-ish shape via (mean, spread) rather
# than a point estimate, so generated users don't all log in identically.
# Anchored loosely to the VIP/standard check-in cadence above (a VIP-heavy
# user checks in closer to daily) but the exact session COUNT per week is
# assumed, not measured.
ASSUMED_SESSIONS_PER_WEEK = {"light": (1, 1), "regular": (3, 1.5), "power": (6, 2)}
# Mix of the three usage tiers across an 80-lawyer cohort. Assumed shape
# (most users regular, a tail of light and power users) -- a common product
# usage distribution in general, not one measured for this product.
USER_TIER_WEIGHTS = {"light": 0.30, "regular": 0.55, "power": 0.15}

# Within one session, how many of each action a user does. Assumed
# sequence-shape, but the ORDER (feed first, then investigate, then act) is
# structurally real: it mirrors book.py's own "today feed surfaces
# draft-bearing updates first" design, not a guess about UI flow.
SESSION_ACTION_RANGE = {
    "view_today_feed": (1, 1),      # always exactly once, first
    "investigate_contact": (0, 3),
    "generate_draft": (0, 2),
    "edit_draft": (0, 2),           # never more than generate_draft this session
    "search": (0, 2),
}

# Draft disposition: send-as-drafted vs edit-then-send vs discard. Assumed;
# this is exactly the distribution the moat memo's drafting-quality flywheel
# would replace with the real one, the moment real approve/edit logging
# exists (see the memo's section 4/5 -- this cohort's drafts are the
# stand-in for that not-yet-collected feedback data).
DRAFT_DISPOSITION_WEIGHTS = {"approved_as_is": 0.45, "edited_then_sent": 0.40, "discarded": 0.15}
