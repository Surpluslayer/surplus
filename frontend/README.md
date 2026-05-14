# frontend/

`App.jsx` — the single-file React demo of the five-stage flow (intake →
prospecting → auto-outreach → symbiotic matching → ROI ledger).

Right now it runs **fully self-contained** with mocked data — it does *not*
call the backend. It exists to show the mechanism and the UI.

## Wiring it to the backend

The backend exposes the same five stages as endpoints. To make the UI live,
replace the mocked stage data with `fetch` calls:

| UI stage            | call                                             |
|---------------------|--------------------------------------------------|
| Intake → Run        | `POST /events` then `POST /events/{id}/run`      |
| Auto-outreach       | read `prospects[]` + `counts` from the run result|
| Matching            | `POST /events/{id}/match`                        |
| ROI ledger          | `GET  /events/{id}/roi`                          |

The backend's `EventCreate` fields are snake_case (`co_stage`) where the UI
state is camelCase (`coStage`) — map them at the fetch boundary.

CORS is open in `backend/main.py` for local dev.
