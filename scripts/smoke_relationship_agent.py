"""
Live smoke test for the agentic relationship loop.

Seeds an in-memory spine with a couple of contacts (one clearly stale, one
freshly touched) and runs the REAL relationship agent against the Anthropic
API. Prints the full tool-by-tool transcript + the staged proposals so we can
eyeball whether the loop actually reasons well — the unit tests only prove the
mechanics with a scripted client.

Run:  python3 -m scripts.smoke_relationship_agent
Reads ANTHROPIC_API_KEY from the environment (load .env first).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import relationships as rel
from backend.agents import agent_loop
from backend.agents.relationship_agent import run_relationship_agent


def _seed(db):
    u = models.User(name="Daniel Wang", email="daniel@abundant.ai",
                    unipile_account_id="acct_demo")
    db.add(u); db.commit()

    seed_dinner = models.Event(user_id=u.id, kind="in_person",
                               label="Seed-Stage Founders Dinner", city="SF")
    ml_mixer = models.Event(user_id=u.id, kind="in_person",
                            label="ML Infra Mixer", city="SF")
    last_week = models.Event(user_id=u.id, kind="in_person",
                             label="AI Tinkerers", city="SF")
    db.add_all([seed_dinner, ml_mixer, last_week]); db.commit()

    now = datetime.now(timezone.utc)

    # Maya: met at TWO events, last touched 45 days ago -> clearly stale,
    # strong relationship, no next step. The agent should prioritise her.
    maya1 = models.Prospect(
        event_id=seed_dinner.id, identity="maya-rodriguez", name="Maya Rodriguez",
        role="Staff Infra Engineer", company="Loom91",
        linkedin_url="https://linkedin.com/in/maya-rodriguez",
        status="contacted", source="scan", connection_status="connected",
        captured_at=now - timedelta(days=70),
        note="Building internal ML platform; cares about eval tooling.")
    maya2 = models.Prospect(
        event_id=ml_mixer.id, identity="maya-rodriguez", name="Maya Rodriguez",
        role="Staff Infra Engineer", company="Loom91",
        linkedin_url="https://linkedin.com/in/maya-rodriguez",
        status="replied", source="scan", connection_status="connected",
        captured_at=now - timedelta(days=45))
    # Priya: met once, also infra/eval focused -> a plausible intro target.
    priya = models.Prospect(
        event_id=seed_dinner.id, identity="priya-nair", name="Priya Nair",
        role="Founder, eval tooling startup", company="RubricAI",
        linkedin_url="https://linkedin.com/in/priya-nair",
        status="contacted", source="scan", connection_status="connected",
        captured_at=now - timedelta(days=30),
        note="Just raised seed for an LLM eval product.")
    # Jordan: touched THREE days ago -> fresh, agent should leave alone.
    jordan = models.Prospect(
        event_id=last_week.id, identity="jordan-lee", name="Jordan Lee",
        role="PM", company="Northwind",
        linkedin_url="https://linkedin.com/in/jordan-lee",
        status="replied", source="scan", connection_status="connected",
        captured_at=now - timedelta(days=3))
    db.add_all([maya1, maya2, priya, jordan]); db.commit()

    for p in (maya1, maya2, priya, jordan):
        rel.link_contact(db, p, u.id)
    return u


def main():
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set — `set -a; . .env; set +a` first.")

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    u = _seed(db)

    print(f"Model: {agent_loop.DEFAULT_MODEL}")
    print(f"Seeded {len(rel.list_contacts(db, u.id))} contacts "
          f"(Maya=stale x2 events, Priya=30d, Jordan=fresh 3d)\n")
    print("Running agent (real Anthropic call)...\n")

    res = run_relationship_agent(db, u.id)

    print("=" * 70)
    print(f"stop_reason : {res.stop_reason}")
    print(f"steps       : {res.steps}")
    print(f"error       : {res.error}")
    print(f"proposals   : {len(res.proposals)}")
    print("-" * 70)
    print("SUMMARY:")
    print(res.summary or "(none)")
    print("-" * 70)
    for i, p in enumerate(res.proposals, 1):
        print(f"\n[{i}] {p.kind.upper()} -> {p.contact_name} (contact {p.contact_id})")
        print(f"    {p.text}")
        if p.rationale:
            print(f"    rationale: {p.rationale}")
    # Safety check: a propose-only run must not have sent anything.
    sent = db.query(models.OutreachLog).count()
    print("\n" + "=" * 70)
    print(f"SAFETY: OutreachLog rows written = {sent} (must be 0)")
    db.close()


if __name__ == "__main__":
    main()
