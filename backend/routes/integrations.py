"""routes/integrations.py : connect external SOURCES for relationship context.

The host authorizes Google (Gmail + Calendar) etc.; we store refreshable tokens in
ConnectedAccount and later poll for context. Owner-scoped.

    GET    /api/integrations                  list this host's connections + availability
    GET    /api/integrations/{provider}/connect    -> {url} to send the host to consent
    GET    /api/integrations/{provider}/callback    OAuth redirect target (uses signed state)
    DELETE /api/integrations/{provider}/{account_id}  disconnect

The callback identifies the host from the HMAC-SIGNED state (unforgeable, short TTL),
not the session cookie -- OAuth redirects can drop SameSite cookies, so the signed
state is the reliable user binding.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user
from ..db import get_db
from ..integrations import oauth
from .auth import _surplus_base_url

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

_PROVIDERS = ["google", "microsoft", "calendly"]   # static-client OAuth providers


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_surplus_base_url(request)}/api/integrations/{provider}/callback"


@router.get("")
def list_integrations(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    rows = (db.query(models.ConnectedAccount)
            .filter_by(user_id=user.id).all())
    return {
        "connected": [{
            "id": r.id, "provider": r.provider, "account_email": r.account_email,
            "status": r.status, "scopes": (r.scopes or "").split(),
            "connected_at": r.created_at, "last_synced_at": r.last_synced_at,
        } for r in rows],
        # which providers the server can actually run. OAuth ones need client creds;
        # Granola (DCR) needs none, so it's always available.
        "available": {**{name: oauth.configured(name) for name in _PROVIDERS},
                      "granola": True},
    }


# ── Booking (Phase-2 WRITE action) — create a calendar meeting with a contact.
# Declared BEFORE the /{provider} routes so the literal 'calendar' wins.
class BookIn(BaseModel):
    contact_id: Optional[int] = None        # resolve attendee email/name from a Contact
    attendee_email: Optional[str] = None    # ...or pass them directly
    attendee_name: Optional[str] = ""
    title: str
    start_iso: str                          # ISO 8601 w/ offset, e.g. 2026-07-01T15:00:00-07:00
    duration_min: int = 30
    tz: str = "UTC"
    description: str = ""
    add_video: bool = True                  # Google Meet / Teams link
    notify: bool = True                     # email the attendee the invite
    provider: Optional[str] = None          # force google|microsoft (else auto)


@router.post("/calendar/book")
def calendar_book(
    body: BookIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Create a calendar event inviting a contact, with a native video link. Owner-scoped;
    the explicit host action (no agent auto-call). 409 if no calendar connected, 400 on a
    bad time / missing attendee email / upstream error."""
    email = (body.attendee_email or "").strip()
    name = body.attendee_name or ""
    if body.contact_id and not email:
        c = db.get(models.Contact, body.contact_id)
        if c is None or c.user_id != user.id:
            raise HTTPException(404, "contact not found")
        email = (c.email or "").strip()
        name = name or (c.name or "")
    if not email:
        raise HTTPException(400, "no attendee email (contact has none; pass attendee_email)")
    from ..integrations.booking import book_meeting
    try:
        return book_meeting(
            db, user, attendee_email=email, attendee_name=name, title=body.title,
            start_iso=body.start_iso, duration_min=body.duration_min, tz=body.tz,
            description=body.description, add_video=body.add_video,
            notify=body.notify, provider=body.provider)
    except ValueError as exc:
        msg = str(exc)
        code = 409 if ("connected" in msg or "reconnection" in msg) else 400
        raise HTTPException(code, msg)


# ── Granola (MCP: DCR + PKCE) — its own connect/callback, NOT the generic OAuth one.
# Declared BEFORE the /{provider} routes so the literal 'granola' wins.
@router.get("/granola/connect")
def granola_connect(
    request: Request,
    user: models.User = Depends(current_user),
):
    """Granola uses Dynamic Client Registration + PKCE (no preset client creds), so it
    has a dedicated connect. Returns the consent URL to send the host to."""
    from ..integrations import granola
    redirect_uri = f"{_surplus_base_url(request)}/api/integrations/granola/callback"
    return {"url": granola.authorize_url(redirect_uri=redirect_uri, user_id=user.id)}


