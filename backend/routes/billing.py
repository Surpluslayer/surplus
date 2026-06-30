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
    stripe_customer_id so require_can_send_linkedin() lets the user through.

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

from .. import billing_plans as bp
from .. import models
from ..auth import (
    SESSION_COOKIE,
    create_session,
    current_user,
    set_session_cookie,
    _load_user_by_session,
)
from ..db import get_db
from ..rate_limit import per_ip_rate_limit

router = APIRouter(prefix="/api/billing", tags=["billing"])

# Anonymous checkout : per Tech-Week threat model, a bot creating prepay
# users to spam the prepay-* email domain. 10/min per IP : generous for
# legitimate retry patterns (Stripe redirect failures, refresh during
# pay-flow) and tight enough to bound bot pressure.
_rl_checkout = per_ip_rate_limit(limit=10, window_s=60, tag="checkout_session")


def _env(key: str) -> Optional[str]:
    v = (os.environ.get(key) or "").strip()
    return v or None


# Internal placeholder email domains. We mint these on anonymous prepay
# (routes/billing.py:checkout-session) and on triage-quick-start
# (routes/auth.py:triage_quick_start) so every row has a unique-ish email,
# but they're NOT real email addresses. Don't ever ship them to Stripe's
# prefilled_email param — Jiahui flagged that surfacing
# `prepay-b22b...@anonymous.surplus` in the Checkout email field makes
# the form look broken / spammy. Stripe should ask the user for their
# actual email instead.
_PLACEHOLDER_EMAIL_DOMAINS = (
    "anonymous.surplus",
    "demo.surpluslayer.com",
)


def _is_real_email(email: Optional[str]) -> bool:
    """True iff this email looks like one the user actually owns.
    Rejects our internal placeholders (prepay-*, triage-*, demo-*)."""
    if not email:
        return False
    e = email.strip().lower()
    if "@" not in e:
        return False
    domain = e.rsplit("@", 1)[-1]
    return not any(domain == d or domain.endswith("." + d)
                   for d in _PLACEHOLDER_EMAIL_DOMAINS)


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


def build_checkout_url(request: Request, db: DbSession,
                       user: models.User) -> str:
    """Return the Stripe URL to send `user` to, tagged with their id so the
    webhook stamps THIS row's paid_at.

    Two modes (same as create_checkout_session) :
      - STRIPE_PAYMENT_LINK : append client_reference_id (+ prefilled_email)
        to the preconfigured dashboard link. No Stripe API call.
      - STRIPE_PRICE_ID     : create a Checkout Session via the API.

    Shared by the SPA checkout endpoint AND the LinkedIn-callback pay-gate
    (pay-at-connect, tied to the LinkedIn identity), so both produce an
    identically-tagged checkout the webhook can resolve back to the user.
    """
    payment_link = _env("STRIPE_PAYMENT_LINK")
    if payment_link:
        from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
        parsed = urlparse(payment_link)
        params = dict(parse_qsl(parsed.query))
        params["client_reference_id"] = str(user.id)
        if _is_real_email(user.email):
            params["prefilled_email"] = user.email
        return urlunparse(parsed._replace(query=urlencode(params)))

    price_id = _env("STRIPE_PRICE_ID")
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="neither STRIPE_PAYMENT_LINK nor STRIPE_PRICE_ID configured",
        )
    stripe = _stripe()
    success_url, cancel_url = _success_cancel_urls(request)
    real_email = user.email if _is_real_email(user.email) else None
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=str(user.id),
            customer=user.stripe_customer_id or None,
            customer_email=real_email if not user.stripe_customer_id else None,
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
    return session.url


@router.post("/checkout-session",
             dependencies=[Depends(_rl_checkout)])
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
    url = build_checkout_url(request, db, user)
    resp = JSONResponse({"url": url})
    if new_session_token:
        set_session_cookie(resp, new_session_token, host=request.headers.get("host"))
    return resp


