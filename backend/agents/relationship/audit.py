"""agents/relationship/audit.py : the one write path into the team audit log.

Every team-plane event goes through write() so rows are uniform and the
call sites stay one line. Fail-soft BY DESIGN for reads (a broken audit
insert must never take down the team view — availability) but the flush
happens in the caller's transaction for mutations (a wall change and its
audit row commit or roll back TOGETHER — an unaudited wall change must be
impossible).

detail must never contain relationship content (summaries, message text,
facts). Counts, ids, reasons, old/new policy values only.
"""
from __future__ import annotations

import json

from ... import models


def write(db, *, team_id: int, actor_user_id: int, event: str,
          subject_company_id: int | None = None,
          detail: dict | None = None, best_effort: bool = False) -> None:
    """Append one audit row in the caller's transaction (no commit here —
    the caller owns the boundary, so mutation + audit are atomic). With
    best_effort=True (reads), swallow any failure after a rollback-free
    attempt so the view path never 500s on audit trouble."""
    try:
        db.add(models.TeamAuditLog(
            team_id=team_id,
            actor_user_id=actor_user_id,
            event=event,
            subject_company_id=subject_company_id,
            detail_json=json.dumps(detail or {}, default=str)[:2000],
        ))
        db.flush()
    except Exception as exc:  # noqa: BLE001
        if best_effort:
            print(f"  [audit] best-effort write dropped ({event}): "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return
        raise
