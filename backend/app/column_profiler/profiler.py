"""
Deep, per-column statistical profiling (cardinality, distribution, length,
word counts, boundaries, coverage, outliers, common values) computed with
DuckDB SQL aggregates over the already-preprocessed dataset.

This is much heavier than the lightweight schema/quality profile in
data_loader.py (quantiles, entropy, skewness/kurtosis, IQR outliers, common
values with soundex). It's computed once, up front, alongside the schema
profile - see app/processing.py, which drives both to completion for every
dataset right after a project is created/activated - and persisted to disk
(app/storage.py) so it's a plain file read afterward, including across
backend restarts, until the source CSV changes or the project's data is
cleared.

Integration differs from the original version this was ported from: rather
than uploading CSVs into a persistent DuckDB file and querying it with one
query per metric group, the already-preprocessed pandas DataFrame is
materialized into a throwaway in-memory DuckDB table per computation, and
all of a dataset's columns are profiled concurrently (DuckDB releases the
GIL during query execution, and each worker gets its own cursor onto the
same table - cursors share the catalog, so this is safe).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb
import pandas as pd

from ..core import data_loader, data_type, projects, storage
from .metrics import (
    BoundaryMetrics,
    CardinalityMetrics,
    ColumnFlagsMetrics,
    CommonValuesMetrics,
    CoverageMetrics,
    DistributionMetrics,
    LengthMetrics,
    NumericalMetrics,
    WordCountMetrics,
)

_TABLE = "profile_target"
_MAX_WORKERS = 8


def _round_floats(obj: Any, decimals: int = 6) -> Any:
    if isinstance(obj, float):
        return round(obj, decimals)
    if isinstance(obj, dict):
        return {k: _round_floats(v, decimals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(i, decimals) for i in obj]
    return obj


class ColumnProfiler:
    def __init__(self, cursor: duckdb.DuckDBPyConnection, dataset_name: str, column_name: str, series: pd.Series):
        self.cursor = cursor
        self.dataset_name = dataset_name
        self.column_name = column_name
        # DuckDB (like standard SQL) escapes an embedded double-quote inside
        # a quoted identifier by doubling it. metrics.py's queries already
        # wrap {column} in literal quotes, so pre-escaping the value passed
        # in is the one place this needs handling - it covers any column
        # name containing a literal quote character, whatever put it there
        # (a mis-parsed BOM merging into the first header - see
        # data_loader._read_raw_cached - or just unusual source data),
        # instead of that breaking SQL generation.
        self._column_sql = column_name.replace('"', '""')
        self.row_count = len(series)
        type_info = data_type.classify_column(series)
        self.main_type = type_info["main_type"]
        self.profile: dict[str, Any] = {
            "dataset": dataset_name,
            "column": column_name,
            "column_type": str(series.dtype),
            "main_type": self.main_type,
            "secondary_type": type_info["secondary_type"],
        }

    def run(self) -> dict[str, Any]:
        self.profile.update(self._cardinality())
        self.profile.update(self._distribution())
        self.profile.update(self._coverage())
        self.profile.update(self._common_values())
        self.profile.update(self._boundary())
        self.profile.update(self._column_flags())
        # Length/word-count are computed for every column regardless of
        # type (DuckDB casts to VARCHAR internally either way) - the
        # joinability model (app/joinability_model.py) needs these as
        # features for numeric columns too, to compare them against
        # string columns and against each other.
        self.profile.update(self._length())
        self.profile.update(self._word_count())

        if self.main_type == "numeric":
            self.profile.update(self._numerical())

        return _round_floats(self.profile)

    def _execute_dict(self, query: str) -> dict[str, Any]:
        cursor = self.cursor.execute(query)
        row = cursor.fetchone()
        if row is None:
            return {}
        return {desc[0]: value for desc, value in zip(cursor.description, row)}

    def _cardinality(self) -> dict[str, Any]:
        metrics = CardinalityMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result, self.row_count)

    def _distribution(self) -> dict[str, Any]:
        metrics = DistributionMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        if result.get("frequency_avg") is None:
            # Column has zero non-null values, so the inner GROUP BY has no
            # groups and every aggregate came back NULL - nothing to divide.
            return metrics.empty_result()
        return metrics.process_result(result, self.row_count)

    def _common_values(self) -> dict[str, Any]:
        metrics = CommonValuesMetrics()
        rows = self.cursor.execute(metrics.build_query(self._column_sql, _TABLE)).fetchall()
        return metrics.process_result(rows)

    def _boundary(self) -> dict[str, Any]:
        metrics = BoundaryMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result)

    def _length(self) -> dict[str, Any]:
        metrics = LengthMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result)

    def _word_count(self) -> dict[str, Any]:
        metrics = WordCountMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result)

    def _coverage(self) -> dict[str, Any]:
        metrics = CoverageMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result)

    def _numerical(self) -> dict[str, Any]:
        metrics = NumericalMetrics()
        result = self._execute_dict(metrics.build_query(self._column_sql, _TABLE))
        return metrics.process_result(result)

    def _column_flags(self) -> dict[str, Any]:
        metrics = ColumnFlagsMetrics()
        row = self.cursor.execute(metrics.build_query(self._column_sql, _TABLE)).fetchone()
        return metrics.process_result(row)


def _profile_one(cursor, dataset_name: str, column_name: str, series: pd.Series) -> dict[str, Any]:
    return ColumnProfiler(cursor, dataset_name, column_name, series).run()


def _compute(dataset_name: str) -> list[dict[str, Any]]:
    df = data_loader.read_preprocessed(dataset_name)
    con = duckdb.connect(":memory:")
    try:
        con.register("df_src", df)
        con.execute(f'CREATE TABLE "{_TABLE}" AS SELECT * FROM df_src')
        con.unregister("df_src")

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(1, len(df.columns)))) as pool:
            futures = [
                pool.submit(_profile_one, con.cursor(), dataset_name, col, df[col])
                for col in df.columns
            ]
            return [f.result() for f in futures]
    finally:
        con.close()


def get_column_profiles(dataset_name: str) -> list[dict[str, Any]]:
    """Loads the deep profile from disk if a fresh copy is already
    persisted for the active project; otherwise computes it and persists
    the result before returning. In the normal flow app/processing.py has
    already done this for every dataset by the time the UI asks, so this
    is a disk read; the compute-on-miss path is a fallback (e.g. this
    dataset wasn't part of the last processing pass), not the common case.

    Raises data_loader.DatasetNotFoundError if the dataset doesn't exist or
    there's no active project."""
    project = projects.get_active_project()
    if project is None:
        raise data_loader.DatasetNotFoundError("No active project")
    path = data_loader.get_dataset_path(dataset_name)
    mtime = path.stat().st_mtime

    stored = storage.load_deep_profile(project.id, dataset_name, mtime)
    if stored is not None:
        return stored

    profile = _compute(dataset_name)
    storage.save_deep_profile(project.id, dataset_name, mtime, profile)
    return profile
