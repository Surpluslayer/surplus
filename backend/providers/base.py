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
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Outbound copy hygiene ────────────────────────────────────────────
# Em/en/figure dashes are a giveaway that copy was machine-written and read
# stiff in a LinkedIn note. We replace them with a comma so every message
# that leaves the box reads human. Applied in LeadPayload.__post_init__ so
# EVERY send path (invite note, post-accept DM, follow-up, AI auto-reply)
# is covered : they all build a LeadPayload before hitting a provider.
#
# Two cases collapse to a comma:
#   1. Real em/en/figure dashes (and the unicode minus), tight or spaced.
#   2. An ASCII hyphen-minus used AS a dash, i.e. surrounded by whitespace
#      ("Vancouver - let's ..."). A *spaced* hyphen is the same machine-copy
#      tell as an em dash. A hyphen WITHOUT surrounding spaces ("co-founder")
#      is a real hyphenated word and is deliberately left untouched.
_DASH_RE = re.compile(r"\s*[—–―−]\s*|\s+-\s+")


def strip_em_dashes(text: Optional[str]) -> Optional[str]:
    """Replace em/en/figure dashes (and the unicode minus), plus a spaced
    ASCII hyphen used as a dash, with ', ' so outbound copy reads human.
    Tight ('a—b') and spaced ('a — b' / 'a - b') dashes collapse to a comma.
    A hyphen inside a word ('co-founder') is left untouched.

    Surgical: if the text contains no dash it is returned byte-for-byte
    unchanged (we must not reflow the app's intentional ' : ' style or any
    other spacing). Only the comma artifacts that the replacement itself
    could create are tidied."""
    if not text or not _DASH_RE.search(text):
        return text
    out = _DASH_RE.sub(", ", text)
    out = re.sub(r",\s*,", ", ", out)        # collapse commas adjacent to a dash
    out = re.sub(r",\s*([.!?,;:])", r"\1", out)  # 'coffee —.' -> 'coffee.', no dangling comma
    out = re.sub(r"\s+,", ",", out)          # tighten space before the new comma
    out = re.sub(r"[ \t]{2,}", " ", out)     # collapse the odd double space
    out = re.sub(r"^\s*,\s*", "", out)       # leading comma artifact
    out = re.sub(r"\s*,\s*$", "", out)       # trailing comma artifact
    return out.strip()


# ── No-call hygiene ──────────────────────────────────────────────────
# Outreach must never propose a phone / video call. A host's own past
# messages (used as voice examples) routinely close with "open to a quick
# call soon?", and the model copies that closer verbatim into nearly every
# note. The compose prompt forbids it, but a prompt rule can't be trusted
# against an example the model is literally shown. This is the deterministic
# backstop: drop the call ask at the clause level so the rest of the
# sentence ("Let's stay in touch") survives. Applied in
# LeadPayload.__post_init__, so EVERY send path is covered.
_CALL_ASK_RE = re.compile(
    r"""(?ix)
    (?:
        (?:hop|jump|get|grab|getting)\s+on\s+(?:a\s+)?(?:quick\s+|short\s+|brief\s+)?call\b
      | grab\s+(?:a\s+)?(?:quick\s+|short\s+|brief\s+)?call\b
      | \b(?:quick|short|brief|phone|video|catch[-\s]?up)\s+call\b
      | give\s+(?:you|me)\s+a\s+call\b
      | \ba\s+call\s+(?:soon|sometime|this\s+week|next\s+week)\b
      | \bcall\s+(?:soon|sometime|this\s+week|next\s+week)\b
      | (?:let['’]?s|love\s+to|happy\s+to|able\s+to|can\s+we|could\s+we|we\s+could|wanna|want\s+to)\s+(?:quickly\s+)?call\b
      | \bcalling\s+works\b
      | \bzoom\b
      | video\s+chat\b
    )
    """,
)


def strip_call_asks(text: Optional[str]) -> Optional[str]:
    """Remove any call ask from outbound copy, surgically.

    Works clause-by-clause: within each sentence, drop only the comma-clause
    that proposes a call, keeping the rest. If a whole sentence is nothing but
    a call ask, drop the sentence. A trailing '?' left behind by removing the
    asking clause is downgraded to '.' so we don't end on a dangling question.

    Returns the text unchanged when no call ask is present (byte-for-byte)."""
    if not text or not _CALL_ASK_RE.search(text):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept_sentences: list[str] = []
    for sent in sentences:
        m = re.search(r"([.!?]+)\s*$", sent)
        term = m.group(1) if m else ""
        core = sent[: m.start()] if m else sent
        clauses = [c.strip() for c in core.split(",")]
        kept = [c for c in clauses if c and not _CALL_ASK_RE.search(c)]
        if not kept:
            continue  # the whole sentence was the call ask
        # If the dropped clause was the one carrying the '?', the survivors
        # are statements : end on a period, not a dangling question mark.
        if term == "?" and _CALL_ASK_RE.search(clauses[-1]):
            term = "."
        kept_sentences.append(", ".join(kept) + (term or "."))
    result = " ".join(kept_sentences).strip()
    return result or text  # never blank out the whole message


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
    "unconfirmed",         # request dispatched but outcome unknown (timeout
                           # mid-flight) : it MAY have landed on LinkedIn.
                           # Retrying blindly risks a double-send; send paths
                           # hold off (see send_flow's recent-send guard).
)


class AmbiguousSendError(RuntimeError):
    """A send request was dispatched to the provider but the response never
    arrived (read timeout / connection dropped mid-flight). The action MAY
    have been performed on LinkedIn. Callers must NOT auto-retry: surface the
    ambiguity (canonical state "unconfirmed") and let a human or a later
    webhook reconcile it. Distinct from a clean failure (4xx/5xx/connect
    error), where the provider definitely did not act."""


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

    def __post_init__(self) -> None:
        # Last line of defense: strip em/en dashes from everything we send,
        # no matter which composer produced it. frozen=True, so mutate via
        # object.__setattr__.
        object.__setattr__(
            self, "note", strip_call_asks(strip_em_dashes(self.note)) or "")
        object.__setattr__(
            self, "message", strip_call_asks(strip_em_dashes(self.message)) or "")


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

    def register_inbound_webhook(self, callback_url: str) -> dict:
        """Ensure a provider-side webhook delivers inbound messaging events to
        ``callback_url``. Idempotent. Returns a small status dict. Default is a
        no-op for providers that don't have (or don't need) a registerable
        messaging webhook."""
        return {"ok": False, "reason": "provider has no inbound webhook to register"}

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
