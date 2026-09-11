"""
routes/civic.py : Civic policy search.

    POST /api/civic/ask          question (+ place or dropped pin) -> Answer
    GET  /api/civic/answer/{id}  the cached answer behind a permalink
    GET  /civic                  the map + ladder page
    GET  /civic/r/{id}           the same page, opened on a shared answer

No DB, no auth, no accounts : an answer is a pure function of the question and
the place, cached for 24h by sha256(question|location). The Anthropic key
lives only here, server-side -- the browser never sees it, which is the whole
reason this route exists instead of the prototype's direct fetch to the API.

Civic is a bolt-on, not part of the product, so it is fenced off from the rest
of surplus rather than trusted to behave:

  * It shares the process with the CRM. A synthesis call blocks a threadpool
    thread for 15-25s, and the pool (~40 threads, WEB_CONCURRENCY=1 by
    default) is the same one every sync CRM route runs on. `_SLOTS` caps how
    many of those threads Civic may ever hold ; past the cap it sheds load
    with a 503 instead of queueing behind them.
  * It shares ANTHROPIC_API_KEY and EXA_API_KEY with drafting and prospecting,
    so a busy day here is spend and rate-limit pressure there. `_DAILY_CAP`
    bounds the number of answers it will synthesize per day.
  * CIVIC_ENABLED=0 turns the whole surface off -- page included -- without a
    deploy, if it ever does become someone else's problem.
  * It touches no database, no session, no user. The only thing it imports
    from the app is the per-IP rate limiter, and tests pin that.

The synthesis itself (prompt, evidence ladder, URL grounding) lives in
backend/civic.py; this module is the HTTP skin and the fence around it.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import civic, civic_geo, civic_sources
from ..rate_limit import per_ip_rate_limit

router = APIRouter(prefix="/api/civic", tags=["civic"])

# The page lives outside /api so it can be linked and shared as a plain URL.
pages_router = APIRouter(include_in_schema=False)

_UI_DIR = Path(__file__).resolve().parent.parent / "civic_ui"
_UI_HTML = _UI_DIR / "index.html"

# 10/min/IP: a query costs real money in search and tokens, so this is a cost
# gate as much as an abuse gate. Its own tag, so it doesn't share a window
# with signup or checkout.
_ASK_LIMIT = per_ip_rate_limit(limit=10, window_s=60, tag="civic_ask")


def _redact(detail: dict, admin: bool) -> dict:
    """A failure's shape for anyone, its text for an operator.

    Type, status and time are what tell you a deploy is broken. The upstream
    message can carry a URL, a quota, an account hint -- detail that belongs
    to whoever runs this, not to whoever found the URL.
    """
    if not detail:
        return {}
    if admin:
        return dict(detail)
    return {k: detail[k] for k in ("at", "type", "status", "mode", "boot")
            if k in detail}


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


def enabled() -> bool:
    """CIVIC_ENABLED=0 (or false/off/no) takes the surface down, page included.

    Read per request on purpose: turning Civic off is an env change and a
    restart, not a redeploy, and it must not need one at all if the process
    can be signalled another way later.
    """
    raw = (os.environ.get("CIVIC_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


# At most this many synthesis calls in flight, ever. Two of ~40 shared threads
# is enough to serve a small launch city and small enough that the CRM cannot
# notice. Everything past it is shed immediately -- a queued request would hold
# a thread while it waited, which is the exact harm this prevents.
_MAX_CONCURRENCY = _int_env("CIVIC_MAX_CONCURRENCY", 2)
_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENCY or 1)

# Uncached answers per process per UTC day. The keys are shared with the rest
# of the app, so this is the ceiling on what Civic can spend of them.
_DAILY_CAP = _int_env("CIVIC_DAILY_ANSWERS", 250)
_day_lock = threading.Lock()
_day = ""
_day_count = 0


def _take_daily_slot() -> bool:
    """Count one answer against today's budget. False when the day is spent."""
    global _day, _day_count
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _day_lock:
        if today != _day:
            _day, _day_count = today, 0
        if _DAILY_CAP and _day_count >= _DAILY_CAP:
            return False
        _day_count += 1
        return True


