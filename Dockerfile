# --- Stage 1: build the React frontend -------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
# output: /app/frontend/dist/

# --- Stage 2: python runtime, serves API + built frontend ------------------
FROM python:3.12-slim

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

# Git commit baked at build time so /api/health can report what's live.
# Pass it at deploy: `flyctl deploy --build-arg GIT_SHA=$(git rev-parse --short HEAD)`.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Railway injects $PORT. Default 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# exec-form CMD so $PORT expands properly via sh
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
