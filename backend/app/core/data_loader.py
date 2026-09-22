"""
Stage 1: Load & preprocess.

Responsible for discovering raw CSV files, reading them, and applying light,
generic preprocessing so downstream stages (and the UI) see clean-ish data:
  - trims stray whitespace from string cells and headers
  - infers better dtypes than pandas' default (e.g. numeric-looking strings)
  - attempts to parse date-like columns (mixed formats are normalized)
  - reports null counts and duplicate row counts per dataset (data-quality
    signal for the analyst, nothing is silently dropped)

Nothing here mutates the source CSVs - these are read-only, in-memory
transforms applied when a dataset needs (re)computing. The *result* of that
computation (the schema profile) is persisted to disk via app/storage.py,
keyed to the active project (app/projects.py) - see profile_dataset() below.
"""
from __future__ import annotations

import functools
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import data_type, projects, quality, storage


class DatasetNotFoundError(Exception):
    pass


@dataclass
class DatasetProfile:
    name: str
    row_count: int
    column_count: int
    size_bytes: int
    columns: list[dict[str, Any]] = field(default_factory=list)
    duplicate_row_count: int = 0
    flags: list[dict[str, Any]] = field(default_factory=list)


def _active_project_dir() -> Path | None:
    project = projects.get_active_project()
    return Path(project.folder) if project else None


def list_csv_files() -> list[Path]:
    data_dir = _active_project_dir()
    if data_dir is None or not data_dir.exists():
        return []
    return sorted(data_dir.glob("*.csv"))