@router.get("/granola/callback")
def granola_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Granola OAuth redirect target. Verifies the signed state, recomputes the PKCE
    verifier from its nonce, exchanges the code, stores the tokens. User comes from
    the signed state (no session dependency)."""
    from ..integrations import granola, oauth
    base = _surplus_base_url(request)
    if error or not code:
        return RedirectResponse(
            f"{base}/settings?integration=granola&status=denied", status_code=302)
    payload = granola.verify_state(state)
    if not payload:
        raise HTTPException(400, "invalid or expired state")
    user = db.get(models.User, int(payload.get("u") or 0))
    if user is None:
        raise HTTPException(400, "unknown user for this state")
    redirect_uri = f"{base}/api/integrations/granola/callback"
    tokens = granola.exchange_code(
        code=code, redirect_uri=redirect_uri, nonce=payload.get("n") or "")
    email = ""
    try:                                   # best-effort label; live shape unverified
        info = granola.call_tool(tokens.get("access_token", ""), "get_account_info", {})
        email = (info.get("email") or "") if isinstance(info, dict) else ""
    except Exception:  # noqa: BLE001
        email = ""
    oauth.save_tokens(db, user_id=user.id, provider="granola",
                      account_email=email, tokens=tokens)
    return RedirectResponse(
        f"{base}/settings?integration=granola&status=connected", status_code=302)


@router.get("/{provider}/connect")
def connect(
    provider: str,
    request: Request,
    user: models.User = Depends(current_user),
):
    """Return the consent URL to send the host to. State is signed with their id."""
    if not oauth.configured(provider):
        raise HTTPException(409, f"{provider} OAuth is not configured on this server")
    url = oauth.authorize_url(
        provider, redirect_uri=_redirect_uri(request, provider), user_id=user.id)
    return {"url": url}


@router.get("/{provider}/callback")
def callback(
    provider: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """OAuth redirect target. Verifies the signed state, exchanges the code, stores
    the tokens, and bounces the host back to the app. No session dependency -- the
    signed state is the user binding."""
    base = _surplus_base_url(request)
    if error or not code:
        return RedirectResponse(
            f"{base}/settings?integration={provider}&status=denied", status_code=302)
    payload = oauth.verify_state(state)
    if not payload or payload.get("p") != provider:
        raise HTTPException(400, "invalid or expired state")
    user = db.get(models.User, int(payload.get("u") or 0))
    if user is None:
        raise HTTPException(400, "unknown user for this state")
    tokens = oauth.exchange_code(
        provider, code=code, redirect_uri=_redirect_uri(request, provider))
    email = oauth.fetch_account_email(provider, tokens.get("access_token", ""))
    oauth.save_tokens(db, user_id=user.id, provider=provider,
                      account_email=email, tokens=tokens)
    return RedirectResponse(
        f"{base}/settings?integration={provider}&status=connected", status_code=302)


def _account_syncer(provider: str):
    """Per-provider 'sync one connected account' fn (lazy import). None when a
    provider has no read sync."""
    if provider == "google":
        from ..integrations.google_sync import sync_google_account
        return sync_google_account
    if provider == "microsoft":
        from ..integrations.outlook_sync import sync_outlook_account
        return sync_outlook_account
    if provider == "calendly":
        from ..integrations.calendly_sync import sync_calendly_account
        return sync_calendly_account
    return None


@router.post("/{provider}/sync")
def provider_sync(
    provider: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Pull the caller's connected account(s) for `provider` into the spine: recent
    mail -> contacts/timeline, upcoming meetings -> dated triggers. Owner-scoped;
    409 if none connected; 404 if the provider has no read sync. READ-only."""
    fn = _account_syncer(provider)
    if fn is None:
        raise HTTPException(404, f"no read sync for provider {provider!r}")
    accts = (db.query(models.ConnectedAccount)
             .filter_by(user_id=user.id, provider=provider, status="active").all())
    if not accts:
        raise HTTPException(409, f"no connected {provider} account")
    return {"accounts": [{"account_email": a.account_email,
                          **fn(db, user, a)} for a in accts]}


@router.delete("/{provider}/{account_id}")
def disconnect(
    provider: str,
    account_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    row = db.get(models.ConnectedAccount, account_id)
    if row is None or row.user_id != user.id or row.provider != provider:
        raise HTTPException(404, "connection not found")
    db.delete(row)
    db.commit()
    return {"disconnected": True}
