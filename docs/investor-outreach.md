# Investor outreach campaign

A batched, throttled LinkedIn connection-request campaign against a fixed,
hand-curated investor roster. It reuses the product's one guarded send path, so
every invite gets the same double-send hold, 300-char note check, dry-run
gating, and `OutreachLog` row as any other outreach.

Use it to work through a long, pre-written list of people slowly and safely,
instead of firing dozens of invites in one burst (which trips LinkedIn's
anti-spam limits and risks a restriction on the account).

## What's in the box

| Piece | Path |
|------|------|
| The roster (name, LinkedIn URL, per-person note, confidence) | `backend/data/investor_outreach.json` |
| Seed + batch-send logic | `backend/agents/relationship/investor_campaign.py` |
| CLI (seed / preview / send) | `scripts/investor_outreach.py` |
| Daily Modal cron | `modal_jobs.py::investor_outreach_sweep` |
| Tests | `tests/test_investor_campaign.py` |

Each roster entry carries a `confidence` (`high` / `medium` / `low`) from the
profile-resolution step. Only `high` rows auto-send; `medium`/`low` (a company
page, or a common name that couldn't be pinned to one profile) are seeded but
held out of the automated batch until a human confirms the match.

## Safety model — nothing sends by accident

Sending is **double-gated**. Both must be true for a real invite to leave the box:

1. `UNIPILE_DRY_RUN=false` — otherwise every send is a dry-run preview that logs
   `dry_run_queued` and flips no status.
2. `INVESTOR_OUTREACH_ENABLED=true` — otherwise the daily Modal job no-ops.

Plus:

- **Daily cap** — `INVESTOR_OUTREACH_DAILY_CAP` (default 12) per run. With the
  daily schedule, the whole ~59-person roster takes about a week to clear.
- **Idempotent** — a person who already has an `invite_sent` / `unconfirmed` /
  `message_sent` log is never re-sent.
- **Confidence gate** — auto-send is `high`-only.
- **Jitter** — a randomized pause between live sends.

> Note: sending only works where Unipile egress is allowed — i.e. the deployed
> backend / Modal, not a network-restricted CI or coding session.

## Run it

### Locally (against whatever `DATABASE_URL` points at)

```bash
# 1. Seed the campaign event + prospects (idempotent) and list the roster:
python -m scripts.investor_outreach seed

# 2. Preview the next batch — always dry-run, never sends:
python -m scripts.investor_outreach preview --limit 12

# 3. Send for real (requires a connected LinkedIn account for the sender):
UNIPILE_DRY_RUN=false python -m scripts.investor_outreach send --limit 12
```

Pick the sending account with `INVESTOR_OUTREACH_USER_EMAIL` when more than one
user has LinkedIn connected. Add `--all` to include the medium/low-confidence
rows once you've eyeballed them.

### Recommended first run: a test batch of 5

Before enabling the daily schedule, send a small test batch and confirm the
invites landed on the right profiles (a wrong-profile match on a common name is
the one thing you can't undo):

```bash
# 1. Eyeball the exact matched profiles first (dry-run, never sends):
python -m scripts.investor_outreach preview --limit 5

# 2. Send just 5 for real, then check LinkedIn that all 5 look right:
UNIPILE_DRY_RUN=false python -m scripts.investor_outreach send --limit 5
```

Only after those 5 look correct should you enable the daily cron below (or keep
running `send --limit 12` by hand). Sends are idempotent, so the 5 already sent
are never repeated.

### On a schedule (Modal)

`investor_outreach_sweep` is already registered with `schedule=modal.Period(days=1)`.
It stays dormant until you set both gates in the `surplus-jobs` secret:

```bash
modal secret create surplus-jobs \
  ... existing vars ... \
  UNIPILE_DRY_RUN=false \
  INVESTOR_OUTREACH_ENABLED=true \
  INVESTOR_OUTREACH_DAILY_CAP=12 \
  INVESTOR_OUTREACH_USER_EMAIL=you@example.com

modal deploy modal_jobs.py
```

To pause: set `INVESTOR_OUTREACH_ENABLED=false` (or `UNIPILE_DRY_RUN=true`) and
redeploy. The schedule keeps firing but does nothing.

## Editing the roster

Edit `backend/data/investor_outreach.json` (or regenerate it). Re-running
`seed` upserts on `identity`: it refreshes name / URL / note / role in place and
adds any new rows, without duplicating or resetting send state. Notes must be
≤300 characters (LinkedIn's connection-note limit) — the tests enforce this.
