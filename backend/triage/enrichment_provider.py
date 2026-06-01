"""triage/enrichment_provider.py : DESIGN STUB — prod enrichment migration.

STATUS
------
This is a *design document in code form*, not a wired-in module. Nothing here is
imported by the live triage path yet. The classes are interface stubs whose
methods raise NotImplementedError on purpose: they pin down the seams we will cut
when we migrate enrichment off "one Unipile call per applicant, every run" and
onto a cached, provider-pluggable, value-of-information-gated pipeline. Keeping it
as a real .py (typed signatures, importable symbols) means the contract is
greppable and test-targetable before a single line of behavior lands.

Read this top-to-bottom; build it bottom-up behind a flag.

--------------------------------------------------------------------------------
WHY MIGRATE
--------------------------------------------------------------------------------
Today `enrich.enrich_applicant()` is the only enrichment entry point. For every
applicant, every run, it makes *live* Unipile (LinkedIn) + Exa calls. Three
problems at prod scale:

  1. COST/RISK PER LOOKUP IS A REAL ACCOUNT ACTION. Unipile proxies a logged-in
     LinkedIn account; each profile read is a human-equivalent action subject to
     rate limits and soft-throttle ("stripped-200": HTTP 200 with the experience
     section silently emptied). A 530-row event burns ~530 real actions against a
     tiny pool of team accounts. This is the binding constraint, not API dollars.
  2. NO REUSE ACROSS EVENTS OR TENANTS. The same person applies to many events.
     We re-fetch their identical LinkedIn profile every time. The marginal fetch
     buys nothing the previous fetch didn't already know.
  3. NO BRING-YOUR-OWN-DATA PATH. Customers who have a compliant data API (e.g. a
     People Data API, a CRM export, an internal graph) cannot feed it in; they're
     forced through our LinkedIn egress even when they have cheaper/safer truth.

The migration replaces the hardcoded Unipile call with a *provider cascade behind
a cache behind a value-of-information gate*, so that:
  - a cache hit costs zero account actions,
  - a customer's own data API is tried before our LinkedIn egress,
  - and we only spend a live LinkedIn action when the answer could actually change
    the applicant's bucket (accept / maybe / reject).

--------------------------------------------------------------------------------
ARCHITECTURE  (target)
--------------------------------------------------------------------------------

    enrich_applicant(applicant, claims)
        │
        ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  EnrichmentOrchestrator.gather(identity, need)                │
    │                                                              │
    │   1. CacheLayer.get(identity)         ── cheap, cross-tenant │
    │        └─ fresh enough for `need`? ──▶ return cached         │
    │                                                              │
    │   2. VOI gate: would a live fetch change the bucket?         │
    │        └─ no ──▶ return cached/partial, do NOT fetch         │
    │                                                              │
    │   3. provider cascade (ordered, cheapest-safest first):      │
    │        DataApiProvider (customer BYO)                         │
    │        → UnipileProvider (our LinkedIn egress, last resort)   │
    │        → ExaProvider     (open web, corroboration only)       │
    │                                                              │
    │   4. CacheLayer.put(identity, evidence, ttl=by_volatility)   │
    └──────────────────────────────────────────────────────────────┘
        │
        ▼
    RawEvidence  (UNCHANGED shape — reconcile/score/disambiguate downstream
                  do not need to know enrichment was cached or provider-sourced)

The crucial invariant: the **output contract is `RawEvidence`** (see enrich.py),
exactly what `enrich_applicant` returns today. Everything downstream — reconcile,
disambiguate, score, consolidate — is untouched. This is a swap-the-source
migration, not a rewrite of the scoring engine. The engine stays EVENT-AGNOSTIC.

--------------------------------------------------------------------------------
IDENTITY KEY  (the cache's primary key)
--------------------------------------------------------------------------------
A cache is only as good as its key. We key on *resolved identity*, not on the raw
applicant row, because two events spell the same person differently.

  PersonIdentity:
     linkedin_slug      — canonical, from _linkedin_slug(); STRONGEST key
     email_sha256       — salted hash of lowercased email; strong, privacy-safe
     name + company     — weak fallback, only with a corroborating signal

  CompanyIdentity:
     linkedin_company_id  — strongest
     domain               — strong (registrable domain, eTLD+1)
     normalized_name      — weak fallback

Resolution order mirrors the existing match flags in CompanyCandidate
(matches_submitted_domain > matches_email_domain > work_experience > name). We
NEVER cache under a weak key alone — a name-only hit can collide across people
(the Brittany/Kyndred class of bug). Weak keys are cache-readable for a *hint*
but a write requires at least one strong key. This keeps the cache from
poisoning disambiguation.

CROSS-TENANT, BUT PII-SAFE:
  - The cache is shared across events/tenants because a LinkedIn profile is the
    same fact regardless of who asked. This is where the reuse win comes from.
  - Store only what scoring needs (the trimmed evidence — see _trim_raw), never
    raw scraped HTML. Email is stored ONLY as a salted hash for keying, never in
    cleartext in the cache. A tenant can read evidence keyed by an identity it
    legitimately resolved; it cannot enumerate the cache.

--------------------------------------------------------------------------------
TTL BY VOLATILITY  (freshness policy)
--------------------------------------------------------------------------------
Not all facts decay at the same rate. The cache stores a `fetched_at` and the
orchestrator decides "fresh enough" per-field-class:

    job title / current company   ~30  days   (changes a few times/career)
    headline / bio                 ~30  days
    follower count                 ~90  days   (slow, and we only use a floor)
    company stage / size           ~90  days
    "person exists / slug valid"   ~365 days   (near-immutable)

`need.freshness` lets a caller demand tighter freshness for a high-stakes event.
A near-the-cutline applicant can justify a fresher fetch than a clear accept.

--------------------------------------------------------------------------------
VALUE-OF-INFORMATION (VOI) GATE  (the spend governor)
--------------------------------------------------------------------------------
This is the single most important new idea. Before spending a live LinkedIn
action, ask: *could the result of this fetch change the applicant's bucket?* If
not, don't fetch — the information has zero decision value.

Inputs to the gate:
  - current best evidence (cache/partial) and the confidence band it supports,
  - the event thresholds (accept_fit_min / maybe_fit_min from triage_config),
  - the applicant's provisional fit estimate from cheap signals.

Decision:
  - If provisional fit is comfortably ABOVE accept or comfortably BELOW reject
    (outside a margin band), the bucket is settled → SKIP the live fetch.
  - If provisional fit sits in the contested band around a threshold, a fetch
    could flip it → SPEND the fetch.
  - A claimed-but-uncorroborated identity that would earn a big archetype boost
    (e.g. "I founded X") is always worth corroborating → SPEND (this is exactly
    the disambiguation-critical case).

Net effect: live LinkedIn actions concentrate on the cutline, where they change
outcomes, instead of being spread flat across all 530 rows. On a typical event
this should cut live fetches by a large fraction while leaving the top-N invite
list identical.

--------------------------------------------------------------------------------
PROVIDER CASCADE  (ordered, fail-soft)
--------------------------------------------------------------------------------
Each provider implements the same `fetch_person` / `fetch_company` contract and
returns the SAME evidence shapes (PersonEvidence / CompanyCandidate). The
orchestrator tries them in cost-and-safety order and stops as soon as it has
enough to satisfy the VOI need:

  1. DataApiProvider  — customer's own compliant data API (BYO). Cheapest and
     safest: no LinkedIn action, customer owns the data + consent. Tried first.
  2. UnipileProvider  — our team LinkedIn egress. The current behavior, but now
     LAST in the cascade and GATED by VOI, so it's the exception not the rule.
  3. ExaProvider      — open-web corroboration (co-occurrence, company site).
     Used to corroborate a claim, never as sole identity proof.

FAIL-SOFT: a provider that errors or rate-limits returns empty and the cascade
continues; enrichment degrades to whatever evidence we have. Matches the existing
"never raise out of enrichment" contract — a bad fetch must never sink a run.

--------------------------------------------------------------------------------
BRING-YOUR-OWN-ACCOUNTS  (Unipile pool, per-tenant)
--------------------------------------------------------------------------------
Today the team pool is hardcoded to two team-owned accounts. In prod a customer
may connect THEIR OWN LinkedIn account(s) to spend their own rate budget. The
UnipileProvider takes an explicit, per-tenant account pool at construction:

  - Triage/inbound MUST use only the pool passed for that tenant.
  - The selection + pacing logic already exists (_next_account_id, _pace_account,
    UNIPILE_MIN_FETCH_INTERVAL keyed by account_id) — it moves behind the
    provider unchanged.
  - HARD RULE (carried from the security constraint): the triage path must NEVER
    silently fall back to another tenant's or a team account when a customer pool
    is supplied. Cross-tenant account use is a correctness AND a compliance bug.

--------------------------------------------------------------------------------
*** PRE-DEPLOY BLOCKER — DO NOT SKIP, DO NOT AUTO-EXECUTE ***
--------------------------------------------------------------------------------
PROD Railway currently has SIX customer LinkedIn accounts connected to the shared
workspace. The triage enrichment path must use ONLY team-owned accounts
(Jiahui Jin, Daniel Wang) or a customer's explicitly-supplied own pool — NEVER
the other workspace-connected customer accounts.

BEFORE any deploy that exercises this path against prod:
  1. Purge the 6 customer LinkedIn accounts from the prod workspace (or hard-fence
     them out of the triage account pool selection).
  2. Verify _account_ids() / the per-tenant pool resolves to team-or-BYO only.
  3. Add a startup assertion that refuses to boot triage if the resolved triage
     pool intersects the known customer-account id set.

This step is intentionally NOT executed by this module or any code in this change.
It is an operator action gated on human review. Document, surface, refuse-to-boot
on violation — do not auto-purge.

--------------------------------------------------------------------------------
ROLLOUT PLAN  (incremental, reversible)
--------------------------------------------------------------------------------
  Phase 0 (this file): contract + design, no behavior change.
  Phase 1: implement CacheLayer with a read-through wrapper around the EXISTING
           enrich_applicant. Flag TRIAGE_ENRICH_CACHE=off by default. Pure
           additive — cache miss == today's behavior. Measure hit rate.
  Phase 2: add the VOI gate in shadow mode (log "would skip" without skipping),
           validate that skipped fetches never change the top-N. Then enforce.
  Phase 3: introduce DataApiProvider + the provider cascade; Unipile moves last.
  Phase 4: per-tenant Unipile pools + the pre-deploy purge blocker above.

Each phase is independently revertible by flag. The output contract (RawEvidence)
never changes, so downstream scoring is frozen throughout — every phase is
validated by "did the ranked top-N move?" not by reading enrichment internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

# Reuse the EXISTING evidence shapes; this module changes the *source* of
# evidence, never its shape. Downstream (reconcile/score/disambiguate) is frozen.
from .enrich import PersonEvidence, CompanyCandidate, RawEvidence


# ── identity keys ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PersonIdentity:
    """Resolved person key for the cross-tenant cache.

    A WRITE requires at least one strong key (linkedin_slug or email_sha256). A
    name+company-only identity is read-as-hint only — never a cache write key —
    so weak keys cannot poison disambiguation (the Brittany/Kyndred class of bug).
    """
    linkedin_slug: str = ""        # strongest
    email_sha256: str = ""         # strong, privacy-safe (salted hash, never raw)
    name: str = ""                 # weak
    company: str = ""              # weak

    @property
    def has_strong_key(self) -> bool:
        return bool(self.linkedin_slug or self.email_sha256)


@dataclass(frozen=True)
class CompanyIdentity:
    linkedin_company_id: str = ""  # strongest
    domain: str = ""               # strong (eTLD+1)
    normalized_name: str = ""      # weak

    @property
    def has_strong_key(self) -> bool:
        return bool(self.linkedin_company_id or self.domain)


# ── what the caller needs (drives freshness + VOI) ────────────────────────────
@dataclass
class EnrichmentNeed:
    """Why we're enriching, so the orchestrator can gate spend.

    freshness_days : max acceptable age of cached evidence for THIS decision.
    fit_estimate   : cheap provisional fit (0-100) from CSV/claims before any live
                     fetch; feeds the VOI gate.
    accept_fit_min / maybe_fit_min : the event's thresholds (from triage_config),
                     so the gate knows where the bucket boundaries are.
    boost_at_stake : True if a fetch could corroborate a claim worth a big
                     archetype boost (always worth spending — disambiguation case).
    """
    freshness_days: int = 30
    fit_estimate: Optional[float] = None
    accept_fit_min: int = 72
    maybe_fit_min: int = 55
    boost_at_stake: bool = False
    margin: int = 8                # contested band half-width around a threshold


@dataclass
class CachedEvidence:
    """A cache entry: the trimmed evidence plus provenance for freshness checks."""
    person: Optional[PersonEvidence] = None
    company_candidates: list[CompanyCandidate] = field(default_factory=list)
    fetched_at_epoch: float = 0.0
    source: str = ""               # "data_api" | "unipile" | "exa" | "cache"
    raw_searches: list[dict] = field(default_factory=list)


# ── provider contract ─────────────────────────────────────────────────────────
class EnrichmentProvider(Protocol):
    """One evidence source. Same contract for BYO data API, Unipile, Exa.

    Implementations MUST fail-soft: on error/rate-limit return empty, never raise.
    They return the EXISTING evidence shapes so the cascade output is uniform.
    """
    name: str
    spends_linkedin_action: bool   # True only for UnipileProvider; gates VOI

    def fetch_person(self, ident: PersonIdentity,
                     need: EnrichmentNeed) -> tuple[PersonEvidence, list[dict]]:
        ...

    def fetch_company(self, ident: CompanyIdentity, *, person: PersonEvidence,
                      need: EnrichmentNeed) -> tuple[list[CompanyCandidate], list[dict]]:
        ...


class DataApiProvider:
    """Customer bring-your-own compliant data API. Cheapest + safest → tried FIRST.

    No LinkedIn action; the customer owns the data and the consent. Maps the
    customer's payload into PersonEvidence / CompanyCandidate. STUB."""
    name = "data_api"
    spends_linkedin_action = False

    def fetch_person(self, ident: PersonIdentity, need: EnrichmentNeed):
        raise NotImplementedError("design stub: map customer data API → PersonEvidence")

    def fetch_company(self, ident: CompanyIdentity, *, person, need):
        raise NotImplementedError("design stub: map customer data API → CompanyCandidate[]")


