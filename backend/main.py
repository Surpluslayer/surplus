"""
main.py — FastAPI app.

The five-stage mechanism, exposed as one API:

    01  intake     POST /events                   define the event
    02  pipeline   POST /events/{id}/run          fan-out + score + outreach
    03   "         (folded into /run)
    04  matching   POST /events/{id}/match        symbiotic value graph
    05  roi        GET  /events/{id}/roi          verified conversion ledger

Run it:  uvicorn backend.main:app --reload
Docs at: http://localhost:8000/docs
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routes import events, pipeline, matching, roi


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create tables on startup
    yield


app = FastAPI(
    title="surplus · event ROI engine",
    description="AI prospecting, autonomous outreach, symbiotic matching, and "
                "verified per-guest ROI for events.",
    version="0.1.0",
    lifespan=lifespan,
)

# the frontend demo is a separate client — allow it through in dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router)
app.include_router(pipeline.router)
app.include_router(matching.router)
app.include_router(roi.router)


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "surplus-roi-engine",
        "version": "0.1.0",
        "stages": ["01 intake", "02-03 pipeline", "04 matching", "05 roi"],
        "docs": "/docs",
    }