def _reset_daily_budget() -> None:
    """Test seam. Nothing in the request path calls this."""
    global _day, _day_count
    with _day_lock:
        _day, _day_count = "", 0


class AskIn(BaseModel):
    question: str = Field(..., max_length=500)
    location: str = Field("", max_length=160)
    # Set when the answer came from a dropped pin rather than a typed city.
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    # Set when the question came from dropping a pin rather than typing : the
    # answer is then a briefing about the place, never "ask something narrower".
    brief: bool = False


def _reader_notes(notes: dict) -> dict:
    """The part of the synthesis notes a reader is owed.

    Everything else in there is a counter for the log. This is the one thing
    that changes how an answer should be read: whether we could search
    properly at all.
    """
    degraded = (notes or {}).get("search_degraded") or ""
    return {"search_degraded": degraded} if degraded else {}


class AskOut(BaseModel):
    # `id` is a share token: the question, packed. A permalink that carries
    # its own question can be answered by either replica and survives a
    # deploy, which a cache id could not.
    id: str
    question: str
    location: str
    answer: dict | None = None
    cached: bool
    # Only ever carries `search_degraded`: why the ranked search did not run.
    notes: dict = {}
    # True when the link is good but this replica has no answer for it yet :
    # the page re-asks rather than showing "expired".
    rebuild: bool = False


@router.post("/ask", response_model=AskOut, dependencies=[Depends(_ASK_LIMIT)])
def ask(payload: AskIn) -> AskOut:
    """One question in, one evidence-ranked answer out."""
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})

    question = (payload.question or "").strip()
    location = (payload.location or "").strip()
    if not question:
        raise HTTPException(400, {"code": "empty_question",
                                  "message": "Ask a question first."})

    # A cache hit costs nothing and holds no slot : serve it before the fence.
    key = civic.cache_key(question, location)
    token = civic.share_token(question, location, payload.brief)
    hit = civic.cache_get(key)
    if hit:
        print(f"  [civic] cache hit {key} tiers={''.join(hit['answer']['tiers']) or '-'}")
        return AskOut(id=token, cached=True, **hit)

    if not civic.available():
        # Deliberately explicit: the failure is configuration, not the question.
        # ANTHROPIC_API_KEY is the same key the rest of the app uses; EXA_API_KEY
        # is optional and only changes how the sources are found.
        raise HTTPException(503, {
            "code": "unconfigured",
            "message": ("Search isn't set up on this deployment: the server has "
                        "no ANTHROPIC_API_KEY."),
        })

    # Non-blocking : a request that waits for a slot is a held thread, which is
    # the harm this cap exists to prevent. Shed it instead and let the caller
    # retry. Taken before the daily budget so a shed request costs no quota.
    if not _SLOTS.acquire(blocking=False):
        print("  [civic] busy : all synthesis slots in use")
        raise HTTPException(503, {
            "code": "busy",
            "message": "Civic is answering as many questions as it can at once. Try again in a moment.",
        }, headers={"Retry-After": "20"})

    if not _take_daily_slot():
        # The shared keys are the reason this cap exists : better a bounded
        # surface than a surprise bill and a rate-limited CRM.
        _SLOTS.release()
        print(f"  [civic] daily cap reached ({_DAILY_CAP} answers)")
        raise HTTPException(429, {
            "code": "daily_cap",
            "message": ("Civic has answered as many questions as it can today. "
                        "Try again tomorrow."),
        }, headers={"Retry-After": "3600"})

    try:
        answer, notes = civic.synthesize(
            question, location, lat=payload.lat, lon=payload.lon,
            brief=payload.brief,
        )
    except civic.CivicError as exc:
        # Log the shape of the failure; the question and place are already in
        # the request log, and we keep nothing else about the caller.
        print(f"  [civic] failed code={exc.code} q={question[:80]!r} loc={location!r}")
        raise HTTPException(422, {
            "code": exc.code,
            "message": str(exc),
            # What the model did say, so the page can offer "show what we found"
            # instead of a dead end.
            "raw": exc.raw[:4000],
        }) from exc
    finally:
        _SLOTS.release()

    print(
        f"  [civic] answered q={question[:80]!r} loc={location!r} "
        f"via={notes.get('sources')} found={notes.get('sources_found')} "
        f"tiers={''.join(answer['tiers']) or '-'} "
        f"evidence={len(answer['evidence'])} "
        f"dropped={notes['evidence_dropped']}/{notes['actions_dropped']} "
        f"retiered={notes.get('tiers_corrected')} "
        f"retried={notes.get('retried')} {notes['latency_ms']}ms"
    )

    record = {"question": question, "location": location, "answer": answer}
    civic.cache_put(key, record)
    return AskOut(id=token, cached=False, **record, notes=_reader_notes(notes))


