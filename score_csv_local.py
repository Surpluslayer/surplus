"""
Local one-shot triage scorer.

Runs the EXACT prod inbound path against a Luma CSV on a throwaway SQLite DB,
then prints a ranked table. No Railway, no Cloudflare, no UI.

Usage:
    python score_csv_local.py <path/to/luma.csv> [icp.json]

icp.json (all optional) shapes BOTH the structured ICP and the free-text
triage_config the rubric is synthesized from:
{
  "role": "Founder / eng building consumer AI",
  "seniority": "Founder / Staff+",
  "co_stage": "Pre-seed to Series A",
  "format": "Sit-down dinner",
  "city": "NYC",
  "goal": "high-signal builders shipping product",
  "triage_config": {
     "event_type": "sponsor_dinner",
     "sponsor_name": "...",
     "event_goal": "...",
     "ideal_attendee_profile": "...",
     "hard_filters": ["Must be in NYC"],
     "nice_to_have_signals": ["ships consumer AI"],
     "anti_fit_examples": ["agencies", "students"],
     "capacity": 30
  }
}

Requires ANTHROPIC_API_KEY. EXA_API_KEY / Unipile creds are optional —
without them enrichment degrades to CSV-only claims (scoring still runs).
"""
from __future__ import annotations
import asyncio
import json
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)  # pull ANTHROPIC_API_KEY / EXA_API_KEY from repo .env
                            # override: the shell has an EMPTY ANTHROPIC_API_KEY

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.triage.csv_parser import parse_csv_file
from backend.triage.rubric import synthesize_rubric, icp_from_event
from backend.triage import score as _score
from backend.triage.score import evaluate_all
from backend.routes.triage import _clip

# Single team account (Daniel Wang) in the pool now → lower concurrency so 530
# lookups don't trip LinkedIn's per-account throttle. Override via SCORE_CONCURRENCY.
import os as _os
_score.SCORE_CONCURRENCY = int(_os.environ.get("SCORE_CONCURRENCY", "5"))
print(f"[config] enrichment/scoring concurrency = {_score.SCORE_CONCURRENCY}", flush=True)


