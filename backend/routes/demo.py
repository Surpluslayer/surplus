"""
routes/demo.py : hidden-link demo entry point.

Goal: hand someone (an investor, a friend, a candidate user) a single URL
that drops them into the full surplus app without making them sign in
with their own LinkedIn first. They get a real signed-in session backed
by the operator user (i.e., the LinkedIn account configured via the
UNIPILE_ACCOUNT_ID env var), so every action : including real outreach
sends : works end-to-end.

Security model:
  - Gated by a shared secret in the DEMO_ACCESS_TOKEN env var.
  - URL is NOT meant to be public. Don't post it on Twitter. The trade-off
    for "everything works including real sends" is that anyone with the
    link can spend the operator's daily LinkedIn quota and send DMs from
    your account.
  - When DEMO_ACCESS_TOKEN is unset, the route returns 404 : it doesn't
    exist in production unless you opt in by setting the env var.
  - constant-time comparison on the token to avoid timing attacks.

Share URL shape:
  https://www.surpluslayer.com/api/demo/enter?key=<DEMO_ACCESS_TOKEN>

Effect:
  - 303 redirect to "/" with the surplus_session cookie set
  - Session is tied to the operator User row (same one auto-created by
    _ensure_operator_user_and_backfill at startup)
  - From that point the SPA behaves identically to a real signed-in user

To revoke a leaked link: rotate DEMO_ACCESS_TOKEN in Railway env. Active
sessions issued by the old link continue to work until their 30-day TTL
expires (they're indistinguishable from a normal session) : to kill them
immediately, delete the corresponding rows from the sessions table.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from ..auth import create_session, set_session_cookie
from ..db import get_db
from ..models import User


router = APIRouter(prefix="/api/demo", tags=["demo"])


def _demo_token() -> Optional[str]:
    """The shared secret. None when the feature is disabled."""
    tok = (os.environ.get("DEMO_ACCESS_TOKEN") or "").strip()
    return tok or None


def _operator_account_id() -> Optional[str]:
    aid = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    return aid or None


@router.get("/enter")
def demo_enter(
    key: str = Query(..., description="Shared secret matching DEMO_ACCESS_TOKEN"),
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    """Issue a session for the operator user and redirect to /.

    Returns 404 when:
      - DEMO_ACCESS_TOKEN env var is unset (feature disabled)
      - UNIPILE_ACCOUNT_ID env var is unset (no operator user to attach)
      - key doesn't match the configured token (don't leak existence)

    All three are 404 (not 403/401) so probing the URL with a wrong key
    is indistinguishable from the feature being off.
    """
    expected = _demo_token()
    if not expected:
        raise HTTPException(status_code=404, detail="not found")

    # constant-time compare : avoid leaking the token length / prefix via
    # response timing.
    if not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=404, detail="not found")

    operator_account_id = _operator_account_id()
    if not operator_account_id:
        # Misconfigured deploy : feature is on but there's no operator user
        # to issue a session for. 404 so we don't leak the misconfiguration.
        raise HTTPException(status_code=404, detail="not found")

    operator = (
        db.query(User)
        .filter(User.unipile_account_id == operator_account_id)
        .first()
    )
    if not operator:
        # Should be impossible : _ensure_operator_user_and_backfill creates
        # this row at startup whenever UNIPILE_ACCOUNT_ID is set. Surface
        # as 500 so it's visible in logs if it ever happens.
        raise HTTPException(status_code=500, detail="operator user missing")

    sess = create_session(db, operator)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, sess.session_token)
    return response
