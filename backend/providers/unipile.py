"""
providers/unipile.py : Unipile implementation of LinkedInProvider.

Unipile is action-based, not campaign-based. Every send is a direct API
call. No template setup in their UI, no sequence engine, no campaign IDs
to manage. Our backend owns the entire sequence:

    1. surplus calls send_connection(lead)
       └─ Unipile: GET /api/v1/users/{public_id}     → look up provider_id
       └─ Unipile: POST /api/v1/users/invite          → submit connection
       └─ persist linkedin_provider_id on Prospect for webhook matching
    2. recipient accepts the connection on LinkedIn
       └─ Unipile fires `new_relation` webhook
    3. surplus's /webhooks/unipile route:
       └─ normalizes to canonical invite_accepted
       └─ since provider.auto_dm_after_accept is True, immediately:
            └─ provider.send_message(lead, linkedin_provider_id)
                  └─ Unipile: POST /api/v1/chats     → DM lands

DRY_RUN behavior (default, gated by UNIPILE_DRY_RUN):
    - All HTTP paths short-circuit before the network.
    - send_connection returns a deterministic-shape ProviderResult.
    - linkedin_provider_id is set to a fake "dry_li_<hex>" so the rest of
      the pipeline (persistence + webhook matching) can be exercised
      without ever calling Unipile.

Live mode: requires UNIPILE_DSN, UNIPILE_API_KEY, UNIPILE_ACCOUNT_ID.
"""
from __future__ import annotations
import hmac
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .base import (
    LinkedInProvider,
    LeadPayload,
    ProviderResult,
    CanonicalEvent,
    CANONICAL_STATES,
    AmbiguousSendError,
)


# Unipile webhook event -> our canonical state.
# Unipile's dialect varies by webhook source : the "messaging" source emits
# `message_received` for inbound DMs, while older/relations payloads use
# `new_message`. We accept both so auto-reply fires regardless of which the
# dashboard subscription produces.
_EVENT_MAP: dict[str, str] = {
    "new_relation":     "invite_accepted",   # they accepted the invite
    "relation":         "invite_accepted",
    "invite_sent":      "invite_sent",
    "new_message":      "message_replied",    # incoming msg = they replied
    "message_received": "message_replied",    # "messaging" source dialect
    "message_sent":     "message_sent",
}


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _linkedin_handle(linkedin_url: str) -> str:
    """Extract the public identifier from a LinkedIn URL.

    https://www.linkedin.com/in/maya-rodriguez/  ->  maya-rodriguez
    """
    return (linkedin_url or "").rstrip("/").split("/")[-1]


