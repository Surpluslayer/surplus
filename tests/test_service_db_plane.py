"""The service-plane DB rule: admin + webhook routes are token/signature
authenticated and cross-tenant, so they must use get_service_db (jobs engine,
bypasses RLS) — the request engine would silently scope them to zero rows the
moment SURPLUS_RLS_ENABLED bites (a query that succeeds but returns nothing is
the worst failure mode: no error, no data). Session routes must stay on get_db.
Found the hard way: the first prod content-retention dry-run reported 0 of
8,542 expirable bodies because /admin ran RLS-scoped."""
from __future__ import annotations

import inspect

from backend.db import ENGINE, REQUEST_ENGINE, SessionLocal, get_service_db
from backend.routes import admin as admin_routes
from backend.routes import webhooks as webhook_routes
# The relationship-side session routes (/api/book + /api/relationships) live
# in routes/book.py now; routes/relationships.py is a thin re-export shim.
from backend.routes import book as session_routes


def test_get_service_db_uses_jobs_engine():
    gen = get_service_db()
    db = next(gen)
    try:
        assert db.get_bind() is ENGINE
    finally:
        gen.close()


def test_admin_and_webhook_routes_never_use_request_engine():
    for mod in (admin_routes, webhook_routes):
        src = inspect.getsource(mod)
        assert "Depends(get_db)" not in src, (
            f"{mod.__name__} has a route on the RLS request engine; "
            "service planes must Depends(get_service_db)")
        assert "get_service_db" in src


def test_session_routes_stay_on_request_engine():
    src = inspect.getsource(session_routes)
    assert "Depends(get_db)" in src
    assert "get_service_db" not in src
