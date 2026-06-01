"""Re-score a single applicant by name match and dump full reasoning."""
from __future__ import annotations
import asyncio, json, sys
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(override=True)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend import models
from backend.db import Base
from backend.triage.csv_parser import parse_csv_file
from backend.triage.rubric import synthesize_rubric, icp_from_event
from backend.triage.score import evaluate_all
from backend.routes.triage import _clip

CSV = "/Users/daniel04wang/Downloads/FiresidechatwBryanKimbyElevenLabsandVerciNYTechWeek_5-30_guests.csv"
NEEDLE = sys.argv[1].lower()


async def main():
    icp = json.load(open("icp_bryankim.json"))
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    u = models.User(name="L", email="l@x.com", unipile_account_id=None); db.add(u); db.flush()
    tc = icp.get("triage_config", {})
    ev = models.Event(user_id=u.id, role=icp.get("role",""), seniority=icp.get("seniority",""),
                      co_stage=icp.get("co_stage",""), headcount=tc.get("capacity",40),
                      format=icp.get("format",""), city=icp.get("city",""), goal=icp.get("goal",""),
                      budget=0, sources="luma", triage_config=json.dumps(tc))
    db.add(ev); db.commit()
    with open(CSV, "rb") as f:
        rows = parse_csv_file(f)
    now = datetime.now(timezone.utc)
    hit = next(r for r in rows if NEEDLE in (r.get("name") or "").lower())
    db.add(models.Applicant(event_id=ev.id, name=_clip(hit.get("name"),"name") or "",
        email=_clip(hit.get("email"),"email") or None, role=_clip(hit.get("role"),"role") or None,
        company=_clip(hit.get("company"),"company") or None, website=_clip(hit.get("website"),"website") or None,
        linkedin_url=_clip(hit.get("linkedin_url"),"linkedin_url") or None,
        raw_application_data=json.dumps(hit.get("raw_application_data") or {}), created_at=now, updated_at=now))
    db.commit()
    print("CSV row:", json.dumps({k: hit.get(k) for k in ("name","email","role","company","website","linkedin_url")}, indent=2))
    print("raw answers:", json.dumps(hit.get("raw_application_data") or {}, indent=2))
    rubric = synthesize_rubric(ev.id, ev.triage_config or "", list(ev.applicants), icp=icp_from_event(ev))
    await evaluate_all(db, ev, rubric)
    a = ev.applicants[0]; e = a.evaluation
    print("\n=== EVALUATION ===")
    for f in ("fit_score","confidence_score","recommendation","archetype","one_sentence_summary",
              "why_fit","why_not_fit","suggested_review_action","verifier_ran","verifier_reason","verifier_adjustments"):
        print(f"{f}: {getattr(e, f, None)}")
    print("dimension scores:", {k: getattr(e, k) for k in ("sponsor_fit","event_fit","role_relevance","company_relevance","stage_relevance","seriousness_legitimacy","room_value","application_quality")})
    print("evidence_used:", e.evidence_used)
    print("missing_info:", e.missing_info)
    if a.enrichment_data:
        pkt = json.loads(a.enrichment_data)
        print("\n=== EVIDENCE PACKET (trimmed) ===")
        print(json.dumps({k: pkt.get(k) for k in ("selected_company","warnings","contradictions","manual_review","review_reasons")}, indent=2)[:2500])

asyncio.run(main())
