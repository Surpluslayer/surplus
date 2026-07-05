"""integrations/email_sender.py : transactional email via Resend (HTTPS API).

Used for the email-verification + password-reset links. HTTPS (not SMTP) because Railway
blocks outbound port 25. DORMANT until RESEND_API_KEY is set -- send_email() then no-ops
and returns False, so the auth flows work (account is created) but no mail goes out until
the key is configured, exactly like the other connectors.
"""
from __future__ import annotations

import os

import httpx

_API = "https://api.resend.com/emails"


def configured() -> bool:
    return bool((os.environ.get("RESEND_API_KEY") or "").strip())


def _from() -> str:
    return (os.environ.get("SURPLUS_FROM_EMAIL") or "surplus <onboarding@resend.dev>").strip()


def send_email(*, to: str, subject: str, html: str = "", text: str = "") -> bool:
    """Send one transactional email. Returns True on success, False if not configured
    (dormant) or on any provider error -- never raises, so an email hiccup can't break
    signup/reset."""
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not key or not (to or "").strip():
        return False
    body = {"from": _from(), "to": [to], "subject": subject}
    if html:
        body["html"] = html
    if text:
        body["text"] = text
    if not html and not text:
        return False
    try:
        r = httpx.post(_API, headers={"Authorization": f"Bearer {key}",
                                      "content-type": "application/json"},
                       json=body, timeout=20)
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # Surface WHY a send failed instead of silently swallowing it. The most
        # common prod cause is the Resend SANDBOX sender (onboarding@resend.dev):
        # it only delivers to your own Resend-account email and 403s every other
        # recipient, so "no one gets their PIN" is invisible until you log the
        # provider's response body. Never raises: a mail hiccup must not break
        # signup/reset.
        resp = getattr(exc, "response", None)
        detail = ""
        if resp is not None:
            try:
                detail = f"status={resp.status_code} body={resp.text[:300]}"
            except Exception:  # noqa: BLE001
                detail = ""
        print(f"  [email_sender] send FAILED to={to} from={_from()} "
              f"err={type(exc).__name__} {detail}", flush=True)
        return False
