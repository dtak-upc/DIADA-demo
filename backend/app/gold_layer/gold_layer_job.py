"""
Background runner for app/gold_layer.py's two algorithms - "starting from
every table" (run_every_table) and "ad hoc" (run_ad_hoc), both of which
spawn a DIADA subprocess per gold table and so can take a while.

Mirrors app/processing.py's pattern: fire-and-forget on a background
thread, progress tracked in a module-level singleton the UI polls - except
now keyed by mode, since the two algorithms that use this can be built
independently (even concurrently) of one another, each with its own
config and its own persisted result (see gold_layer.py's module docstring
and storage.GOLD_LAYER_MODES) - one algorithm's progress state must never
be clobbered by the other's.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import gold_layer
from ..catalog import catalog
from ..core import storage

_lock = threading.Lock()

_RUNNABLE_MODES = ("every_table", "ad_hoc")


@dataclass
class _State:
    project_id: Optional[str] = None
    total: int = 0  # number of items (bases, or ad hoc assets) in the current phase
    completed: int = 0
    # A ready-to-display label for whatever's happening right now - not
    # always a literal dataset name despite the field's history (ad hoc
    # also reports phase labels like "Finding join candidates across the
    # lake" here that aren't about any one dataset - see gold_layer.py's
    # run_ad_hoc/run_every_table for exactly what gets sent).
    current_step: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    running: bool = False
    error: Optional[str] = None


# One slot per runnable mode, created up front rather than lazily on first
# use - get_state(mode) is polled from a request handler with no
# lock-free way to "create if missing" without a race, so every valid key
# already exists from import time instead.
_states: dict[str, _State] = {mode: _State() for mode in _RUNNABLE_MODES}


def _check_mode(mode: str) -> None:
    if mode not in _RUNNABLE_MODES:
        raise ValueError(f"gold_layer_job doesn't run mode '{mode}' in the background - it has no progress state")


def get_state(mode: str) -> dict[str, Any]:
    _check_mode(mode)
    with _lock:
        state = _states[mode]
        elapsed = None
        if state.started_at is not None:
            end = state.finished_at if state.finished_at is not None else time.monotonic()
            elapsed = round(end - state.started_at, 2)
        return {
            "project_id": state.project_id,
            "running": state.running,
            "total": state.total,
            "completed": state.completed,
            "current_step": state.current_step,
            "elapsed_seconds": elapsed,
            "error": state.error,
        }


def start_run(project_id: str, mode: str, config: gold_layer.GoldLayerConfig) -> None:
    """Fire-and-forget. Safe to call again while this mode is already
    running (for this or a different project) - the new run's state
    simply replaces the old one, same as processing.start_processing().
    Building a different mode at the same time is also safe - each has
    its own state slot and its own persisted result, so the two never
    interfere with each other."""
    _check_mode(mode)
    with _lock:
        state = _states[mode]
        state.project_id = project_id
        state.total = 0
        state.completed = 0
        state.current_step = None
        state.started_at = time.monotonic()
        state.finished_at = None
        state.running = True
        state.error = None

    thread = threading.Thread(target=_run, args=(project_id, mode, config), daemon=True)
    thread.start()


def _on_progress(mode: str, label: str, completed: int, total: int) -> None:
    with _lock:
        state = _states[mode]
        state.current_step = label
        state.completed = completed
        state.total = total


def _run(project_id: str, mode: str, config: gold_layer.GoldLayerConfig) -> None:
    try:
        on_progress = lambda label, completed, total: _on_progress(mode, label, completed, total)  # noqa: E731
        if mode == "every_table":
            result = gold_layer.run_every_table(project_id, config, on_progress=on_progress)
        else:  # "ad_hoc" - the only other _RUNNABLE_MODES entry, _check_mode already guards anything else
            result = gold_layer.run_ad_hoc(project_id, config, on_progress=on_progress)
        with _lock:
            _states[mode].completed = _states[mode].total
        storage.save_gold_layer(project_id, mode, gold_layer.run_to_dict(result))
        # Re-layer the catalog graph's governance edges (app/catalog.py) on
        # top of every algorithm's latest persisted run now that this
        # mode's has changed - so GET /api/catalog (the "Catalog Graph"
        # tab) reflects it without a separate fetch/rebuild step.
        catalog.rebuild_and_store(project_id)
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI rather than dying silently
        with _lock:
            _states[mode].error = str(e)
    finally:
        with _lock:
            _states[mode].running = False
            _states[mode].current_step = None
            _states[mode].finished_at = time.monotonic()
