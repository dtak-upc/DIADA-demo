"""
Background warm-up: right after a project is created or activated, compute
and persist (app/storage.py) the schema profile AND the deep DuckDB profile
for every dataset in its folder, then rebuild+persist the catalog graph and
the usable graph derived from it (flagged datasets/columns filtered out -
see app/catalog.py:build_usable_graph) - so browsing afterward, including
after a backend restart, is always a disk read, never a recompute.
Progress is tracked in a module-level singleton the UI polls to render a
progress bar.

Datasets that already have a fresh persisted profile (source CSV mtime
unchanged) are skipped instantly rather than recomputed by
data_loader.profile_dataset / column_profiler.get_column_profiles - so
re-activating an already-processed project, or restarting the backend with
one active, finishes near-instantly instead of redoing work.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .catalog import catalog
from .column_profiler import get_column_profiles
from .core import data_loader, storage

_lock = threading.Lock()


@dataclass
class _State:
    project_id: Optional[str] = None
    total: int = 0
    completed: int = 0
    current_dataset: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    running: bool = False
    error: Optional[str] = None


_state = _State()


def get_state() -> dict[str, Any]:
    with _lock:
        elapsed = None
        if _state.started_at is not None:
            end = _state.finished_at if _state.finished_at is not None else time.monotonic()
            elapsed = round(end - _state.started_at, 2)
        return {
            "project_id": _state.project_id,
            "running": _state.running,
            "total": _state.total,
            "completed": _state.completed,
            "current_dataset": _state.current_dataset,
            "elapsed_seconds": elapsed,
            "error": _state.error,
        }


def reset() -> None:
    with _lock:
        global _state
        _state = _State()


def start_processing(project_id: str) -> None:
    """Fire-and-forget: profiles (schema + deep) every dataset in the given
    project's folder on a background thread, then rebuilds+persists the
    catalog. Assumes project_id is (or is about to be) the active project -
    data_loader/column_profiler resolve the folder via the active project,
    same as every other read path in the app. Safe to call again while
    already running for a different project; the new run's state simply
    replaces the old one, and the abandoned thread's results are just
    storage writes nobody asked for."""
    names = data_loader.dataset_names()
    with _lock:
        _state.project_id = project_id
        _state.total = len(names)
        _state.completed = 0
        _state.current_dataset = None
        _state.started_at = time.monotonic()
        _state.finished_at = None
        _state.running = True
        _state.error = None

    thread = threading.Thread(target=_run, args=(project_id, names), daemon=True)
    thread.start()


def _run(project_id: str, names: list[str]) -> None:
    try:
        storage.prune_stale_datasets(project_id, names)
        for name in names:
            with _lock:
                _state.current_dataset = name
            try:
                data_loader.profile_dataset(name)
                get_column_profiles(name)
            except data_loader.DatasetNotFoundError:
                pass
            with _lock:
                _state.completed += 1
        catalog.rebuild_and_store(project_id)
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI rather than dying silently
        with _lock:
            _state.error = str(e)
    finally:
        with _lock:
            _state.running = False
            _state.current_dataset = None
            _state.finished_at = time.monotonic()
