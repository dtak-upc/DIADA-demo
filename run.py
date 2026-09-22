#!/usr/bin/env python3
"""
One-command launcher for the Demo for DIADA gold-layer tool.

Starts the FastAPI backend (uvicorn) and the React frontend (vite dev
server) together, and shuts both down cleanly on Ctrl+C.

Usage:
    python run.py
    python run.py --backend-port 8000 --frontend-port 5173
    python run.py --skip-install        # don't auto npm-install
    python run.py --backend-only        # just the API
    python run.py --frontend-only       # just the UI (backend must be running elsewhere)

Requires: the conda env from environment.yml (or any env with the packages
in backend/requirements.txt, plus Node/npm on PATH).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"


def npm_cmd() -> str:
    cmd = shutil.which("npm")
    if not cmd:
        sys.exit("npm not found on PATH. Install Node (e.g. via environment.yml) first.")
    return cmd


def ensure_frontend_deps(skip_install: bool) -> None:
    node_modules = FRONTEND_DIR / "node_modules"
    if node_modules.exists() or skip_install:
        return
    print("[run] frontend/node_modules not found - running `npm install` first...")
    subprocess.run([npm_cmd(), "install"], cwd=FRONTEND_DIR, check=True)


def start_backend(port: int) -> subprocess.Popen:
    print(f"[run] starting backend on http://localhost:{port}")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--reload",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        cwd=BACKEND_DIR,
    )


def start_frontend(port: int, backend_port: int) -> subprocess.Popen:
    print(f"[run] starting frontend on http://localhost:{port}")
    env_overrides = {"VITE_BACKEND_PORT": str(backend_port)}
    import os

    env = {**os.environ, **env_overrides}
    return subprocess.Popen(
        [npm_cmd(), "run", "dev", "--", "--host", "0.0.0.0", "--port", str(port)],
        cwd=FRONTEND_DIR,
        env=env,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--skip-install", action="store_true", help="Don't auto-run npm install")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--frontend-only", action="store_true")
    args = parser.parse_args()

    if args.backend_only and args.frontend_only:
        sys.exit("--backend-only and --frontend-only are mutually exclusive")

    processes: list[subprocess.Popen] = []
    try:
        if not args.frontend_only:
            processes.append(start_backend(args.backend_port))
            time.sleep(1)  # give the API a head start before the UI proxies to it

        if not args.backend_only:
            ensure_frontend_deps(args.skip_install)
            processes.append(start_frontend(args.frontend_port, args.backend_port))

        print("[run] all services started - press Ctrl+C to stop")
        while True:
            for p in processes:
                ret = p.poll()
                if ret is not None:
                    print(f"[run] process {p.args[0]} exited with code {ret}, shutting down")
                    raise SystemExit(ret)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run] shutting down...")
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