# --------------------------------------------------------------------------
# The same answer, streamed
# --------------------------------------------------------------------------
#
# A question takes 15-40s : several searches and then a JSON answer written a
# token at a time, with nothing to show until it closes. On the plain POST that
# is a blank wait, and the page's checklist can only guess at what is
# happening. Here the work reports itself -- each search, the moment writing
# starts, the headline as soon as it exists (it is the first key in the JSON),
# then the whole answer. Same fence, same cache, same synthesis : only the
# waiting is different.

_HEARTBEAT_S = 10.0


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_response(gen):
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        # Cloudflare and nginx both buffer event streams without this, which
        # would defeat the entire point.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@router.post("/ask/stream", dependencies=[Depends(_ASK_LIMIT)])
def ask_stream(payload: AskIn):
    """Ask, and watch it happen. Server-sent events, one JSON object each."""
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    question = (payload.question or "").strip()
    location = (payload.location or "").strip()
    if not question:
        raise HTTPException(400, {"code": "empty_question",
                                  "message": "Ask a question first."})

    key = civic.cache_key(question, location)
    token = civic.share_token(question, location, payload.brief)
    hit = civic.cache_get(key)
    if hit:
        print(f"  [civic] cache hit {key} tiers={''.join(hit['answer']['tiers']) or '-'}")

        def cached():
            yield _sse("answer", {"id": token, "cached": True, **hit})
        return _stream_response(cached())

    if not civic.available():
        raise HTTPException(503, {
            "code": "unconfigured",
            "message": ("Search isn't set up on this deployment: the server has "
                        "no ANTHROPIC_API_KEY."),
        })
    if not _SLOTS.acquire(blocking=False):
        print("  [civic] busy : all synthesis slots in use")
        raise HTTPException(503, {
            "code": "busy",
            "message": "Civic is answering as many questions as it can at once. Try again in a moment.",
        }, headers={"Retry-After": "20"})
    if not _take_daily_slot():
        _SLOTS.release()
        print(f"  [civic] daily cap reached ({_DAILY_CAP} answers)")
        raise HTTPException(429, {
            "code": "daily_cap",
            "message": ("Civic has answered as many questions as it can today. "
                        "Try again tomorrow."),
        }, headers={"Retry-After": "3600"})

    events: "queue.Queue" = queue.Queue()

    def work():
        """Synthesize on a worker thread, posting progress into the queue.

        It runs to completion even if the reader hangs up : the answer is paid
        for either way, and finishing means the next person asking the same
        question gets it from cache.
        """
        # The sentinel goes last, always : the outer finally is what closes
        # the stream, and anything queued after it would never be read.
        try:
            try:
                answer, notes = civic.synthesize(
                    question, location, lat=payload.lat, lon=payload.lon,
                    brief=payload.brief,
                    on_event=lambda name, **fields: events.put((name, fields)),
                )
            except civic.CivicError as exc:
                print(f"  [civic] failed code={exc.code} q={question[:80]!r} loc={location!r}")
                events.put(("error", {"code": exc.code, "message": str(exc),
                                      "raw": exc.raw[:4000]}))
                return
            except Exception as exc:  # noqa: BLE001 : a stream must always end
                print(f"  [civic] stream crashed: {type(exc).__name__}: {exc}")
                events.put(("error", {"code": "synthesis_failed",
                                      "message": "The search could not be completed."}))
                return
            finally:
                # Free the slot the moment the model call is done, not when the
                # reader finishes reading.
                _SLOTS.release()

            record = {"question": question, "location": location, "answer": answer}
            civic.cache_put(key, record)
            print(
                f"  [civic] answered q={question[:80]!r} loc={location!r} "
                f"via={notes.get('sources')} found={notes.get('sources_found')} "
                f"tiers={''.join(answer['tiers']) or '-'} "
                f"evidence={len(answer['evidence'])} "
                f"dropped={notes['evidence_dropped']}/{notes['actions_dropped']} "
                f"retiered={notes.get('tiers_corrected')} "
                f"retried={notes.get('retried')} {notes['latency_ms']}ms"
            )
            events.put(("answer", {"id": token, "cached": False, **record,
                                   "notes": _reader_notes(notes)}))
        finally:
            events.put((None, None))

    threading.Thread(target=work, daemon=True, name="civic-ask").start()

    def drain():
        last = time.monotonic()
        while True:
            try:
                name, fields = events.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last > _HEARTBEAT_S:
                    last = time.monotonic()
                    yield ": still working\n\n"   # keeps proxies from closing us
                continue
            if name is None:
                # The worker signalled it is done ; anything it queued before
                # this (the answer, an error) has already been yielded.
                break
            last = time.monotonic()
            yield _sse(name, fields)

    return _stream_response(drain())


