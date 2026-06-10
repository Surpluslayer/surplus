# --- Stage 1: build the React frontend -------------------------------------
# Pinned to a specific minor (was node:20-alpine): the floating alpine tag moves
# and has occasionally broken native builds. Bump deliberately.
FROM node:20.18-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
# Cache-bust knob: a changed commit sha forces the frontend COPY + build below
# to rerun, even on a remote builder whose layer cache is coarser than Docker's
# content hash. (Docker already invalidates COPY on content change; this is the
# belt-and-suspenders so a stale dist can never silently survive a deploy.)
#   - GitHub deploys: Railway injects RAILWAY_GIT_COMMIT_SHA at build, so this
#     auto-busts on every commit with no extra flags.
#   - railway up / docker build: pass `--build-arg GIT_SHA=$(git rev-parse --short HEAD)`.
ARG GIT_SHA=unknown
ARG RAILWAY_GIT_COMMIT_SHA=
RUN echo "frontend build for ${GIT_SHA} ${RAILWAY_GIT_COMMIT_SHA}" > /dev/null
COPY frontend/ ./
RUN npm run build
# output: /app/frontend/dist/

# --- Stage 2: python runtime, serves API + built frontend ------------------
# Pinned minor (was python:3.12-slim) so the runtime can't shift under us.
FROM python:3.12.8-slim

# system deps that some pip packages occasionally need
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install Python deps first (cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# backend code
COPY backend/ ./backend/

# built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# make sure the SQLite data dir exists + is writable
RUN mkdir -p /app/backend/data && chmod -R 777 /app/backend/data

# Build stamp : the timestamp of the build that produced this image. Comes
# AFTER the backend + frontend COPY layers, so it's refreshed whenever either
# actually rebuilds and is a cache hit (unchanged) only when the whole image is.
# /api/health surfaces it, so a deploy that didn't pick up your changes — stale
# source, a full cache hit — is obvious: the stamp won't have moved. Independent
# of GIT_SHA, so it works even without a build-arg (e.g. `railway up`).
RUN date -u +"%Y-%m-%dT%H:%M:%SZ" > /app/backend/.build_time

# Git commit baked at build time so /api/health can report what's live.
# Pass it at deploy: `flyctl deploy --build-arg GIT_SHA=$(git rev-parse --short HEAD)`.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Railway injects $PORT. Default 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# WEB_CONCURRENCY = number of worker processes. Sync route handlers make
# blocking provider/LLM calls, so multiple workers improve concurrency — BUT
# each worker loads the full app (anthropic/numpy/sqlalchemy ≈ hundreds of MB),
# so too many workers OOM a memory-limited instance and crash-loop on boot.
# Default to 1 (the original, safe behavior); raise it deliberately in Railway
# once you've confirmed the instance has RAM headroom (rule of thumb ~2× vCPU,
# and watch memory). Keep DB_POOL_SIZE × WEB_CONCURRENCY under your Postgres
# connection cap (see backend/db.py).
ENV WEB_CONCURRENCY=1

# exec-form CMD so $PORT / $WEB_CONCURRENCY expand via sh
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1} --timeout-keep-alive 30"]
