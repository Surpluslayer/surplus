# Apply the Surplus redesign

Paste the prompt below into Claude Code from the root of the Surplus repo. It
assumes the existing FastAPI + SQLite + vanilla JS app. Two files accompany it:
`surplus-design.html` (the exact target UI + CSS, all six screens) and
`surplus-prompts.md` (the agent prompts).

The six screens in the reference file: **Today, Book, Add contact, Relationship,
Account, Connections.**

Navigation model:
- Bottom nav is **Today · Add · Book** (Add is the centered accent button).
- The **JL avatar** on Today opens **Account** (back arrow → Today).
- In **Account**, the **Connections** row opens **Connections** (back arrow → Account).
- Relationship is reached by tapping a person in Book or Today (back → Book).

---

## Prompt for Claude Code

> You're reskinning Surplus to a new design. I've added two reference files to the
> repo root: `surplus-design.html` and `surplus-prompts.md`. Read both fully before
> changing anything.
>
> **Goal:** make the running app match `surplus-design.html` exactly — same design
> tokens, components, copy, and the six screens (Today, Book, Add contact,
> Relationship, Account, Connections). Do not invent new visual styles; port what's
> in that file.
>
> **Steps:**
> 1. Map the current frontend. List every template/JS file that renders the home
>    view, the relationship list, the add-contact flow, any detail view, and any
>    settings/account view. Show me the map and your plan before editing.
> 2. Lift the `:root` design tokens and the component classes from
>    `surplus-design.html` into the app's main stylesheet as the single source of
>    truth. Keep the class names (`.phone`, `.group`, `.row`, `.health`, `.pill`,
>    `.agent-bar`, `.nav`, `.event`, `.tabs`, `.scan`, `.panel`, `.set-group`,
>    `.set-row`, `.conn-row`, etc.). Remove the `.gallery`/`.frame`/`.caption`
>    scaffolding — that's reference-only.
> 3. Rebuild the screens against real data:
>    - **Today**: dated `Your book today` title, the agent ask bar at top, then two
>      lists only — `Updates` (prospecting signals, newest first, each with a
>      timestamp) and `Needs outreach`. No full relationships list here. The JL
>      avatar is the entry to Account.
>    - **Book**: `Your book` + count, the relationship assistant card pinned on top,
>      filter pills (All / Starred / Cooling / Prospects), list sorted by who needs
>      attention, "Show N more".
>    - **Add contact**: bottom sheet. Event picker on top (current event + dropdown,
>      "Set" field, recent-event chips), the two-step banner, then
>      Scan QR / Paste link / By name tabs over the scanner frame.
>    - **Relationship**: header with name + star + health, a "Why she's [state]"
>      reasoning panel, a drafted-message panel with Send / Refine / Snooze, and a
>      timeline.
>    - **Account** (opens from the JL avatar): profile header (avatar, name, email),
>      a settings group with **Connections** (showing a status hint like
>      "Calendar off" when a source is down) and **Plan**, then a Sign out row.
>      Back arrow returns to Today.
>    - **Connections** (opens from the Account → Connections row): the three
>      integrations — LinkedIn, Gmail, Google Calendar — each with an icon, a
>      one-line description of what it powers, and either a "Connected" status or a
>      "Connect" button. Back arrow returns to Account.
> 4. Bottom nav order is **Today · Add · Book** — Add is the centered accent button.
> 5. Wire the agent features to the prompts in `surplus-prompts.md`. Prompts 1 and 2
>    are nightly batch jobs whose results are cached in SQLite and read on load;
>    prompt 3 fires on each "Draft" tap; prompt 4 backs the ask bar; prompt 5 runs
>    at capture. Add a `relationship_signals` table (or extend the contact table) to
>    store `status, needs_outreach, reason, priority, update_headline,
>    update_detected_at, outreach_trigger`. Don't call the model on page load.
> 6. Build the integrations behind Connections. Each is an OAuth connection stored
>    per user (`provider, access_token, refresh_token, status, account_label`):
>    - **LinkedIn** → feeds enrichment (prompt 5) and the prospecting Updates feed
>      (prompt 2).
>    - **Gmail** → reads thread metadata to compute `last_contact_date` / "quiet Nd",
>      and sends the drafts from prompt 3.
>    - **Google Calendar** → reads events to log meetings (timeline + last-spoke) and
>      writes review events.
>    Show real connection status on the Connections screen, and surface a
>    "Calendar off" (or any disconnected source) hint on the Account → Connections
>    row. If a source that powers a calculation is disconnected, degrade gracefully
>    rather than showing wrong numbers.
> 7. Replace the "Unknown" placeholder everywhere with enriched `name / title /
>    firm` from prompt 5. A record should never show as "Unknown" once enriched.
>
> **Design rules to hold to:**
> - Typeface: `Newsreader` for the "voice" (screen titles + any agent-written
>   message text); `Inter` for all UI/metadata. Two weights only: 400 and 500.
> - Sentence case everywhere. Minimal helper text — let labels and data carry it.
> - The gold star marks a key relationship. Health is a colored dot + word
>   (active=green, warm/quiet=amber, cooling/dormant=red, new=blue).
> - Keep it minimal and flat: 0.5px borders, the radii in the tokens, generous
>   whitespace, no shadows or gradients.
> - Back arrows always point to the parent screen (Connections → Account,
>   Account → Today).
>
> Work screen by screen. After each, show me a diff and a screenshot before moving
> on. Don't touch the data model or routes beyond what steps 5 and 6 require without
> flagging it first.

---

## After it's applied
- Sanity-check the batch jobs actually populate the cache before relying on Today.
- Decide an aging window for Updates (how long a signal stays on Today before it
  drops off) — a promotion ~1 week, a liquidity event longer.
- Notifications was intentionally left out for now — add it later if you build the
  push/email digest, not before.
- The Today vs. agent-first question is still open: this build keeps Today as two
  lists with the agent on top. If you want to lean further into "agent does the
  work," the next step is having Today open on pre-drafted moves you approve.
