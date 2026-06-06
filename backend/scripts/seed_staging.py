"""
scripts/seed_staging.py : populate the STAGING demo workspace with a realistic
applicant-triage queue so the review UI isn't blank on first visit.

    python -m backend.scripts.seed_staging          # create (idempotent)
    python -m backend.scripts.seed_staging --reset  # wipe + recreate the data

WHY THIS EXISTS
---------------
The staging service runs the `demo` branch against a throwaway Postgres, and
the demo entry point (routes/demo.py) drops every visitor into ONE shared
workspace keyed by the DEMO_SEED_EMAIL address. A throwaway DB starts empty, so
the triage Review queue shows "0 applicants". This script seeds that exact user
(DEMO_SEED_EMAIL) with a triage event + a spread of pre-scored applicants
(accept / maybe / needs_review / reject) so a demo immediately shows the product
doing its job.

SAFETY
------
Gated on DEMO_SEED_EMAIL being set AND pointing at the demo email domain. We
only set that env var on the staging service, so running this against prod is a
no-op refusal — it can never create rows under a real user. The seed user is a
demo-domain account (unipile_account_id NULL) so sends still paywall and nothing
real can fire from it.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from ..auth import DEMO_USER_EMAIL_DOMAIN
from ..db import SessionLocal, init_db
from .. import models


SEED_EVENT_NAME = "Stripe × ElevenLabs · Builders Dinner (DEMO)"


def _seed_email() -> str | None:
    em = (os.environ.get("DEMO_SEED_EMAIL") or "").strip().lower()
    if em and em.endswith(f"@{DEMO_USER_EMAIL_DOMAIN}"):
        return em
    return None


# Triage config that puts the event into inbound-triage mode and gives the
# review UI a sponsor/goal to render against. Shaped like a TriageConfig dump.
_TRIAGE_CONFIG = {
    "event_type": "Builders dinner",
    "sponsor_name": "Stripe × ElevenLabs",
    "event_goal": "Hiring pipeline for Staff+ infra / ML engineers at seed startups",
    "ideal_attendee_profile": (
        "Verifiable engineering or technical individual-contributor background; "
        "infra / ML platform focus; seed-stage operator energy."
    ),
    "hard_filters": [
        "Must have verifiable engineering or technical IC background",
    ],
    "nice_to_have_signals": [
        "Public technical signal (GitHub stars, talks, papers)",
        "Worked on infrastructure / ML platforms",
    ],
    "anti_fit_examples": [
        "Founder-led / fundraising-focused, no IC technical history",
        "Pure investor with no build background",
    ],
    "capacity": 30,
    "notes": "Seeded demo data — not a real event.",
}


# (name, role, company, linkedin_url, recommendation, archetype, fit, conf,
#  why_fit, why_not, dims) — dims = the 8 sub-scores in column order.
_APPLICANTS = [
    dict(
        name="Dana Okafor", email="dana@kernelml.dev", role="Founding Engineer",
        company="Kernel ML", linkedin_url="https://www.linkedin.com/in/dana-okafor",
        rec="accept", archetype="engineer", fit=86, conf=78,
        summary="Founding infra engineer with a public ML-systems track record; "
                "exactly the IC profile the event targets.",
        why="Verifiable engineering history (ex-Databricks infra, 4 yrs); GitHub "
            "with 1.2k stars on a feature-store project; current role is hands-on "
            "platform work at a seed startup — squarely on-profile.",
        why_not="Company is pre-Series-A so room value is slightly unproven, but "
                "the technical signal is strong and verifiable.",
        dims=dict(sponsor_fit=85, event_fit=88, role_relevance=90, company_relevance=70,
                  stage_relevance=85, seriousness_legitimacy=88, room_value=80,
                  application_quality=82),
    ),
    dict(
        name="Marcus Lindqvist", email="marcus@vectorhouse.io", role="Staff Engineer",
        company="VectorHouse", linkedin_url="https://www.linkedin.com/in/marcus-lindqvist",
        rec="accept", archetype="engineer", fit=82, conf=74,
        summary="Staff ML-platform engineer at a seed startup; clear IC seniority "
                "and on-topic infra work.",
        why="Staff-level IC at an early-stage ML infra company; LinkedIn confirms "
            "5+ years on distributed training systems. Direct fit for a Staff+ "
            "infra hiring pipeline.",
        why_not="No public open-source footprint to corroborate beyond the resume; "
                "confidence capped accordingly.",
        dims=dict(sponsor_fit=80, event_fit=84, role_relevance=86, company_relevance=72,
                  stage_relevance=80, seriousness_legitimacy=80, room_value=75,
                  application_quality=78),
    ),
    dict(
        name="Priya Nair", email="priya@latentlabs.ai", role="ML Engineer",
        company="Latent Labs", linkedin_url="https://www.linkedin.com/in/priya-nair-ml",
        rec="maybe", archetype="engineer", fit=64, conf=55,
        summary="Solid ML engineer, but more applied/product than infra-platform; "
                "partial fit for the Staff+ infra angle.",
        why="Real engineering background (3 yrs at Latent Labs on model serving); "
            "legitimate and on-theme for an ML builders dinner.",
        why_not="Work skews applied ML rather than the infra / platform IC profile "
                "the sponsor is hiring for; seniority reads mid, not Staff+.",
        dims=dict(sponsor_fit=60, event_fit=68, role_relevance=62, company_relevance=58,
                  stage_relevance=65, seriousness_legitimacy=70, room_value=55,
                  application_quality=66),
    ),
    dict(
        name="Theo Brandt", email="theo@stealth.example", role="Technical Lead",
        company="Stealth", linkedin_url="https://www.linkedin.com/in/theo-brandt",
        rec="needs_review", archetype="engineer", fit=58, conf=38,
        summary="Plausible IC technical lead, but the profile is thin and the "
                "company is unverifiable (stealth) — needs a human look.",
        why="Self-identifies as a hands-on technical lead and the headline suggests "
            "an infra focus.",
        why_not="Stealth company can't be verified; no public technical signal and "
                "no LinkedIn work history pulled — low confidence, flag for review "
                "rather than auto-deciding.",
        dims=dict(sponsor_fit=55, event_fit=60, role_relevance=60, company_relevance=40,
                  stage_relevance=55, seriousness_legitimacy=50, room_value=50,
                  application_quality=52),
    ),
    dict(
        name="Pratik Khandelwal", email="pratik@stealthfin.example", role="Co-Founder",
        company="Stealth FinTech Startup",
        linkedin_url="https://www.linkedin.com/in/pratik-khandelwal",
        rec="reject", archetype="founder", fit=17, conf=42,
        summary="Founder attending to build their own team / raise — explicitly "
                "misaligned with an IC-engineer hiring pipeline.",
        why="Self-identifies as 'Builds' on the application; seniority marked "
            "'Leadership' indicates operational maturity.",
        why_not="Primary role is Co-Founder, not an IC engineer — violates the hard "
                "gate requiring a verifiable engineering / technical IC background. "
                "Company is fintech (not infra/ML), stealth-stage (unverifiable), no "
                "public technical signal. A founder attending to build their own team "
                "or raise capital is explicitly misaligned.",
        dims=dict(sponsor_fit=20, event_fit=25, role_relevance=15, company_relevance=10,
                  stage_relevance=5, seriousness_legitimacy=35, room_value=15,
                  application_quality=30),
    ),
    dict(
        name="Amy Lin", email="amy@outcast.vc", role="Co-founder, Managing Partner",
        company="Outcast Ventures", linkedin_url="https://www.linkedin.com/in/amy-lin-vc",
        rec="reject", archetype="investor", fit=15, conf=35,
        summary="Investor with no IC build background — off-profile for a hiring "
                "pipeline targeting engineers.",
        why="Senior, legitimate professional with a real verifiable profile.",
        why_not="Pure investor / managing partner; no engineering or technical IC "
                "history. The hard filter requires a verifiable engineering "
                "background, which this profile does not meet.",
        dims=dict(sponsor_fit=18, event_fit=20, role_relevance=10, company_relevance=12,
                  stage_relevance=10, seriousness_legitimacy=40, room_value=20,
                  application_quality=28),
    ),
]


def _get_or_create_user(db, email: str) -> models.User:
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is not None:
        return user
    user = models.User(
        name="Surplus Demo",
        email=email,
        headline="Demo account : full workflow, LinkedIn sending disabled",
        unipile_account_id=None,
        linkedin_status="disconnected",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _get_event(db, user: models.User) -> models.Event | None:
    return (db.query(models.Event)
            .filter(models.Event.user_id == user.id,
                    models.Event.event_name == SEED_EVENT_NAME)
            .first())


def _create_event(db, user: models.User) -> models.Event:
    ev = models.Event(
        user_id=user.id,
        kind="planned",
        event_name=SEED_EVENT_NAME,
        city="San Francisco",
        role="Infrastructure / ML platform engineers",
        seniority="Staff+",
        goal="Hiring pipeline",
        format="Dinner",
        brief="Intimate builders dinner for Staff+ infra / ML engineers at "
              "seed-stage startups. Hosted by Stripe × ElevenLabs.",
        triage_config=json.dumps(_TRIAGE_CONFIG),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _create_applicants(db, ev: models.Event) -> int:
    created = 0
    for spec in _APPLICANTS:
        a = models.Applicant(
            event_id=ev.id,
            name=spec["name"],
            email=spec["email"],
            role=spec["role"],
            company=spec["company"],
            linkedin_url=spec["linkedin_url"],
            raw_application_data=json.dumps({
                "What are you building?": spec["summary"],
                "Why this event?": "Seeded demo applicant.",
            }),
        )
        db.add(a)
        db.flush()  # need a.id for the evaluation FK
        dims = spec["dims"]
        ev_row = models.ApplicantEvaluation(
            applicant_id=a.id,
            event_id=ev.id,
            fit_score=spec["fit"],
            confidence_score=spec["conf"],
            recommendation=spec["rec"],
            archetype=spec["archetype"],
            sponsor_fit=dims["sponsor_fit"],
            event_fit=dims["event_fit"],
            role_relevance=dims["role_relevance"],
            company_relevance=dims["company_relevance"],
            stage_relevance=dims["stage_relevance"],
            seriousness_legitimacy=dims["seriousness_legitimacy"],
            room_value=dims["room_value"],
            application_quality=dims["application_quality"],
            one_sentence_summary=spec["summary"],
            why_fit=spec["why"],
            why_not_fit=spec["why_not"],
            evidence_used=json.dumps(["linkedin_profile", "application_answers"]),
            missing_info=json.dumps([]),
            suggested_review_action="",
            model_version="seed-demo",
        )
        db.add(ev_row)
        created += 1
    db.commit()
    return created


def main() -> int:
    reset = "--reset" in sys.argv[1:]

    seed = _seed_email()
    if not seed:
        print("[seed_staging] REFUSING: DEMO_SEED_EMAIL is not set to a "
              f"@{DEMO_USER_EMAIL_DOMAIN} address.\n"
              "  This guard prevents seeding against prod. Set DEMO_SEED_EMAIL on "
              "the staging service (e.g. seed@demo.surpluslayer.com) and re-run.")
        return 1

    init_db()
    db = SessionLocal()
    try:
        user = _get_or_create_user(db, seed)
        ev = _get_event(db, user)

        if ev is not None and reset:
            db.delete(ev)   # cascade clears applicants + evaluations
            db.commit()
            ev = None
            print(f"[seed_staging] --reset: cleared existing event for {seed}")

        if ev is not None and ev.applicants:
            print(f"[seed_staging] already seeded: user={seed} event #{ev.id} "
                  f"({len(ev.applicants)} applicants). Use --reset to rebuild.")
            return 0

        if ev is None:
            ev = _create_event(db, user)

        n = _create_applicants(db, ev)
        print(f"[seed_staging] OK: user={seed} (id={user.id}) "
              f"event #{ev.id} '{SEED_EVENT_NAME}' seeded with {n} applicants.")
        print("  Enter the demo at:  /api/demo/enter?key=<DEMO_ACCESS_TOKEN>")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
