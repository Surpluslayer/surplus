# surplus · event ROI engine

A runnable FastAPI + SQLite backend for the five-stage event mechanism: turn an
intake profile into a prospected, auto-contacted, symbiotically-matched guest
list with a verified per-guest ROI ledger.

The thesis: the product isn't analytics, it's **mechanism design**. The floating
fit threshold, the autonomous outreach with composition reveal, the symbiotic
matching market, and the goal-priced ROI ledger are the levers — this repo is
those levers, wired end to end.

## The five stages → where they live

| Stage | What it does | Code |
|------:|--------------|------|
| **01 intake** | Capture the event profile — ICP, shape, goal, budget. Derive the funnel target. | `routes/events.py`, `models.Event` |
| **02 prospecting** | Fan out across source adapters *concurrently*, merge on identity. | `agents/prospector.py`, `agents/sources/` |
| **03 scoring + outreach** | Deterministic fit score + reasoning; the threshold *floats* to hit funnel supply; autonomous outreach for everyone above it. | `agents/scorer.py`, `agents/outreach.py`, `pipeline.py` |
| **04 matching** | Guest list as a value graph — symbiotic edges (offer↔seek across sides) + affinity edges; side-balanced group formation. | `agents/matcher.py` |
| **05 ROI** | Per-guest conversion ledger + net ROI, settled against the intake goal. | `agents/roi.py` |

`config.py` holds the two tables that make it adapt — `FORMAT_CONFIG` (matching
topology per event format) and `GOAL_CONFIG` (what "converted" means and what
it's worth per goal).

## Repo layout

```
.
├── backend/
│   ├── main.py              FastAPI app — wires the stage routers
│   ├── config.py            mechanism levers (FORMAT_CONFIG, GOAL_CONFIG)
│   ├── db.py                SQLite engine + session
│   ├── models.py            SQLAlchemy: Event, Prospect, OutreachLog, MatchEdge, Conversion
│   ├── schemas.py           Pydantic request/response shapes
│   ├── pipeline.py          stage 02-03 orchestrator (fan-out → score → threshold → outreach)
│   ├── seed.py              run all 5 stages end to end, no HTTP — `python -m backend.seed`
│   ├── agents/
│   │   ├── prospector.py    concurrent fan-out, merge on identity
│   │   ├── scorer.py        fit score + floating threshold
│   │   ├── outreach.py      autonomous compose / send / track
│   │   ├── matcher.py       symbiotic + affinity edges, group formation
│   │   ├── roi.py           conversion ledger + net ROI
│   │   └── sources/         pluggable prospect-source adapters
│   │       ├── base.py      SourceAdapter contract
│   │       ├── github.py    OSS signal
│   │       ├── x.py         reach signal
│   │       └── linkedin.py  profile + contact resolution
│   ├── routes/              one router per stage
│   └── data/
│       └── prospect_pool.json   20-person mock candidate universe
├── frontend/
│   └── App.jsx              the single-file React demo (mocked; see frontend/README.md)
├── tests/                   pytest — unit tests per agent + an end-to-end API test
├── requirements.txt
└── README.md
```

## Run it

```bash
pip install -r requirements.txt

# option A — see the whole mechanism run end to end, no server
python -m backend.seed

# option B — run the API
uvicorn backend.main:app --reload
#   docs:  http://localhost:8000/docs

# tests
pytest -q
```

## API

| Method | Path | Stage |
|--------|------|-------|
| `POST` | `/events` | 01 — create the event profile |
| `GET`  | `/events/{id}` | 01 — read it back |
| `POST` | `/events/{id}/run` | 02-03 — fan-out + score + autonomous outreach |
| `GET`  | `/events/{id}/prospects` | 02-03 — read the resolved pool |
| `POST` | `/events/{id}/match` | 04 — build the symbiotic value graph |
| `GET`  | `/events/{id}/matches` | 04 — read the stored graph |
| `GET`  | `/events/{id}/roi` | 05 — settle the conversion ledger |

Stages are barriers: `/match` 409s until there are confirmed guests from
`/run`; `/roi` 409s until there's something to settle. `/run` and `/match` are
idempotent — re-running clears the prior result first.

Minimal flow:

```bash
curl -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '{"headcount": 9, "format": "Hackathon", "goal": "Hiring pipeline"}'
curl -X POST localhost:8000/events/1/run
curl -X POST localhost:8000/events/1/match
curl localhost:8000/events/1/roi
```

## Known caveat — the mock pool

`prospect_pool.json` is a 20-person universe and the source adapters are mock
(no API keys, no network). With a pool that small the **floating threshold
floors out** for any real-world headcount — `funnel_target = headcount / 0.6`
quickly exceeds 20, so the bar drops to `ABS_FLOOR`. `seed.py` uses
`headcount=9` precisely so the threshold visibly floats (settles around 78)
instead of flooring.

In production this is the one swap that matters: replace the mock
`SourceAdapter` bodies with real HTTP calls against deep pools. The
`fetch(icp) -> list[dict]` contract stays identical and nothing downstream
changes.

## Open design questions (deliberate TODOs)

These are flagged in-code (`agents/matcher.py`, `agents/roi.py`) — they're
product decisions, not bugs:

1. **Matching objective function.** `build_edges` weights every symbiotic edge
   as a flat `avg_fit + 10`. The real lever is weighting *which* cross-side
   pairing it is — a founder↔investor edge and a builder↔hirer edge aren't
   worth the same — and feeding that into `form_groups`.
2. **ROI attribution.** `tier_of()` maps fit score straight to a conversion
   outcome. That's a *prediction*. The trustworthy version reads real
   30/60/90-day per-guest follow-up data; the current mapping is the
   placeholder that lets the pipeline run end to end.
