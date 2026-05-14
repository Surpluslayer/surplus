"""End-to-end pipeline orchestrator.

Wires together: ingest → rubric synthesis → enrich (parallel) → matrix → explain.

Used by both CLI (python -m packages.run) and the FastAPI app.
Streams progress events via an async callback so the UI can show live updates.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend.matching.ingest import load_csv, summarize as summarize_people
from backend.matching.schema import EnrichedPerson, Person
from backend.matching.rubric import synthesize_rubric
from backend.matching.enrich import enrich_batch
from backend.matching.matrix import compute_matrix, save_matrix
from backend.matching.explain import explain_matches


ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
# (event_type, payload)
# event_types emitted:
#   "ingest_done"         {n_people, n_with_handles, summary}
#   "rubric_started"      {}
#   "rubric_done"         {event_type, match_intent, rubric}
#   "enrich_start"        {person_id, name}
#   "enrich_ok"           {person_id, name, status, domains, tech_stack}
#   "enrich_error"        {person_id, name, error}
#   "enrich_done"         {n_enriched, ok_count, partial_count, failed_count}
#   "score_done"          {n_pairs, n_mutual, top_score}
#   "explain_start"       {n_pairs_to_explain}
#   "explain_progress"    {done, total}
#   "explain_done"        {n_explained}
#   "pipeline_done"       {event_id, matrix_path}


async def run_pipeline(
    csv_path: str | Path,
    event_name: str,
    event_description: str,
    *,
    top_k: int = 5,
    enrich_limit: Optional[int] = None,
    enrich_concurrency: int = 100,
    explain_concurrency: int = 30,
    explain_mode: str = "lazy",  # "lazy" = skip in pipeline (gen on click); "upfront" = generate all
    enrich_model: Optional[str] = None,  # override ENRICH_MODEL env var per run
    on_progress: Optional[ProgressCallback] = None,
    use_cache: bool = True,
) -> tuple[dict[str, Any], dict[str, EnrichedPerson], dict[str, Any]]:
    """Run the full pipeline and return the final matrix dict.

    Args:
      csv_path: path to event CSV (any format with name + handles)
      event_name, event_description: free-text used by rubric synthesis
      top_k: how many matches per person to surface and explain
      enrich_limit: if set, only enrich the first N people (for cost control)
      enrich_concurrency: parallel Claude+web_search calls
      explain_concurrency: parallel Haiku rationale calls
      on_progress: async callback for streaming UI updates
    """
    t_start = time.time()

    # Per-run model override — works because packages.enrich reads MODEL at
    # call time from the module-level constant via os.environ.get default.
    if enrich_model:
        os.environ["ENRICH_MODEL"] = enrich_model
        # Also refresh the module-level constant
        import backend.matching.enrich as _enrich
        _enrich.MODEL = enrich_model

    async def emit(event: str, payload: dict[str, Any]) -> None:
        if on_progress:
            try:
                await on_progress(event, payload)
            except Exception:
                pass

    # ---- 1. Ingest ----
    people = load_csv(csv_path)
    if enrich_limit:
        people = people[:enrich_limit]
    summary = summarize_people(people)
    await emit("ingest_done", {
        "n_people": len(people),
        "summary": summary,
    })

    # ---- 2. Rubric synthesis (kicked off in parallel with enrich start) ----
    # Emit rubric_done the moment the task resolves rather than after enrich
    # finishes — otherwise the UI shows "synthesizing…" for the full enrich
    # duration even though the rubric finished ~20 sec earlier.
    await emit("rubric_started", {})

    async def _rubric_with_emit() -> dict[str, Any]:
        rb = await synthesize_rubric(
            event_name=event_name,
            event_description=event_description,
            people=people,
            use_cache=use_cache,
        )
        await emit("rubric_done", {
            "event_type": rb.get("event_type"),
            "match_intent": rb.get("match_intent"),
            "axis_blend": rb.get("weights", {}).get("axis_blend"),
            "notes_for_humans": rb.get("notes_for_humans"),
            "rubric": rb,
        })
        return rb

    rubric_task = asyncio.create_task(_rubric_with_emit())

    # ---- 3. Enrich people (parallel, slow) ----
    async def enrich_progress(event_type: str, ep: EnrichedPerson, meta: dict[str, Any]) -> None:
        if event_type == "start":
            await emit("enrich_start", {"person_id": ep.id, "name": ep.name})
        elif event_type == "ok":
            await emit("enrich_ok", {
                "person_id": ep.id,
                "name": ep.name,
                "status": ep.enrichment_status,
                "domains": ep.domains[:5],
                "tech_stack": ep.tech_stack[:8],
                "bio_text": (ep.bio_text or "")[:200],
            })
        elif event_type == "error":
            await emit("enrich_error", {
                "person_id": ep.id,
                "name": ep.name,
                "error": meta.get("error", ""),
            })

    enriched = await enrich_batch(
        people,
        concurrency=enrich_concurrency,
        use_cache=use_cache,
        on_progress=enrich_progress,
    )
    ok_n = sum(1 for e in enriched if e.enrichment_status == "ok")
    partial_n = sum(1 for e in enriched if e.enrichment_status == "partial")
    failed_n = sum(1 for e in enriched if e.enrichment_status == "failed")
    await emit("enrich_done", {
        "n_enriched": len(enriched),
        "ok_count": ok_n,
        "partial_count": partial_n,
        "failed_count": failed_n,
    })

    # ---- 4. Await rubric (rubric_done was emitted from inside the task) ----
    rubric = await rubric_task

    # ---- 5. Build matrix ----
    matrix = compute_matrix(enriched, rubric, top_k=top_k)
    top_score = (matrix.get("mutual_pairs") or [{}])[0].get("composite", 0)
    await emit("score_done", {
        "n_pairs": matrix["stats"]["n_pairs_passed"],
        "n_mutual": matrix["stats"]["n_mutual_pairs"],
        "top_score": top_score,
    })

    by_id = {e.id: e for e in enriched}

    # ---- 6. Generate rationales (or skip if lazy) ----
    if explain_mode == "upfront":
        needed_pairs = sum(1 for matches in matrix["top_k_per_person"].values() for _ in matches[:top_k])
        await emit("explain_start", {"n_pairs_to_explain": needed_pairs // 2})
        explained_count = [0]
        async def explain_progress(event_type: str, key: str, meta: dict[str, Any]) -> None:
            if event_type in ("ok", "cache_hit"):
                explained_count[0] += 1
                await emit("explain_progress", {"done": explained_count[0], "total": needed_pairs // 2})

        await explain_matches(
            matrix, by_id, rubric,
            top_k=top_k,
            concurrency=explain_concurrency,
            use_cache=use_cache,
            on_progress=explain_progress,
        )
        await emit("explain_done", {"n_explained": explained_count[0]})
    else:
        # Lazy: rationales generated on-demand by /api/events/.../explain/person/{pid}
        await emit("explain_done", {"n_explained": 0, "mode": "lazy"})

    # ---- 7. Persist ----
    save_matrix(matrix)
    matrix["total_elapsed_s"] = round(time.time() - t_start, 2)
    matrix["generated_at"] = datetime.now(timezone.utc).isoformat()

    # pipeline_done is emitted by the API layer after state.matrix is committed,
    # so the client's matrix fetch can't race a 202 "still running" response.
    # CLI callers won't see pipeline_done — they use the returned tuple instead.

    # Return matrix + by_id (for lazy explain) + rubric (also for lazy explain)
    return matrix, by_id, rubric


# ---- CLI entry point ----

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run event-match pipeline on a CSV.")
    parser.add_argument("csv_path", help="Path to the event guest CSV")
    parser.add_argument("--name", required=True, help="Event name")
    parser.add_argument("--desc", required=True, help="Event description")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None, help="Cap enrichment count for cost control")
    parser.add_argument("--enrich-concurrency", type=int, default=20)
    args = parser.parse_args(argv)

    async def progress(event: str, payload: dict[str, Any]) -> None:
        if event == "ingest_done":
            print(f"[ingest] {payload['n_people']} people")
        elif event == "rubric_started":
            print("[rubric] synthesizing...")
        elif event == "rubric_done":
            print(f"[rubric] {payload['event_type']} / {payload['match_intent']}")
        elif event == "enrich_ok":
            print(f"[enrich] OK   {payload['name']:30s} status={payload['status']} domains={payload['domains'][:3]}")
        elif event == "enrich_error":
            print(f"[enrich] FAIL {payload['name']}: {payload['error'][:80]}")
        elif event == "enrich_done":
            print(f"[enrich] done: ok={payload['ok_count']} partial={payload['partial_count']} failed={payload['failed_count']}")
        elif event == "score_done":
            print(f"[score]  {payload['n_pairs']} pairs, {payload['n_mutual']} mutual, top={payload['top_score']:.3f}")
        elif event == "explain_progress":
            if payload["done"] % 10 == 0 or payload["done"] == payload["total"]:
                print(f"[explain] {payload['done']}/{payload['total']}")
        elif event == "pipeline_done":
            print(f"[done] event_id={payload['event_id']} in {payload['total_elapsed_s']}s")

    matrix, _, _ = asyncio.run(run_pipeline(
        args.csv_path,
        event_name=args.name,
        event_description=args.desc,
        top_k=args.top_k,
        enrich_limit=args.limit,
        enrich_concurrency=args.enrich_concurrency,
        explain_mode="upfront",  # CLI generates all rationales by default
        on_progress=progress,
    ))
    print(f"\nSaved to data/matches/{matrix['event_id']}/matrix.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
