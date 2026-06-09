"""Live end-to-end smoke of the REAL follow-up draft pipeline.

Unlike scripts/voice_eval.py (which reassembles the prompts itself to ablate the
voice layers), this driver seeds an in-memory DB and runs the ACTUAL production
entry point — ``relationship_agent.run_relationship_agent_concurrent`` — against
live Claude. So it exercises the genuine path end to end:

  Phase 1 triage  → does the real roster + _thread_signals nominate the right
                    people (and NOT the ones whose ball is in the contact's court)?
  Phase 2 draft   → the real _context_brief (Step 1) + _voice_context_block with
                    channel/message_type scoping (Steps 2 & 4) + _strip_dashes,
                    all on the real Sonnet draft call.

It stages proposals only — nothing is ever sent (asserts OutreachLog stays empty).

Three seeded contacts mirror the eval cases, but here they flow through the live
agent:
  - Sarah  : host promised a deck and never sent it  → expect a grounded DRAFT
  - Tom    : just said "let me check my calendar"     → expect a SKIP (their court)
  - Mia    : 62 days quiet, no hook                    → expect a SKIP / no fabrication

Run:
  set -a; . .env; set +a            # load ANTHROPIC_API_KEY
  python3 -m scripts.draft_smoke
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models import Base
from backend import models
from backend.agents import relationships as rel
from backend.agents import relationship_agent as ragent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed_user(db) -> models.User:
    # A casual host voice. Tag one example to a DIFFERENT channel (email) so
    # Step 4's linkedin/warm_followup scoping has something to filter OUT — the
    # follow-up should sound like the LinkedIn examples, not the formal email one.
    voice_examples = [
        {"text": "Hey Sarah! so good meeting you, lets grab coffee soon 🙌",
         "channel": "linkedin"},
        {"text": "hey! thanks so much for the intro, really appreciate it",
         "channel": "linkedin"},
        {"text": "Dear Mr. Patel, thank you for your correspondence. "
                 "I shall revert in due course. Kind regards.",
         "channel": "email"},
    ]
    u = models.User(name="Host", email="host@x.com", unipile_account_id="acct1",
                    voice_examples=json.dumps(voice_examples))
    db.add(u); db.commit()
    return u


def _contact(db, u, ev, *, name, ident, days_since_capture):
    p = models.Prospect(
        event_id=ev.id, identity=ident, name=name, role="", company="",
        linkedin_url=f"https://linkedin.com/in/{ident}", status="pending",
        source="scan",
        captured_at=_utcnow() - timedelta(days=days_since_capture),
        connection_status="connected")
    db.add(p); db.commit()
    return rel.link_contact(db, p, u.id)


def _msg(db, u, contact, who, text, *, days_ago):
    """Append one LinkedIn message to a contact's thread (who: 'host'|'them')."""
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=contact.id,
        source_type="linkedin_outreach", interaction_type="message",
        direction="outbound" if who == "host" else "inbound",
        occurred_at=_utcnow() - timedelta(days=days_ago),
        summary=text, visibility="private"))
    db.commit()


def _seed(db):
    u = _seed_user(db)
    ev = models.Event(user_id=u.id, kind="in_person", label="AI Founders Dinner",
                      city="SF")
    db.add(ev); db.commit()

    # Sarah : host owes the deck (open promise, host spoke last w/ a commitment).
    sarah = _contact(db, u, ev, name="Sarah Lin", ident="sarah", days_since_capture=6)
    _msg(db, u, sarah, "them", "Loved the chat! would you mind sending that deck?",
         days_ago=6)
    _msg(db, u, sarah, "host", "Of course, I'll send it over this week", days_ago=5)

    # Tom : ball is in his court, host already said "no rush" 1 day ago.
    tom = _contact(db, u, ev, name="Tom Reed", ident="tom", days_since_capture=2)
    _msg(db, u, tom, "them", "Sounds great, let me check my calendar and circle back",
         days_ago=2)
    _msg(db, u, tom, "host", "No rush at all, whenever works", days_ago=1)

    # Mia : 62 days quiet after a single opener, no hook to invent.
    mia = _contact(db, u, ev, name="Mia Park", ident="mia", days_since_capture=62)
    _msg(db, u, mia, "host", "Great meeting you at the mixer!", days_ago=62)
    return u


def main() -> None:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set — `set -a; . .env; set +a` first.")
    from anthropic import Anthropic
    client = Anthropic(api_key=key, max_retries=2)

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        u = _seed(db)
        print("Running the REAL relationship agent (live Sonnet) over 3 seeded "
              "contacts…\n")
        res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)

        print("=" * 80)
        print(f"contacts_seen : {res.contacts_seen}")
        print(f"summary       : {res.summary}")
        print(f"stop_reason   : {res.stop_reason}   error: {res.error}")
        print("-" * 80)
        if not res.proposals:
            print("No proposals staged (the agent held back on everyone).")
        for p in res.proposals:
            print(f"\n[{p.kind}] {p.contact_name}")
            print(f"  rationale: {p.rationale}")
            print(f"  text     : {p.text}")
        print("\n" + "-" * 80)
        # SAFETY invariant: a draft run sends nothing.
        sent = db.query(models.OutreachLog).count()
        print(f"OutreachLog rows (must be 0): {sent}")
        print("Expected: Sarah → grounded deck DRAFT; Tom & Mia → SKIP (no nudge).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