class UnipileProvider:
    """Our LinkedIn egress. Now LAST in the cascade and VOI-GATED.

    Wraps the existing _fetch_person_unipile / _search_company_candidates +
    per-account pacing UNCHANGED, but takes an explicit per-tenant account pool:
    triage MUST use only the pool passed here (team-owned, or a customer's own
    accounts), NEVER another tenant's. STUB."""
    name = "unipile"
    spends_linkedin_action = True

    def __init__(self, account_pool: Sequence[str]):
        # HARD RULE: this is the ONLY set of account ids this provider may touch.
        # No silent fallback to team/other-tenant accounts. The pre-deploy purge
        # (see module docstring) ensures customer accounts never leak in here.
        self.account_pool = tuple(account_pool)

    def fetch_person(self, ident: PersonIdentity, need: EnrichmentNeed):
        raise NotImplementedError("design stub: wrap _fetch_person_unipile w/ pool+pacing")

    def fetch_company(self, ident: CompanyIdentity, *, person, need):
        raise NotImplementedError("design stub: wrap _search_company_candidates")


class ExaProvider:
    """Open-web corroboration only (co-occurrence, company site). Never sole
    identity proof — corroborates a claim, doesn't establish it. STUB."""
    name = "exa"
    spends_linkedin_action = False

    def fetch_person(self, ident: PersonIdentity, need: EnrichmentNeed):
        raise NotImplementedError("design stub: Exa person corroboration")

    def fetch_company(self, ident: CompanyIdentity, *, person, need):
        raise NotImplementedError("design stub: wrap _exa_direct/_exa_cooccurrence")


