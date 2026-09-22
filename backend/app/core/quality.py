"""
Data-quality checks: column-level checks flag issues with a single column's
values; dataset-level checks flag issues with the dataset's shape as a
whole, including how many of its own columns are themselves flagged (a
dataset that's mostly unusable columns is a quality signal in its own
right).

Column checks are recomputed with plain pandas/numpy rather than reusing
column_profiler's DuckDB-based stats (which have the same top-1-coverage
and outlier numbers) on purpose: these flags are part of the eager schema
profile shown the moment a dataset is selected, and column_profiler's deep
profile is intentionally lazy/heavier. Spinning up DuckDB here would drag
that cost back into the eager path this project just added caching for.

Add a column check by writing a `check_*(...) -> QualityFlag | None`
function and registering it in evaluate_column(). Same pattern for dataset
checks in evaluate_dataset().
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class QualityFlag:
    type: str
    severity: str
    message: str


# ---- Column-level checks ---------------------------------------------------

INCOMPLETENESS_THRESHOLD = 0.95  # >95% null
TOP1_COVERAGE_THRESHOLD = 0.95  # >95% of non-null values are the same value
OUTLIER_THRESHOLD = 0.30  # >30% of rows are IQR outliers
UNUSABLE_SECONDARY_TYPES = {"textual", "array", "other"}


def check_all_null(column_name: str, null_count: int, row_count: int) -> QualityFlag | None:
    if row_count > 0 and null_count == row_count:
        return QualityFlag(
            type="all_null",
            severity="warning",
            message=f"'{column_name}' has no non-null values across {row_count} rows",
        )
    return None


def check_high_incompleteness(column_name: str, null_count: int, row_count: int) -> QualityFlag | None:
    if row_count == 0:
        return None
    null_pct = null_count / row_count
    if null_pct > INCOMPLETENESS_THRESHOLD:
        return QualityFlag(
            type="high_incompleteness",
            severity="warning",
            message=f"'{column_name}' is {null_pct * 100:.1f}% null",
        )
    return None


def check_high_top1_coverage(column_name: str, series: pd.Series, null_count: int, row_count: int) -> QualityFlag | None:
    non_null_count = row_count - null_count
    if non_null_count == 0:
        return None
    top_count = int(series.value_counts(dropna=True).iloc[0])
    coverage = top_count / non_null_count
    if coverage > TOP1_COVERAGE_THRESHOLD:
        return QualityFlag(
            type="high_top1_coverage",
            severity="warning",
            message=f"'{column_name}' is {coverage * 100:.1f}% a single repeated value",
        )
    return None


def check_high_outliers(column_name: str, series: pd.Series, main_type: str, row_count: int) -> QualityFlag | None:
    if main_type != "numeric" or row_count == 0:
        return None
    numeric = series.dropna()
    if len(numeric) < 4:  # need at least a handful of points for IQR to mean anything
        return None
    q1, q3 = numeric.quantile(0.25), numeric.quantile(0.75)
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    outlier_count = int(((numeric < lower) | (numeric > upper)).sum())
    pct = outlier_count / row_count
    if pct > OUTLIER_THRESHOLD:
        return QualityFlag(
            type="high_outliers",
            severity="warning",
            message=f"'{column_name}' has {pct * 100:.1f}% values outside the IQR fence",
        )
    return None


def check_unusable_type(column_name: str, secondary_type: str) -> QualityFlag | None:
    if secondary_type in UNUSABLE_SECONDARY_TYPES:
        return QualityFlag(
            type="unusable_type",
            severity="warning",
            message=f"'{column_name}' is classified as '{secondary_type}' - not directly usable without extra handling",
        )
    return None


def evaluate_column(series: pd.Series, null_count: int, main_type: str, secondary_type: str) -> list[QualityFlag]:
    row_count = len(series)
    column_name = series.name
    checks = [
        check_all_null(column_name, null_count, row_count),
        check_high_incompleteness(column_name, null_count, row_count),
        check_high_top1_coverage(column_name, series, null_count, row_count),
        check_high_outliers(column_name, series, main_type, row_count),
        check_unusable_type(column_name, secondary_type),
    ]
    return [flag for flag in checks if flag is not None]


# ---- Dataset-level checks ---------------------------------------------------

MIN_COLUMNS = 2  # fewer than this and there's nothing to join/relate
MIN_ROWS = 20  # below this, most statistics (and these very checks) are unreliable
MIN_VALID_COLUMNS = 2  # "valid" = has no column-level flags at all


def check_too_few_columns(column_count: int) -> QualityFlag | None:
    if column_count < MIN_COLUMNS:
        return QualityFlag(
            type="too_few_columns",
            severity="warning",
            message=f"Dataset has only {column_count} column(s)",
        )
    return None


def check_too_few_rows(row_count: int) -> QualityFlag | None:
    if row_count < MIN_ROWS:
        return QualityFlag(
            type="too_few_rows",
            severity="warning",
            message=f"Dataset has only {row_count} row(s)",
        )
    return None


def check_too_few_valid_columns(columns: list[dict]) -> QualityFlag | None:
    valid_count = sum(1 for c in columns if not c["flags"])
    if valid_count < MIN_VALID_COLUMNS:
        return QualityFlag(
            type="too_few_valid_columns",
            severity="warning",
            message=f"Only {valid_count} of {len(columns)} column(s) have no quality flags",
        )
    return None


def evaluate_dataset(row_count: int, columns: list[dict]) -> list[QualityFlag]:
    checks = [
        check_too_few_columns(len(columns)),
        check_too_few_rows(row_count),
        check_too_few_valid_columns(columns),
    ]
    return [flag for flag in checks if flag is not None]
