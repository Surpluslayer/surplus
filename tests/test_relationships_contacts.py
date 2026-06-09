"""
Tests for spine-deepening : Contact auto-linking at real interaction points and
the contact-centric read model (the durable-person projection over per-event
Prospect rows) + the /api/relationships/contacts API.

Spine-deepening closes "Gap A" : link_contact() now fires on every real touch
(in-person capture, applied webhook funnel event, outbound send), not just on a
manual note — so cross-event recall actually populates. The read model rolls up
every linked Prospect into one durable person.

Direct function/route calls + in-memory SQLite (avoids TestClient + str|None on
3.9), UNIPILE_DRY_RUN=true.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import relationships as rel
from backend.routes import relationships as rel_route


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Op"), email=kw.get("email", "op@x.com"),
                    unipile_account_id=kw.get("acct", "acct1"))
    db.add(u); db.commit()
    return u


def _event(db, user, label="Mixer", city="SF"):
    ev = models.Event(user_id=user.id, kind="in_person", label=label, city=city)
    db.add(ev); db.commit()
    return ev


def _prospect(db, event, *, name="Maya Rodriguez",
              linkedin_url="https://linkedin.com/in/maya", **kw):
    p = models.Prospect(
        event_id=event.id, identity=kw.get("identity", "maya"), name=name,
        role=kw.get("role", "Staff Infra"), company=kw.get("company", "Lo91r"),
        linkedin_url=linkedin_url,
        status=kw.get("status", "pending"), source=kw.get("source", "scan"),
        captured_at=kw.get("captured_at", datetime.now(timezone.utc)),
        note=kw.get("note"),
        connection_status=kw.get("connection_status", "unknown"),
    )
    db.add(p); db.commit()
    return p


# ── contact-centric read model ───────────────────────────────────────────

def test_contact_summary_rolls_up_two_events(db):
    """Same person met at two events -> one Contact, n_events=2, first_met_at is
    the EARLIER capture, last_touch_at the later one."""
    u = _user(db)
    t0 = datetime.now(timezone.utc) - timedelta(days=30)
    t1 = datetime.now(timezone.utc) - timedelta(days=2)
    ev0 = _event(db, u, label="Dinner")
    ev1 = _event(db, u, label="Conference")
    p0 = _prospect(db, ev0, captured_at=t0)
    p1 = _prospect(db, ev1, captured_at=t1)
    c0 = rel.link_contact(db, p0, u.id)
    c1 = rel.link_contact(db, p1, u.id)
    assert c0.id == c1.id  # one durable person

    s = rel.contact_summary(db, c0)
    assert s["n_events"] == 2
    assert s["name"] == "Maya Rodriguez"
    # first_met_at is the earliest capture (within a second tolerance).
    assert abs((s["first_met_at"] - rel._as_aware(t0)).total_seconds()) < 2
    assert abs((s["last_touch_at"] - rel._as_aware(t1)).total_seconds()) < 2
    assert set(s["event_ids"]) == {ev0.id, ev1.id}


def test_contact_summary_strongest_stage_wins(db):
    """The rollup stage is the STRONGEST across events : a 'replied' at one event
    beats a 'captured' at another."""
    u = _user(db)
    ev0 = _event(db, u); ev1 = _event(db, u)
    p0 = _prospect(db, ev0)                       # bare capture -> captured
    p1 = _prospect(db, ev1)
    db.add(models.OutreachLog(prospect_id=p1.id, channel="linkedin",
                              state="message_replied",
                              ts=datetime.now(timezone.utc)))
    db.commit()
    c = rel.link_contact(db, p0, u.id)
    rel.link_contact(db, p1, u.id)
    s = rel.contact_summary(db, c)
    assert s["relationship_stage"] == "replied"


def test_contact_events_one_row_per_event_newest_first(db):
    u = _user(db)
    old = datetime.now(timezone.utc) - timedelta(days=20)
    new = datetime.now(timezone.utc) - timedelta(days=1)
    ev0 = _event(db, u, label="Old"); ev1 = _event(db, u, label="New")
    p0 = _prospect(db, ev0, captured_at=old)
    p1 = _prospect(db, ev1, captured_at=new)
    c = rel.link_contact(db, p0, u.id)
    rel.link_contact(db, p1, u.id)

    rows = rel.contact_events(db, c)
    assert [r["event_title"] for r in rows] == ["New", "Old"]  # newest touch first
    assert {r["prospect_id"] for r in rows} == {p0.id, p1.id}


def test_contact_timeline_dedups_contact_level_note(db):
    """A note tied to the Contact (not a single prospect) must appear ONCE on the
    unified timeline, not once per linked prospect."""
    u = _user(db)
    ev0 = _event(db, u); ev1 = _event(db, u)
    p0 = _prospect(db, ev0)
    p1 = _prospect(db, ev1)
    c = rel.link_contact(db, p0, u.id)
    rel.link_contact(db, p1, u.id)
    # contact-scoped note, attached to neither prospect directly.
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="manual_note",
        interaction_type="note", summary="re-met at SF dinner",
        occurred_at=datetime.now(timezone.utc)))
    db.commit()

    tl = rel.contact_timeline(db, c)
    notes = [it for it in tl if it["summary"] == "re-met at SF dinner"]
    assert len(notes) == 1


def test_contact_timeline_annotates_event_provenance(db):
    """Every derived item on the unified timeline carries which event it came
    from, so the cross-event view can label each touch."""
    u = _user(db)
    ev = _event(db, u, label="Pitch Night")
    p = _prospect(db, ev)
    c = rel.link_contact(db, p, u.id)
    tl = rel.contact_timeline(db, c)
    cap = [it for it in tl if it["interaction_type"] == "captured"]
    assert cap and cap[0]["metadata"]["event_id"] == ev.id
    assert cap[0]["metadata"]["event_title"] == "Pitch Night"
    assert cap[0]["metadata"]["prospect_id"] == p.id


def test_list_contacts_is_owner_scoped(db):
    owner = _user(db, email="owner@x.com", acct="owner")
    other = _user(db, email="other@x.com", acct="other")
    ev_o = _event(db, owner)
    ev_x = _event(db, other)
    rel.link_contact(db, _prospect(db, ev_o, linkedin_url="https://linkedin.com/in/a"), owner.id)
    rel.link_contact(db, _prospect(db, ev_x, linkedin_url="https://linkedin.com/in/b"), other.id)
    assert len(rel.list_contacts(db, owner.id)) == 1
    assert len(rel.list_contacts(db, other.id)) == 1


# ── contacts API routes ──────────────────────────────────────────────────

def test_contacts_route_lists_owned_contacts(db):
    u = _user(db)
    ev = _event(db, u)
    rel.link_contact(db, _prospect(db, ev), u.id)
    out = rel_route.list_contacts(db, u)
    assert out["count"] == 1
    assert out["contacts"][0]["name"] == "Maya Rodriguez"


def test_contact_detail_route_returns_summary_events_timeline(db):
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    c = rel.link_contact(db, p, u.id)
    out = rel_route.contact_detail(c.id, db, u)
    assert out["contact_summary"]["contact_id"] == c.id
    assert len(out["events"]) == 1
    assert any(it["interaction_type"] == "captured" for it in out["timeline"])


def test_contact_detail_route_blocks_unowned(db):
    from fastapi import HTTPException
    owner = _user(db, email="owner@x.com", acct="owner")
    other = _user(db, email="other@x.com", acct="other")
    ev = _event(db, owner)
    c = rel.link_contact(db, _prospect(db, ev), owner.id)
    with pytest.raises(HTTPException) as ei:
        rel_route.contact_detail(c.id, db, other)
    assert ei.value.status_code == 404


def test_contact_detail_route_404_on_missing(db):
    from fastapi import HTTPException
    u = _user(db)
    with pytest.raises(HTTPException) as ei:
        rel_route.contact_detail(999, db, u)
    assert ei.value.status_code == 404


# ── important-person star ────────────────────────────────────────────────

def test_contact_summary_defaults_unstarred(db):
    """A freshly-linked contact is not starred, and the rollup surfaces it so
    the CRM can render the (empty) star."""
    u = _user(db)
    c = rel.link_contact(db, _prospect(db, _event(db, u)), u.id)
    s = rel.contact_summary(db, c)
    assert s["starred"] is False


def test_set_contact_star_toggles_and_surfaces(db):
    """Starring an owned contact flips the flag, and the rollup + roster reflect
    it so both the CRM and the relationship agent see the signal."""
    u = _user(db)
    c = rel.link_contact(db, _prospect(db, _event(db, u)), u.id)

    out = rel_route.set_contact_star(c.id, rel_route.StarIn(starred=True), db, u)
    assert out == {"contact_id": c.id, "starred": True}
    db.refresh(c)
    assert c.starred is True
    assert rel.contact_summary(db, c)["starred"] is True

    # Idempotent un-star.
    out = rel_route.set_contact_star(c.id, rel_route.StarIn(starred=False), db, u)
    assert out["starred"] is False
    db.refresh(c)
    assert c.starred is False


def test_set_contact_star_blocks_unowned(db):
    from fastapi import HTTPException
    owner = _user(db, email="owner@x.com", acct="owner")
    other = _user(db, email="other@x.com", acct="other")
    c = rel.link_contact(db, _prospect(db, _event(db, owner)), owner.id)
    with pytest.raises(HTTPException) as ei:
        rel_route.set_contact_star(c.id, rel_route.StarIn(starred=True), db, other)
    assert ei.value.status_code == 404


# ── auto-link at interaction points (Gap A) ──────────────────────────────

def test_webhook_funnel_event_auto_links_contact(db):
    """An applied webhook funnel event puts the person on the spine even though
    no manual note was ever written."""
    from backend.providers import UnipileProvider
    from backend.providers.base import CanonicalEvent
    from backend.routes.webhooks import _apply_canonical_event

    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev, linkedin_url="https://linkedin.com/in/maya",
                  connection_status="not_connected")
    p.linkedin_provider_id = "li_maya"
    db.commit()
    assert p.contact_id is None

    canonical = CanonicalEvent(
        event_id=ev.id, prospect_id=p.id, state="invite_accepted",
        provider="unipile", provider_lead_id="li_maya",
        ts=datetime.now(timezone.utc), body="", raw={},
    )
    applied, _reason, _p = _apply_canonical_event(db, UnipileProvider(dry_run=True), canonical)
    assert applied is True
    db.refresh(p)
    assert p.contact_id is not None
    assert db.query(models.Contact).count() == 1


def test_send_and_log_auto_links_contact_on_success(db):
    """A successful outbound send is a real touch -> the recipient lands on the
    spine (dry-run send still records success)."""
    from backend.providers import UnipileProvider
    from backend.agents.sender import send_and_log

    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    assert p.contact_id is None
    res = send_and_log(db, p, "hey Maya — great chatting",
                       sent_state="message_sent",
                       fallback_provider=UnipileProvider(dry_run=True))
    assert res.error is None
    db.refresh(p)
    assert p.contact_id is not None


def test_send_and_log_batched_commit_false_does_not_link(db):
    """When the caller batches (commit=False, e.g. the cron follow-up) we must
    NOT auto-link : link_contact commits internally and would break the batch."""
    from backend.providers import UnipileProvider
    from backend.agents.sender import send_and_log

    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    send_and_log(db, p, "batched", sent_state="follow_up_sent",
                 fallback_provider=UnipileProvider(dry_run=True), commit=False)
    # No internal commit happened, so the spine link is deferred to the caller.
    assert p.contact_id is None


# ── chat schedule-send (Gmail-style) ─────────────────────────────────────────

def test_schedule_followup_future_queues_row_and_sends_nothing(db):
    """A future send_at stages a ScheduledFollowup the cron will fire later, and
    sends NOTHING now (no OutreachLog)."""
    u = _user(db)
    ev = _event(db, u)
    c = rel.link_contact(db, _prospect(db, ev), u.id)
    when = datetime.now(timezone.utc) + timedelta(days=2)

    body = rel_route.FollowupScheduleIn(message="hey Maya, lets reconnect", send_at=when)
    out = rel_route.schedule_contact_followup(c.id, body, db, u)

    assert out["status"] == "scheduled"
    rows = db.query(models.ScheduledFollowup).all()
    assert len(rows) == 1
    assert rows[0].status == "scheduled"
    assert rows[0].body == "hey Maya, lets reconnect"
    # Nothing left the system.
    assert db.query(models.OutreachLog).count() == 0


def test_schedule_followup_reschedule_is_idempotent(db):
    """Re-approving the same contact updates the one pending row (body + time)
    instead of stacking duplicate scheduled sends."""
    u = _user(db)
    ev = _event(db, u)
    c = rel.link_contact(db, _prospect(db, ev), u.id)
    t1 = datetime.now(timezone.utc) + timedelta(days=1)
    t2 = datetime.now(timezone.utc) + timedelta(days=3)

    rel_route.schedule_contact_followup(
        c.id, rel_route.FollowupScheduleIn(message="v1", send_at=t1), db, u)
    rel_route.schedule_contact_followup(
        c.id, rel_route.FollowupScheduleIn(message="v2", send_at=t2), db, u)

    rows = db.query(models.ScheduledFollowup).filter_by(status="scheduled").all()
    assert len(rows) == 1
    assert rows[0].body == "v2"


def test_schedule_followup_now_sends_immediately(db):
    """No send_at means 'send now' : dispatches through the shared send path
    (dry-run) and records an OutreachLog, regardless of the auto-send toggle."""
    u = _user(db)  # auto_followups_enabled defaults off
    ev = _event(db, u)
    c = rel.link_contact(db, _prospect(db, ev), u.id)

    out = rel_route.schedule_contact_followup(
        c.id, rel_route.FollowupScheduleIn(message="ping now", send_at=None), db, u)

    assert out["status"] == "sent"
    assert db.query(models.ScheduledFollowup).count() == 0
    assert db.query(models.OutreachLog).count() == 1


# ── chat stream keepalive (the 502 fix) ────────────────────────────────────
# The follow-up chat streams SSE; the gap before the first draft is several
# sequential LLM calls. An edge proxy idle-times-out a silent stream and 502s
# the browser even though the server is fine (confirmed in prod). _drain_stream
# trickles keepalive comments during that silence so the connection stays alive.
import queue as _queue


def test_drain_stream_emits_keepalive_during_silence():
    """A queue that's empty past the heartbeat interval yields a keepalive
    comment, then resumes with real frames once they land."""
    q: "_queue.Queue" = _queue.Queue()
    # Nothing queued yet, then a proposal, then the sentinel — but the FIRST
    # get() will time out (empty), forcing a heartbeat before the proposal.
    out = []
    gen = rel_route._drain_stream(q, heartbeat_secs=0.01)
    out.append(next(gen))                       # times out empty -> keepalive
    q.put(("proposal", {"contact_id": 1}))
    out.append(next(gen))                       # real frame
    q.put((None, None))                         # sentinel -> StopIteration
    with pytest.raises(StopIteration):
        next(gen)

    assert out[0].startswith(":")               # SSE comment (ignored by client)
    assert "keepalive" in out[0]
    assert out[1].startswith("event: proposal")
    assert '"contact_id": 1' in out[1]


def test_drain_stream_stops_on_sentinel_without_keepalive():
    """When frames are already waiting, the drain forwards them and stops at the
    sentinel with no spurious keepalive."""
    q: "_queue.Queue" = _queue.Queue()
    q.put(("done", {"summary": "ok"}))
    q.put((None, None))
    frames = list(rel_route._drain_stream(q, heartbeat_secs=5))

    assert len(frames) == 1
    assert frames[0].startswith("event: done")
    assert all("keepalive" not in f for f in frames)
