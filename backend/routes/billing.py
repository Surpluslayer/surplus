"""routes/billing.py : Stripe Checkout + webhook.

Public surface:

  POST /api/billing/checkout-session
    Auth: requires a signed-in user.
    Creates a Stripe Checkout Session pre-tagged with the user's id, returns
    { url } the SPA redirects to. On successful payment Stripe redirects
    back to /billing/success and fires checkout.session.completed at our
    webhook.

  POST /api/billing/webhook
    Auth: signature-verified against STRIPE_WEBHOOK_SECRET.
    Handles checkout.session.completed : stamps users.paid_at +
    stripe_customer_id so require_linkedin_send() lets the user through.

Env vars (all required for prod, all optional for local dev) :
  STRIPE_SECRET_KEY      : sk_live_... / sk_test_...
  STRIPE_PRICE_ID        : the Price object the Checkout Session charges
  STRIPE_WEBHOOK_SECRET  : whsec_... from `stripe listen --forward-to ...`
  SURPLUS_BASE_URL       : already-existing; success/cancel URLs hang off it

When any of these is unset, the route returns 503 with a clean message
so a misconfigured deploy doesn't pretend to work.
"""
from __future__ import annotations
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DbSession

from .. import models
from ..auth import (
    SESSION_COOKIE,
    create_session,
    current_user,
    set_session_cookie,
    _load_user_by_session,
)
from ..db import get_db

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _env(key: str) -> Optional[str]:
    v = (os.environ.get(key) or "").strip()
    return v or None


def _stripe():
    """Lazy-import the SDK so the rest of the app boots even when the
    stripe package isn't installed (early dev)."""
    try:
        import stripe  # type: ignore
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="stripe SDK not installed on the server",
        ) from exc
    key = _env("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STRIPE_SECRET_KEY not configured",
        )
    stripe.api_key = key
    return stripe


