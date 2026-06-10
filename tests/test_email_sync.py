"""Email channel pull/push core: mailbox→contacts sync, host-confirmed
thread helpers, and in-thread reply payloads. All offline (injected fetch)."""
from __future__ import annotations
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import email_sync as es


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _user(db):
    u = models.User(name="Host", email="host@x.com",
                    unipile_email_account_id="mail_1",
                    email_account_address="host@gmail.com",
                    email_status="active")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _mail(frm, to, *, date, role="inbox", subject="hey", thread="t1", pid="m1"):
    return {"provider_id": pid, "thread_id": thread, "subject": subject,
            "date": date, "role": role,
            "from_attendee": {"identifier": frm[0], "display_name": frm[1]},
            "to_attendees": [{"identifier": a, "display_name": n} for a, n in to]}


def test_junk_and_fanout_are_skipped():
    assert es.is_junk_address("no-reply@github.com")
    assert es.is_junk_address("notifications+abc@linear.app")
    assert not es.is_junk_address("maya@lo91r.com")
    blast = _mail(("maya@x.com", "Maya"),
                  [(f"p{i}@x.com", "") for i in range(8)], date="2026-06-01T00:00:00Z")
    assert es.counterparts_of(blast, "host@gmail.com")[0] == "skip"


def test_sync_builds_contacts_and_rollup_idempotently(db):
    u = _user(db)
    mails = [
        _mail(("maya@lo91r.com", "Maya Rodriguez"), [("host@gmail.com", "Host")],
              date="2026-06-08T10:00:00Z", pid="m1"),
        _mail(("host@gmail.com", "Host"), [("maya@lo91r.com", "Maya Rodriguez")],
              date="2026-06-07T10:00:00Z", role="sent", pid="m2"),
        _mail(("noreply@stripe.com", "Stripe"), [("host@gmail.com", "Host")],
              date="2026-06-08T11:00:00Z", pid="m3"),
    ]
    fetch = lambda cursor: {"items": mails, "cursor": None}
    stats = es.sync_email_contacts(db, u, dsn="d", api_key="k", fetch_page=fetch)
    assert stats["error"] is None
    assert stats["contacts_created"] == 1          # Maya only; Stripe is junk
    c = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert c.email == "maya@lo91r.com" and c.name == "Maya Rodriguez"
    r = (db.query(models.RelationshipInteraction)
         .filter_by(contact_id=c.id, source_type="email_sync").one())
    assert "2 messages" in r.summary
    assert r.direction == "in"                     # last word was theirs
    meta = json.loads(r.meta_json)
    assert meta["n_in"] == 1 and meta["n_out"] == 1

    # Re-sync: same contact, same single rollup row (updated in place).
    es.sync_email_contacts(db, u, dsn="d", api_key="k", fetch_page=fetch)
    assert db.query(models.Contact).count() == 1
    assert db.query(models.RelationshipInteraction).count() == 1


def test_thread_candidates_group_and_sort():
    page = {"items": [
        _mail(("maya@x.com", "Maya"), [("host@gmail.com", "")],
              date="2026-06-01T00:00:00Z", thread="tA", subject="Dinner", pid="a1"),
        _mail(("maya@x.com", "Maya"), [("host@gmail.com", "")],
              date="2026-06-09T00:00:00Z", thread="tB", subject="Intro", pid="b1"),
        _mail(("host@gmail.com", ""), [("maya@x.com", "Maya")],
              date="2026-06-02T00:00:00Z", thread="tA", role="sent",
              subject="Re: Dinner", pid="a2"),
    ]}
    out = es.list_threads_for_address(dsn="d", api_key="k", account_id="m",
                                      address="maya@x.com",
                                      own_address="host@gmail.com",
                                      fetch=lambda **kw: page)
    assert [t["thread_id"] for t in out] == ["tB", "tA"]   # newest first
    assert out[1]["n"] == 2


def test_thread_messages_oldest_first_with_direction():
    page = {"items": [
        _mail(("host@gmail.com", ""), [("maya@x.com", "Maya")],
              date="2026-06-02T00:00:00Z", role="sent", pid="a2", subject="Re: Dinner"),
        _mail(("maya@x.com", "Maya"), [("host@gmail.com", "")],
              date="2026-06-01T00:00:00Z", pid="a1", subject="Dinner"),
    ]}
    msgs = es.thread_messages(dsn="d", api_key="k", account_id="m",
                              thread_id="tA", own_address="host@gmail.com",
                              fetch=lambda **kw: page)
    assert [m["provider_id"] for m in msgs] == ["a1", "a2"]
    assert msgs[0]["direction"] == "in" and msgs[1]["direction"] == "out"


def test_provider_send_email_dry_run_carries_reply_to():
    from backend.providers.unipile import UnipileProvider
    p = UnipileProvider(dry_run=True, api_key=None, dsn=None, account_id=None)
    res = p.send_email(email_account_id="mail_1", to_address="maya@x.com",
                       subject="Re: Dinner", body="see you there",
                       prospect_id=7, reply_to="a2")
    assert res.state == "dry_run_queued"
    assert res.payload["reply_to"] == "a2"
    assert res.payload["to"][0]["identifier"] == "maya@x.com"


def test_set_contact_email_clears_stale_thread(db):
    """Changing a contact's address must clear the linked thread (it belonged
    to the old address) and backfill linked prospects."""
    from backend.routes import relationships as rel_routes
    u = _user(db)
    c = models.Contact(user_id=u.id, primary_identity_key="li:maya",
                       name="Maya", email="old@x.com", email_thread_id="tOLD")
    db.add(c); db.commit(); db.refresh(c)

    out = rel_routes.set_contact_email(
        c.id, rel_routes.ContactEmailIn(email="Maya@Lo91r.com"), db, u)
    db.refresh(c)
    assert c.email == "maya@lo91r.com"
    assert c.email_thread_id is None          # stale link cleared
    assert out["linked_thread_id"] is None

    # Same address again: no-op on the thread link.
    c.email_thread_id = "tNEW"; db.commit()
    rel_routes.set_contact_email(
        c.id, rel_routes.ContactEmailIn(email="maya@lo91r.com"), db, u)
    db.refresh(c)
    assert c.email_thread_id == "tNEW"