@router.get("/answer/{answer_id}", response_model=AskOut)
def get_answer(answer_id: str) -> AskOut:
    """The answer behind a permalink.

    Free either way: a hit is served from this replica's cache, and a miss
    hands the question back so the page can re-ask it through the normal
    path -- with the fence, the rate limit and the progress stream. The link
    only truly fails when it is not one of ours.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})

    answer_id = answer_id.strip()
    shared = civic.read_token(answer_id)
    if shared:
        hit = civic.cache_get(civic.cache_key(shared["question"], shared["location"]))
        if hit:
            return AskOut(id=answer_id, cached=True, **hit)
        # The other replica answered this, or a deploy cleared the cache.
        # The question is right here, so nothing is lost but the wait.
        return AskOut(id=answer_id, cached=False, rebuild=True,
                      question=shared["question"], location=shared["location"])

    # Links made before permalinks carried their question : cache-only.
    hit = civic.cache_get(answer_id.lower())
    if not hit:
        raise HTTPException(404, {
            "code": "expired",
            "message": "That link is from an older version and its answer has "
                       "expired. Ask the question again.",
        })
    return AskOut(id=answer_id, cached=True, **hit)


# 30/min/IP: this costs two HTTP calls and no tokens, so it is bounded for
# politeness to the free APIs rather than for money.
_PLACE_LIMIT = per_ip_rate_limit(limit=30, window_s=60, tag="civic_place")


class PlaceOut(BaseModel):
    subject: str
    items: list[dict]
    ran: dict


@router.get("/place", response_model=PlaceOut, dependencies=[Depends(_PLACE_LIMIT)])
def place(subject: str, near: str = "") -> PlaceOut:
    """What has been written lately about one thing on the map.

    The fast lane. Tapping a school should say something about that school in
    about a second, so this asks the two news indexes directly and returns
    what they have -- no model, no synthesis, no evidence ladder, no cost. The
    side panel is where a question gets the full treatment.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    subject = (subject or "").strip()
    if not subject:
        raise HTTPException(400, {"code": "empty_subject",
                                  "message": "Name the place first."})
    found = civic_sources.headlines(subject, (near or "").strip())
    print(f"  [civic] place {subject[:60]!r} near={near[:40]!r} "
          f"items={len(found['items'])} ran={found['ran']}")
    return PlaceOut(subject=subject, items=found["items"], ran=found["ran"])


# 60/min/IP : two HTTP calls, no tokens, and the map asks on every click.
_GEO_LIMIT = per_ip_rate_limit(limit=60, window_s=60, tag="civic_geo")


