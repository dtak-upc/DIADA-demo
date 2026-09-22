"""
FastAPI app for the gold-layer tool - Stage 1: load & preprocess.

Endpoints:
  GET /api/health
  GET /api/projects                                -> list all projects + which one is active + sample folder
  POST /api/projects   {name, folder}               -> create a project (becomes active), starts warm-up processing
  POST /api/projects/{id}/activate                  -> switch active project, starts warm-up processing
  DELETE /api/projects/{id}                         -> delete a project entirely (registry + all its stored data)
  DELETE /api/projects/{id}/data                     -> clear a project's stored data only (keeps name/folder), reprocesses if active
  GET /api/projects/progress                        -> background warm-up progress (per-dataset, elapsed time)
  GET /api/datasets                                 -> list datasets in the active project + summary stats + flags
  GET /api/datasets/{name}                          -> per-column profile (dtype, nulls, uniques, flags) + dataset-level flags
  GET /api/datasets/{name}/preview?limit&offset&raw -> paginated rows for the UI table
  GET /api/datasets/{name}/column-profiles          -> deep per-column statistical profile (DuckDB)
  GET /api/catalog                                  -> metadata graph (datasets -> columns -> flags) for the active project
  GET /api/catalog/usable                            -> catalog filtered to unflagged datasets/columns only
  GET /api/join-discovery?dataset&column              -> rank every other usable column by predicted joinability with the given one
  POST /api/join/preview?limit&offset                 -> multi-way join preview (base + N candidates), paginated
  POST /api/join/download                              -> the same join, computed in full and streamed back as a CSV file
  POST /api/join/curate                                -> run DIADA once on the materialized join, return CF1 + CF2 previews
  POST /api/join/curate/cf1/download                    -> CF1 ("remove univariate noise") result, computed in full and streamed as CSV
  POST /api/join/curate/cf2/download                    -> one CF2 ("structure dataset") cluster, computed in full and streamed as CSV
  POST /api/gold-layer/run {mode, ...config}               -> build one of the two independent gold-layer algorithms (mode: every_table | ad_hoc - see app/gold_layer/gold_layer.py), as a background job (see app/gold_layer/gold_layer_job.py)
  GET /api/gold-layer/progress?mode                     -> poll a background job's progress
  GET /api/gold-layer?mode                              -> one algorithm's last persisted run's results + explainability graph
  POST /api/gold-layer/preview?limit&offset              -> paginated preview of one gold table, recomputed fresh
  POST /api/gold-layer/download                          -> one gold table, computed in full and streamed as CSV

In the Docker image, the frontend's built static assets are also mounted
at "/" (see the bottom of this file and core.config.FRONTEND_DIST_DIR) -
absent in local dev, where the Vite dev server serves the frontend instead.
"""
import io
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import diada, join_builder, processing
from .catalog import catalog, joinability_model
from .column_profiler import get_column_profiles
from .core import data_loader, projects, storage
from .core.config import DEFAULT_PREVIEW_LIMIT, FRONTEND_DIST_DIR, MAX_PREVIEW_LIMIT, SAMPLE_DATA_DIR
from .gold_layer import gold_layer, gold_layer_job

