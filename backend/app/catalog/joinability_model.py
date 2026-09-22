"""
Join discovery: given a "base" column, rank every usable column from every
*other* dataset in the active project's usable graph (app/catalog.py) by
predicted joinability. Other columns of the base column's own dataset are
excluded entirely - a join is between two datasets, so same-table
candidates aren't a meaningful result here.

Two steps, ported from an earlier project:
  1. Distance computation (was core/column/distancer.py) - for each of the
     model's input features, compute a distance between the base column's
     profile value and every candidate's, using the rule the feature was
     tagged with when it was defined (column_profiler.metrics.Metric.
     distance_pattern): numeric difference ("substraction"), Jaccard-style
     overlap of the two columns' top-10 common values ("containment"), or
     edit distance between representative values ("levenshtein").
     Vectorized where the original was: substraction is one numpy op over
     every candidate at once; containment/levenshtein still iterate
     per-candidate (same as the source), but at real-world candidate counts
     (tens to low hundreds of columns) that's not the bottleneck.
  2. A pre-trained gradient boosting regressor (was
     core/joinability_model/get_ranking.py) turns those per-feature
     distances into a single joinability score per candidate. Predicting
     with an already-fitted small GBR (100 shallow trees) over a few dozen
     rows is sub-millisecond; loading the ~140KB model file happens once,
     at import time.

The model's own feature_names_in_ - not a separate config - is the source
of truth for which distance features get computed at all: DISTANCE_PATTERNS
below is every known profile metric's distance rule, filtered down to just
the ones this particular model was trained on.

Candidates are also restricted to the base column's main_type (numeric vs
string, see app/data_type.py) before distances are even computed - a
customer_id integer and a product-name string aren't meaningful join
candidates for each other regardless of what the distance features say,
and this model's feature set (word/length stats, common-value overlap) was
built with string-typed join keys in mind. Numeric-to-numeric matching is
still supported (it's just not the primary use case), so this restriction
still leaves same-type candidates in play - it only removes cross-type
noise.
"""
from __future__ import annotations

import warnings
from typing import Any

import Levenshtein
import numpy as np
import pandas as pd

