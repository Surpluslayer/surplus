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

_PROVIDERS = ["google", "microsoft", "calendly", "zoom"]   # static-client OAuth providers


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
        # which providers the server can actually run (OAuth ones need client creds).
        "available": {name: oauth.configured(name) for name in _PROVIDERS},
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
    with_zoom: bool = False                 # use a Zoom link instead (if Zoom connected)
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
            with_zoom=body.with_zoom, notify=body.notify, provider=body.provider)
    except ValueError as exc:
        msg = str(exc)
        code = 409 if ("connected" in msg or "reconnection" in msg) else 400
        raise HTTPException(code, msg)


# ── LinkedIn one-tap connect from a captured browser cookie (the plugin path).
# Declared BEFORE the /{provider} routes so the literal 'linkedin' wins.
class CookieConnectIn(BaseModel):
    li_at: str               # the user's LinkedIn session cookie, captured by the plugin
    user_agent: str = ""


@router.get("/linkedin/status")
def linkedin_status(user: models.User = Depends(current_user)):
    """Whether THIS user has a live LinkedIn (Unipile) connection -- the plugin checks
    this before offering to connect, so it never creates a duplicate."""
    return {
        "connected": bool(user.unipile_account_id) and user.linkedin_status == "active",
        "account_id": user.unipile_account_id,
        "status": user.linkedin_status,
    }


@router.post("/linkedin/connect-cookie")
def linkedin_connect_cookie(
    body: CookieConnectIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Connect LinkedIn from the plugin's captured cookie. Dedup guard (one User = one
    Unipile account): if already actively connected, no-op and reuse -- never create a
    second account. Otherwise hand the cookie to Unipile and bind the account to the user."""
    if user.unipile_account_id and user.linkedin_status == "active":
        return {"connected": True, "account_id": user.unipile_account_id, "reused": True}
    from ..integrations import linkedin_cookie
    try:
        res = linkedin_cookie.connect_with_cookie(
            li_at=body.li_at, user_agent=body.user_agent)
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(409 if "not configured" in msg else 400, msg)

    new_account_id = res["account_id"]

    # Orphan-dedup, mirroring the hosted-auth flow (routes/auth.py): reconnecting
    # with a DIFFERENT account_id must release the user's previous Unipile seat so
    # we don't leak a billed account. Capture it for a best-effort delete after
    # commit; a delete failure must never roll back the connect.
    orphan_unipile_account_id: Optional[str] = None
    if user.unipile_account_id and user.unipile_account_id != new_account_id:
        orphan_unipile_account_id = user.unipile_account_id

    # If this account_id already belongs to ANOTHER user, release it from them
    # (same dedup the webhook/callback do) so one Unipile account maps to one User.
    prior = (db.query(models.User)
             .filter(models.User.unipile_account_id == new_account_id,
                     models.User.id != user.id).first())
    if prior is not None:
        prior.unipile_account_id = None
        prior.linkedin_status = "disconnected"

    user.unipile_account_id = new_account_id
    user.linkedin_status = "active"
    db.commit()

    # Fire-and-forget : drop the orphan Unipile account AFTER commit so a Unipile
    # delete failure can't roll back the user-attachment. Best-effort.
    if orphan_unipile_account_id:
        linkedin_cookie.delete_account(orphan_unipile_account_id)

    return {"connected": True, "account_id": new_account_id, "reused": False}


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