def _load_icp(path: str | None) -> dict:
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    csv_path = sys.argv[1]
    icp_cfg = _load_icp(sys.argv[2] if len(sys.argv) > 2 else None)

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    user = models.User(name="Local", email="local@example.com",
                       unipile_account_id=None)
    db.add(user); db.flush()
    # triage_config precedence:
    #   1. an explicit "triage_config" block (hand-tuned, e.g. icp_bryankim.json)
    #   2. otherwise, deterministically COMPILE one from the high-level ICP fields
    #      (role/format/priority_archetypes/...) via icp_compiler. This exercises
    #      the real prod path: structured ICP -> compile_icp -> triage_config.
    triage_config = icp_cfg.get("triage_config", {})
    if not triage_config and icp_cfg:
        from backend.triage.icp_compiler import compile_icp
        triage_config = compile_icp(icp_cfg)
        print(f"[icp] compiled triage_config from ICP fields "
              f"(archetype_priority={triage_config.get('archetype_priority', {})})",
              flush=True)
    ev = models.Event(
        user_id=user.id,
        role=icp_cfg.get("role", ""),
        seniority=icp_cfg.get("seniority", ""),
        co_stage=icp_cfg.get("co_stage", ""),
        headcount=triage_config.get("capacity", 40),
        format=icp_cfg.get("format", ""),
        city=icp_cfg.get("city", ""),
        goal=icp_cfg.get("goal", ""),
        budget=0, sources="luma",
        triage_config=json.dumps(triage_config) if triage_config else None,
    )
    db.add(ev); db.commit()

    with open(csv_path, "rb") as f:
        rows = parse_csv_file(f)
    now = datetime.now(timezone.utc)
    for row in rows:
        db.add(models.Applicant(
            event_id=ev.id,
            name=_clip(row.get("name"), "name") or "",
            email=_clip(row.get("email"), "email") or None,
            role=_clip(row.get("role"), "role") or None,
            company=_clip(row.get("company"), "company") or None,
            website=_clip(row.get("website"), "website") or None,
            linkedin_url=_clip(row.get("linkedin_url"), "linkedin_url") or None,
            raw_application_data=json.dumps(row.get("raw_application_data") or {}),
            created_at=now, updated_at=now,
        ))
    db.commit()
    print(f"parsed {len(rows)} rows; scoring…", flush=True)

    rubric = synthesize_rubric(ev.id, ev.triage_config or "",
                               list(ev.applicants), icp=icp_from_event(ev))
    if rubric.error:
        print(f"[rubric] WARNING: {rubric.error} (using fallback)", flush=True)
    summary = await evaluate_all(db, ev, rubric)
    print(f"[done] {summary}", flush=True)

    rows = (db.query(models.Applicant)
            .filter(models.Applicant.event_id == ev.id).all())

    # ---- Founders-first ranking -------------------------------------------
    # The operator wants to fill seats with founders first as we go DOWN the
    # list. So the ordering is tiered, not a raw fit sort:
    #   1. rejects sink to the bottom (never surface junk above a real invite)
    #   2. among the invitable, corroborated founders > founders > everyone else
    #   3. within a tier, by recommendation strength, then fit, then confidence
    # This makes the top-50 cutline naturally founder-heavy while still keeping
    # quality (a reject founder never outranks a strong accept).
    _REC_RANK = {"accept": 0, "maybe": 1, "needs_review": 2, "reject": 3}

    def _founder_tier(e) -> int:
        arch = (e.archetype or "").strip().lower()
        if arch != "founder":
            return 2
        try:
            adj = json.loads(e.verifier_adjustments or "[]")
        except Exception:
            adj = []
        corroborated = any("priority boost" in str(s) for s in adj)
        return 0 if corroborated else 1

    def _rank_key(a):
        e = a.evaluation
        if not e:
            return (9, 9, 9, 0, 0)
        is_reject = 1 if e.recommendation == "reject" else 0
        return (is_reject, _founder_tier(e), _REC_RANK.get(e.recommendation, 4),
                -e.fit_score, -e.confidence_score)

    rows.sort(key=_rank_key)

    CAPACITY = 50
    print()
    print(f"{'#':>3} {'fit':>4} {'conf':>4}  {'rec':<12} {'archetype':<10} "
          f"{'name':<24} {'company':<22} summary")
    print("-" * 140)
    dist: dict[str, int] = {}
    arch_dist_top: dict[str, int] = {}
    results = []
    rank = 0
    for a in rows:
        e = a.evaluation
        if not e:
            continue
        rank += 1
        dist[e.recommendation] = dist.get(e.recommendation, 0) + 1
        if rank <= CAPACITY:
            arch_dist_top[e.archetype] = arch_dist_top.get(e.archetype, 0) + 1
        try:
            adj = json.loads(e.verifier_adjustments or "[]")
        except Exception:
            adj = []
        results.append({
            "rank": rank, "name": a.name, "company": a.company,
            "email": a.email, "linkedin_url": a.linkedin_url,
            "fit": e.fit_score, "confidence": e.confidence_score,
            "recommendation": e.recommendation, "archetype": e.archetype,
            "summary": e.one_sentence_summary,
            "why_fit": e.why_fit, "why_not_fit": e.why_not_fit,
            "adjustments": adj,
        })
        if rank == CAPACITY + 1:
            print("=" * 60 + f"  TOP-{CAPACITY} CUTLINE  " + "=" * 60)
        marker = "*" if _founder_tier(e) == 0 else (
            "f" if _founder_tier(e) == 1 else " ")
        print(f"{rank:>3} {e.fit_score:>4} {e.confidence_score:>4}  "
              f"{e.recommendation:<12} {e.archetype:<10} "
              f"{marker}{(a.name or '')[:23]:<23} "
              f"{(a.company or '')[:21]:<22} {(e.one_sentence_summary or '')[:46]}")
    print("-" * 140)
    print("distribution:", dist)
    print(f"top-{CAPACITY} archetype mix:", arch_dist_top)

    out_path = "/tmp/bryankim_results.json"
    with open(out_path, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"[saved] {len(results)} ranked results → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