app = FastAPI(title="Demo for DIADA API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


class CreateProjectPayload(BaseModel):
    name: str
    folder: str


def _project_json(p: projects.Project) -> dict:
    return {"id": p.id, "name": p.name, "folder": p.folder}


@app.get("/api/projects")
def list_projects():
    active = projects.get_active_project()
    sample = SAMPLE_DATA_DIR if SAMPLE_DATA_DIR.is_dir() else None
    return {
        "projects": [_project_json(p) for p in projects.list_projects()],
        "active_project_id": active.id if active else None,
        "sample_path": str(sample) if sample else None,
    }


@app.post("/api/projects")
def create_project(payload: CreateProjectPayload):
    try:
        project = projects.create_project(payload.name, payload.folder)
    except projects.InvalidProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    processing.start_processing(project.id)
    return _project_json(project)


@app.post("/api/projects/{project_id}/activate")
def activate_project(project_id: str):
    try:
        project = projects.set_active_project(project_id)
    except projects.ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    processing.start_processing(project.id)
    return _project_json(project)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    was_active = projects.get_active_project()
    was_active = was_active is not None and was_active.id == project_id
    try:
        projects.delete_project(project_id)
    except projects.ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if was_active:
        processing.reset()
    return {"deleted": project_id}


@app.delete("/api/projects/{project_id}/data")
def clear_project_data(project_id: str):
    if projects.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No project with id '{project_id}'")
    storage.clear_project_data(project_id)
    active = projects.get_active_project()
    if active is not None and active.id == project_id:
        processing.start_processing(project_id)
    return {"cleared": project_id}


@app.get("/api/projects/progress")
def get_processing_progress():
    return processing.get_state()


@app.on_event("startup")
def _warm_up_active_project():
    """If a project was already active in a previous run, its persisted
    data survives (see app/storage.py) but this process's memory doesn't -
    kick off the same warm-up as activating it fresh. In the common case
    (nothing changed on disk since last run) every dataset is a fresh-mtime
    hit and this finishes near-instantly; it only does real work if CSVs
    changed since the backend last ran."""
    active = projects.get_active_project()
    if active is not None:
        processing.start_processing(active.id)


@app.get("/api/datasets")
def list_datasets():
    names = data_loader.dataset_names()
    if not names:
        return {"datasets": []}
    out = []
    for name in names:
        try:
            profile = data_loader.profile_dataset(name)
        except data_loader.DatasetNotFoundError:
            continue
        out.append(
            {
                "name": profile.name,
                "row_count": profile.row_count,
                "column_count": profile.column_count,
                "size_bytes": profile.size_bytes,
                "duplicate_row_count": profile.duplicate_row_count,
                "flags": profile.flags,
            }
        )
    return {"datasets": out}


@app.get("/api/datasets/{name}")
def get_dataset_profile(name: str):
    try:
        profile = data_loader.profile_dataset(name)
    except data_loader.DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "name": profile.name,
        "row_count": profile.row_count,
        "column_count": profile.column_count,
        "size_bytes": profile.size_bytes,
        "duplicate_row_count": profile.duplicate_row_count,
        "columns": profile.columns,
        "flags": profile.flags,
    }


@app.get("/api/datasets/{name}/column-profiles")
def get_dataset_column_profiles(name: str):
    try:
        columns = get_column_profiles(name)
    except data_loader.DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"name": name, "columns": columns}


@app.get("/api/catalog")
def get_catalog():
    """Full metadata graph: datasets -> columns -> quality flags, plus (once
    ad hoc has a persisted run) the governance/traceability edges layered
    on top of it - see app/catalog.py's module docstring for why ad hoc
    specifically, not "every table" too. Loaded from
    disk (app/storage.py) - see app/processing.py, which rebuilds+
    persists it after every warm-up, and app/gold_layer_job.py, which does
    the same after a run.

    score_bounds/has_gold_layer_run ride alongside the graph itself (not
    baked into the persisted catalog.json) so the frontend's "Joinability
    graph"/"Composition graph" catalog views can normalize scores and tell
    "ad hoc hasn't been built yet" apart from "ad hoc found nothing"
    without a second request."""
    active = projects.get_active_project()
    if active is None:
        return {"nodes": [], "edges": [], "score_bounds": None, "has_gold_layer_run": False}
    return {
        **catalog.get_catalog(active.id),
        "score_bounds": {"min": joinability_model.SCORE_MIN, "max": joinability_model.SCORE_MAX},
        "has_gold_layer_run": storage.load_gold_layer(active.id, "ad_hoc") is not None,
    }


@app.get("/api/catalog/usable")
def get_usable_catalog():
    """The full catalog filtered down to datasets/columns with zero
    quality flags - the trusted backbone for downstream analysis like join
    discovery (see app/catalog.py:build_usable_graph). Loaded from disk,
    same as /api/catalog."""
    active = projects.get_active_project()
    if active is None:
        return {"nodes": [], "edges": []}
    return catalog.get_usable_graph(active.id)


