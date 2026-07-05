# Accounts: cross-company relationship capture and proactive outreach

Status: DESIGN v2 (not implemented). 2026-07-05.
Scope: how surplus captures relationships at the account (company) level, portrays them, turns them into proactive outreach, and protects the underlying data when relationship signals are shared beyond their owner.

The functionality target is what the market calls collective relationship intelligence: "who do we know at this company, how warm, and what should we do next." Nexl, Affinity, Introhive, and 4Degrees each ship a variant with materially different architectures. This doc does not adopt any one of them; section 9 is the decision log where each major choice is picked against alternatives for surplus's specific context: LinkedIn-first (not email-first), solo operators as the wedge, small law firms and VC funds (2-10 people) as the expansion -- a confidentiality-sensitive segment that requires strict walls -- and individual data ownership as a product promise.

---

## 0. The ultimate decision (TL;DR)

**The account layer is a lens, not a bucket.** Concretely, one sentence per pillar:

1. **Entities:** a global, read-only Company tier (public data, one row per real org, per-user overlays for corrections) + per-user relationship graphs, joined by time-bounded AccountMembership edges.
2. **Reads:** company-level views are assembled at query time from individual graphs, through structural gates applied in fixed order: ethical wall -> team compliance profile -> owner's per-account level. Derived aggregates may be cached; source data is never copied across owners.
3. **Freshness:** internal event bus fed by external webhooks (instant where they exist) and one global company watcher (one poll per distinct company); the hourly sweep survives only as poll-initiator and reconciliation backstop.
4. **Proactivity:** signals detected deterministically, moves composed and ranked by LLM, sent only through the existing approval/autonomy gates.
5. **Rollout:** Phase 1 account spine (single-user) -> Phase 2 proactive signals -> Phase 3 team plane with walls and audit.

What this buys: provable confidentiality (walls take effect on the next query, auditable), clean departures (nothing copied, nothing to claw back), shared enrichment economics (cost scales with distinct companies), and freedom-by-default sharing that strict firms can cap with one setting.

What it costs: reads do more work than reading a precomputed bucket (mitigated by derived-only caching); the sharing model is the most complex option considered and its query-layer enforcement must be airtight; the LLM company resolver is a quality-critical dependency; and three freshness mechanisms (bus, watcher, sweep) replace one cron. §9 records each trade against its alternatives.

## 1. What exists today (and what's missing)

Today the graph has exactly one durable node type: `Contact` (per-user, identity-deduped via `ContactIdentity`). Company is a string field on `Contact`, `Prospect`, `Applicant`, and `Attendee`, plus `Contact.company_domain` used only as a merge hint. There is no Company entity, no person-to-company membership with history, no Team/Workspace entity, and `RelationshipInteraction.visibility` has a "team" value with nothing to point at.

We already have the right precedent for shared entities: `MonitoredPerson` is a globally deduped person node (one per real LinkedIn member) with `HostPersonLink` as the per-user fan-out. The account layer follows the same shape one level up.

## 2. Entity model

Four new tables plus a small overlay.

### Company (global, read-only enrichment, no user data)
One row per real-world organization, shared across all users.

- `id`, `canonical_name`, `primary_domain`
- `linkedin_company_id`, `linkedin_slug`
- enrichment snapshot: industry, headcount band, stage, location (from Exa / Bright Data company scrape)
- watch state, mirroring Contact's: `last_news_checked_at`, `seen_signal_ids`, `profile_baselined_at`

Two hard properties, both decided in §9:

1. **Public information only.** Never anything derived from a user's messages, notes, or graph. This is what makes it safe to share globally and lets one company scrape serve every user who has contacts there (same economics as MonitoredPerson).
2. **Users cannot mutate it.** Global Company rows are written only by the enrichment pipeline. A user who disagrees ("this contact's 'Acme' is actually Acme Health, not Acme Corp") writes a per-user `CompanyOverlay` row (company_id, user_id, corrected fields / rejected-match flag). Without this, one user's bad merge silently corrupts every other user's graph, and there is no clean undo. Overlays also become training signal for the resolver.

### CompanyIdentity (global)
Mirror of ContactIdentity, one row per strong identifier:

- `kind`: `domain` | `linkedin_company` | `name_norm`
- `value` (normalized), `confidence`, `source`
- UniqueConstraint(kind, value)