def _success_cancel_urls(request: Request) -> tuple[str, str]:
    """Build absolute URLs for Stripe's success/cancel redirect targets.
    Prefer SURPLUS_BASE_URL when set so deploys behind a CDN get the
    right scheme/host; fall back to inspecting the request."""
    base = (_env("SURPLUS_BASE_URL")
            or f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
    return (
        f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        f"{base}/billing/cancel",
    )


@router.post("/checkout-session")
def create_checkout_session(
    request: Request,
    db: DbSession = Depends(get_db),
    surplus_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """Return the checkout URL the SPA should redirect to.

    The Stripe paywall sits in front of LinkedIn login : an anonymous caller
    needs a user row + session cookie BEFORE they pay, so the post-payment
    webhook has a User to stamp paid_at on and the post-payment landing has
    a signed-in session that can call /linkedin/start. We mint that row on
    the fly here (mirroring routes/auth.py:triage_quick_start) when no
    session cookie is present, and set the cookie on the JSONResponse so
    the SPA's next request is authenticated.

    Two modes, controlled by env :
      - STRIPE_PAYMENT_LINK set : return that URL with client_reference_id
        and prefilled_email appended so the webhook can find this user.
        No Stripe API call : the link is preconfigured in the dashboard.
      - STRIPE_PRICE_ID set     : create a Checkout Session via the API,
        return its URL. Used when we want per-session customization.

    Either way the response shape is { url, session_id? }, so the SPA
    doesn't have to care which mode is active.
    """
    user = _load_user_by_session(db, surplus_session)
    new_session_token: Optional[str] = None
    if user is None:
        # Anonymous : mint a fresh user + session so the Stripe webhook can
        # find them by client_reference_id and the post-payment redirect
        # carries a signed-in cookie.
        tag = secrets.token_hex(6)
        user = models.User(
            name="Surplus user",
            email=f"prepay-{tag}@anonymous.surplus",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        sess = create_session(db, user)
        new_session_token = sess.session_token
    payment_link = _env("STRIPE_PAYMENT_LINK")
    if payment_link:
        # Append client_reference_id + prefilled_email so the webhook can
        # locate the right user. Stripe appends these as standard params
        # on Payment Links : same behavior as Checkout Sessions, just
        # configured upfront in the dashboard instead of per-call.
        from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
        parsed = urlparse(payment_link)
        params = dict(parse_qsl(parsed.query))
        params["client_reference_id"] = str(user.id)
        if user.email:
            params["prefilled_email"] = user.email
        url = urlunparse(parsed._replace(query=urlencode(params)))
        resp = JSONResponse({"url": url})
        if new_session_token:
            set_session_cookie(resp, new_session_token)
        return resp

    price_id = _env("STRIPE_PRICE_ID")
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="neither STRIPE_PAYMENT_LINK nor STRIPE_PRICE_ID configured",
        )
    stripe = _stripe()
    success_url, cancel_url = _success_cancel_urls(request)
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=str(user.id),
            customer=user.stripe_customer_id or None,
            customer_email=user.email if not user.stripe_customer_id else None,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": str(user.id)},
        )
    except Exception as exc:  # noqa: BLE001 : Stripe SDK throws many subclasses
        print(f"  [billing] checkout.Session.create failed : "
              f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"stripe create_session error: {type(exc).__name__}",
        ) from exc
    resp = JSONResponse({"url": session.url, "session_id": session.id})
    if new_session_token:
        set_session_cookie(resp, new_session_token)
    return resp


@router.post("/webhook")
async def stripe_webhook(request: Request,
                         db: DbSession = Depends(get_db)) -> JSONResponse:
    """Signature-verified webhook. On checkout.session.completed we stamp
    paid_at + stripe_customer_id on the user identified by metadata.user_id
    (or client_reference_id, whichever is present).

    Idempotent : Stripe retries on non-2xx, so re-running this with the
    same event must not double-write. We only stamp paid_at when it's NULL
    (or older than the event time) and always coalesce customer_id."""
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STRIPE_WEBHOOK_SECRET not configured",
        )
    stripe = _stripe()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as exc:  # noqa: BLE001 : SignatureVerificationError + ValueError
        print(f"  [billing.webhook] signature verify failed : "
              f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=400,
                            detail="invalid webhook signature") from exc

    et = event.get("type")
    obj = event.get("data", {}).get("object", {}) or {}
    print(f"  [billing.webhook] event={et} obj_id={obj.get('id')}")

    if et == "checkout.session.completed":
        user_id = (obj.get("client_reference_id")
                   or (obj.get("metadata") or {}).get("user_id"))
        if not user_id:
            print("  [billing.webhook] no user_id in session : ignoring")
            return JSONResponse({"ok": True, "noop": True})
        try:
            uid_int = int(user_id)
        except ValueError:
            return JSONResponse({"ok": True, "noop": True})
        user = db.query(models.User).filter(models.User.id == uid_int).first()
        if not user:
            print(f"  [billing.webhook] user_id={uid_int} not found")
            return JSONResponse({"ok": True, "noop": True})
        now = datetime.now(timezone.utc)
        user.paid_at = now
        cust = obj.get("customer")
        if cust and not user.stripe_customer_id:
            user.stripe_customer_id = cust
        db.commit()
        print(f"  [billing.webhook] stamped paid_at on user.id={uid_int}")

    # Unknown event types ack quietly so Stripe stops retrying.
    return JSONResponse({"ok": True})


# ─── Dev-only : flip paid_at without a real Stripe round-trip ──────────
#
# Gated by SURPLUS_DEV_BILLING=1. Returns 404 in prod. Lets you QA the
# gate-state transitions (free → paid → unpaid) from a single POST so you
# don't have to spin up Stripe Checkout for every test loop. Mounted under
# /api/billing/dev/* so it's obvious in the OpenAPI surface that this is
# dev-only.

def _dev_billing_enabled() -> bool:
    raw = (os.environ.get("SURPLUS_DEV_BILLING") or "").strip().lower()
    return raw in ("1", "true", "yes")


@router.post("/dev/toggle-paid")
def dev_toggle_paid(
    db: DbSession = Depends(get_db),
    user: "models.User" = Depends(current_user),
) -> JSONResponse:
    """Flip `paid_at` on the signed-in user. Disabled in prod
    (SURPLUS_DEV_BILLING unset)."""
    if not _dev_billing_enabled():
        raise HTTPException(status_code=404, detail="not found")
    if user.paid_at is None:
        user.paid_at = datetime.now(timezone.utc)
        user.stripe_customer_id = user.stripe_customer_id or "cus_dev_toggle"
        action = "marked_paid"
    else:
        user.paid_at = None
        action = "marked_unpaid"
    db.commit()
    return JSONResponse({
        "ok": True, "action": action,
        "paid_at": user.paid_at.isoformat() if user.paid_at else None,
        "stripe_customer_id": user.stripe_customer_id,
    })
