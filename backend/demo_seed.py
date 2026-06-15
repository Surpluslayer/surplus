"""
demo_seed.py : the seed universe behind the public /demo walkthrough.

One source of truth for two consumers:
  - seed_demo_workspace(db, user) writes real DB rows (an in_person Event +
    captured Prospect rows) so a demo session is a legitimately signed-in,
    non-empty workspace : if the visitor pokes past the guided tour, the book
    isn't blank.
  - build_demo_payload() returns the script the guided coach-mark tour renders
    (capture -> book -> notification). Same people, same copy, so the tour and
    the underlying data never drift.

Nothing here ever sends : demo users have unipile_account_id=NULL, so the send
gate (auth.require_can_send_linkedin) already 402s every real outreach path.
This module only creates local rows + returns JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import Event, Prospect


DEMO_EVENT_LABEL = "SF Tech Week · Founders mixer"
DEMO_EVENT_CITY = "San Francisco"
DEMO_ADVISOR_NAME = "Demo"

# The seed people. `note` is what you "talked about" at the event (the line that
# personalizes the draft); `draft` is the warm note the composer would produce;
# `update` (optional) is the noteworthy signal the notification step fires on.
DEMO_PEOPLE: list[dict] = [
    {
        "key": "maya-chen",
        "name": "Maya Chen",
        "headline": "Founding engineer @ Ramp · ex-Stripe payments",
        "company": "Ramp",
        "role": "Founding Engineer",
        "side": "Builds",
        "works_on": "payments infra",
        "offers": "hard-won infra lessons, intros to fintech eng leads",
        "seeks": "design partners for an internal tooling idea",
        "note": "Swapped war stories about scaling payments infra; she's "
                "noodling on an internal-tools side project.",
        "draft": "Maya — great meeting you at the Founders mixer. Loved your "
                 "take on scaling payments infra at Ramp; the internal-tools "
                 "idea stuck with me. Would love to keep comparing notes — open "
                 "to connecting here?",
        "linkedin_url": "https://www.linkedin.com/in/demo-maya-chen",
        "update": {
            "kind": "job_change",
            "headline": "Maya just started a new role",
            "detail": "Maya Chen → Head of Platform at Ramp. A warm "
                      "congrats now is the perfect reason to reconnect.",
        },
    },
    {
        "key": "deon-okafor",
        "name": "Deon Okafor",
        "headline": "Partner @ Foundry Capital · seed-stage infra & dev tools",
        "company": "Foundry Capital",
        "role": "Partner",
        "side": "Invests",
        "works_on": "seed infra investing",
        "offers": "seed checks, intros to infra founders",
        "seeks": "technical founders in dev tools / infra",
        "note": "He's actively writing seed checks into dev-tools founders and "
                "asked to see anything early in infra.",
        "draft": "Deon — really enjoyed our chat about where seed-stage infra "
                 "is heading. You mentioned wanting to meet technical dev-tools "
                 "founders early — I know a couple worth an intro. Connect and "
                 "I'll send them your way?",
        "linkedin_url": "https://www.linkedin.com/in/demo-deon-okafor",
        "update": None,
    },
    {
        "key": "priya-nair",
        "name": "Priya Nair",
        "headline": "Design lead @ Linear · building the next-gen issue tracker",
        "company": "Linear",
        "role": "Design Lead",
        "side": "Builds",
        "works_on": "product design",
        "offers": "product-design crits, a sharp eye on onboarding flows",
        "seeks": "early users for a side project on team rituals",
        "note": "Talked through onboarding-flow design; she offered to crit our "
                "first-run experience.",
        "draft": "Priya — thanks for the thoughtful crit of our onboarding flow "
                 "at the mixer. I'd genuinely love a closer look when you have a "
                 "spare 20 min. Connecting here so it's easy to pick this back up.",
        "linkedin_url": "https://www.linkedin.com/in/demo-priya-nair",
        "update": {
            "kind": "launch",
            "headline": "Priya's team just shipped",
            "detail": "Linear announced a major redesign Priya led. A quick "
                      "'congrats, this looks incredible' keeps you top of mind.",
        },
    },
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_demo_payload() -> dict:
    """The script the guided tour renders. Pure data : no DB, no network, so the
    tour is deterministic regardless of LLM / provider availability."""
    people = []
    for p in DEMO_PEOPLE:
        people.append({
            "key": p["key"],
            "name": p["name"],
            "headline": p["headline"],
            "company": p["company"],
            "role": p["role"],
            "side": p["side"],
            "note": p["note"],
            "draft": p["draft"],
            "linkedin_url": p["linkedin_url"],
            "update": p.get("update"),
        })
    return {
        "event_label": DEMO_EVENT_LABEL,
        "city": DEMO_EVENT_CITY,
        "advisor_name": DEMO_ADVISOR_NAME,
        "people": people,
    }


def seed_demo_workspace(db, user) -> Event:
    """Create the in_person Event + captured Prospect rows for a demo user.

    Idempotent per user : if this demo user already has a seeded in_person
    event, return it instead of duplicating. Best-effort to be robust on a
    schema that may pre-date some optional columns : every Prospect field used
    here is core / long-present.
    """
    existing = (
        db.query(Event)
        .filter(Event.user_id == user.id, Event.kind == "in_person")
        .first()
    )
    if existing is not None:
        return existing

    now = _utcnow()
    event = Event(
        user_id=user.id,
        kind="in_person",
        label=DEMO_EVENT_LABEL,
        city=DEMO_EVENT_CITY,
    )
    db.add(event)
    db.flush()  # need event.id

    for p in DEMO_PEOPLE:
        db.add(Prospect(
            event_id=event.id,
            identity=f"demo:{p['key']}",
            name=p["name"],
            role=p["role"],
            company=p["company"],
            headline=p["headline"],
            bio=p["headline"],
            side=p["side"],
            works_on=p["works_on"],
            offers=p["offers"],
            seeks=p["seeks"],
            note=p["note"],
            linkedin_url=p["linkedin_url"],
            li_resolved=True,
            sources="inperson",
            source="scan",
            status="pending",
            captured_at=now,
            connection_status="unknown",
        ))

    db.commit()
    db.refresh(event)
    return event
