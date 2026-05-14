"""
agents/outreach.py — stage 03b, autonomous outreach.

Every prospect above the floating threshold is contacted with no human in the
loop. The agent:
  1. composes a message from the goal's outreach template + a composition
     reveal (who else is already in), personalized on the prospect's domain;
  2. "sends" it and simulates the funnel — higher fit converts at a higher
     rate through opened -> replied.

`run_outreach` returns, per prospect, the ordered outreach events and the
prospect's resulting status (contacted | rsvp). The pipeline persists them.

The funnel uses a seeded RNG so a given event always replays identically.
"""
from __future__ import annotations
import random
from datetime import datetime, timedelta, timezone

from .. import config


def compose(p, event, peers: list[str]) -> str:
    """Build one personalized outreach message with a composition reveal."""
    framing = config.goal_cfg(event.goal)["outreach"].format(
        headcount=event.headcount,
        format=event.format.lower(),
        city=event.city,
        seniority=event.seniority.lower(),
        role=event.role.lower(),
        co_stage=event.co_stage,
    )
    first = p.name.split()[0]
    reveal = ""
    if peers:
        names = " and ".join(n.split()[0] for n in peers[:2])
        reveal = f" {names} are already in."
    return (
        f"Hi {first} — pulling together {framing}. "
        f"Given your work on {p.works_on.replace('-', ' ')}, thought the room "
        f"would be worth your time.{reveal}"
    )


def run_outreach(prospects, event, rng: random.Random | None = None):
    """
    Autonomously contact every above-threshold prospect.

    Returns: list of (prospect, outreach_events, status) where outreach_events
    is a list of {"state", "body", "ts"} dicts in send order.
    """
    rng = rng or random.Random(event.id or 0)
    confirmed = [p for p in prospects if p.fit_score >= event.threshold]
    all_names = [p.name for p in confirmed]
    results = []

    for p in confirmed:
        peers = [n for n in all_names if n != p.name]
        body = compose(p, event, peers)
        now = datetime.now(timezone.utc)
        events = [{"state": "sent", "body": body, "ts": now}]

        # higher fit -> higher open + reply rates
        if rng.random() < min(0.97, 0.55 + p.fit_score / 200):
            events.append({"state": "opened", "body": "", "ts": now + timedelta(hours=2)})
            if rng.random() < min(0.90, 0.30 + p.fit_score / 160):
                events.append(
                    {"state": "replied", "body": "RSVP confirmed", "ts": now + timedelta(hours=6)}
                )

        status = "rsvp" if any(e["state"] == "replied" for e in events) else "contacted"
        results.append((p, events, status))

    return results