from . import catalog
from ..column_profiler import get_column_profiles
from ..column_profiler.metrics import (
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
from ..core.config import JOINABILITY_MODEL_PATH


class NotUsableColumnError(Exception):
    pass


def _load_model():
    import joblib
    from sklearn.exceptions import InconsistentVersionWarning

    with warnings.catch_warnings():
        # Model was trained under an older scikit-learn than this app runs;
        # it loads and predicts fine under the current one (verified by
        # hand against feature_names_in_ and a sample prediction), it just
        # warns on unpickle. Silencing only this specific warning class so
        # a genuinely broken unpickle still surfaces normally.
        warnings.simplefilter("ignore", category=InconsistentVersionWarning)
        return joblib.load(JOINABILITY_MODEL_PATH)


_MODEL = _load_model()


def _score_bounds(model) -> tuple[float, float]:
    """The true global min/max this GradientBoostingRegressor can ever
    output, for any input whatsoever - not just the range seen in one
    project's results, which is why the raw scores look like an arbitrary,
    hard-to-read range (typically a small fraction of a percent) to users.

    A GBR's prediction is init_.constant_ + learning_rate * sum(one leaf
    value per tree). Within a single regression tree, every internal
    node's value is the (weighted) mean of its children's values, so by
    induction the tree's own min/max is always attained at a leaf - taking
    max()/min() over every node's value (leaves and internal alike) is
    therefore already exactly the tree's true min/max, no leaf-filtering
    needed. And because every tree only ever splits on "feature <=
    threshold", an arbitrarily extreme feature value still lands in one of
    the tree's existing extreme leaves - so summing each tree's own
    min/max independently gives this model's true global bounds for *any*
    input, not merely an approximation over the training data's range.
    """
    init_constant = float(model.init_.constant_.ravel()[0])
    max_leaf_sum = sum(estimator[0].tree_.value.max() for estimator in model.estimators_)
    min_leaf_sum = sum(estimator[0].tree_.value.min() for estimator in model.estimators_)
    return (
        init_constant + model.learning_rate * min_leaf_sum,
        init_constant + model.learning_rate * max_leaf_sum,
    )


SCORE_MIN, SCORE_MAX = _score_bounds(_MODEL)

_ALL_METRIC_GROUPS = [
    CardinalityMetrics(), DistributionMetrics(), CommonValuesMetrics(), LengthMetrics(),
    WordCountMetrics(), BoundaryMetrics(), ColumnFlagsMetrics(), CoverageMetrics(), NumericalMetrics(),
]
_ALL_DISTANCE_PATTERNS = {
    metric.name: metric.distance_pattern
    for group in _ALL_METRIC_GROUPS
    for metric in group.metrics_list
}

MODEL_FEATURES: list[str] = list(_MODEL.feature_names_in_)
# "name_dist" isn't a profile metric - it's the Levenshtein distance
# between the two column *names*, computed directly in compute_distances().
DISTANCE_PATTERNS: dict[str, str] = {
    f: _ALL_DISTANCE_PATTERNS[f] for f in MODEL_FEATURES if f in _ALL_DISTANCE_PATTERNS
}


def _levenshtein(a: Any, b: Any) -> float:
    if a is None and b is None:
        return float("inf")
    if a is None:
        return len(str(b))
    if b is None:
        return len(str(a))
    return Levenshtein.distance(str(a), str(b))


def compute_distances(base_profile: dict[str, Any], candidates: pd.DataFrame) -> pd.DataFrame:
    """One row per candidate, one column per distance feature the model uses."""
    result = pd.DataFrame(index=candidates.index)

    for feature, pattern in DISTANCE_PATTERNS.items():
        query_value = base_profile.get(feature)
        column_values = candidates[feature]

        if pattern == "substraction":
            result[feature] = np.where(
                pd.isna(column_values) | pd.isna(query_value), 0, query_value - column_values
            )
        elif pattern == "containment":
            # Jaccard-style overlap of two columns' top-10 common values.
            query_set = set(query_value) if query_value else set()
            result[feature] = column_values.apply(
                lambda x: len(query_set & set(x)) / len(query_set) if query_set and x else 0.0
            )
        elif pattern == "levenshtein":
            result[feature] = column_values.apply(lambda x: _levenshtein(query_value, x))

    result["name_dist"] = candidates["column"].apply(lambda x: _levenshtein(base_profile["column"], x))
    return result


def rank_candidates(base_profile: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ranks candidate column profiles by predicted joinability with the
    base column profile. Returns every candidate, best (highest score)
    first."""
    if not candidates:
        return []

    candidates_df = pd.DataFrame(candidates)
    distances = compute_distances(base_profile, candidates_df)
    # Defensive: the "substraction" pattern already maps NaN to 0 above, and
    # containment/levenshtein shouldn't produce NaN given usable-graph-only
    # inputs, but the model can't handle NaN at all if any slips through.
    features = distances[MODEL_FEATURES].fillna(0.0)

    scores = _MODEL.predict(features)

    ranked = pd.DataFrame({
        "dataset": candidates_df["dataset"].values,
        "column": candidates_df["column"].values,
        "score": scores,
    }).sort_values("score", ascending=False)

    return ranked.to_dict(orient="records")


def find_join_candidates(
    project_id: str, base_dataset: str, base_column: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Public entry point: validates the base column is actually in the
    project's usable graph, gathers every usable column from every *other*
    dataset's deep profile (already persisted - see app/processing.py - so
    this is disk reads, not recomputation; the base dataset's other columns
    are excluded entirely, not just the base column itself - a join is
    between two datasets, so same-table candidates aren't meaningful
    results here), restricts what's left to the base column's main_type,
    and ranks it against the base column.

    Returns (base_profile, ranked_candidates) - the base profile is handed
    back so callers can tell the user when it's numeric (this model's
    intended use is string-typed join keys; numeric-to-numeric matching is
    supported but secondary).

    Raises NotUsableColumnError if the base column doesn't exist or was
    filtered out of the usable graph by a quality flag."""
    usable_columns = catalog.usable_columns_by_dataset(project_id)
    if base_column not in usable_columns.get(base_dataset, set()):
        raise NotUsableColumnError(
            f"'{base_dataset}.{base_column}' isn't in the usable graph - "
            "it's either missing or was filtered out by a quality flag"
        )

    base_profile = None
    for profile in get_column_profiles(base_dataset):
        if profile["column"] == base_column:
            base_profile = profile
            break
    if base_profile is None:
        raise NotUsableColumnError(f"Could not load a profile for '{base_dataset}.{base_column}'")

    # Every other column of the base dataset is excluded too, not just the
    # base column itself - a join is between two datasets, so candidates
    # from the same table aren't meaningful results here.
    candidates = []
    for dataset_name, column_names in usable_columns.items():
        if dataset_name == base_dataset:
            continue
        for profile in get_column_profiles(dataset_name):
            if profile["column"] in column_names:
                candidates.append(profile)

    same_type_candidates = [c for c in candidates if c["main_type"] == base_profile["main_type"]]
    return base_profile, rank_candidates(base_profile, same_type_candidates)
