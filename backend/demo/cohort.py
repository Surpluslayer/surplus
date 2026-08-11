"""backend/demo/cohort.py : the baseline 80-lawyer / 30-day demo generator.

Writes real User / Contact / RelationshipInteraction rows -- the same
production tables and the same taxonomy the live product uses
(source_type/interaction_type values match updates_engine.py's real
vocabulary; contact-state diversity follows messaging_eval.py's real
scenario categories). Every row this module writes gets a DemoProvenance
tag with provenance=BASELINE (see provenance.py) -- calibrated against real
in-code product design constants where they exist, and clearly-labeled
assumptions where they don't (see distributions.py's module docstring for
exactly which is which; do not treat this generator's output as measured
usage data).

PRODUCT-FACING POSTURE: nothing this module writes should be labeled
"FAKE DATA" or "SYNTHETIC LAWYER" record-by-record in the UI. Generated
Users carry is_demo=True (the SAME flag routes/demo.py already uses to keep
demo users out of real counts/queries and to purge them on a schedule --
this cohort is not a new concept, it's more rows behind an existing one).
A product surface that wants to disclose the environment should show one
minimal "Demo Environment" indicator, not per-record clutter -- that's a
frontend decision, out of scope for this module, and named as a remaining
step rather than silently skipped.

Run it:
    python -m backend.demo.cohort_seed              # writes to the configured DB
    python -m backend.demo.cohort_seed --lawyers 80 --days 30
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import distributions as dist
from . import provenance as prov
from .. import models

_UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(_UTC)


# ── realistic content: a lawyer's professional network, not a tech mixer ────
# Enough variety that 80 lawyers don't all capture the same 10 people; each
# contact is built from an independently-sampled name/company/context, not a
# fixed cast (contrast with demo_seed.py's intentionally-fixed 3-person tour).

_FIRST_NAMES = ("Sarah", "Marcus", "Elena", "David", "Priya", "James", "Anaya",
                 "Thomas", "Grace", "Michael", "Lucia", "Robert", "Wen", "Daniel",
                 "Fatima", "Christopher", "Nadia", "Andrew", "Yuki", "Samuel")
_LAST_NAMES = ("Chen", "Whitfield", "Okonkwo", "Reyes", "Bergstrom", "Patel",
                "Marchetti", "Osei", "Lindqvist", "Alvarez", "Novak", "Han",
                "Fitzgerald", "Kowalski", "Nakamura", "Odom", "Delacroix", "Singh")
_COMPANIES = ("Meridian Health Systems", "Northgate Capital", "Vantage Logistics",
              "Bristow Manufacturing", "Ashford & Kline", "Cobalt Partners",
              "Redwood Property Group", "Halden Insurance", "Turnkey Ventures",
              "Ferris Industrial", "Corvus Analytics", "Palmer & Voss")
_TITLES = ("General Counsel", "VP Operations", "Managing Partner", "CFO",
           "Director of Risk", "Founder", "Head of Compliance", "Principal")
_MET_AT = ("a bar association mixer", "a CLE panel", "a client's closing dinner",
           "a referral call", "a deposition", "a state bar conference",
           "an intro from a mutual client")

_SIGNAL_DETAILS = {
    "job_change": "just moved to {title} at {company}",
    "new_post": "posted about a matter relevant to {company}'s industry",
}


def _seeded_rng(cohort_id: str, salt: str) -> random.Random:
    """Deterministic per-(cohort, salt) RNG -- a re-run with the same
    cohort_id reproduces the same cohort, useful for tests and for diffing
    a distributions.py change against a stable baseline."""
    h = hashlib.sha256(f"{cohort_id}:{salt}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _weighted_choice(rng: random.Random, weights: dict) -> str:
    keys = list(weights.keys())
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


@dataclass
class GeneratedContact:
    contact: models.Contact
    state_category: str


def _make_lawyer_user(db, rng: random.Random, i: int, cohort_id: str) -> models.User:
    tier = _weighted_choice(rng, dist.USER_TIER_WEIGHTS)
    jurisdiction = rng.choice(["NY", "CA", "FL", None, None])  # some unset, honestly
    user = models.User(
        email=f"demo-lawyer-{i:03d}@example.com",
        name=f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}",
        is_demo=True,
        bar_jurisdiction=jurisdiction,
        autonomy_mode="ask",
    )
    db.add(user)
    db.flush()
    prov.tag(db, user, provenance=prov.BASELINE, cohort_id=cohort_id)
    user._demo_tier = tier  # not persisted; carried for this generation run only
    return user


def _make_contact(db, rng: random.Random, user: models.User, cohort_id: str,
                   idx: int) -> GeneratedContact:
    name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
    company = rng.choice(_COMPANIES)
    title = rng.choice(_TITLES)
    state_cat = rng.choice(dist.CONTACT_STATE_CATEGORIES)
    is_vip = rng.random() < dist.ASSUMED_VIP_FRACTION
    # Most contacts are unset (-> PROSPECT); a minority carry an explicit
    # exemption category, same conservative default posture as production
    # (see models.Contact.relationship_type's own docstring).
    relationship_type = rng.choices(
        [None, "existing_client", "former_client", "sophisticated_business_user"],
        weights=[0.75, 0.15, 0.05, 0.05], k=1,
    )[0]
    contact = models.Contact(
        user_id=user.id,
        primary_identity_key=f"demo:{user.id}:{idx}",
        name=name, company=company, title=title, headline=f"{title} at {company}",
        vip=is_vip, relationship_type=relationship_type,
        watched_at=_utcnow() - timedelta(days=rng.randint(0, 5)),
    )
    db.add(contact)
    db.flush()
    prov.tag(db, contact, provenance=prov.BASELINE, cohort_id=cohort_id)
    return GeneratedContact(contact=contact, state_category=state_cat)


def _session_dates(rng: random.Random, tier: str, days: int) -> list[datetime]:
    lo, hi = dist.ASSUMED_SESSIONS_PER_WEEK[tier]
    weeks = max(1, days // 7)
    total_sessions = max(1, round(rng.gauss(lo, hi) * weeks))
    start = _utcnow() - timedelta(days=days)
    return sorted(start + timedelta(
        seconds=rng.uniform(0, days * 86400),
    ) for _ in range(total_sessions))


def _emit_session(db, rng: random.Random, user: models.User,
                   contacts: list[GeneratedContact], cohort_id: str, when: datetime) -> None:
    """One simulated login: view feed (always first), then a bounded,
    randomly-ordered mix of investigate/draft/edit/search -- the sequence
    shape from distributions.SESSION_ACTION_RANGE, not independent random
    events with no relation to each other."""
    if not contacts:
        return
    drafts_this_session = 0

    def _touch(contact, interaction_type, title, summary, direction="outbound", minutes_offset=0):
        row = models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id,
            source_type="activity_update" if interaction_type == "note" else "manual_note",
            interaction_type=interaction_type, direction=direction,
            occurred_at=when + timedelta(minutes=minutes_offset),
            title=title, summary=summary,
        )
        db.add(row)
        db.flush()
        prov.tag(db, row, provenance=prov.BASELINE, cohort_id=cohort_id)
        return row

    n_investigate = rng.randint(*dist.SESSION_ACTION_RANGE["investigate_contact"])
    n_draft = rng.randint(*dist.SESSION_ACTION_RANGE["generate_draft"])
    n_edit = min(rng.randint(*dist.SESSION_ACTION_RANGE["edit_draft"]), n_draft)
    n_search = rng.randint(*dist.SESSION_ACTION_RANGE["search"])

    minute = 0
    for _ in range(n_investigate):
        gc = rng.choice(contacts)
        minute += rng.randint(1, 4)
        _touch(gc.contact, "note", "Reviewed contact",
               f"Looked into {gc.contact.name}'s recent activity.",
               direction="none", minutes_offset=minute)

    for _ in range(n_draft):
        gc = rng.choice(contacts)
        minute += rng.randint(1, 5)
        disposition = _weighted_choice(rng, dist.DRAFT_DISPOSITION_WEIGHTS)
        kind = rng.choice(dist.DRAFTWORTHY_SIGNAL_KINDS)
        detail = _SIGNAL_DETAILS[kind].format(title=gc.contact.title or "a new role",
                                               company=gc.contact.company or "their company")
        draft_row = _touch(
            gc.contact, "message",
            f"Drafted follow-up ({gc.state_category})",
            f"Draft referencing: {gc.contact.name} {detail}. Disposition: {disposition}.",
            minutes_offset=minute,
        )
        drafts_this_session += 1
        if disposition == "edited_then_sent":
            minute += rng.randint(1, 3)
            _touch(gc.contact, "message", "Edited draft before sending",
                   f"Host edited the draft for {gc.contact.name} before sending.",
                   minutes_offset=minute)

    for _ in range(n_search):
        minute += rng.randint(1, 2)
        # Searches aren't contact-scoped; log against a random contact as the
        # nearest real anchor rather than inventing a schema-free event type.
        gc = rng.choice(contacts)
        _touch(gc.contact, "note", "Searched the book", "Searched contacts.",
               direction="none", minutes_offset=minute)


def generate(db, *, n_lawyers: int = 80, days: int = 30,
             contacts_per_lawyer: tuple[int, int] = (15, 60),
             cohort_id: str | None = None) -> str:
    """Generate the baseline cohort. Returns the cohort_id (pass it to
    provenance.cohort_row_counts / delete_cohort)."""
    cohort_id = cohort_id or f"baseline-{_utcnow():%Y%m%d%H%M%S}"
    rng = _seeded_rng(cohort_id, "top")

    for i in range(n_lawyers):
        user_rng = _seeded_rng(cohort_id, f"user{i}")
        user = _make_lawyer_user(db, user_rng, i, cohort_id)

        n_contacts = user_rng.randint(*contacts_per_lawyer)
        contacts = [
            _make_contact(db, user_rng, user, cohort_id, idx)
            for idx in range(n_contacts)
        ]

        for when in _session_dates(user_rng, user._demo_tier, days):
            _emit_session(db, user_rng, user, contacts, cohort_id, when)

    db.commit()
    return cohort_id


if __name__ == "__main__":
    import argparse

    from ..db import SessionLocal, init_db

    ap = argparse.ArgumentParser()
    ap.add_argument("--lawyers", type=int, default=80)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        cohort_id = generate(db, n_lawyers=args.lawyers, days=args.days)
        counts = prov.cohort_row_counts(db, cohort_id)
        print(f"cohort_id={cohort_id}")
        for table, by_prov in counts.items():
            print(f"  {table}: {by_prov}")
    finally:
        db.close()