@router.get("/jurisdictions", dependencies=[Depends(_GEO_LIMIT)])
def jurisdictions(lat: float, lon: float) -> dict:
    """Every government standing over one point, and what each one decides.

    The map's lens. Picking a layer paints the boundary you are inside and
    asks that layer's question in that layer's vocabulary -- which is the
    difference between "what is happening in Oakland" and "what does my
    school board decide, and when do I vote for it".
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, {"code": "bad_point",
                                  "message": "That is not a point on Earth."})
    found = civic_geo.stack(lat, lon)
    print(f"  [civic] jurisdictions {lat:.4f},{lon:.4f} "
          f"layers={[l['key'] for l in found['layers']]} sources={found['sources']}")
    return found


@router.get("/officials", dependencies=[Depends(_GEO_LIMIT)])
def officials(lat: float, lon: float, congress: str = "",
              upper: str = "", lower: str = "") -> dict:
    """Who currently holds the seats this point votes for.

    The static list of what a chamber decides is the same in every district
    in the country. Who sits in it, and since when, is the part that is
    actually about you -- so the card leads with this.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, {"code": "bad_point",
                                  "message": "That is not a point on Earth."})
    try:
        return civic_geo.officials(lat, lon, congress, upper, lower)
    except Exception as exc:  # noqa: BLE001 : an unnamed seat is not an error
        print(f"  [civic] officials failed: {type(exc).__name__}")
        return {"by_layer": {}, "sources": {"error": type(exc).__name__}}


@router.get("/activity", dependencies=[Depends(_GEO_LIMIT)])
def activity(layer: str = "", person: int = 0, place: str = "") -> dict:
    """What the body behind this lens has actually done lately.

    A roll call and a council agenda are facts with dates and links on them.
    They are the part of this card that answers "what is this district doing"
    without anyone having to characterise it -- no search, no model, no cost
    beyond one call to a public record.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    return civic_geo.activity(layer[:40], person, place[:120])


# How a drawn boundary was arrived at, worst case first. The map says which
# one it is, because a shape the reader cannot tell apart from a surveyed one
# is a shape they will trust more than it deserves.
_BASIS_NOTE = {
    "census": "Drawn by the US Census from the same district record that named it.",
    "relation": "The mapped boundary of the district this point falls inside.",
    "name": "Matched by name, not by your location — it may be a different "
            "body that shares this name.",
    "partial": "Only part of this boundary was available, so it is drawn as "
               "the edges we have rather than filled as a whole district.",
}


@router.get("/outline", dependencies=[Depends(_GEO_LIMIT)])
def outline(relation: int = 0, name: str = "", layer: str = "",
            geoid: str = "", detail: str = "") -> dict:
    """One boundary's geometry, so the map can draw the district you are in.

    Three ways in, best first. The Census names five of the eight lenses and
    hands back no shape, so its own published boundary is asked for by the
    same GEOID it returned ; then the OpenStreetMap relation the point is
    actually inside ; then, last, a boundary carrying the same name.

    The answer says which of those it was. A name match is a guess about
    identity even when the geometry is exact, and a half-received relation is
    a fragment -- both are drawn, and both are labelled as estimates rather
    than passed off as the district itself.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    shape, basis, tried = {}, "", []

    def attempt(label: str, produce):
        nonlocal shape, basis
        if shape:
            return
        try:
            found = produce()
        except Exception as exc:  # noqa: BLE001 : a missing outline is not an error
            tried.append(f"{label}:{type(exc).__name__}")
            return
        if found:
            shape, basis = found, label
        else:
            tried.append(f"{label}:empty")

    if layer and geoid:
        attempt("census", lambda: civic_geo.outline_by_geoid(layer, geoid, detail))
    if relation:
        attempt("relation", lambda: civic_geo.outline(relation))
    if name:
        attempt("name", lambda: civic_geo.outline_by_name(name))

    if tried:
        print(f"  [civic] outline {layer or relation or name!r}: {' '.join(tried)}")
    # Rings that would not close come back as lines. That is a fragment
    # whatever route found it, and it outranks how it was found.
    if shape and "line" in str(shape.get("type", "")).lower():
        basis = "partial"
    return {"relation": relation, "name": name, "geometry": shape,
            "basis": basis,
            "exact": basis in ("census", "relation"),
            "note": _BASIS_NOTE.get(basis, "")}


