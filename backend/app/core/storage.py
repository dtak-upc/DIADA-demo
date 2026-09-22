"""
Disk persistence for computed profiles + the catalog graph: one JSON file
per dataset (holding both the schema profile and the deep DuckDB profile,
since they're always computed together now - see app/processing.py) plus
one JSON file per project for its catalog graph, and one for the "usable"
graph derived from it (flagged datasets/columns filtered out - see
app/catalog.py:build_usable_graph).

Everything under here is derived from source CSVs, never source data
itself, so wiping a project's directory is always a safe, recoverable
operation - the next load just recomputes and re-persists it.

Freshness is tracked two ways: by storing the source CSV's mtime alongside
each profile (if the file's current mtime doesn't match what's stored, the
entry is treated as missing rather than served stale), and by a schema
version (bump SCHEMA_VERSION whenever what gets computed for a profile
changes shape - e.g. adding a field - so profiles computed by older code
get recomputed instead of silently served incomplete forever).

The schema profile and deep profile each get their *own* mtime field
(schema_mtime/deep_profile_mtime) even though both live in one JSON file -
they're computed and saved by two independent call sites (app/data_loader.
py's profile_dataset and app/column_profiler's get_column_profiles,
normally called back-to-back by app/processing.py, but not atomically). A
single shared "mtime" field used to cover both; that was wrong; whichever
of the two ran second would stamp the shared field with the current mtime
as a side effect of saving *its own* result, which made the *other*
artifact - not actually recomputed this pass - look fresh under a mtime it
was never actually computed against. Harmless the first time a dataset is
ever profiled (nothing stale is sitting there yet to be wrongly trusted),
but on a dataset whose source CSV is later replaced with different
content at the same name, it meant the deep profile could silently keep
serving pre-replacement data forever, since load_deep_profile had no way
to tell "this mtime matches" apart from "this mtime matches AND this
specific artifact was actually recomputed under it".
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import PROJECT_DATA_DIR

# v2: length/word-count metrics are now computed for every column (not just
# string ones) - needed as features by the joinability model.
# v3: CSVs are read with encoding="utf-8-sig" (strips a leading BOM instead
# of letting it corrupt the first column's name) - files affected by this
# need their column names/profiles recomputed.
SCHEMA_VERSION = 3


def _project_dir(project_id: str) -> Path:
    return PROJECT_DATA_DIR / project_id


def _datasets_dir(project_id: str) -> Path:
    return _project_dir(project_id) / "datasets"


def _dataset_file(project_id: str, dataset_name: str) -> Path:
    return _datasets_dir(project_id) / f"{dataset_name}.json"


def _catalog_file(project_id: str) -> Path:
    return _project_dir(project_id) / "catalog.json"


def _usable_graph_file(project_id: str) -> Path:
    return _project_dir(project_id) / "usable_graph.json"


# The two gold-layer algorithms (app/gold_layer.py: run_every_table/
# run_ad_hoc) build and persist completely independently of each other -
# see that module's docstring - so each gets its own file rather than
# sharing the one gold_layer.json a single combined run used to produce.
GOLD_LAYER_MODES = ("every_table", "ad_hoc")


def _gold_layer_file(project_id: str, mode: str) -> Path:
    return _project_dir(project_id) / f"gold_layer_{mode}.json"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _load_raw(project_id: str, dataset_name: str) -> dict | None:
    return _read_json(_dataset_file(project_id, dataset_name))


def _is_fresh(data: dict | None, current_mtime: float, mtime_key: str) -> bool:
    return data is not None and data.get(mtime_key) == current_mtime and data.get("version") == SCHEMA_VERSION


def load_schema(project_id: str, dataset_name: str, current_mtime: float) -> dict | None:
    data = _load_raw(project_id, dataset_name)
    if not _is_fresh(data, current_mtime, "schema_mtime"):
        return None
    return data.get("schema")


def save_schema(project_id: str, dataset_name: str, mtime: float, schema: dict) -> None:
    # Read-modify-write: a deep profile may already be stored for this
    # dataset (or vice versa) - don't clobber it. Its own schema_mtime/
    # deep_profile_mtime field is untouched here, so a stale deep_profile
    # left over from before this write still correctly reads as stale to
    # load_deep_profile - seeing this write's freshness stamp requires
    # get_column_profiles to have actually recomputed it (see module
    # docstring for the bug this replaced, where both shared one field).
    data = _load_raw(project_id, dataset_name) or {}
    data["schema_mtime"] = mtime
    data["version"] = SCHEMA_VERSION
    data["schema"] = schema
    _write_json(_dataset_file(project_id, dataset_name), data)


def load_deep_profile(project_id: str, dataset_name: str, current_mtime: float) -> list[dict] | None:
    data = _load_raw(project_id, dataset_name)
    if not _is_fresh(data, current_mtime, "deep_profile_mtime"):
        return None
    return data.get("deep_profile")


def save_deep_profile(project_id: str, dataset_name: str, mtime: float, deep_profile: list[dict]) -> None:
    data = _load_raw(project_id, dataset_name) or {}
    data["deep_profile_mtime"] = mtime
    data["version"] = SCHEMA_VERSION
    data["deep_profile"] = deep_profile
    _write_json(_dataset_file(project_id, dataset_name), data)


def load_catalog(project_id: str) -> dict | None:
    return _read_json(_catalog_file(project_id))


def save_catalog(project_id: str, catalog: dict[str, list[dict[str, Any]]]) -> None:
    _write_json(_catalog_file(project_id), catalog)


def load_usable_graph(project_id: str) -> dict | None:
    return _read_json(_usable_graph_file(project_id))


def save_usable_graph(project_id: str, graph: dict[str, list[dict[str, Any]]]) -> None:
    _write_json(_usable_graph_file(project_id), graph)


def load_gold_layer(project_id: str, mode: str) -> dict | None:
    """The last run of one gold-layer algorithm for this project
    (app/gold_layer.py: run_every_table/run_ad_hoc, mode one
    of GOLD_LAYER_MODES) - the config used, every gold table's metadata
    (columns, source datasets, scores - not row data, which is cheap
    enough to recompute on demand via the same join_builder+diada calls
    the ad-hoc join/curate endpoints use), and the explainability graph.
    None if this specific algorithm has never been built for this project."""
    return _read_json(_gold_layer_file(project_id, mode))


def load_all_gold_layers(project_id: str) -> dict[str, dict | None]:
    """{"every_table": ..., "ad_hoc": ...} - every
    algorithm's last persisted run at once, for a caller (app/catalog.py's
    governance layering, the "has any run at all" check) that doesn't
    care which specific one, unlike most callers which know exactly which
    mode they want."""
    return {mode: load_gold_layer(project_id, mode) for mode in GOLD_LAYER_MODES}


def save_gold_layer(project_id: str, mode: str, run: dict[str, Any]) -> None:
    _write_json(_gold_layer_file(project_id, mode), run)


def stored_dataset_names(project_id: str) -> list[str]:
    """Datasets that currently have some persisted data for this project."""
    datasets_dir = _datasets_dir(project_id)
    if not datasets_dir.exists():
        return []
    return [p.stem for p in datasets_dir.glob("*.json")]


def prune_stale_datasets(project_id: str, live_names: list[str]) -> None:
    """Removes persisted files for datasets no longer present in the
    project's source folder (e.g. a CSV was deleted or renamed), so storage
    doesn't accumulate orphaned entries for files that no longer exist."""
    live = set(live_names)
    for name in stored_dataset_names(project_id):
        if name not in live:
            _dataset_file(project_id, name).unlink(missing_ok=True)


def clear_project_data(project_id: str) -> None:
    """Wipes everything computed for a project (all dataset profiles + the
    catalog), leaving the project's registry entry (name, folder) intact."""
    project_dir = _project_dir(project_id)
    if project_dir.exists():
        shutil.rmtree(project_dir)
