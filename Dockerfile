# Demo for DIADA - single-container deployment: FastAPI backend + the
# frontend's built static assets, served together from one process on one
# port. See README.md's "Deploying with Docker" section for usage.
#
# Three stages so the final image carries only what it needs to run, not
# what it took to build it: Node (frontend build) and a C compiler
# (backend build) never end up in the shipped image, just their outputs.

# ---- Stage 1: compile the frontend into static assets ----
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: install the backend's Python deps into a venv ----
FROM python:3.11-slim AS backend-build
WORKDIR /app
# jellyfish/python-Levenshtein don't always ship a wheel for every
# platform this might build on - a C compiler makes that a non-issue
# either way. Doesn't affect the final image; this stage is discarded
# after its venv is copied out below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt backend/requirements.txt
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r backend/requirements.txt

# ---- Stage 3: runtime ----
FROM python:3.11-slim
WORKDIR /app

# DIADA (backend/tools/DIADA-0.8.jar) is a Java tool invoked as a
# subprocess (see backend/app/diada.py) - needs a JRE 11+ on PATH.
# headless: no X11/GUI libs needed for a subprocess that's never given a
# display. openjdk-21, not -17: this base image's current Debian release
# doesn't carry 17 in its default repos any more (confirmed by a failed
# build - "no installation candidate"), and DIADA only needs 11+ anyway.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend-build /venv /venv
ENV PATH="/venv/bin:${PATH}"

COPY backend/ backend/
COPY --from=frontend-build /app/frontend/dist frontend/dist

# Bundled demo lake - lands at core.config.SAMPLE_DATA_DIR (PROJECT_ROOT /
# "data"), offered in the UI as a one-click "Use bundled sample data"
# project so the container is immediately explorable without pointing it
# at real data first.
COPY urban_join_demo/*.csv data/

# Persisted app state (project registry + every computed profile - see
# core.config.DIADA_DIR) - mount a named volume here so it survives
# container recreation, not just restarts. Nothing under it is source
# data, so losing it (an unmounted run) just means recomputing profiles
# from source CSVs on next launch, not losing anything irreplaceable.
VOLUME ["/app/.diada"]

WORKDIR /app/backend
EXPOSE 8000
# python -m (not a bare `uvicorn ...`) so cwd lands on sys.path the same
# way run.py's own dev-mode invocation already relies on - `app.main:app`
# resolves as a package relative to the working directory either way.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