def _sub_period(sub: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """Pull (period_start, period_end) out of a Stripe Subscription object,
    converting the unix timestamps to aware UTC. Missing → (None, None)."""
    def _ts(v) -> Optional[datetime]:
        try:
            return datetime.fromtimestamp(int(v), tz=timezone.utc) if v else None
        except (TypeError, ValueError):
            return None
    return _ts(sub.get("current_period_start")), _ts(sub.get("current_period_end"))


def _sub_price_id(sub: dict) -> Optional[str]:
    """The price id off the first line item of a Subscription object."""
    items = ((sub.get("items") or {}).get("data") or [])
    if not items:
        return None
    price = (items[0] or {}).get("price") or {}
    return price.get("id")


def _apply_subscription(user: models.User, sub: dict) -> None:
    """Stamp recurring-plan fields onto `user` from a Stripe Subscription.

    Maps the price → plan via bp.price_to_plan, records the subscription id +
    status + billing window, and resets the metered counters ONLY when the
    period actually rolls (start changed) so frequent subscription.updated
    events don't wipe mid-period usage. Caller commits. Does NOT touch
    paid_at — the recurring relationship-layer plan is independent of the
    legacy one-time LinkedIn-send unlock."""
    price_id = _sub_price_id(sub)
    plan = bp.price_to_plan(price_id)
    p_start, p_end = _sub_period(sub)

    prev_start = getattr(user, "billing_period_start", None)
    period_rolled = (
        p_start is not None
        and (prev_start is None or _aware(prev_start) != p_start))

    user.plan = plan
    user.subscription_status = (sub.get("status") or "active")
    sub_id = sub.get("id")
    if sub_id:
        user.stripe_subscription_id = sub_id
    if price_id:
        user.stripe_price_id = price_id
    cust = sub.get("customer")
    if cust and not user.stripe_customer_id:
        user.stripe_customer_id = cust
    if p_start is not None:
        user.billing_period_start = p_start
    if p_end is not None:
        user.billing_period_end = p_end
    if period_rolled:
        user.drafts_used_this_period = 0
        user.contacts_scanned_this_period = 0


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes (SQLite round-trips drop tzinfo) → treat as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _retrieve_subscription(sub_id: Optional[str]) -> Optional[dict]:
    """Fetch a full Subscription object from Stripe (a checkout.session only
    carries the id). Returns a plain dict, or None if we can't (no id, SDK
    missing, API error) — the caller degrades gracefully."""
    if not sub_id:
        return None
    try:
        stripe = _stripe()
        sub = stripe.Subscription.retrieve(sub_id)
        # Stripe objects behave dict-like; normalize to a plain dict so the
        # downstream helpers can .get() uniformly.
        return dict(sub)
    except Exception as exc:  # noqa: BLE001 : SDK missing / API / network
        print(f"  [billing.webhook] Subscription.retrieve({sub_id}) failed : "
              f"{type(exc).__name__}: {exc}")
        return None


def _user_by_subscription(db: DbSession, sub: dict) -> Optional[models.User]:
    """Resolve the owning user for a Subscription event: by stored
    stripe_subscription_id first, then by stripe_customer_id."""
    sub_id = sub.get("id")
    if sub_id:
        u = (db.query(models.User)
             .filter(models.User.stripe_subscription_id == sub_id).first())
        if u:
            return u
    cust = sub.get("customer")
    if cust:
        return (db.query(models.User)
                .filter(models.User.stripe_customer_id == cust).first())
    return None


@router.post("/webhook")
async def stripe_webhook(request: Request,
                         db: DbSession = Depends(get_db)) -> JSONResponse:
    """Signature-verified webhook. Handles two billing surfaces:

      - One-time LinkedIn-send unlock (legacy) : a checkout.session.completed
        in `mode=payment` stamps paid_at + stripe_customer_id on the user
        identified by client_reference_id / metadata.user_id.

      - Recurring relationship-layer plan : a checkout.session.completed in
        `mode=subscription` (and the later customer.subscription.updated /
        .deleted events) maps the Stripe price → plan and stamps the plan /
        status / billing window via _apply_subscription. paid_at is left
        untouched — the two surfaces are independent.

    Idempotent : Stripe retries on non-2xx, so re-running this with the
    same event must not double-write. We coalesce customer_id, only reset
    metered counters when the billing period actually rolls, and ack
    unknown event types quietly so Stripe stops retrying."""
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

    # ── Idempotency ledger ────────────────────────────────────────────────
    # Stripe delivery is at-least-once: timeouts / deploys / transient
    # non-2xx all trigger re-delivery of the SAME event (possibly hours
    # later, possibly out of order). Ack anything we've fully processed
    # before, WITHOUT re-running side effects — re-applying a stale
    # subscription.updated can overwrite a newer plan, and equality-based
    # heuristics (period_rolled) shouldn't be the only line of defense on
    # the money path. The marker row is committed in the same transaction
    # as the handler's mutations (each branch's db.commit() below), so a
    # crash mid-handler rolls back BOTH and the retry processes cleanly.
    evt_id = event.get("id")
    if evt_id:
        seen = db.get(models.StripeWebhookEvent, evt_id)
        if seen is not None:
            print(f"  [billing.webhook] duplicate {evt_id} ({et}) : acking, "
                  "no re-processing")
            return JSONResponse({"ok": True, "duplicate": True})
        db.add(models.StripeWebhookEvent(event_id=evt_id, event_type=et))

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
        cust = obj.get("customer")
        if cust and not user.stripe_customer_id:
            user.stripe_customer_id = cust
        # Upgrade the user's email if they typed a real one at Checkout
        # AND we currently have the prepay-* placeholder on file. Stripe
        # surfaces the buyer email under `customer_details.email` (and
        # historically also under `customer_email`). Either works.
        stripe_email = (
            (obj.get("customer_details") or {}).get("email")
            or obj.get("customer_email")
        )
        if stripe_email and not _is_real_email(user.email):
            print(f"  [billing.webhook] upgrading placeholder email "
                  f"{user.email!r} → {stripe_email!r} for user.id={uid_int}")
            user.email = stripe_email.strip().lower()
        # Same for name : if we minted "Surplus user" earlier and the
        # buyer supplied one at Checkout, prefer the real one.
        stripe_name = (obj.get("customer_details") or {}).get("name")
        if stripe_name and (user.name or "").strip() in ("", "Surplus user"):
            user.name = stripe_name.strip()

        # Subscription vs one-time. A subscription checkout carries a
        # `subscription` id (and mode == "subscription"); retrieve the full
        # object so we can map price → plan + stamp the billing window. A
        # one-time payment stamps the legacy paid_at LinkedIn-send unlock.
        sub_id = obj.get("subscription")
        if sub_id or obj.get("mode") == "subscription":
            sub = _retrieve_subscription(sub_id)
            if sub is not None:
                _apply_subscription(user, sub)
                print(f"  [billing.webhook] applied subscription "
                      f"plan={user.plan!r} to user.id={uid_int}")
            else:
                # Couldn't expand the subscription : at least mark them active
                # so the SPA stops paywalling; updated event will fill details.
                user.subscription_status = "active"
                if sub_id:
                    user.stripe_subscription_id = sub_id
        else:
            user.paid_at = datetime.now(timezone.utc)
            print(f"  [billing.webhook] stamped paid_at on user.id={uid_int}")
        db.commit()

    elif et in ("customer.subscription.updated",
                "customer.subscription.created"):
        user = _user_by_subscription(db, obj)
        if not user:
            print(f"  [billing.webhook] no user for sub {obj.get('id')}")
            return JSONResponse({"ok": True, "noop": True})
        _apply_subscription(user, obj)
        db.commit()
        print(f"  [billing.webhook] {et} → plan={user.plan!r} "
              f"status={user.subscription_status!r} user.id={user.id}")

    elif et == "customer.subscription.deleted":
        user = _user_by_subscription(db, obj)
        if not user:
            print(f"  [billing.webhook] no user for sub {obj.get('id')}")
            return JSONResponse({"ok": True, "noop": True})
        user.plan = "free"
        user.subscription_status = "canceled"
        user.stripe_subscription_id = None
        user.stripe_price_id = None
        user.drafts_used_this_period = 0
        user.contacts_scanned_this_period = 0
        db.commit()
        print(f"  [billing.webhook] subscription canceled → free "
              f"user.id={user.id}")

    # Unknown event types ack quietly so Stripe stops retrying. Persist the
    # ledger marker for them too (the branches above already committed it
    # alongside their mutations; noop early-returns deliberately don't — a
    # 200-acked noop is never retried, and re-running one writes nothing).
    db.commit()
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