`domain` and `linkedin_company` are strong keys (confidence 1.0). `name_norm` is a weak key: it can propose a match but never auto-merge (this is the Brittany/Kyndred class of bug; see resolution below). Subsidiaries/rebrands are handled by adding identities to one Company row, or an explicit `merged_into_id` tombstone.

### AccountMembership (per-user, person <-> company over time)
The load-bearing join. Not "Contact.company_id" -- a person's company affiliation is a time-bounded edge, and job changes are the single most valuable proactive signal we have.

- `user_id`, `contact_id`, `company_id`
- `role_title`, `seniority_band` (nullable)
- `is_current`, `started_at`, `ended_at` (nullable)
- `source` (enrichment | job_change_event | manual | backfill), `confidence`
- UniqueConstraint(user_id, contact_id, company_id, started_at)

When the updates engine detects `job_change`, it closes the old membership (`is_current=false, ended_at=now`) and opens a new one. This gives us both sides of the move for free: the relationship travels with the person (you now know someone at NewCo), and your coverage at OldCo visibly drops.

Scoped per-user (not global) because "Daniel believes Jane works at Acme" is part of Daniel's graph, may come from his private data, and may be wrong independently of another user's copy.

### Account (per-owner view of a Company)
Your relationship with the company, analogous to what Contact is for a person.

- `owner_type` + `owner_id` (user now, team later -- see section 6)
- `company_id`
- `tier` (key | active | tracked), `vip`-equivalent starring
- `notes`, `objective` (free text: "want intro to their platform team")
- `sharing_level` (see §6: private | metadata | elevated) -- per-account, owner-set
- cached rollups (recomputed, not source of truth): `strength_score`, `last_touch_at`, `contact_count`, `warmest_contact_id`
- UniqueConstraint(owner_type, owner_id, company_id)

Created lazily, exactly like Contact: the first time a resolved membership lands for a company, the owner gets an Account row.

## 3. Company resolution pipeline

New module: `agents/relationship/company_resolve.py`. Same philosophy as `identity.py`: strong keys auto-link, weak signals go to review.

1. **Strong path (deterministic):** email domain from any ContactIdentity (minus a freemail blocklist: gmail, outlook, etc.), or LinkedIn company URL/ID from enrichment payloads -> exact CompanyIdentity lookup -> link or create.
2. **Weak path (LLM disambiguation):** company name string only. Claude gets the name plus contact context (title, headline, location, linkedin url) and candidate Company rows, and returns match / new-company / ambiguous with a confidence. This is the same component already planned for triage disambiguation (the Brittany/Kyndred bug) -- build it once here and let triage call it. Below threshold -> `pending_review` membership surfaced in the UI, same pattern as medium-confidence person merges.
3. **Backfill:** one-shot Modal job (`modal_jobs.py`) sweeping existing `Contact.company` / `company_domain` strings through the resolver. Domain-keyed contacts link cleanly; name-only contacts go through the LLM path in batches. Everything lands with `source=backfill` and its confidence, so a bad backfill is auditable and reversible.
4. **Ongoing hooks:** contact create/link (`link_contact()`), enrichment writes (capture_enrich, live_enrich, triage enrich), and `apply_profile()` job-change detection all call the resolver. No new pipeline -- the resolver rides the existing ingest points.
5. **Corrections:** user rejects/fixes a match -> CompanyOverlay row, membership relinked, global row untouched.

## 4. Portrayal: the account read model

New route (`routes/accounts.py`) + read-model assembly next to `spine/relationships.py`. The account page answers four questions:

1. **Who do we know there?** Current memberships joined to Contacts, each with its `score_health()` output, sorted warmth-first. Former members shown separately ("used to be here, now at X" -- each one is a live thread into another account).
2. **How strong is the account relationship?** Account strength = weighted rollup of member-contact health: recency-weighted, seniority-weighted, and bidirectionality-weighted (inbound replies count more than outbound sends -- direction already exists on RelationshipInteraction). Computed on read like `score_health()`, with a small `AccountStrengthSnapshot` history table (weekly write from the sweep) so trends ("cooling for 3 weeks") are portrayable.
3. **What happened?** Unioned timeline: every member-contact's `build_timeline()` merged chronologically, plus company-level signals (fundraise, news) interleaved.
4. **What's the gap?** Coverage view: seniority/function spread of who you know vs. don't ("3 engineers, nobody commercial"), single-threaded warnings (account depends on one warm contact), and dormant-account flags.