class UnipileProvider(LinkedInProvider):
    name = "unipile"

    def __init__(
        self,
        dsn: Optional[str] = None,
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        dry_run: bool = True,
        require_signature: bool = True,
    ) -> None:
        # Normalize the DSN : Unipile's dashboard shows it as
        # `api40.unipile.com:17054` without a scheme, so prepend https://
        # if the caller forgot. Also strip trailing slash.
        raw_dsn = (dsn or "").strip().rstrip("/")
        if raw_dsn and not raw_dsn.startswith(("http://", "https://")):
            raw_dsn = f"https://{raw_dsn}"
        self.dsn = raw_dsn
        self.api_key = api_key
        self.account_id = account_id
        self.webhook_secret = webhook_secret
        self._dry_run = dry_run
        self.require_signature = require_signature

    @classmethod
    def from_env(cls) -> "UnipileProvider":
        return cls(
            dsn=os.environ.get("UNIPILE_DSN"),
            api_key=os.environ.get("UNIPILE_API_KEY"),
            account_id=os.environ.get("UNIPILE_ACCOUNT_ID"),
            webhook_secret=os.environ.get("UNIPILE_WEBHOOK_SECRET"),
            # default TRUE : never send by accident
            dry_run=_env_bool("UNIPILE_DRY_RUN", True),
            require_signature=_env_bool("UNIPILE_REQUIRE_SIGNATURE", True),
        )

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def auto_dm_after_accept(self) -> bool:
        # KILL SWITCH: post-accept auto-DM disabled. Previously returned True,
        # which fired an unattended DM from the host's LinkedIn on every
        # invite_accepted. Hard-off so it can't be re-enabled by an env flip;
        # manual sends are unaffected. Flip back to True to restore.
        return False

    # ---- payload building ------------------------------------------------

    def build_lead_payload(self, prospect, event, note: str, message: str) -> LeadPayload:
        parts = (prospect.name or "").split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        return LeadPayload(
            event_id=event.id,
            prospect_id=prospect.id,
            identity=prospect.identity,
            first_name=first,
            last_name=last,
            full_name=prospect.name or "",
            linkedin_url=prospect.linkedin_url or "",
            company=prospect.company,
            position=prospect.role,
            note=note,
            message=message,
            works_on=prospect.works_on,
            offers=prospect.offers,
            seeks=prospect.seeks,
            fit_score=prospect.fit_score,
            fit_reason=prospect.fit_reason,
            sources=prospect.sources,
        )

    # ---- send_connection -------------------------------------------------

    def _build_invite_payload(self, lead: LeadPayload, provider_id: str) -> dict:
        """POST /api/v1/users/invite request body.

        An empty note means "connect without a note" : omit the `message` key
        entirely so the invite goes out bare (LinkedIn rejects an empty-string
        note, and sending no key is the documented way to skip it)."""
        body = {
            "account_id": self.account_id or "<UNSET-UNIPILE_ACCOUNT_ID>",
            "provider_id": provider_id,
        }
        if (lead.note or "").strip():
            body["message"] = lead.note
        return body

    def send_connection(self, lead: LeadPayload) -> ProviderResult:
        handle = _linkedin_handle(lead.linkedin_url)

        if self._dry_run:
            # produce realistic-shape result without touching the network
            fake_provider_id = f"dry_li_{uuid.uuid4().hex[:16]}"
            fake_action_id = f"dry_{uuid.uuid4().hex[:12]}"
            payload = self._build_invite_payload(lead, fake_provider_id)
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="dry_run_queued",
                provider=self.name,
                provider_lead_id=fake_action_id,
                dry_run=True,
                payload={"_lookup_handle": handle, **payload},
                linkedin_provider_id=fake_provider_id,
            )

        # --- live path: only reached when UNIPILE_DRY_RUN=false ----------
        try:
            provider_id = self._lookup_provider_id(handle)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="failed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload={"_lookup_handle": handle},
                error=f"profile lookup failed: {exc}",
            )

        payload = self._build_invite_payload(lead, provider_id)
        try:
            data = self._post("/api/v1/users/invite", payload)
        except AmbiguousSendError as exc:
            # The invite was dispatched but the response was lost : it MAY be
            # live on LinkedIn. "unconfirmed" (not "failed") so send paths
            # refuse a blind retry — a retry here is a double-invite.
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="unconfirmed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload=payload,
                error=f"invite unconfirmed: {exc}",
                linkedin_provider_id=provider_id,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="failed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload=payload,
                error=f"invite failed: {exc}",
                linkedin_provider_id=provider_id,
            )

        action_id = (data or {}).get("invitation_id") or (data or {}).get("id")
        return ProviderResult(
            prospect_id=lead.prospect_id,
            state="invite_sent",
            provider=self.name,
            provider_lead_id=str(action_id) if action_id else None,
            dry_run=False,
            payload=payload,
            linkedin_provider_id=provider_id,
        )

    # ---- send_message (post-accept DM) ----------------------------------

    def _build_chat_payload(self, lead: LeadPayload, provider_id: str) -> dict:
        """POST /api/v1/chats request body."""
        return {
            "account_id": self.account_id or "<UNSET-UNIPILE_ACCOUNT_ID>",
            "text": lead.message,
            "attendees_ids": [provider_id],
        }

    def resolve_linkedin_user(self, linkedin_url: str) -> str:
        """
        Resolve a LinkedIn profile URL to Unipile's internal provider_id.

        In dry-run, returns a deterministic fake id so the rest of the
        pipeline can be exercised without HTTP.
        """
        handle = _linkedin_handle(linkedin_url)
        if self._dry_run:
            return f"dry_li_{handle}"
        return self._lookup_provider_id(handle)

    def send_message(self, lead: LeadPayload, linkedin_provider_id: Optional[str] = None) -> ProviderResult:
        provider_id = linkedin_provider_id
        if not provider_id and not self._dry_run:
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="failed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload={},
                error="missing linkedin_provider_id (was send_connection ever called?)",
            )

        if self._dry_run:
            provider_id = provider_id or f"dry_li_{uuid.uuid4().hex[:16]}"
            payload = self._build_chat_payload(lead, provider_id)
            fake_id = f"dry_{uuid.uuid4().hex[:12]}"
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="dry_run_queued",
                provider=self.name,
                provider_lead_id=fake_id,
                dry_run=True,
                payload=payload,
                linkedin_provider_id=provider_id,
            )

        payload = self._build_chat_payload(lead, provider_id)
        try:
            data = self._post("/api/v1/chats", payload)
        except AmbiguousSendError as exc:
            # DM dispatched, response lost : it MAY have been delivered.
            # "unconfirmed" blocks the blind retry that double-texts them.
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="unconfirmed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload=payload,
                error=f"send_message unconfirmed: {exc}",
                linkedin_provider_id=provider_id,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                prospect_id=lead.prospect_id,
                state="failed",
                provider=self.name,
                provider_lead_id=None,
                dry_run=False,
                payload=payload,
                error=f"send_message failed: {exc}",
                linkedin_provider_id=provider_id,
            )

        chat_id = (data or {}).get("chat_id") or (data or {}).get("id")
        return ProviderResult(
            prospect_id=lead.prospect_id,
            state="message_sent",
            provider=self.name,
            provider_lead_id=str(chat_id) if chat_id else None,
            dry_run=False,
            payload=payload,
            linkedin_provider_id=provider_id,
        )

    # ---- is_relation (cold vs warm routing) ----------------------------

    def is_relation(self, linkedin_url: str) -> bool:
        """True when the operator's account is already connected to this
        profile on LinkedIn. Drives the smart-routing of /invite : warm
        prospects skip send_connection and go straight to send_message.

        Dry-run returns False (everyone treated as cold) so tests + demos
        exercise the full invite flow. Override by passing a real DSN.

        Implementation: resolves the LinkedIn URL to a provider_id, then
        checks whether that profile's relationship has been established
        with the operator account. Unipile's `/users/{public_id}` response
        includes a `relation` block when the user is a connection.
        """
        if self._dry_run:
            return False
        handle = _linkedin_handle(linkedin_url)
        if not handle:
            return False
        # Missing creds → treat as cold (safer default than raising and
        # crashing the caller's request).
        if not (self.api_key and self.dsn and self.account_id):
            return False
        import httpx
        url = f"{self.dsn}/api/v1/users/{handle}"
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=headers,
                                  params={"account_id": self.account_id})
        except Exception:
            return False
        if resp.status_code >= 400:
            return False
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            return False
        # Unipile surfaces the relationship under a few key names depending
        # on endpoint version; check the most common ones. is_connection is
        # boolean; network_distance is "DISTANCE_1" for direct connections.
        if data.get("is_connection") is True:
            return True
        nd = data.get("network_distance") or (data.get("relation") or {}).get("network_distance")
        if isinstance(nd, str) and nd.upper() in ("DISTANCE_1", "FIRST_DEGREE", "1"):
            return True
        return False

    # ---- fetch_thread (for the AI reply agent) --------------------------

    def fetch_thread(self, chat_id: str) -> list[dict]:
        """Return the full message history for a chat as a list of dicts:

            [{"direction": "outbound"|"inbound", "text": str, "ts": str}, ...]

        Chronological order. Direction is from OUR perspective : "outbound"
        is what we sent, "inbound" is the recipient.

        Dry-run returns a 2-message fixture so the reply-agent harness can
        be exercised end-to-end without Unipile.
        """
        if self._dry_run:
            return [
                {"direction": "outbound", "text": "[dry-run] our first DM", "ts": ""},
                {"direction": "inbound",  "text": "[dry-run] their reply",  "ts": ""},
            ]
        if not chat_id:
            return []
        self._require_creds()
        import httpx
        url = f"{self.dsn}/api/v1/chats/{chat_id}/messages"
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=headers,
                                  params={"account_id": self.account_id})
        except Exception:
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            return []
        items = data.get("items") or data.get("messages") or []
        out: list[dict] = []
        for it in items:
            # Unipile shapes vary across endpoints; check a few likely keys.
            text = (it.get("text") or it.get("body") or "").strip()
            if not text:
                continue
            is_sender = bool(it.get("is_sender") or it.get("from_me"))
            out.append({
                "direction": "outbound" if is_sender else "inbound",
                "text": text,
                "ts": str(it.get("timestamp") or it.get("created_at") or ""),
            })
        return out

    # ---- profile + voice enrichment -------------------------------------

    def fetch_profile(self, linkedin_url: str) -> dict:
        """GET /users/{handle} (+ their recent posts) -> grounding dict.

        Pulls the prospect's live LinkedIn so outreach can reference real,
        current specifics about THEM instead of ICP-derived guesses. Returns
        only the fields we could resolve; network/parse errors degrade to {}
        so the caller falls back to discovery-time (Exa) data."""
        if self._dry_run:
            return {
                "headline": "[dry-run] Founding Engineer @ Acme",
                "summary": "[dry-run] Building low-latency LLM serving; "
                           "previously led inference infra at Bigco.",
                "position": "Founding Engineer @ Acme",
                "recent_posts": ["[dry-run] Shipped a 3x faster KV-cache today."],
            }
        handle = _linkedin_handle(linkedin_url)
        if not handle or not (self.api_key and self.dsn and self.account_id):
            return {}
        data = self._get(f"/api/v1/users/{handle}",
                         params={"account_id": self.account_id})
        if not data:
            return {}
        out: dict = {}
        # provider_id (the internal LinkedIn id) is what the /posts subpath
        # requires — the public handle 422s there. Surface it so callers (the
        # CRM refresh job) can fetch posts without a second profile lookup.
        prov = (data.get("provider_id") or "").strip()
        if prov:
            out["provider_id"] = prov
        headline = (data.get("headline") or data.get("occupation") or "").strip()
        if headline:
            out["headline"] = headline
        summary = (data.get("summary") or data.get("about") or "").strip()
        if summary:
            out["summary"] = summary[:1200]
        # Current position : first work-experience entry, else parsed headline.
        exp = data.get("work_experience") or data.get("experience") or []
        if isinstance(exp, list) and exp:
            top = exp[0] or {}
            title = (top.get("position") or top.get("title") or "").strip()
            company = (top.get("company") or top.get("company_name") or "").strip()
            pos = " @ ".join([s for s in (title, company) if s])
            if pos:
                out["position"] = pos
        posts = self._fetch_recent_posts(handle)
        if posts:
            out["recent_posts"] = posts
        return out

    def _fetch_recent_posts(self, handle: str, limit: int = 3) -> list[str]:
        """GET /users/{handle}/posts -> text of their last few posts.

        Best-effort : a 404 (posts not exposed) or any error returns []."""
        data = self._get(f"/api/v1/users/{handle}/posts",
                         params={"account_id": self.account_id, "limit": limit})
        items = (data.get("items") or data.get("posts") or []) if data else []
        out: list[str] = []
        for it in items[:limit]:
            text = (it.get("text") or it.get("content") or it.get("commentary") or "").strip()
            if text:
                out.append(text[:500])
        return out

    def fetch_recent_posts_detailed(
        self, provider_id: str, limit: int = 5
    ) -> list[dict]:
        """GET /users/{provider_id}/posts -> [{id, text, date}] for change
        detection.

        IMPORTANT: the posts subpath is keyed by the internal `provider_id`,
        NOT the public handle — passing the handle returns 422 invalid_recipient
        (verified live). Callers get provider_id from fetch_profile()'s output.

        Unlike _fetch_recent_posts (text-only, for outreach grounding), this
        keeps a stable post id + ISO timestamp so the CRM refresh job can dedupe
        and alert on each NEW post exactly once. id prefers the numeric `id` /
        `social_id`; a post with no resolvable id is dropped (can't dedupe it).
        `date` uses `parsed_datetime` (real ISO ts) — the bare `date` field is
        a relative string like "1mo" and is useless for ordering. Best-effort:
        errors / dry-run degrade to a small deterministic sample so the pipeline
        stays testable."""
        if self._dry_run:
            return [
                {"id": "dry-post-1",
                 "text": "[dry-run] Shipped a 3x faster KV-cache today.",
                 "date": ""},
            ]
        if not provider_id or not (self.api_key and self.dsn and self.account_id):
            return []
        data = self._get(f"/api/v1/users/{provider_id}/posts",
                         params={"account_id": self.account_id, "limit": limit})
        items = (data.get("items") or data.get("posts") or []) if data else []
        out: list[dict] = []
        for it in items[:limit]:
            pid = (it.get("id") or it.get("social_id")
                   or it.get("share_url") or it.get("urn") or "")
            if not pid:
                continue
            text = (it.get("text") or it.get("content")
                    or it.get("commentary") or "").strip()
            date = (it.get("parsed_datetime") or it.get("date")
                    or it.get("created_at") or "")
            out.append({"id": str(pid), "text": text[:500], "date": str(date)})
        return out

    def fetch_recent_sent_messages(self, limit: int = 20) -> list[str]:
        """Sample the account owner's recent OUTBOUND messages to learn their
        voice. Walks the most recent chats and collects messages we sent.

        Bounded + best-effort : capped chat scan, errors degrade to []."""
        if self._dry_run:
            return [
                "[dry-run] Hey {name}, loved your post on inference infra. "
                "Building something similar, worth a quick chat?",
            ]
        if not (self.api_key and self.dsn and self.account_id):
            return []
        chats = self._get("/api/v1/chats",
                         params={"account_id": self.account_id, "limit": 15})
        chat_items = (chats.get("items") or chats.get("chats") or []) if chats else []
        out: list[str] = []
        for ch in chat_items:
            chat_id = ch.get("id") or ch.get("chat_id")
            if not chat_id:
                continue
            for m in self.fetch_thread(str(chat_id)):
                if m.get("direction") == "outbound" and m.get("text"):
                    out.append(m["text"].strip())
                    if len(out) >= limit:
                        return out
        return out

    def _get(self, path: str, params: dict) -> dict:
        """GET helper for read-only enrichment calls. Returns {} on any
        non-200 / network / parse error (callers all degrade gracefully)."""
        import httpx
        url = f"{self.dsn}{path}"
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}
        try:
            with httpx.Client(timeout=12.0) as client:
                resp = client.get(url, headers=headers, params=params)
        except Exception:
            return {}
        if resp.status_code >= 400:
            return {}
        try:
            return resp.json() if resp.text else {}
        except Exception:
            return {}

    # ---- HTTP plumbing (live mode only : never reached in dry-run) ------

    def _require_creds(self) -> None:
        if not self.api_key:
            raise RuntimeError("UNIPILE_API_KEY is not set : refusing live call")
        if not self.dsn:
            raise RuntimeError("UNIPILE_DSN is not set : refusing live call")
        if not self.account_id:
            raise RuntimeError("UNIPILE_ACCOUNT_ID is not set : refusing live call")

    def _lookup_provider_id(self, public_handle: str) -> str:
        """GET /api/v1/users/{public_handle} → extract provider_id."""
        self._require_creds()
        import httpx
        url = f"{self.dsn}/api/v1/users/{public_handle}"
        headers = {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers=headers, params={"account_id": self.account_id})
        if resp.status_code >= 400:
            raise RuntimeError(f"Unipile lookup {resp.status_code}: {resp.text[:300]}")
        data = resp.json() if resp.text else {}
        pid = data.get("provider_id") or data.get("public_identifier") or data.get("id")
        if not pid:
            raise RuntimeError(f"Unipile lookup: no provider_id in response body keys={list(data)[:10]}")
        return str(pid)

    # 429 backoff schedule (seconds). Bounded: 3 tries total, then give up.
    # A 429 means Unipile REJECTED the call (nothing was actioned), so a
    # retry is always safe — unlike a timeout, which may have landed.
    _RETRY_429_SLEEPS = (1.0, 2.0)

    def _post(self, path: str, body: dict) -> dict:
        """POST to Unipile with two failure modes kept strictly apart:

        - CLEAN failures (4xx/5xx response, connect error before the request
          left) raise RuntimeError : the action did NOT happen, retry freely.
          429s are retried here with bounded backoff before giving up.
        - AMBIGUOUS failures (read timeout / connection dropped AFTER the
          request was dispatched) raise AmbiguousSendError : the action MAY
          have happened on LinkedIn. Callers must not blind-retry — for send
          endpoints a retry here is exactly how a contact gets two invites.
        """
        self._require_creds()
        import httpx
        url = f"{self.dsn}{path}"
        headers = {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.post(url, headers=headers, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Never reached the server : clean failure, safe to retry.
                raise RuntimeError(f"Unipile {path} connect failed: {exc}") from exc
            except httpx.HTTPError as exc:
                # Read timeout / reset / protocol error AFTER dispatch : the
                # request may have been processed. Do NOT mask this as a
                # plain failure — callers treat it as state="unconfirmed".
                raise AmbiguousSendError(
                    f"Unipile {path} dispatched but response lost "
                    f"({type(exc).__name__}: {exc}) — it may have gone out; "
                    "verify on LinkedIn before retrying") from exc

            if resp.status_code == 429 and attempt < len(self._RETRY_429_SLEEPS):
                # Honor Retry-After when sane, else our backoff schedule.
                delay = self._RETRY_429_SLEEPS[attempt]
                ra = resp.headers.get("retry-after")
                try:
                    if ra is not None:
                        delay = min(max(float(ra), delay), 30.0)
                except ValueError:
                    pass
                print(f"  [unipile] 429 on {path}, retry {attempt + 1}/"
                      f"{len(self._RETRY_429_SLEEPS)} in {delay:.1f}s")
                time.sleep(delay)
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Unipile {path} {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json() if resp.text else {}
            except Exception:
                return {}

    # ---- webhook normalization + verification ---------------------------

    def normalize_webhook(self, raw: dict) -> Optional[CanonicalEvent]:
        """
        Translate a Unipile webhook body to our canonical event.

        Best-effort tolerant of several Unipile event envelope shapes:

            {
              "event": "new_relation",
              "account_id": "...",
              "user_provider_id": "ACoAAA...",
              "user": {"provider_id": "ACoAAA...", "public_identifier": "..."},
              "timestamp": "2026-05-14T18:00:00Z",
              "message": {"text": "..."},
            }

        We resolve the matching Prospect via linkedin_provider_id, NOT via
        any back-pointer we embedded (Unipile doesn't have a customUserFields
        analogue). Resolution happens at the route layer; here we just
        canonicalize what we can extract from the webhook body and let the
        route do the DB lookup.

        We return event_id=0 / prospect_id=0 as sentinels : the route is
        expected to call resolve_prospect_for_event() to fill them in.
        """
        if not isinstance(raw, dict):
            return None
        kind = (raw.get("event") or raw.get("event_type") or raw.get("type") or "").lower()
        state = _EVENT_MAP.get(kind)
        if state is None or state not in CANONICAL_STATES:
            return None

        # try multiple places Unipile may surface the OTHER party's
        # provider_id. The "messaging" source nests the counterpart under
        # `sender` (inbound DM) as attendee_provider_id; relations payloads
        # surface it at the top level. We match a Prospect by this id, so
        # checking every known shape is what makes inbound messages resolve.
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        provider_user_id = (
            raw.get("user_provider_id")
            or (raw.get("user") or {}).get("provider_id")
            or (raw.get("user") or {}).get("id")
            or sender.get("attendee_provider_id")
            or sender.get("provider_id")
            or raw.get("attendee_provider_id")
            or raw.get("provider_id")
        )

        ts_raw = raw.get("timestamp") or raw.get("created_at")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else \
                 datetime.now(timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        body_text = ""
        msg = raw.get("message")
        if isinstance(msg, dict):
            body_text = str(msg.get("text") or msg.get("body") or "")
        elif isinstance(msg, str):
            body_text = msg

        return CanonicalEvent(
            event_id=0,                     # filled in by the route via DB lookup
            prospect_id=0,                  # filled in by the route via DB lookup
            state=state,
            provider=self.name,
            provider_lead_id=str(provider_user_id) if provider_user_id else None,
            ts=ts,
            body=body_text,
            raw=raw,
        )

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        if not self.webhook_secret:
            return not self.require_signature
        lower = {k.lower(): v for k, v in (headers or {}).items()}

        # Path A : static shared-secret header. Unipile doesn't body-HMAC its
        # webhooks : it lets you attach STATIC custom headers to every
        # delivery. register_inbound_webhook() sets X-Webhook-Secret to our
        # secret, so a constant-time match on that header is how real inbound
        # traffic authenticates.
        token = (lower.get("x-webhook-secret") or "").strip()
        if token and hmac.compare_digest(token, self.webhook_secret):
            return True

        # Path B : body-HMAC (legacy / providers that do sign the body).
        sig = lower.get("x-unipile-signature") or lower.get("x-signature") or ""
        if not sig:
            return False
        if sig.startswith("sha256="):
            sig = sig[len("sha256="):]
        mac = hmac.new(
            self.webhook_secret.encode("utf-8"),
            msg=body or b"",
            digestmod=hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(mac, sig.strip())

    # ---- webhook registration (the inbound trigger wiring) --------------

    def register_inbound_webhook(self, callback_url: str) -> dict:
        """Ensure a Unipile "messaging" webhook exists that POSTs inbound
        DMs to ``callback_url`` (our /webhooks/unipile route). Idempotent :
        if one already targets this URL we return it instead of creating a
        duplicate. This is the auto-reply analog of the follow-up cron : the
        message_received handler already exists, this is what makes Unipile
        actually call it.

        Attaches X-Webhook-Secret (our UNIPILE_WEBHOOK_SECRET) as a static
        custom header so verify_webhook can authenticate each delivery.
        """
        if not (self.api_key and self.dsn):
            return {"ok": False, "reason": "UNIPILE_DSN + UNIPILE_API_KEY required"}

        import httpx
        headers = {"X-API-KEY": self.api_key, "accept": "application/json"}

        # 1. Idempotency : is one already pointed here?
        try:
            with httpx.Client(timeout=15.0) as client:
                listed = client.get(f"{self.dsn}/api/v1/webhooks", headers=headers)
            existing = listed.json() if listed.text else {}
        except Exception:
            existing = {}
        items = existing.get("items") if isinstance(existing, dict) else existing
        for w in (items or []):
            if isinstance(w, dict) and (w.get("request_url") or w.get("url")) == callback_url:
                return {"ok": True, "created": False, "webhook": w}

        # 2. Create it.
        body: dict = {
            "source": "messaging",
            "request_url": callback_url,
            "name": "surplus-inbound",
            "enabled": True,
        }
        if self.webhook_secret:
            # Static header echoed back on every delivery -> verify_webhook.
            body["headers"] = [
                {"key": "X-Webhook-Secret", "value": self.webhook_secret}
            ]
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(f"{self.dsn}/api/v1/webhooks",
                                   headers={**headers, "Content-Type": "application/json"},
                                   json=body)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if resp.status_code >= 400:
            return {"ok": False, "reason": f"unipile {resp.status_code}: {resp.text[:300]}"}
        try:
            created = resp.json() if resp.text else {}
        except Exception:
            created = {}
        return {"ok": True, "created": True, "webhook": created}