@app.get("/api/join-discovery")
def get_join_discovery(dataset: str, column: str):
    """Ranks every usable column from every other dataset in the active
    project by predicted joinability with (dataset, column) - other columns
    of (dataset)'s own table are excluded, and remaining candidates are
    restricted to the same main_type (numeric/string) as the base column -
    see app/joinability_model.py for why. Both the base column and every
    candidate must already be in the usable graph (see /api/catalog/usable)
    - quality-flagged columns/datasets are excluded from consideration
    entirely, not just from the results."""
    active = projects.get_active_project()
    if active is None:
        raise HTTPException(status_code=400, detail="No active project")
    try:
        base_profile, candidates = joinability_model.find_join_candidates(active.id, dataset, column)
    except joinability_model.NotUsableColumnError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "base": {"dataset": dataset, "column": column, "main_type": base_profile["main_type"]},
        "candidates": candidates,
        "score_bounds": {"min": joinability_model.SCORE_MIN, "max": joinability_model.SCORE_MAX},
    }


class ChainStepPayload(BaseModel):
    dataset: str  # candidate dataset to attach
    column: str  # candidate's own key column
    base_column: str  # original (never renamed) column name of base_column_dataset this attaches from
    base_column_dataset: str  # which real dataset base_column belongs to - root_dataset for a "multi-attribute" step, or any dataset an earlier step already folded in for an "iterative" one
    aggregations: dict[str, str] = {}


class JoinRequestPayload(BaseModel):
    root_dataset: str
    steps: list[ChainStepPayload]  # applied one at a time, in order - see join_builder.build_chain
    exclude_unused_primary_keys: bool = False  # drop columns that are a primary key of their own dataset but weren't the join key - see join_builder.unused_primary_key_columns
    exclude_redundant_columns: bool = False  # keep only one column per redundant (near-bijective) group - see join_builder.redundant_column_groups
    exclude_mostly_null_columns: bool = False  # drop columns that are almost entirely null in the joined result - see join_builder.mostly_null_columns


def _build_chain(
    payload: JoinRequestPayload,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, str], list[str], dict[str, str], list[str]]:
    active = projects.get_active_project()
    if active is None:
        raise HTTPException(status_code=400, detail="No active project")
    if not payload.steps:
        raise HTTPException(status_code=400, detail="Pick at least one candidate dataset to join.")
    steps = [
        join_builder.ChainStep(
            dataset=s.dataset, column=s.column, base_column=s.base_column,
            base_column_dataset=s.base_column_dataset, aggregations=s.aggregations,
        )
        for s in payload.steps
    ]
    try:
        return join_builder.build_chain(active.id, payload.root_dataset, steps)
    except join_builder.JoinError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _declutter_info(
    df: pd.DataFrame, column_provenance: dict[str, str], original_columns: dict[str, str], protected: list[str]
) -> dict[str, Any]:
    """The three independent "declutter" signals for one materialized join:
    unused_primary_keys and redundant_column_groups (both metadata-aware,
    scoped to one source dataset's own string columns) and
    mostly_null_columns (needs only the values, applies to any column
    regardless of source or type).

    unused_primary_key_columns/redundant_column_groups/mostly_null_columns
    each only know how to exempt a single base column, but a chain can have
    several protected columns at once - every base_column a step actually
    used. One is picked as the representative passed into each function,
    and every other protected column is filtered back out of whatever that
    function flagged - the same "pick one, filter the rest back out"
    approach as gold_layer.py's declutter_columns, which faces the
    identical problem for its own multi-column groups (see that function's
    docstring for why this is safe even when a *different* protected
    column ends up not being a group's naive "kept" pick: it's still never
    added to what gets flagged, because of the trailing `if c not in
    protected` filter)."""
    active = projects.get_active_project()
    string_columns = catalog.usable_columns_by_dataset(active.id, main_type="string")
    column_uniqueness = catalog.usable_column_uniqueness(active.id)
    representative = protected[0]
    # unused_primary_key_columns only reads .columns/.column_provenance/
    # .original_columns off a PreparedJoin, never .con - a plain con=None
    # stand-in is safe, same established pattern as gold_layer.py's own
    # declutter_columns caller.
    prepared_like = join_builder.PreparedJoin(
        con=None, columns=list(df.columns), total_rows=len(df),
        aggregated_datasets=[], renamed_columns={},
        column_provenance=column_provenance, original_columns=original_columns,
    )
    redundant_column_groups = []
    for group in join_builder.redundant_column_groups(df, column_provenance, original_columns, representative, string_columns):
        cols = [c for c in group["columns"] if c not in protected]
        if len(cols) < 2:
            continue
        kept = group["kept"] if group["kept"] in cols else sorted(cols)[0]
        redundant_column_groups.append({"columns": cols, "kept": kept})
    return {
        "unused_primary_keys": [
            c for c in join_builder.unused_primary_key_columns(prepared_like, representative, column_uniqueness, string_columns)
            if c not in protected
        ],
        "redundant_column_groups": redundant_column_groups,
        "mostly_null_columns": {
            c: v for c, v in join_builder.mostly_null_columns(df, representative).items() if c not in protected
        },
    }


