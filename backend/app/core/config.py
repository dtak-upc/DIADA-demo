"""Central configuration for the backend."""
from pathlib import Path

# Project root is three levels up from this file (backend/app/core/config.py -> project root)
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Bundled synthetic CSVs. Not read automatically - just offered in the UI as
# a one-click way to try the tool before pointing it at real data.
SAMPLE_DATA_DIR = PROJECT_ROOT / "data"

# The frontend's built static assets (`npm run build`'s output - see
# frontend/vite.config.js). Only present in the Docker image (the multi-stage
# build compiles the frontend and copies its dist/ in - see the repo root
# Dockerfile); absent in local dev, where the Vite dev server serves the
# frontend instead and main.py's static mount is skipped entirely.
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"

# All persisted app state lives here: the project registry and, per project,
# every computed profile + the catalog graph (see app/core/projects.py and
# app/core/storage.py). Nothing under here is source data - it's all derived
# and safe to delete wholesale to force a full recompute.
DIADA_DIR = PROJECT_ROOT / ".diada"
PROJECTS_FILE = DIADA_DIR / "projects.json"
PROJECT_DATA_DIR = DIADA_DIR / "data"

# Max rows returned in a single preview request, regardless of what the
# client asks for, to keep responses light.
MAX_PREVIEW_LIMIT = 500
DEFAULT_PREVIEW_LIMIT = 50

# Pre-trained joinability model (see app/catalog/joinability_model.py).
# Checked into the repo since it's small (~140KB) and this app has to be
# usable standalone, not depend on a path into a different, unrelated
# project.
JOINABILITY_MODEL_PATH = PROJECT_ROOT / "backend" / "models" / "gradient_boosting_ne100_lr0.05_md3_ss0.8_msl10.pkl"

# DIADA (see app/diada.py) - a Java tool, invoked as a subprocess, that
# scores relationships between column pairs. Vendored into the repo (~20MB)
# for the same standalone-usability reason as the joinability model above.
DIADA_JAR_PATH = PROJECT_ROOT / "backend" / "tools" / "DIADA-0.8.jar"
