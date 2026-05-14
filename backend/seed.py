"""
seed.py — run the whole mechanism end to end, no HTTP.

    python -m backend.seed

Resets the database, creates a sample event, runs all five stages in order,
and prints what each one produced. The fastest way to confirm the engine is
wired correctly — and a readable trace of the flow.
"""
from __future__ import annotations
import asyncio

from . import models, config
from .db import reset_db, SessionLocal
from .pipeline import run_pipeline
from .agents.matcher import build_edges, form_groups
from .agents.roi import settle

# headcount kept modest so the floating threshold actually floats against the
# 20-person mock pool — see README. Production swaps in real, deep-pool adapters.
SAMPLE_EVENT = dict(
    role="Infrastructure / ML platform engineers",
    seniority="Senior",
    co_stage="Seed",
    headcount=9,
    format="Hackathon",
    city="San Francisco",
    goal="Hiring pipeline",
    budget=9000,
)

LINE = "─" * 64


def _h(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


async def main() -> None:
    reset_db()
    db = SessionLocal()
    try:
        # ---- stage 01: intake -------------------------------------------
        _h("STAGE 01 · intake")
        ev = models.Event(**SAMPLE_EVENT)
        db.add(ev)
        db.commit()
        db.refresh(ev)
        target = round(ev.headcount / config.FUNNEL_CONVERSION)
        print(f"  event #{ev.id}: {ev.headcount}-person {ev.format} in {ev.city}")
        print(f"  goal: {ev.goal}  ·  budget: ${ev.budget:,}  ·  funnel target: {target}")

        # ---- stage 02-03: pipeline --------------------------------------
        _h("STAGE 02-03 · pipeline (fan-out, score, autonomous outreach)")
        prospects = await run_pipeline(db, ev)
        print(f"  surfaced {len(prospects)} candidates across source adapters")
        print(f"  floating threshold settled at fit {ev.threshold}")
        for p in sorted(prospects, key=lambda p: -p.fit_score):
            mark = "✓" if p.fit_score >= ev.threshold else "·"
            print(f"   {mark} {p.fit_score:>3}  {p.name:<20} {p.side:<9} "
                  f"{p.status:<10} [{p.sources}]")

        # ---- stage 04: matching -----------------------------------------
        _h("STAGE 04 · symbiotic matching")
        attending = [p for p in prospects if p.status == "rsvp"]
        if not attending:
            print("  no RSVPs this run — re-run to reseed the outreach funnel")
            return
        edges = build_edges(attending)
        groups = form_groups(attending, ev)
        sym = [e for e in edges if e["edge_type"] == "symbiotic"]
        aff = [e for e in edges if e["edge_type"] == "affinity"]
        print(f"  {len(attending)} confirmed  ·  {len(sym)} symbiotic + "
              f"{len(aff)} affinity edges")
        word = config.format_cfg(ev.format)["group_word"]
        for gid, members in sorted(groups.items()):
            who = ", ".join(f"{m.name.split()[0]}({m.side[0]})" for m in members)
            print(f"   {word} {gid}: {who}")

        # ---- stage 05: ROI ----------------------------------------------
        _h("STAGE 05 · verified ROI ledger")
        ledger, metrics = settle(ev, attending)
        for r in ledger:
            print(f"   {r['name']:<20} {r['label']:<14} "
                  f"${r['value']:>8,}  ({r['detail']})")
        print(f"\n  value generated: ${metrics['value_generated']:,}  "
              f"·  budget: ${metrics['budget']:,}")
        print(f"  net ROI: {metrics['net_roi_pct']}%  ·  "
              f"{metrics['converted']}/{metrics['attended']} converted to goal")
        print(f"\n  done — database at backend/data/surplus.db\n")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