def _subset_declutter_info(info: dict[str, Any], present_columns: list[str], base_column: str) -> dict[str, Any]:
    """Adapts a full-join declutter_info dict down to whichever columns
    survived into one particular output (e.g. a CF1/CF2 curation result)."""
    present = set(present_columns)
    return {
        "unused_primary_keys": [c for c in info["unused_primary_keys"] if c in present],
        "redundant_column_groups": join_builder.subset_redundant_groups(
            info["redundant_column_groups"], present_columns, base_column
        ),
        "mostly_null_columns": {c: v for c, v in info["mostly_null_columns"].items() if c in present},
    }


def _declutter_drop_columns(
    df: pd.DataFrame,
    info: dict[str, Any],
    exclude_unused_primary_keys: bool,
    exclude_redundant_columns: bool,
    exclude_mostly_null_columns: bool,
) -> pd.DataFrame:
    """Applies whichever of the three declutter exclusions the request
    asked for to an already-materialized dataframe, before it's written
    out."""
    drop: set[str] = set()
    if exclude_unused_primary_keys:
        drop.update(c for c in info["unused_primary_keys"] if c in df.columns)
    if exclude_redundant_columns:
        for group in info["redundant_column_groups"]:
            drop.update(c for c in group["columns"] if c != group["kept"] and c in df.columns)
    if exclude_mostly_null_columns:
        drop.update(c for c in info["mostly_null_columns"] if c in df.columns)
    return df.drop(columns=list(drop)) if drop else df