The book UI gets an Accounts tab as a peer of People; each contact card links to its account and vice versa.

## 5. Proactive engine: reactive CRM -> outreach agent

No new machinery -- the account layer widens what the existing `updates_engine` sweep can see and emit. Today every trigger is person-scoped (job change, post). The account tier adds signals that only exist in aggregate:

| Signal | Detection | Play (autodraft) |
|---|---|---|
| Job change into a tracked account | existing `job_change` + membership close/open | congratulate + re-anchor; flag warm path into NewCo |
| Departure from an account | membership close | continuity: who else do we know there / follow the person |
| Company event (fundraise, launch, news) | Company-level watch: Exa/Bright Data company sweep, cadenced by account tier, one scrape shared globally | congratulate the warmest contact, in-voice |
| Account cooling | strength snapshot trend across ALL member contacts | reconnect play targeting the warmest contact |
| Whitespace / single-threaded | coverage math | expansion play: draft intro request via existing warm contact |
| Objective match | `Account.objective` vs new signals/facts | surface "this is your moment" with evidence |

Implementation: `run_sweep()` gains an account pass after the contact pass (per-user rollup recompute + due company watches). Signals `_emit()` RelationshipInteractions -- new `account_signal` source_type, attached to the relevant contact -- so they flow into the existing timeline, Today feed, and the 4-stage drafting pipeline unchanged. Account plays respect the same autonomy gates (`SURPLUS_AUTO_FOLLOWUPS` for built-in follow-ups, `SURPLUS_AUTOMATED_SENDS` for anything autonomous): the agent proposes, the user approves, exactly like today.

This is the reframe: the Today feed stops being "contacts you haven't touched" and becomes "accounts where something changed and here's the drafted move."

### Event-driven updates; the sweep is the backstop, not the engine

The sweep is not the only (or primary) mover. Freshness comes from three sources, fastest first:

1. **External webhooks (already live).** Unipile POSTs inbound messages, replies, and connection accepts to `routes/webhooks.py`; Bright Data delivers scrape results the same way. These land in real time today. New behavior: each webhook, after writing its interaction, emits an internal event (below) instead of waiting for the next sweep to notice.
2. **Internal events (build ours).** A small in-process dispatcher (same pattern as the existing 60s outbox dispatcher): any ingest write -- webhook arrival, in-person capture, email sync hit, the user's own send or note -- fires `entity_changed(contact_id / company_id)`. The handler incrementally recomputes just the affected rollups (that contact's health, that account's strength/last-touch/warmest-contact) and runs the §5 signal detectors for that entity only. Cheap (indexed, single-entity), near-instant, and it means a reply you get at 9:04 updates the account view at 9:04, not at the top of the hour.
3. **Polling where no event source exists.** LinkedIn does not push "this person changed jobs" to anyone; company news has no reliable push feed at our price point. Profile and news watches stay cadence-polled (tiered: VIP daily / standard weekly / tail monthly) because the provider boundary forces it -- this is a fact about the data sources, not our architecture.

The hourly sweep remains for exactly two jobs: initiating the forced polls (3), and **reconciliation** -- recomputing rollups wholesale to self-heal anything a missed webhook or dead dispatcher dropped. Event-driven systems without a reconciliation loop drift; this one can't, and the sweep infrastructure already exists (`updates_scheduler.py` + Modal fallback), so the backstop is free.

## 6. Cross-user layer and data protection

The team question -- "who at our firm knows someone at this client" -- is where architectures in this market genuinely diverge, and the sharing model is the single biggest product decision in this doc. §9.1 walks the alternatives; the outcome separates **mechanism from policy**:

> **Mechanism: tiered sharing levels per account. Policy: a team-level compliance profile caps what levels are reachable, and ethical walls override everything. Nothing is ever silently copied.**

The stance (Daniel, 2026-07-05): **be free by default, with guardrails that are structural rather than behavioral.** Sharing defaults to open-within-the-team because the guardrails below (walls, owner levels, derived-only serializers, audit) hold regardless of profile -- confidentiality is guaranteed by architecture, not by choosing a restrictive default. Teams that need a hard ceiling (conflict-screening law firms) flip to the strict profile at onboarding with one question.