# 30/min/IP. The endpoint is cheap, but it is unauthenticated and it exists to
# be hit when something is wrong, which is exactly when it gets hammered.
_SELFTEST_LIMIT = per_ip_rate_limit(limit=30, window_s=60, tag="civic_selftest")


def _is_admin(x_admin_token: str = Header(default="", alias="X-Admin-Token")) -> bool:
    """The same ADMIN_TOKEN the rest of the app uses, checked locally.

    Deliberately not routes/admin._require_admin_token: that one takes a DB
    session, and Civic's whole isolation story is that it never touches the
    database. Same secret, constant-time compare, no audit row -- and no
    coupling that could break the CRM.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    given = (x_admin_token or "").strip()
    return bool(expected) and hmac.compare_digest(expected, given)


@router.get("/selftest", dependencies=[Depends(_SELFTEST_LIMIT)])
def selftest(probe: int = 0, admin: bool = Depends(_is_admin)) -> dict:
    """Why is every question failing? Read this instead of the Railway logs.

    Two tiers, because the useful half is also the disclosing half. Anyone can
    read how the surface is configured and the SHAPE of the last failure --
    "NotFoundError, 404" is what diagnoses a broken deploy. The upstream error
    text, and the probe that makes ten outbound calls, need X-Admin-Token:
    unauthenticated error strings are an information leak and an
    unauthenticated fan-out is a cost-DoS, which is the same pair this repo
    already fixed once for /api/diagnostics (security review H-2).
    """
    report = {
        # Production runs two replicas behind one URL and every counter here
        # is one process's. Refresh until you have seen both boot ids.
        "boot": civic.BOOT_ID,
        "booted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(civic.BOOTED_AT)),
        "uptime_s": int(time.time() - civic.BOOTED_AT),
        "enabled": enabled(),
        "anthropic_key": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        "anthropic_sdk": civic.available() or bool(
            (os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        # `exa_key` was true while every Exa call 402'd, which read as "the
        # fast path is on" when it was not. Report what Exa last did.
        "exa_key": bool((os.environ.get("EXA_API_KEY") or "").strip()),
        "exa_status": civic_sources.exa_status(),
        "sources": "exa + keyless" if civic_sources.available() else "keyless",
        "model_configured": civic.MODEL,
        "model_in_use": civic.active_model(),
        "max_tokens": civic.MAX_TOKENS,
        "effort": civic.EFFORT or None,
        "answers_today": _day_count,
        "daily_cap": _DAILY_CAP,
        "max_concurrency": _MAX_CONCURRENCY,
        "cached_answers": civic.cache_size(),
        # The shape of the last failure is public ; its text is not.
        "last_error": _redact(civic.LAST_ERROR, admin),
        "recent_errors": [_redact(e, admin) for e in civic.RECENT_ERRORS],
        # What each search backend returned on the last question this replica
        # answered : a count, or the error it raised.
        "sources_last_run": (dict(civic_sources.LAST_RUN) if admin
                             else {k: (v if isinstance(v, int) else "error")
                                   for k, v in civic_sources.LAST_RUN.items()}),
    }
    if probe and not admin:
        report["probe"] = "needs X-Admin-Token: it makes ten outbound calls"
    elif probe:
        # ?probe=1 runs every keyless backend once against a fixed query. Read
        # it after a deploy: it says which indexes answer from THIS network,
        # which are rate-limiting us, and which have changed shape.
        report["probe"] = civic_sources.probe()
        report["probe"]["jurisdictions"] = civic_geo.probe()
    return report


def _page():
    resp = FileResponse(str(_UI_HTML), media_type="text/html")
    # Shell-style no-store, matching the landing page: the HTML is the app, so
    # a deploy must not be served from a stale browser cache.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return resp


if _UI_HTML.is_file():
    @pages_router.get("/civic")
    def civic_page():
        """The map. Spin the globe, drop a pin, ask."""
        if not enabled():
            raise HTTPException(404, "Not found")
        return _page()

    @pages_router.get("/civic/r/{answer_id}")
    def civic_permalink(answer_id: str):
        """A shared answer. Same page; the client fetches the id on load."""
        if not enabled():
            raise HTTPException(404, "Not found")
        return _page()