@app.post("/api/join/preview")
def join_preview(
    payload: JoinRequestPayload,
    limit: int = Query(DEFAULT_PREVIEW_LIMIT, ge=1, le=MAX_PREVIEW_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Builds the join chain (root + every step, each attaching from its
    own base_column/base_column_dataset - see app/join_builder.py's
    build_chain for the granularity/aggregation, name-collision, and
    multi-step-resolution handling) and returns one page of rows, plus the
    three "declutter" signals (unused_primary_keys, redundant_column_groups,
    mostly_null_columns) - all need to see the whole result to be
    meaningful, not just one page, so this materializes the full join
    rather than a paginated DuckDB query. Nothing is persisted - the join
    is rebuilt from the current profiles on every call, same as a plain
    dataset preview.

    original_columns/protected_columns (alongside the always-present
    column_provenance) are what let a caller treat this result as the base
    of a *further* join step - the frontend's Join Discovery tab does
    exactly that to offer "join further from this result" as its own next
    iteration, without needing this endpoint to remember anything between
    calls."""
    df, column_provenance, original_columns, aggregated_datasets, renamed_columns, protected = _build_chain(payload)
    window = df.iloc[offset : offset + limit]
    rows = window.astype(object).where(window.notna(), None).to_dict(orient="records")
    info = _declutter_info(df, column_provenance, original_columns, protected)
    return {
        "columns": list(df.columns),
        "rows": rows,
        "total_rows": len(df),
        "offset": offset,
        "limit": limit,
        "aggregated_datasets": aggregated_datasets,
        "renamed_columns": renamed_columns,
        "column_provenance": column_provenance,
        "original_columns": original_columns,
        "protected_columns": protected,
        **info,
    }


@app.post("/api/join/download")
def join_download(payload: JoinRequestPayload):
    """Same join as /api/join/preview, computed in full and streamed back as
    a CSV file rather than paginated JSON. Drops unused primary keys,
    redundant-group columns, and/or mostly-null columns first per whichever
    of payload.exclude_unused_primary_keys / exclude_redundant_columns /
    exclude_mostly_null_columns are set."""
    df, column_provenance, original_columns, _aggregated_datasets, _renamed_columns, protected = _build_chain(payload)
    info = _declutter_info(df, column_provenance, original_columns, protected)
    df = _declutter_drop_columns(
        df, info,
        payload.exclude_unused_primary_keys, payload.exclude_redundant_columns, payload.exclude_mostly_null_columns,
    )

    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"{payload.root_dataset}_join.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class CurateCf2DownloadPayload(JoinRequestPayload):
    cluster: str


def _rows_json(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict(orient="records")


def _materialized_join(payload: JoinRequestPayload) -> tuple[pd.DataFrame, dict[str, str], list[str], dict[str, Any]]:
    """Builds the chain once and computes its declutter info alongside -
    shared by curate_join and the two curate/*/download endpoints below,
    which all need the same (df, column_provenance, protected, info)
    starting point before running their own DIADA call."""
    df, column_provenance, original_columns, _aggregated_datasets, _renamed_columns, protected = _build_chain(payload)
    info = _declutter_info(df, column_provenance, original_columns, protected)
    return df, column_provenance, protected, info


@app.post("/api/join/curate")
def curate_join(payload: JoinRequestPayload):
    """Builds the join, runs DIADA on it once (see app/diada.py), and
    returns a preview of both curation results: CF1 ("remove univariate
    noise" - columns kept/removed) and CF2 ("structure dataset" - one entry
    per relationship cluster, plus an "unclustered" one if any column has no
    qualifying relationship at all). Only a preview (first
    DEFAULT_PREVIEW_LIMIT rows of each) - see the download endpoints below
    for a given piece's full result."""
    df, column_provenance, protected, info = _materialized_join(payload)
    try:
        result = diada.curate(df, protected_columns=protected)
    except diada.DiadaError as e:
        raise HTTPException(status_code=500, detail=str(e))

    cf1, cf2 = result["cf1"], result["cf2"]
    representative = protected[0]
    return {
        "row_count": len(df),
        "cf1": {
            "columns": cf1["columns"],
            "removed_columns": cf1["removed_columns"],
            "rows": _rows_json(cf1["dataframe"].head(DEFAULT_PREVIEW_LIMIT)),
            "column_provenance": {c: column_provenance[c] for c in cf1["columns"]},
            **_subset_declutter_info(info, cf1["columns"], representative),
        },
        "cf2": {
            "clusters": [
                {
                    "name": c["name"],
                    "columns": c["columns"],
                    "rows": _rows_json(c["dataframe"].head(DEFAULT_PREVIEW_LIMIT)),
                    "column_provenance": {col: column_provenance[col] for col in c["columns"]},
                    **_subset_declutter_info(info, c["columns"], representative),
                }
                for c in cf2["clusters"]
            ],
            "graph": cf2["graph"],
        },
        "soundness_pairs": _rows_json(result["soundness_pairs"]),
    }


@app.post("/api/join/curate/cf1/download")
def curate_cf1_download(payload: JoinRequestPayload):
    """CF1 ("remove univariate noise"), computed in full (join + DIADA
    rerun from scratch, same "nothing persisted" approach as every other
    download endpoint here) and streamed back as a CSV file. Drops unused
    primary keys, redundant-group columns, and/or mostly-null columns
    first per whichever payload.exclude_* flags are set."""
    df, _column_provenance, protected, info = _materialized_join(payload)
    try:
        cf1 = diada.remove_univariate_noise(df, diada.compute_soundness_for_curation(df, protected), protected_columns=protected)
    except diada.DiadaError as e:
        raise HTTPException(status_code=500, detail=str(e))

    out_df = cf1["dataframe"]
    info = _subset_declutter_info(info, list(out_df.columns), protected[0])
    out_df = _declutter_drop_columns(
        out_df, info,
        payload.exclude_unused_primary_keys, payload.exclude_redundant_columns, payload.exclude_mostly_null_columns,
    )

    buffer = io.StringIO()
    out_df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"{payload.root_dataset}_cf1_cleaned.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/join/curate/cf2/download")
def curate_cf2_download(payload: CurateCf2DownloadPayload):
    """One CF2 ("structure dataset") cluster, computed in full and streamed
    back as a CSV file. `payload.cluster` must match one of the cluster
    names from a prior /api/join/curate response (e.g. "cluster_1",
    "unclustered"). Drops unused primary keys, redundant-group columns,
    and/or mostly-null columns first per whichever payload.exclude_* flags
    are set."""
    df, _column_provenance, protected, info = _materialized_join(payload)
    try:
        cf2 = diada.structure_dataset(df, diada.compute_soundness_for_curation(df, protected), protected_columns=protected)
    except diada.DiadaError as e:
        raise HTTPException(status_code=500, detail=str(e))

    cluster = next((c for c in cf2["clusters"] if c["name"] == payload.cluster), None)
    if cluster is None:
        raise HTTPException(status_code=400, detail=f"No cluster named '{payload.cluster}' in this curation result")

    out_df = cluster["dataframe"]
    info = _subset_declutter_info(info, list(out_df.columns), protected[0])
    out_df = _declutter_drop_columns(
        out_df, info,
        payload.exclude_unused_primary_keys, payload.exclude_redundant_columns, payload.exclude_mostly_null_columns,
    )

    buffer = io.StringIO()
    out_df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"{payload.root_dataset}_cf2_{payload.cluster}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class GoldLayerConfigPayload(BaseModel):
    mode: Literal["every_table", "ad_hoc"]
    discovery_threshold: float = gold_layer.DEFAULT_DISCOVERY_THRESHOLD
    max_candidates_per_join: int = gold_layer.DEFAULT_MAX_CANDIDATES_PER_JOIN
    max_workers: int = gold_layer.DEFAULT_MAX_WORKERS
    candidate_uniqueness_threshold: float = gold_layer.DEFAULT_CANDIDATE_UNIQUENESS_THRESHOLD
    ad_hoc_candidate_uniqueness_threshold: float = gold_layer.DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD
    ad_hoc_max_columns_per_asset: int = gold_layer.DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET
    ad_hoc_min_match_rate: float = gold_layer.DEFAULT_AD_HOC_MIN_MATCH_RATE


@app.post("/api/gold-layer/run")
def run_gold_layer(payload: GoldLayerConfigPayload):
    """Builds one of the two independent gold-layer algorithms
    (payload.mode) - see app/gold_layer.py's module docstring; building
    one never touches whatever the other last produced. Both start on a
    background thread (app/gold_layer_job.py) and return
    {"background": True} immediately - poll /api/gold-layer/
    progress?mode=... for status, then GET /api/gold-layer?mode=... for
    the persisted result once it finishes."""
    active = projects.get_active_project()
    if active is None:
        raise HTTPException(status_code=400, detail="No active project")
    config = gold_layer.GoldLayerConfig(**payload.model_dump(exclude={"mode"}))

    gold_layer_job.start_run(active.id, payload.mode, config)
    return {"background": True}


@app.get("/api/gold-layer/progress")
def get_gold_layer_progress(mode: Literal["every_table", "ad_hoc"]):
    return gold_layer_job.get_state(mode)


@app.get("/api/gold-layer")
def get_gold_layer(mode: Literal["every_table", "ad_hoc"]):
    """One algorithm's last persisted run for the active project - config
    used, every gold table's metadata/scores, and the explainability
    graph. `run` is null if this specific algorithm has never completed a
    build for this project (the other may still have one).

    all_join_scores is deliberately stripped before returning: it's every
    raw (base column, candidate column) pair discovery ever scored (see
    gold_layer.GoldLayerRun.all_join_scores) - for a real data lake,
    hundreds of thousands of entries and tens of MB of JSON - and this
    endpoint's only consumer, the Automatic composition tab (see
    frontend/src/components/GoldLayer.jsx), never reads it. It exists
    purely for app/catalog.py's governance layering, which reads it
    straight off the persisted file via storage.load_gold_layer whenever
    a build finishes, never through this endpoint - so dropping it here
    doesn't affect that at all, it just stops the tab shipping (and the
    browser re-parsing) a multi-megabyte field nothing on screen uses
    every time it's opened."""
    active = projects.get_active_project()
    if active is None:
        return {"run": None}
    run = storage.load_gold_layer(active.id, mode)
    if run is not None:
        run = {k: v for k, v in run.items() if k != "all_join_scores"}
    return {"run": run}


class GoldTableRequestPayload(BaseModel):
    table_id: str


def _materialize_gold_table_df(project_id: str, table_id: str) -> pd.DataFrame:
    """Looks up + recomputes one gold table's row data, translating
    app/gold_layer.py's domain exceptions to the HTTP status codes this
    endpoint has always returned (404 for an unknown table/no persisted
    run, 500 for a DIADA failure, 409 for a vanished CF2 cluster)."""
    try:
        table = gold_layer.find_table(project_id, table_id)
    except gold_layer.GoldLayerRecomputeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return gold_layer.materialize_table_df(project_id, table)
    except diada.DiadaError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except gold_layer.GoldLayerRecomputeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/gold-layer/preview")
def preview_gold_table(
    payload: GoldTableRequestPayload,
    limit: int = Query(DEFAULT_PREVIEW_LIMIT, ge=1, le=MAX_PREVIEW_LIMIT),
    offset: int = Query(0, ge=0),
):
    active = projects.get_active_project()
    if active is None:
        raise HTTPException(status_code=400, detail="No active project")
    df = _materialize_gold_table_df(active.id, payload.table_id)
    window = df.iloc[offset : offset + limit]
    rows = window.astype(object).where(window.notna(), None).to_dict(orient="records")
    return {"columns": list(df.columns), "rows": rows, "total_rows": len(df), "offset": offset, "limit": limit}


@app.post("/api/gold-layer/download")
def download_gold_table(payload: GoldTableRequestPayload):
    active = projects.get_active_project()
    if active is None:
        raise HTTPException(status_code=400, detail="No active project")
    df = _materialize_gold_table_df(active.id, payload.table_id)

    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"{payload.table_id.replace('::', '_')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/datasets/{name}/preview")
def get_dataset_preview(
    name: str,
    limit: int = Query(DEFAULT_PREVIEW_LIMIT, ge=1, le=MAX_PREVIEW_LIMIT),
    offset: int = Query(0, ge=0),
    raw: bool = Query(False, description="Return unprocessed source data instead of preprocessed"),
):
    try:
        return data_loader.preview_dataset(name, limit=limit, offset=offset, raw=raw)
    except data_loader.DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Mounted last, at "/", so every /api/* route above still matches first -
# Starlette tries routes in registration order and a Mount is just a
# catch-all for whatever nothing earlier claimed. Only present in the
# Docker image (see FRONTEND_DIST_DIR's own docstring); a no-op in local
# dev, where frontend/dist was never built and the Vite dev server handles
# the frontend instead. html=True serves index.html for "/" - the app has
# no client-side router (tabs are just component state), so there's no
# other path that needs an SPA-style fallback to it.
if FRONTEND_DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST_DIR, html=True), name="frontend")
