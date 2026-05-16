"""
agents/roi.py — stage 05, verified settlement.

ROI settles against the goal set at intake. For each confirmed guest it derives
a conversion tier from verified fit, maps that to the goal's outcome
(won / partial / lost) and dollar value, and rolls the whole thing up into the
ledger + aggregate metrics.

settle() -> (ledger, metrics)
  ledger  : one row per guest — the deliverable. "Who actually converted."
  metrics : net ROI %, value generated vs budget, the invited->converted funnel.

NOTE — open design question: tier_of() maps fit score straight to outcome
tier. That is a *prediction*, not verification. The trustworthy version reads
real 30/60/90-day follow-up data per guest; this is the placeholder that lets
the rest of the pipeline run end to end.
"""
from __future__ import annotations

from .. import config


def tier_of(score: int) -> str:
    """Verified-fit score -> conversion tier."""
    if score >= 90:
        return "high"
    if score >= 82:
        return "mid"
    return "low"


def linkedin_outreach_stats(event) -> dict:
    """
    Roll up the LinkedIn outreach funnel for the event: how many connection
    invites went out, how many were accepted, how many post-accept DMs the agent
    sent, and how many got replies. Acceptance and response rates are derived.

    Walks event.prospects[].outreach where channel == "linkedin". Tests that pass
    a SimpleNamespace event (no .prospects) get zeroed counts.
    """
    prospects = getattr(event, "prospects", None) or []
    sent = accepted = msg_sent = msg_replied = 0
    for p in prospects:
        for o in getattr(p, "outreach", None) or []:
            if getattr(o, "channel", None) != "linkedin":
                continue
            s = getattr(o, "state", None)
            if s == "invite_sent":
                sent += 1
            elif s == "invite_accepted":
                accepted += 1
            elif s == "message_sent":
                msg_sent += 1
            elif s == "message_replied":
                msg_replied += 1
    return {
        "li_invites_sent": sent,
        "li_invites_accepted": accepted,
        "li_messages_sent": msg_sent,
        "li_messages_replied": msg_replied,
        "li_acceptance_rate_pct": round(accepted / sent * 100) if sent else 0,
        "li_response_rate_pct": round(msg_replied / msg_sent * 100) if msg_sent else 0,
    }


def settle(event, attending: list) -> tuple[list[dict], dict]:
    """Build the per-guest conversion ledger and the aggregate ROI metrics."""
    gcfg = config.goal_cfg(event.goal)
    tiers, values = gcfg["tiers"], gcfg["value"]

    ledger: list[dict] = []
    for p in attending:
        tier = tier_of(p.fit_score)
        outcome = tiers[tier]
        value = values[outcome["state"]]
        ledger.append({
            "prospect_id": p.id,
            "name": p.name,
            "company": p.company,
            "side": p.side,
            "goal": event.goal,
            "tier": tier,
            "state": outcome["state"],
            "label": outcome["label"],
            "detail": outcome["detail"],
            "value": value,
        })

    ledger.sort(key=lambda r: -r["value"])
    value_generated = sum(r["value"] for r in ledger)
    converted = sum(1 for r in ledger if r["state"] == "won")
    invited = round(event.headcount / config.FUNNEL_CONVERSION)

    metrics = {
        "ledger_head": gcfg["ledger_head"],
        "goal": event.goal,
        "invited": invited,
        "attended": len(attending),
        "converted": converted,
        "value_generated": value_generated,
        "budget": event.budget,
        "net_roi_pct": round((value_generated - event.budget) / event.budget * 100)
        if event.budget else 0,
        **linkedin_outreach_stats(event),
    }
    return ledger, metrics