### Data classes

| Class | Examples | Default visibility |
|---|---|---|
| A. Public company data | Company row, enrichment, news | Global (all users) |
| B. Relationship existence + strength | "Daniel knows Jane at Acme, warm, last touch 2w ago" | Team, derived-only |
| C. Interaction content | message bodies, email text, notes, ContactFacts | Private; owner can elevate per-account |
| D. Credentials | Unipile/OAuth tokens | Server-only, never surfaced |

### Sharing levels (per-account, owner-controlled)

- **Level 0 -- private:** the account is invisible to the team entirely (sensitive relationships: your own investors, personal contacts).
- **Level 1 -- metadata (default):** teammates see Class B aggregates only: who knows whom, warmth tier, last-touch recency band, direction counts. Never the messages or facts that produced them. Strength scoring runs entirely on Class C *metadata* (timestamps, directions, counts), so Level 1 exports nothing content-derived -- the current `score_health()` heuristic already satisfies this.
- **Level 2 -- elevated:** the owner shares a specific account's timeline (interaction summaries and notes, still not raw message bodies) with the team, for accounts being worked jointly. Explicit per-account action, revocable, badge-visible in the UI. **Only reachable if the team's compliance profile allows it.**

### Compliance profile and ethical walls (the policy layer)

- **Team compliance profile** (set by team admin at onboarding):
  - `collaborative` (default): Level 2 available, still owner-initiated per account. Walls and audit always on.
  - `strict` (law firms running conflict screening, funds with LP-sensitive books): Level 1 is the ceiling -- Level 2 does not exist in the UI.
  - The profile caps, never raises: an owner's Level 0 is respected under every profile. Onboarding asks one question ("does your firm run conflict screening / information barriers?") to pick the profile.
- **Ethical walls** (`Wall` table: team_id, subject kind = company | contact, subject id, excluded member ids or allowed member ids, reason, created_by, created_at): the conflicts-of-interest primitive (information barrier / screen) law firms are required to run. Concretely, for an excluded member the walled subject ceases to exist on the team plane, in both directions:
  - **Inbound (they see nothing):** no search hits, no account row, no appearance in "who knows whom," no contribution to counts ("3 people know someone at Acme" is itself a leak -- revealing that a relationship *exists* can be a breach), no proactive plays, no intro suggestions touching the subject.
  - **Outbound (they leak nothing):** the excluded member's own relationships with the subject are withheld from everyone else's team aggregates -- teammates don't see them as a path into the walled company, and no one can route an intro request through them.
  - **Personal graph untouched:** their own private book keeps their own contacts and history -- a wall governs team-plane information flow, it never confiscates or edits anyone's personal data.
  - **Overrides everything:** a wall beats an owner's Level 2 elevation and any profile. Enforcement is in the query layer, before aggregation, so there is no serializer to get wrong.
  - **Auditable:** wall create/modify/delete are themselves audited events; the audit log is the compliance evidence a firm shows when asked to demonstrate the screen.
- **Access audit log:** first-class table (who viewed which account/contact aggregate, when, at what level), not just an ops nicety -- it is part of what the legal buyer is purchasing. Exportable per team.

### Team model

- New `Team` + `TeamMembership` (role: admin | member) tables.
- **Contacts stay per-user. Nothing is re-parented.** A team view is a query-time join: each user's Contacts resolve through global person identity (extend the MonitoredPerson pattern: a global person node keyed by LinkedIn member id / normalized email hash, with per-user Contact links) and through Company. "Who knows someone at Acme" = traverse team members' memberships for that company_id, returning the aggregate shape allowed by each owner's sharing level.
- Team-scoped `Account` rows (`owner_type=team`) hold the shared objective/tier; each member's underlying data stays theirs.

### Protections (in priority order)

