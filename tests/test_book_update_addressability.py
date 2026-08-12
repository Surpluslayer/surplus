"""tests/test_book_update_addressability.py : an update card on the Today
feed previously carried only contact_id, with no way to point at the
SPECIFIC RelationshipInteraction row that produced it -- one contact can
carry several activity_update rows and only the newest was ever surfaced.
Verifies the fix threads the row id (and, for new_post rows, milestone_type)
from spine.relationships._latest_update_view through book.build_today to
the real /api/book/today response, without changing any existing field.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend.auth import current_user
from backend.demo import cohort
from backend.main import app
from backend import models
from backend.agents.relationship.spine import relationships as spine


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override_db

    db = Session()
    cohort.generate(db, n_lawyers=8, days=30)
    user = db.execute(select(models.User)).scalars().first()
    user_id = user.id
    db.close()

    def _current():
        s = Session()
        try:
            return s.get(models.User, user_id)
        finally:
            s.expunge_all()
            s.close()
    app.dependency_overrides[current_user] = _current

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_latest_update_view_includes_the_row_id():
    row = models.RelationshipInteraction(id=42, actor_user_id=1, contact_id=1,
                                         source_type="activity_update", interaction_type="new_post",
                                         direction="none", title="New LinkedIn post",
                                         summary="s", meta_json='{"milestone_type": "acquisition"}')
    view = spine._latest_update_view([row])
    assert view["id"] == 42
    assert view["milestone_type"] == "acquisition"


def test_latest_update_view_on_empty_list_is_none():
    assert spine._latest_update_view([]) is None


def test_today_feed_cards_backed_by_a_real_activity_update_row_carry_update_id(client):
    r = client.get("/api/book/today")
    assert r.status_code == 200
    updates = r.json()["updates"]
    assert updates  # the generated cohort produces real update rows

    with_id = [u for u in updates if u.get("update_id") is not None]
    assert with_id, "no update card carried a real update_id"
    for u in with_id:
        assert isinstance(u["update_id"], int)
        # every field that existed before this change is still present
        assert "contact_id" in u and "headline" in u and "type" in u


def test_update_id_matches_a_real_interaction_row(client):
    r = client.get("/api/book/today")
    updates = r.json()["updates"]
    addressable = next(u for u in updates if u.get("update_id") is not None)

    # Verify against the SAME app-level db override the fixture wired up.
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        row = db.get(models.RelationshipInteraction, addressable["update_id"])
        assert row is not None
        assert row.source_type == "activity_update"
        assert str(row.contact_id) == addressable["contact_id"]
    finally:
        db.close()
