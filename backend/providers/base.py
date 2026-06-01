"""
providers/base.py : the LinkedInProvider contract.

Every provider (Unipile, future Manual, future others) implements this
interface. The rest of the app only ever sees the canonical types defined
here:

    LeadPayload      : what we hand to the provider (already personalized)
    ProviderResult   : what the provider hands back (dry-run or real)
    CanonicalEvent   : normalized webhook event ready to apply to OutreachLog
    CANONICAL_STATES : the only state strings the rest of the app deals with

Providers translate between their own dialect and these canonical shapes
inside their own module. Outside the providers/ package, no provider-specific
strings should ever appear.
"""
from __future__ import annotations
import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# The full canonical state machine. Webhooks must normalize into these.
CANONICAL_STATES: tuple[str, ...] = (
    "dry_run_queued",      # we built a payload but didn't send (DRY_RUN)
    "queued",              # accepted by the provider, awaiting send
    "invite_sent",         # connection request submitted on LinkedIn
    "invite_accepted",     # recipient accepted the connection
    "message_sent",        # post-accept DM delivered
    "message_replied",     # recipient replied to the DM
    "follow_up_sent",      # an additional follow-up DM went out
    "failed",              # provider returned an error for this lead
)


@dataclass(frozen=True)
class LeadPayload:
    """
    The fully-resolved, personalized lead we hand to a provider.

    Providers map this into their own request shape. Internal IDs
    (event_id, prospect_id) ride along so webhooks can find the DB row.
    """
    event_id: int
    prospect_id: int
    identity: str               # stable cross-source key, e.g. "maya-rodriguez"
    first_name: str
    last_name: str
    full_name: str
    linkedin_url: str
    company: Optional[str]
    position: Optional[str]
    note: str                   # ≤300 chars, for the connection request
    message: str                # longer, for the post-accept DM
    # extra signals to embed for the provider's templates / audit
    works_on: Optional[str] = None
    offers: Optional[str] = None
    seeks: Optional[str] = None
    fit_score: Optional[int] = None
    fit_reason: Optional[str] = None
    sources: Optional[str] = None


@dataclass(frozen=True)
class ProviderResult:
    """
    What a provider returns for one lead it processed.

    Set `dry_run=True` and `state='dry_run_queued'` when nothing left the box.
    On real failure: `state='failed'` and populate `error`.

    `linkedin_provider_id` is the recipient's LinkedIn internal user ID
    (Unipile resolves and returns it). When present, the pipeline persists
    it on Prospect.linkedin_provider_id so subsequent webhooks can be
    matched back to the right DB row.
    """
    prospect_id: int
    state: str                      # one of CANONICAL_STATES
    provider: str                   # e.g. "unipile"
    provider_lead_id: Optional[str] # provider-side action id (or "dry_<uuid>")
    dry_run: bool
    payload: dict                   # the JSON we would/did POST : captured for audit
    error: Optional[str] = None
    linkedin_provider_id: Optional[str] = None


@dataclass(frozen=True)
class CanonicalEvent:
    """Normalized webhook event ready to apply to OutreachLog."""
    event_id: int
    prospect_id: int
    state: str                      # one of CANONICAL_STATES
    provider: str
    provider_lead_id: Optional[str]
    ts: datetime
    body: str = ""
    raw: dict = field(default_factory=dict)


class LinkedInProvider(abc.ABC):
    """Provider interface. All concrete providers must implement this."""

    #: short identifier, e.g. "unipile". Stored on OutreachLog.provider.
    name: str = "base"

    @property
    @abc.abstractmethod
    def dry_run(self) -> bool:
        """True if calls will NOT touch the real provider API."""
        ...

    @property
    def auto_dm_after_accept(self) -> bool:
        """
        Does THIS provider need us to explicitly call send_message() after we
        see an invite_accepted webhook?

        - True  → our webhook handler calls send_message() to push the DM.
        - False → the provider's own engine fires the DM autonomously.

        Unipile is True (no sequence engine : our platform owns it).
        """
        return False

    @abc.abstractmethod
    def build_lead_payload(self, prospect, event, note: str, message: str) -> LeadPayload:
        """Translate a Prospect + composed Message into a canonical LeadPayload."""
        ...

    @abc.abstractmethod
    def send_connection(self, lead: LeadPayload) -> ProviderResult:
        """
        Submit a connection-request action for one lead. In dry-run, build the
        exact payload and return it without HTTP. In live, POST and return the
        provider's response.

        For providers that need to translate a LinkedIn URL into an internal
        provider_id (Unipile), the lookup happens here and the resolved id is
        returned on ProviderResult.linkedin_provider_id so the pipeline can
        persist it for later webhook matching.
        """
        ...

    def resolve_linkedin_user(self, linkedin_url: str) -> Optional[str]:
        """
        Resolve a LinkedIn profile URL to the provider's internal user id.
        Providers that don't need this can leave it None : `send_message`
        will then fail with a clear error in live mode.
        """
        return None

    def send_message(self, lead: LeadPayload, linkedin_provider_id: Optional[str] = None) -> ProviderResult:
        """
        Send a DM to an already-connected lead. Only invoked by the webhook
        handler for providers where `auto_dm_after_accept` is True.

        Default implementation is a no-op for providers whose own engine
        handles the DM step autonomously after the invite is accepted.
        """
        return ProviderResult(
            prospect_id=lead.prospect_id,
            state="message_sent",
            provider=self.name,
            provider_lead_id=None,
            dry_run=self.dry_run,
            payload={"skipped": "provider does not require explicit send_message"},
        )

    def fetch_thread(self, chat_id: str) -> list[dict]:
        """Return chronological message history for one chat as a list of
        ``{"direction": "outbound"|"inbound", "text": str, "ts": str}``
        dicts. Default returns [] : providers that support the AI reply
        agent must override (Unipile does)."""
        return []

    def fetch_profile(self, linkedin_url: str) -> dict:
        """Fetch a person's live LinkedIn profile for outreach grounding.

        Returns a dict with whatever the provider could resolve; absent
        fields are simply omitted. Canonical keys:
            headline       : str  - the one-liner under their name
            summary        : str  - their "About" section
            position       : str  - current title @ company
            recent_posts   : list[str] - text of their last few posts/activity

        Default returns {} : providers without a profile endpoint can't
        enrich, and the caller falls back to discovery-time (Exa) data."""
        return {}

    def fetch_recent_sent_messages(self, limit: int = 20) -> list[str]:
        """Return the text of the account owner's most recent OUTBOUND
        LinkedIn messages, newest first. Used to derive the host's own
        writing voice so composed outreach sounds like them.

        Default returns [] : providers without message history can't sample
        a voice, and the caller falls back to configured voice examples."""
        return []

    def is_relation(self, linkedin_url: str) -> bool:
        """True if the operator's LinkedIn is already connected to this
        profile (warm) : drives the cold vs warm send routing. Default
        False (treat everyone as cold) so a provider without a relations
        endpoint stays safe."""
        return False

    @abc.abstractmethod
    def normalize_webhook(self, raw: dict) -> Optional[CanonicalEvent]:
        """
        Map a raw incoming webhook body into a CanonicalEvent. Return None if
        the event type is unknown (caller logs + 200s : never crash).
        """
        ...

    @abc.abstractmethod
    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """
        Validate a webhook is authentic. Implementations should:
          - return True if signature checks out
          - return False if it fails
          - return True only when a secret is configured AND the check passes,
            OR when verification is explicitly disabled via env (dev mode).
        """
        ...