1. **Consent at the edge:** joining a team turns on Level 1 with explicit onboarding copy; a per-user kill switch drops all their accounts to Level 0. Per-account overrides in both directions.
2. **Departure semantics:** the individual owns their graph. Leaving a team removes their edges from the team view immediately -- no orphaned copies, because nothing was ever copied. Level 2 elevations are revoked on departure. (This is also the pitch to individual users: your rolodex remains yours.)
3. **Level and wall enforcement in the API layer:** team-scope endpoints are physically separate routes whose serializers can only emit the shape for the row's effective level (owner level, capped by team profile). Wall filtering is applied in the query layer before aggregation, so a walled company never contributes to counts or "who knows whom" results for excluded members. No code path exists that serializes raw RelationshipInteraction bodies or ContactFacts across user boundaries at any level.
4. **Salted-hash identity join:** cross-user person matching uses the same salted email hashes as today, plus stable LinkedIn ids. Raw emails never move across user scope.
5. **Prerequisites before any team launch:** encrypt `ConnectedAccount` tokens at rest (already flagged as plaintext-v1 in models.py), add an access audit log (who viewed which account/contact aggregate), and cascade-delete verification (delete contact -> memberships, identities, interactions, and global-person links all go).
6. **Intro-request flow instead of data flow:** at Level 1, when teammate B wants A's contact, the product motion is "ask A for an intro" (A gets a drafted intro message), not "show B the contact's info." Warmth is shared; access is granted by the owner, per-instance.

### Deliberate non-goals
- No pooled raw message content, ever (not "with permission," not for training) -- Level 2 shares summaries and notes, not bodies.
- No global cross-tenant relationship graph visible across teams. Company rows are global; edges never are.
- No silent contact syncing between users. Merging two users' knowledge of the same person happens only in query-time team aggregates.

## 6b. Setup: how a user or firm gets onboarded

Design principle: **accounts are derived, never data-entered.** A classic CRM makes you create accounts and file people into them; here the resolver materializes accounts from relationships that already exist. Setup is therefore consent and policy, not data work.

### Solo user (Phase 1) -- zero setup
Connecting channels (LinkedIn/email) is the existing onboarding, unchanged. The backfill sweeps their existing contacts through the company resolver and Account rows materialize lazily. The only manual inputs, both optional: star/tier the accounts they care about, and write an objective on an account ("want intro to their platform team") to sharpen the proactive engine. A solo user wakes up to an Accounts tab that is already populated.

### Firm (Phase 3) -- a ~15-minute admin flow, then per-member consent
1. **Create team + one policy question.** "Does your firm run conflict screening / information barriers?" -> sets the compliance profile (strict caps at Level 1; collaborative enables Level 2). Changeable later by admin, always audited.
2. **Import conflicts (strict firms, optional but first).** Paste/CSV a list of company names -> resolver maps them to Company rows (same strong-key/LLM path, ambiguous ones surfaced for confirmation) -> walls created *before any member joins*. Because the team view is query-time, a wall created later still applies fully and retroactively (nothing was ever copied that would need scrubbing) -- but walls-before-members is the clean compliance story.
3. **Invite members.** Each member joining sees the consent screen: exactly what Level 1 shares (warmth + who-knows-whom, never content), the per-user kill switch, the departure guarantee (leave and your edges vanish). An existing solo user links their account and their graph comes with them; a new user connects channels as in solo onboarding.
4. **Auto-join, no data entry.** On each join, the cross-user identity join and company overlap run automatically. The system proposes team accounts from overlap ("14 companies where 2+ of you have warm paths") plus each member's key-tier accounts; admin confirms which become team accounts and adds objectives.
5. **Day-one output.** Team coverage map (who knows whom, where the firm is single-threaded) and the first proactive digest. This is the aha moment and it requires zero manual input -- it is assembled entirely from graphs the members already had.

Ongoing: new member -> steps 3-4 for them alone; departure -> automatic (edges vanish, Level 2 elevations revoked); wall changes -> instant, audited. Nothing about a firm's setup ever requires a member to hand over or re-enter data.

### Compressing setup to ~2 admin minutes

Principle: **only what gates exposure may block; everything else defers.** Exposure begins at exactly one moment -- the team view going live. Query-time assembly makes deferral safe (a wall added later applies fully and retroactively; there are no copies to scrub), so the flow splits into a tiny blocking set and an async remainder:

