"""
Re-enrich + re-score ONLY the current top-50 invite list.

Reads the ranked results JSON from the full run, takes the top-N emails, then
re-runs the EXACT prod inbound path against just those applicants on a throwaway
SQLite DB. At 50 people (vs 530) the single LinkedIn account is far less likely
to throttle, so empty work-histories should repair and confidence firms up.

Usage:
    python score_top50.py <luma.csv> <icp.json> [results.json] [N]
"""
from __future__ import annotations
import asyncio, json, sys, csv
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.triage.csv_parser import parse_csv_file
from backend.triage.rubric import synthesize_rubric, icp_from_event
from backend.triage import score as _score
from backend.triage.score import evaluate_all
from backend.routes.triage import _clip

import os as _os
_score.SCORE_CONCURRENCY = int(_os.environ.get("SCORE_CONCURRENCY", "5"))


async def main() -> None:
    csv_path = sys.argv[1]
    icp_cfg = json.load(open(sys.argv[2]))
    results_path = sys.argv[3] if len(sys.argv) > 3 else "/tmp/bryankim_results.json"
    topn = int(sys.argv[4]) if len(sys.argv) > 4 else 50

    prev = json.load(open(results_path))
    top = prev[:topn]
    top_emails = {(r.get("email") or "").strip().lower() for r in top if r.get("email")}
    top_names = {(r.get("name") or "").strip().lower() for r in top}
    prev_by_email = {(r.get("email") or "").strip().lower(): r for r in top}
    print(f"[top50] targeting {len(top)} invite-list people "
          f"({len(top_emails)} by email)", flush=True)

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    user = models.User(name="Local", email="local@example.com", unipile_account_id=None)
    db.add(user); db.flush()
    tc = icp_cfg.get("triage_config", {})
    ev = models.Event(
        user_id=user.id, role=icp_cfg.get("role", ""), seniority=icp_cfg.get("seniority", ""),
        co_stage=icp_cfg.get("co_stage", ""), headcount=tc.get("capacity", 50),
        format=icp_cfg.get("format", ""), city=icp_cfg.get("city", ""),
        goal=icp_cfg.get("goal", ""), budget=0, sources="luma",
        triage_config=json.dumps(tc) if tc else None,
    )
    db.add(ev); db.commit()

    with open(csv_path, "rb") as f:
        rows = parse_csv_file(f)
    now = datetime.now(timezone.utc)
    kept = 0
    for row in rows:
        em = (_clip(row.get("email"), "email") or "").strip().lower()
        nm = (_clip(row.get("name"), "name") or "").strip().lower()
        if not ((em and em in top_emails) or (nm and nm in top_names)):
            continue
        kept += 1
        db.add(models.Applicant(
            event_id=ev.id, name=_clip(row.get("name"), "name") or "",
            email=_clip(row.get("email"), "email") or None,
            role=_clip(row.get("role"), "role") or None,
            company=_clip(row.get("company"), "company") or None,
            website=_clip(row.get("website"), "website") or None,
            linkedin_url=_clip(row.get("linkedin_url"), "linkedin_url") or None,
            raw_application_data=json.dumps(row.get("raw_application_data") or {}),
            created_at=now, updated_at=now,
        ))
    db.commit()
    print(f"[top50] matched {kept} applicants in CSV; re-enriching…", flush=True)

    rubric = synthesize_rubric(ev.id, ev.triage_config or "",
                               list(ev.applicants), icp=icp_from_event(ev))
    summary = await evaluate_all(db, ev, rubric)
    print(f"[done] {summary}", flush=True)

    apps = (db.query(models.Applicant).filter(models.Applicant.event_id == ev.id).all())

    _REC = {"accept": 0, "maybe": 1, "needs_review": 2, "reject": 3}
    def _tier(e):
        if (e.archetype or "").lower() != "founder":
            return 2
        try: adj = json.loads(e.verifier_adjustments or "[]")
        except Exception: adj = []
        return 0 if any("priority boost" in str(s) for s in adj) else 1
    def _key(a):
        e = a.evaluation
        if not e: return (9, 9, 9, 0, 0)
        return (1 if e.recommendation == "reject" else 0, _tier(e),
                _REC.get(e.recommendation, 4), -e.fit_score, -e.confidence_score)
    apps.sort(key=_key)

    # Report movement vs the prior full run
    print("\n=== TOP-50 RE-ENRICHED (was → now) ===")
    print(f"{'name':<24} {'company':<20} {'was':>10}  {'now':>10}  change")
    print("-" * 92)
    out_rows = []
    for a in apps:
        e = a.evaluation
        if not e: continue
        em = (a.email or "").strip().lower()
        p = prev_by_email.get(em, {})
        was = f"{p.get('fit','?')}/{p.get('confidence','?')} {p.get('recommendation','?')[:6]}"
        now_s = f"{e.fit_score}/{e.confidence_score} {e.recommendation[:6]}"
        chg = ""
        if p:
            df = e.fit_score - (p.get('fit') or 0)
            dc = e.confidence_score - (p.get('confidence') or 0)
            if p.get('recommendation') != e.recommendation:
                chg = f"{p.get('recommendation')}→{e.recommendation}"
            elif df or dc:
                chg = f"Δfit{df:+d} Δconf{dc:+d}"
        print(f"{(a.name or '')[:23]:<24} {(a.company or '')[:19]:<20} {was:>10}  {now_s:>10}  {chg}")
        try: adj = json.loads(e.verifier_adjustments or "[]")
        except Exception: adj = []
        out_rows.append({
            "name": a.name, "company": a.company, "email": a.email,
            "linkedin_url": a.linkedin_url, "fit": e.fit_score,
            "confidence": e.confidence_score, "recommendation": e.recommendation,
            "archetype": e.archetype, "summary": e.one_sentence_summary,
            "why_fit": e.why_fit, "why_not_fit": e.why_not_fit, "adjustments": adj,
        })

    json.dump(out_rows, open("/tmp/bryankim_top50_reenriched.json", "w"), indent=2)
    print(f"\n[saved] {len(out_rows)} → /tmp/bryankim_top50_reenriched.json")


if __name__ == "__main__":
    asyncio.run(main())
