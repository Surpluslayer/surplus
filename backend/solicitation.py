"""solicitation.py : jurisdiction-aware Rule 7.3 gate (scaffolding, not certified).

ABA Model Rule 7.3 restricts a lawyer's live/real-time/targeted solicitation
of a *prospective* client for pecuniary gain. It does not restrict outreach to
someone who is already exempt (an existing or former client, family/close
personal relationship, a fellow lawyer, or a sophisticated business user of
legal services). States adopt the model rule with real variation: which
channels count as "real-time" (near-universally: phone/live chat; email and
async DMs are usually treated as written solicitation, not real-time -- but
this is NOT settled everywhere), whether written solicitation must carry a
disclosure label ("ADVERTISING MATERIAL" or equivalent -- NY/TX/FL each word
this differently), whether there's a cooling-off window for solicitation tied
to a specific triggering event (Florida's 30-day post-accident/disaster rule,
Fla. Bar Reg. 4-7.18(b)(1)(A), is the canonical example), and whether volume/
frequency of solicitation is independently capped.

WHY THIS IS MORE THAN A YES/NO "IS THIS SOLICITATION" CHECK: the same send can
be fine in one state and barred in another, fine on one channel and barred on
another, fine for one relationship type and barred for another, and fine in
isolation but barred as the Nth send in a rolling window. `evaluate()` takes
all four signals and returns a verdict with a reason, not a single boolean --
that's the actual shape of the rule, and collapsing it to one flag would hide
exactly the distinctions that make "got it wrong" a bar complaint instead of a
bounced email.

STATUS: this is architecture, not a certified ruleset. `JURISDICTION_RULES`
below is illustrative -- every value needs verification and sign-off from
licensed counsel in that jurisdiction before this gate can be trusted for real
enforcement. Ship the shape now; populate the table for real launch states
only after legal review, and re-review on any material rule change (state bar
rules are amended more often than most compliance tables assume).

Deliberately pure / side-effect-free (mirrors redaction.py and config.py):
no DB reads, no network calls. Callers resolve jurisdiction, relationship
type, and the rolling-window send count themselves and pass them in -- this
module is the deterministic decision, not the data-gathering. Intended call
site: `pipeline/send/sender.py`, alongside the existing
`automated_send_enabled()` gate, before an autonomous send is dispatched to a
contact who is not yet linked to a Matter/client-status record.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class RelationshipType(str, Enum):
    """The Rule 7.3(a) exemption categories, plus the default "everyone else"
    bucket the rule actually targets. Surplus has no Matter/client-status
    entity yet (see docs/accounts-architecture.md §8) -- callers must supply
    this explicitly today; it cannot be inferred from Contact alone."""
    EXISTING_CLIENT = "existing_client"
    FORMER_CLIENT = "former_client"
    FAMILY_OR_CLOSE_PERSONAL = "family_or_close_personal"
    LAWYER = "lawyer"
    SOPHISTICATED_BUSINESS_USER = "sophisticated_business_user"
    PROSPECT = "prospect"  # unknown / cold -- the only category Rule 7.3 restricts


# Exemption categories are consistent across states under the ABA Model Rule;
# jurisdictions vary on channel/disclosure/cooldown/volume, not on who's
# exempt. Kept as one constant rather than per-jurisdiction to avoid
# implying state-by-state exemption differences this module doesn't verify.
_EXEMPT_RELATIONSHIP_TYPES = frozenset({
    RelationshipType.EXISTING_CLIENT,
    RelationshipType.FORMER_CLIENT,
    RelationshipType.FAMILY_OR_CLOSE_PERSONAL,
    RelationshipType.LAWYER,
    RelationshipType.SOPHISTICATED_BUSINESS_USER,
})

# Channels Surplus actually sends through today (providers/base.py, unipile.py).
# "phone_call" / "live_chat" are included so the rule table has somewhere to
# encode a real-time prohibition even though Surplus doesn't dial phones today.
REALTIME_CHANNELS = frozenset({"phone_call", "live_chat"})
WRITTEN_CHANNELS = frozenset({"email", "linkedin_dm", "whatsapp"})


@dataclass(frozen=True)
class JurisdictionRule:
    state: str  # USPS 2-letter code
    prohibited_realtime_channels: frozenset[str]
    requires_disclosure_label: bool
    disclosure_text: Optional[str]
    sensitive_matter_cooldown_days: int  # 0 = no cooldown rule
    max_solicitations_per_window: Optional[tuple[int, int]]  # (count, days)
    # Pointer to the bar provision this entry was MODELED ON, when one was
    # recorded alongside the values. Deliberately None where no citation was
    # recorded rather than filled in with a plausible-looking rule number: an
    # unverified citation on a compliance record is worse than a visibly
    # missing one, because it reads as authority nobody actually checked.
    # backend/observe/logstream.py surfaces the None case as a gap for
    # counsel to close. Populating these is a legal-review task, not a
    # coding one.
    citation: Optional[str] = None


# ILLUSTRATIVE VALUES ONLY -- see module docstring. Not legal advice, not
# verified against current bar text, not a complete 50-state table.
JURISDICTION_RULES: dict[str, JurisdictionRule] = {
    "FL": JurisdictionRule(
        state="FL",
        prohibited_realtime_channels=REALTIME_CHANNELS,
        requires_disclosure_label=True,
        disclosure_text="ADVERTISEMENT",
        sensitive_matter_cooldown_days=30,  # Fla. Bar Reg. 4-7.18(b)(1)(A) shape
        max_solicitations_per_window=(1, 30),
        # The only citation this table carries, and it was already recorded
        # in-code beside the cooldown value above. Still unverified against
        # current bar text -- see the block comment above this dict.
        citation="Fla. Bar Reg. 4-7.18(b)(1)(A) (shape only, unverified)",
    ),
    "NY": JurisdictionRule(
        state="NY",
        prohibited_realtime_channels=REALTIME_CHANNELS,
        requires_disclosure_label=True,
        disclosure_text="Attorney Advertising",
        sensitive_matter_cooldown_days=0,
        max_solicitations_per_window=None,
    ),
    "CA": JurisdictionRule(
        state="CA",
        prohibited_realtime_channels=REALTIME_CHANNELS,
        requires_disclosure_label=False,
        disclosure_text=None,
        sensitive_matter_cooldown_days=0,
        max_solicitations_per_window=None,
    ),
}

# Fail-closed default for any state not yet in the table above: no known
# jurisdiction, so treat every non-exempt send as the most restrictive case
# rather than silently allowing it. Mirrors the fail-closed pattern already
# used for webhook signature verification (routes/webhooks.py).
_UNKNOWN_JURISDICTION_RULE = JurisdictionRule(
    state="UNKNOWN",
    prohibited_realtime_channels=REALTIME_CHANNELS,
    requires_disclosure_label=True,
    disclosure_text="ADVERTISEMENT",
    sensitive_matter_cooldown_days=30,
    max_solicitations_per_window=(1, 30),
)


@dataclass(frozen=True)
class SolicitationContext:
    """Everything the gate needs, resolved by the caller. `evaluate()` does
    not query the DB or infer any of these -- see the module docstring on
    where each signal would need to come from."""
    jurisdiction: str  # USPS 2-letter code of the governing state bar
    channel: str
    relationship_type: RelationshipType
    matter_is_sensitive: bool = False  # e.g. personal injury / disaster-adjacent
    triggering_event_date: Optional[date] = None  # for cooldown calc
    today: Optional[date] = None
    recent_solicitation_count_in_window: int = 0


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    requires_disclosure_label: bool = False
    disclosure_text: Optional[str] = None


def evaluate(ctx: SolicitationContext) -> Verdict:
    """Deterministic. No LLM in this path -- a send-gating decision with bar-
    complaint consequences should not depend on model sampling."""
    if ctx.relationship_type in _EXEMPT_RELATIONSHIP_TYPES:
        return Verdict(allowed=True, reason=f"exempt: {ctx.relationship_type.value}")

    rule = JURISDICTION_RULES.get(ctx.jurisdiction.upper(), _UNKNOWN_JURISDICTION_RULE)

    if ctx.channel in rule.prohibited_realtime_channels:
        return Verdict(
            allowed=False,
            reason=f"real-time channel '{ctx.channel}' barred for solicitation in {rule.state}",
        )

    if ctx.matter_is_sensitive and rule.sensitive_matter_cooldown_days > 0:
        today = ctx.today or date.today()
        if ctx.triggering_event_date is None:
            return Verdict(
                allowed=False,
                reason=f"sensitive matter with no triggering-event date; "
                       f"{rule.state} requires a {rule.sensitive_matter_cooldown_days}-day cooldown",
            )
        elapsed = (today - ctx.triggering_event_date).days
        if elapsed < rule.sensitive_matter_cooldown_days:
            return Verdict(
                allowed=False,
                reason=f"within {rule.state}'s {rule.sensitive_matter_cooldown_days}-day "
                       f"cooldown ({elapsed}d elapsed)",
            )

    if rule.max_solicitations_per_window is not None:
        cap, window_days = rule.max_solicitations_per_window
        if ctx.recent_solicitation_count_in_window >= cap:
            return Verdict(
                allowed=False,
                reason=f"{rule.state} caps solicitation at {cap} per {window_days}d "
                       f"({ctx.recent_solicitation_count_in_window} already sent)",
            )

    return Verdict(
        allowed=True,
        reason=f"permitted written solicitation in {rule.state}",
        requires_disclosure_label=rule.requires_disclosure_label,
        disclosure_text=rule.disclosure_text,
    )