def _clean_column_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _strip_strings(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
        df[col] = df[col].replace({"": None, "nan": None, "None": None})
    return df


def _try_parse_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Best-effort: if a column name suggests a date and most values parse,
    normalize it to ISO (YYYY-MM-DD) strings. Source files intentionally mix
    date formats (%Y-%m-%d, %m/%d/%Y, %d-%m-%Y) to simulate messy raw data."""
    for col in df.columns:
        if not any(hint in col for hint in ("date", "_at", "signup")):
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
        # Only commit the conversion if we didn't blow away too much data.
        non_null_original = df[col].notna().sum()
        non_null_parsed = parsed.notna().sum()
        if non_null_original == 0:
            continue
        if non_null_parsed / max(non_null_original, 1) >= 0.9:
            df[col] = parsed.dt.strftime("%Y-%m-%d")
    return df


def _try_numeric_coercion(df: pd.DataFrame) -> pd.DataFrame:
    """Columns pandas read as object but that are actually numeric (common
    when a CSV has occasional blanks) get coerced to a proper numeric dtype."""
    for col in df.select_dtypes(include="object").columns:
        coerced = pd.to_numeric(df[col], errors="coerce")
        non_null_original = df[col].notna().sum()
        non_null_coerced = coerced.notna().sum()
        if non_null_original == 0:
            continue
        if non_null_coerced / max(non_null_original, 1) >= 0.95:
            df[col] = coerced
    return df


def preprocess(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the full light-preprocessing pipeline to a raw DataFrame."""
    df = df.copy()
    df.columns = [_clean_column_name(c) for c in df.columns]
    df = _strip_strings(df)
    df = _try_numeric_coercion(df)
    df = _try_parse_dates(df)
    return df


@functools.lru_cache(maxsize=None)
def _read_raw_cached(path_str: str, mtime: float) -> pd.DataFrame:
    """In-memory, this-process-only cache of the raw CSV read, keyed on file
    path + mtime. This is unrelated to the disk-persisted profiles below -
    even with this cache cold (e.g. right after a restart), previews and
    any (re)computation still need the actual rows at some point; this just
    avoids re-reading the same file twice in one process lifetime.

    encoding="utf-8-sig" strips a leading UTF-8 byte-order-mark if present
    (common in CSVs exported from Excel/Windows tools). Without it, the BOM
    bytes silently attach to the first column's header text - pandas' CSV
    parser then sees that field as starting with garbage instead of a quote
    character, so if the header was quoted (`"species"`) it stops treating
    it as quoted at all and the literal quote marks end up baked into the
    "column name" (e.g. 'ï»¿"species"'), which then breaks DuckDB SQL
    identifier quoting downstream (see ColumnProfiler._column_sql). Falls
    back to latin-1 (never raises a decode error) for files that aren't
    UTF-8 at all, rather than failing the whole import over an encoding
    guess."""
    try:
        return pd.read_csv(path_str, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path_str, dtype=str, keep_default_na=False, encoding="latin-1")


def _path_for(name: str) -> Path:
    data_dir = _active_project_dir()
    if data_dir is None:
        raise DatasetNotFoundError("No active project")
    path = data_dir / f"{name}.csv"
    if not path.exists():
        raise DatasetNotFoundError(f"No dataset named '{name}' in {data_dir}")
    return path


def get_dataset_path(name: str) -> Path:
    return _path_for(name)


def read_raw(name: str) -> pd.DataFrame:
    path = _path_for(name)
    df = _read_raw_cached(str(path), path.stat().st_mtime)
    return df.copy()


def read_preprocessed(name: str) -> pd.DataFrame:
    return preprocess(read_raw(name))


def dataset_names() -> list[str]:
    return [p.stem for p in list_csv_files()]


def _column_profile(series: pd.Series) -> dict[str, Any]:
    null_count = int(series.isna().sum()) if series.dtype != object else int(
        series.isna().sum() + (series == "").sum()
    )
    row_count = len(series)
    type_info = data_type.classify_column(series)
    flags = quality.evaluate_column(series, null_count, type_info["main_type"], type_info["secondary_type"])
    return {
        "name": series.name,
        "dtype": str(series.dtype),
        "main_type": type_info["main_type"],
        "secondary_type": type_info["secondary_type"],
        "null_count": null_count,
        "null_pct": round(null_count / max(row_count, 1) * 100, 1),
        "unique_count": int(series.nunique(dropna=True)),
        "sample_values": [str(v) for v in series.dropna().unique()[:3].tolist()],
        "flags": [asdict(f) for f in flags],
    }


def _compute_profile(name: str) -> DatasetProfile:
    raw = read_raw(name)
    clean = preprocess(raw)
    path = _path_for(name)
    columns = [_column_profile(clean[c]) for c in clean.columns]
    dataset_flags = quality.evaluate_dataset(len(clean), columns)
    return DatasetProfile(
        name=name,
        row_count=len(clean),
        column_count=len(clean.columns),
        size_bytes=path.stat().st_size,
        columns=columns,
        duplicate_row_count=int(clean.duplicated().sum()),
        flags=[asdict(f) for f in dataset_flags],
    )


def profile_dataset(name: str) -> DatasetProfile:
    """Loads the schema profile from disk (app/storage.py) if a fresh copy
    is already persisted for the active project; otherwise computes it
    (quality checks + type classification for every column) and persists
    the result before returning. Once persisted, this is a plain file read
    on every subsequent call - including across backend restarts - until
    the source CSV's mtime changes or the project's data is cleared.

    app/processing.py drives this to completion for every dataset in the
    background right after a project is created/activated, with progress
    reporting, so in the common case this is always a disk hit by the time
    the UI asks for it."""
    project = projects.get_active_project()
    if project is None:
        raise DatasetNotFoundError("No active project")
    path = _path_for(name)
    mtime = path.stat().st_mtime

    stored = storage.load_schema(project.id, name, mtime)
    if stored is not None:
        return DatasetProfile(**stored)

    profile = _compute_profile(name)
    storage.save_schema(project.id, name, mtime, asdict(profile))
    return profile


def preview_dataset(
    name: str, limit: int = 50, offset: int = 0, raw: bool = False
) -> dict[str, Any]:
    df = read_raw(name) if raw else read_preprocessed(name)
    total = len(df)
    window = df.iloc[offset : offset + limit]
    # Cast to object dtype first so None survives per-cell instead of being
    # coerced back to NaN by float64 columns, which breaks JSON serialization.
    records = window.astype(object).where(window.notna(), None).to_dict(orient="records")
    return {
        "name": name,
        "total_rows": total,
        "offset": offset,
        "limit": limit,
        "returned_rows": len(records),
        "columns": list(df.columns),
        "rows": records,
    }