**Blocking (the 2 minutes):**
1. One policy question -> profile. (Pre-suggest from the firm's domain/industry; admin confirms with one tap.)
2. Send invite links.
3. Each member's consent screen (never removable -- it IS the ownership promise; one screen, join now, connect channels later).

**Deferred, with a safety interlock instead of a to-do list:**
- **Conflict list (strict firms):** team view starts in `pending` -- members join, graphs link, coverage *counts* compute, but no relationship data is visible to anyone until the admin either finishes the conflict import or explicitly skips it (skip is an audited event). So deferral never creates an exposure gap, and the compliance story ("walls existed before anything was visible") survives compression.
- **Conflict import is agent work, not form work:** accept the list in any format -- CSV, a forwarded email, a screenshot of their DMS -- LLM-parse it, resolve names to Company rows, and come back with only the ambiguous ones ("does 'Meridian' mean Meridian Health or Meridian Capital?"). Later: direct import from practice-management tools (Clio-class) where the list already lives. The LLM's role here is proposal-only; see the deterministic gates below.

### Deterministic gates on conflict import (LLM proposes, never commits)

Error asymmetry drives the design: a false-positive wall (walling something that needn't be) is an inconvenience an admin can remove; a false-negative (a dropped or mis-mapped conflict) is a breach. Every gate therefore fails toward over-walling, and no LLM output ever writes a wall:

1. **Deterministic parse first.** Structured input (CSV, spreadsheet, Clio export) is parsed by code -- column mapping, not language modeling. The LLM touches only genuinely unstructured input (email prose, screenshots), and there it *extracts*, producing a proposal table, nothing else.
2. **Provisional name-walls, instantly, in code.** The moment a name string is extracted -- before any entity resolution, before any admin review -- a provisional wall is written against the *normalized name identity* (`name_norm`). Any Company matching that string is walled immediately. Over-broad by design ("Meridian" walls both Meridians until disambiguated); resolution and admin answers then *narrow* it. The fail-safe state is walled, and reaching it involves no model judgment.
3. **Coverage invariant, enforced by code.** Every source row/line must end in exactly one audited state: `walled` | `pending-disambiguation` (provisionally walled per gate 2) | `admin-rejected`. Row counts in = states out, checked deterministically; a parse that silently drops a line cannot pass. For unstructured input, a second independent extraction pass runs and any diff between the two flags the import for line-by-line review.
4. **The admin confirms the mapping, not the vibes.** The proposal table renders side-by-side with the source document (their CSV rows / their email text), so verification is a visual diff of "your line -> our wall," not trust in a summary. The confirmed table is the artifact that commits entity-level walls -- stored with the source-document hash in the audit log.
5. **Exposure is gated on confirmation, not on parsing.** The strict-profile team view stays `pending` until the admin confirms the mapping (or audited-skips). So even a wrong parse exposes nothing: the LLM sits entirely upstream of a deterministic human-confirmed gate, and the walls that ultimately enforce are rows the admin looked at.

Same principle as §9.8 (deterministic detection, LLM composition), applied to compliance: models do reading and drafting; code and the admin do the committing.
- **Team accounts: auto-created, not confirmed.** Overlap suggestions become team accounts automatically at Level 1 (metadata-only, so auto-creation exposes nothing new); the admin demotes rather than approves. Objectives are collected lazily, in the flow of use ("this account looks active -- what's the goal here?"), not at setup.
- **Tiering/starring, channel connects, voice sync:** all post-join, all individual, none block the team.

Net: admin does question + invites (+ forwards a conflict list in whatever form it exists); the system and the agent do the rest. The 15-minute version above remains the *logical* order; this is the same flow with everything non-gating moved off the critical path.

## 7. Build order

**Phase 1 -- Account spine (single-user, no privacy surface):**
Company + CompanyIdentity + CompanyOverlay + AccountMembership + Account tables (idempotent `_migrate_*` in db.py), `company_resolve.py` with the LLM disambiguator, backfill job, account read model + Accounts tab. Ships standalone value: "here's everyone I know at every company, portrayed."

**Phase 2 -- Proactive account signals:**
Account pass in `run_sweep()`, company-level watch, strength snapshots, the six plays wired into the existing emit -> autodraft path. This is the reactive->proactive flip.

**Phase 3 -- Team layer:**
Team/TeamMembership (with compliance profile), Wall table + query-layer wall filtering, global person nodes, sharing levels + level-enforcing aggregate API, consent + kill switch, access audit log, token encryption. Ship only after 1-2 are stable, and pilot with one real multi-user team -- ideally one strict-profile firm, since walls and audit are the hard parts to get right.

Phase 1 is deliberately cheap: five tables, one resolver, one page, no changes to sends or privacy posture. Phases 2 and 3 are each independently shippable on top.

## 8. Open questions for Daniel

1. **Wall granularity:** are company-level and contact-level walls enough for the law-firm case, or do we need matter-level walls (one company, walled per engagement)? Matter-level is a real legal-industry concept but adds an entity we otherwise don't have. Lean: launch with company/contact walls, add matters only if a design partner requires it.
2. **Level 2 scope (collaborative profile only):** should elevated sharing include interaction *summaries* only (current design) or full notes too? Summaries are safer; notes are more useful for joint deal work.
3. **Freemail contacts:** people with only a gmail address and a company name string will lean heavily on the LLM resolver. Acceptable to leave them account-less until enrichment finds a LinkedIn/company id?
4. **Account tiers vs contact tiers:** does account tier override member-contact cadence (VIP account => all members watched daily), or stay independent? Lean: account tier sets a floor on member cadence.
5. **Company watch cost:** company news sweeps add Exa/Bright Data spend per tracked account. Gate to key-tier accounts only at first?

## 9. Decision log: alternatives considered

Each major choice, the real options, and why the pick. "Best solution for surplus" means judged against: LinkedIn-first data, solo-first wedge, small collaborative teams, individual ownership promise, existing codebase patterns.

### 9.1 Sharing model (the crux)

| Option | Exemplar | Pros | Cons | Verdict |
|---|---|---|---|---|
| Fully private, no team layer | Clay (personal CRM) | zero privacy risk; simplest | no collective intelligence at all; caps the product at solo | rejected as endpoint (it's just Phase 1-2) |
| Metadata-only, hard-coded firm-wide | Nexl, Introhive | bottoms-up trust; passive; matches the segment's confidentiality needs | one compliance stance baked into the schema; no ethical-wall granularity in a naive version (revealing that a relationship *exists* can itself be a breach); no path for teams that do co-work accounts | rejected as-is, but its posture becomes our default profile |
| Full shared workspace | Affinity, 4Degrees | rich collaboration; one source of truth for a dedicated deal team | everything you sync is the firm's; departure = data stays; unusable under law-firm confidentiality; contradicts our ownership promise | rejected |
| **Tiered mechanism + team compliance profile + walls** | (ours) | strict-by-default (Level 1 ceiling + walls + audit) satisfies law firms and funds; strictness is policy, not schema, so a collaborative team can deliberately enable Level 2; ownership preserved at every profile | most states to build; wall filtering and level enforcement must be airtight and audited | **chosen** |

Two facts drive this. First, the expansion buyers (small law firms, VC funds) require strict confidentiality to be *available and provable* -- ethical walls and audit, which pure "share warmth firm-wide" designs leak. Second, the product stance is freedom by default: because walls, owner levels, and derived-only serializers are structural guarantees that hold under every profile, the default can be collaborative without weakening confidentiality where it's invoked. Strictness is a per-team policy choice backed by architecture, not a schema-wide posture imitated from any competitor.

### 9.2 Global Company entity: mutable-shared vs read-only + overlay vs per-tenant copies

- **Per-tenant company rows** (classic CRM): no cross-user surface at all, but every user pays for their own enrichment/news scrapes and the "one scrape serves everyone" economics die. Rejected.
- **Global mutable rows**: best economics, but any user's bad merge or edit corrupts everyone's graph with no clean undo, and moderation becomes our job. Rejected.
- **Global read-only enrichment + per-user overlay** (chosen): pipeline-written global rows, user corrections live in CompanyOverlay. Shared economics, zero cross-user write contention, corrections double as resolver training data. Cost: one more table and a merge-on-read.

### 9.3 Person-company link: time-bounded edge vs current-only column vs event-sourced history

- `Contact.company_id` column: cheapest, but job changes destroy history and the two best proactive signals (arrival, departure) become undetectable after the fact. Rejected.
- Full event-sourcing of employment history: maximal fidelity, but we'd be modeling careers, not relationships; heavy for zero extra plays. Rejected.
- **Time-bounded membership edge** (chosen): exactly enough history for the plays in §5, natural close/open semantics on `job_change`.

### 9.4 Storage: relational + join tables vs graph database

Neo4j-style graph DBs earn their keep at multi-hop traversals over millions of edges. Our hottest query is 1-2 hops (user -> memberships -> company; team -> members -> memberships -> company) at thousands-of-contacts scale, well inside Postgres comfort. A second datastore adds operational surface (backups, migrations, Railway topology) for no current query we can't index. Revisit only if multi-hop pathfinding ("shortest warm path to any employee of X via mutuals") becomes a headline feature. **Relational chosen.**

### 9.5 Account strength: computed-on-read vs materialized snapshots vs event ledger

Computed-on-read matches `score_health()` and is always fresh but portrays no trend. A full score ledger is event-sourcing again. **Chosen: computed-on-read + weekly `AccountStrengthSnapshot`** -- trends with one small table, and the score function stays swappable (heuristic now, LLM-assisted later) without backfill pain.

### 9.6 Read-time assembly vs write-time fan-out (performance)

Assembly-on-read (§6) trades per-query compute for correctness. Is it too slow? No, for three reasons:

1. **The hot query is small and indexed.** "Who knows someone at Acme" for a 10-person team = one indexed scan on AccountMembership(company_id) intersected with team member ids, then a wall filter (one indexed lookup) -- tens of rows, single-digit milliseconds in Postgres. There is no fan-out explosion at 2-10 users x thousands of contacts.
2. **Nothing expensive runs on the read path.** Strength scores, warmest-contact, last-touch are *cached rollups on the Account row*, recomputed by the background sweep (Account.strength_score etc., §2). LLM work (scoring, drafting) happens only in the sweep, never at query time. The proactive feed is likewise precomputed -- push mode reads the sweep's output, not the graph.
3. **Caching derived aggregates is allowed and unbounded; copying source data is not.** The rule that keeps performance work safe: any Class B aggregate (per-asker or per-team) may be materialized/cached, because it is derived, owner-scoped, and disposable -- a wall or level change just invalidates the cache entry. What is forbidden is materializing *source* rows (interactions, facts) outside their owner's scope. If big teams ever make reads hot, the fix is a per-team derived-aggregate table with invalidation, not a schema change.

The alternative -- write-time fan-out into company-level buckets -- is not actually faster where it counts: it moves cost to every write, and makes wall enforcement O(all copies): creating a wall would require finding and scrubbing every materialized row that ever included the walled subject. Query-time assembly makes a new wall take effect instantly and provably. **Read-time assembly + derived-only caching chosen.**

### 9.7 Company-event sourcing: own watcher vs push vendor vs per-user polling

Three ways to learn "Acme raised a Series B":

- **Per-user polling** (naive): every user's sweep asks Exa/Bright Data about every company in their book. Cost scales users x companies; the same Acme question gets asked N times. Rejected.
- **Own watcher on the global Company tier** (chosen for Phase 2): one service polls each *distinct* Company on its cadence (tier-driven), diffs against `seen_signal_ids`, and emits `company_event` onto the internal bus -- fanning out to every user with an Account on that company. Cost scales with distinct companies, not subscriptions; from every consumer's perspective it IS a webhook (they never poll). Uses providers we already integrate; no new vendor; cadence and spend stay under our control.
- **Push vendor** (upgrade path, not now): firmographic-event APIs (PredictLeads/Specter/Harmonic class) will do the watching and POST fundraise/leadership/news events to a real webhook. Better latency (same-day congrats on a fundraise) and zero watcher ops, but per-tracked-company pricing and another vendor dependency. Plug one in later as just another producer on the same bus -- the consumer side doesn't change. Trigger to revisit: tracked-company count or latency demands outgrow the watcher.

Building true push infrastructure ourselves (crawling the web continuously to detect events) is a data-vendor business, not ours -- permanently out of scope.

### 9.8 Proactive plays: rules vs LLM-decided vs hybrid

Pure rules can't rank "what matters most today" across accounts; pure LLM sweeps are expensive and non-deterministic about *whether* something fired (bad for trust and testing). **Chosen: deterministic detection (the §5 table -- a signal either fired or didn't), LLM only for composing the move and ranking the feed.** Same split the drafting pipeline already uses: deterministic gather/resolve, model render.