# ── cache ─────────────────────────────────────────────────────────────────────
class CacheLayer(Protocol):
    """Cross-tenant, PII-safe evidence cache.

    - Keyed on resolved identity (strong key required to WRITE).
    - Stores trimmed evidence only (never raw HTML); email only as salted hash.
    - get() returns None on miss or when the entry is staler than `need.freshness`.
    """
    def get_person(self, ident: PersonIdentity,
                   need: EnrichmentNeed) -> Optional[CachedEvidence]:
        ...

    def put_person(self, ident: PersonIdentity, ev: CachedEvidence) -> None:
        ...


# ── VOI gate ──────────────────────────────────────────────────────────────────
def voi_should_fetch_live(current: Optional[CachedEvidence],
                          need: EnrichmentNeed) -> bool:
    """Would a live (account-spending) fetch change the applicant's bucket?

    Pure + deterministic + side-effect free, so it's unit-testable in isolation.
    Spend a live LinkedIn action ONLY when the answer could flip a decision:

      - boost_at_stake (uncorroborated claim worth a big archetype boost) → True
      - provisional fit sits in the contested band around a threshold      → True
      - fit comfortably above accept or below maybe (outside the margin)   → False
      - no fit estimate yet (nothing cheap to gate on)                     → True

    STUB: real impl reads need.fit_estimate vs accept/maybe ± margin.
    """
    raise NotImplementedError("design stub: implement bucket-flip margin logic")


# ── orchestrator ──────────────────────────────────────────────────────────────
class EnrichmentOrchestrator:
    """Cache → VOI gate → provider cascade → cache write. Returns RawEvidence,
    the SAME shape enrich_applicant returns today, so downstream is frozen.

    Providers are supplied in cascade order (cheapest/safest first). Wire as a
    flagged read-through wrapper around the existing enrich_applicant in Phase 1;
    add the gate (Phase 2) and the cascade (Phase 3) behind their own flags."""

    def __init__(self, cache: CacheLayer, providers: Sequence[EnrichmentProvider]):
        self.cache = cache
        self.providers = tuple(providers)

    async def gather(self, applicant, claims, need: EnrichmentNeed) -> RawEvidence:
        raise NotImplementedError(
            "design stub: 1) resolve identity 2) cache.get 3) voi gate "
            "4) provider cascade (skip linkedin-spending providers when gate=False) "
            "5) cache.put under strong key 6) assemble RawEvidence")
