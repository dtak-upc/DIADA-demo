"""
Multi-way join: combine a base dataset with one or more candidate datasets
(picked from join discovery - see app/joinability_model.py) on their
respective key columns, via DuckDB.

Every candidate joins against the same base_column by default, but a
candidate can override that via its own JoinCandidateSpec.base_column - so
one call can join different candidates on different columns of the base
dataset (app/gold_layer.py needs this: it tries every string column of a
base dataset as a discovery key, so two candidates picked from the same run
may have been discovered through two different base columns).

Two things are handled automatically, both configurable:
  - "granularity": if a candidate's key column isn't unique, joining
    naively would fan extra rows into the base (each base row could match
    more than one candidate row). To keep the join at one output row per
    base row, a non-unique candidate is aggregated first (GROUP BY the key
    column) - every other column in that candidate gets collapsed to one
    value per key via an aggregation function (default: mean for numeric
    columns, most-common-value for string columns; user-overridable per
    column - see NUMERIC_AGGREGATIONS/STRING_AGGREGATIONS). DuckDB's native
    aggregates (AVG, MODE, MEDIAN, ...) do this in one vectorized pass per
    candidate instead of a slow per-group Python loop - that's what "make
    the aggregation fast" means in practice here.
  - name collisions: once several datasets' columns land in one flat
    result, two might share a name. The base dataset's columns are never
    renamed; a colliding candidate column becomes "<column>__<dataset>".

Every result column's provenance (which dataset it actually came from) is
tracked as it's built - PreparedJoin.column_provenance - rather than
inferred after the fact from renamed_columns; callers (main.py's join/curate
endpoints, app/gold_layer.py) use it to show "columns by source" and to
color-code preview tables by origin dataset in the UI. column_provenance is
paired with original_columns (result column -> its original name in that
source dataset, before any collision-renaming) so a caller can look its
uniqueness up in its own dataset's profile - see unused_primary_key_columns
below, which uses exactly that to flag columns like a second country-code
column that rode along on a join performed through country name: a genuine
primary key of its own dataset, just not the one anything was actually
joined on, so it adds no information a model could use to predict with.

Nothing here is persisted - a join is computed fresh on every preview or
download request (see main.py), same as dataset previews elsewhere in the
app. All the actual join/aggregation work happens inside DuckDB, not in a
Python loop over rows, so recomputing on each request stays fast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb
import pandas as pd

from .catalog import catalog
from .core import data_loader
from .core.sql_utils import quote_ident

NUMERIC_AGGREGATIONS = ["mean", "sum", "min", "max", "median"]
STRING_AGGREGATIONS = ["most_common", "first", "count_distinct"]
DEFAULT_AGGREGATION = {"numeric": "mean", "string": "most_common"}

_AGG_SQL = {
    "mean": "AVG({c})",
    "sum": "SUM({c})",
    "min": "MIN({c})",
    "max": "MAX({c})",
    "median": "MEDIAN({c})",
    "most_common": "MODE({c})",
    "first": "FIRST({c})",
    "count_distinct": "COUNT(DISTINCT {c})",
}


class JoinError(Exception):
    pass


@dataclass
class JoinCandidateSpec:
    dataset: str
    column: str
    base_column: str | None = None  # overrides the join's base_column for this candidate only; None = use the shared one
    aggregations: dict[str, str] = field(default_factory=dict)  # column -> function, overrides only


@dataclass
class PreparedJoin:
    con: duckdb.DuckDBPyConnection
    columns: list[str]
    total_rows: int
    aggregated_datasets: list[str]
    renamed_columns: dict[str, str]
    column_provenance: dict[str, str]  # result column name -> source dataset
    original_columns: dict[str, str]  # result column name -> its original name in that source dataset

    def close(self) -> None:
        self.con.close()


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series)


def _agg_sql(func: str, quoted_col: str) -> str:
    template = _AGG_SQL.get(func)
    if template is None:
        raise JoinError(f"Unknown aggregation function '{func}'")
    return template.format(c=quoted_col)


def _validate_aggregation(func: str, column: str, is_numeric: bool) -> None:
    valid = NUMERIC_AGGREGATIONS if is_numeric else STRING_AGGREGATIONS
    if func not in valid:
        kind = "numeric" if is_numeric else "string"
        raise JoinError(
            f"'{func}' isn't a valid aggregation for {kind} column '{column}' (expected one of {valid})"
        )


def _materialize(con: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame) -> None:
    src_name = f"{table_name}_src"
    con.register(src_name, df)
    con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM {src_name}")
    con.unregister(src_name)


def prepare_join(
    project_id: str,
    base_dataset: str,
    base_column: str,
    candidates: list[JoinCandidateSpec],
    base_df: pd.DataFrame | None = None,
    base_column_provenance: dict[str, str] | None = None,
    base_original_columns: dict[str, str] | None = None,
) -> PreparedJoin:
    """Validates everything (usable-graph membership, distinct candidate
    datasets), builds the join in a fresh in-memory DuckDB connection, and
    returns it as a queryable "joined" view - caller must call .close() on
    the result (or use it as a context via `with`... it isn't one, just
    call .close() in a finally) when done with preview/download.

    base_df/base_column_provenance/base_original_columns (all None by
    default, and every existing caller leaves them that way) let a caller
    use an *already-joined* DataFrame as the base instead of loading
    base_dataset fresh from disk - app/gold_layer.py's grouped mode needs
    this to attach one more dataset onto a group that's already been joined
    from several others, where "the base" isn't a literal single dataset
    file anymore. When given, base_column is validated against base_df's
    own columns (not catalog.usable_columns_by_dataset(base_dataset), which
    wouldn't know about a multi-dataset frame), and column_provenance/
    original_columns are seeded from the passed-in dicts - which the caller
    must have carried forward cumulatively from every earlier step, not just
    the immediately-preceding one, so every column keeps resolving to its
    true origin dataset however many hops it took to get here."""
    if not candidates:
        raise JoinError("Pick at least one candidate dataset to join.")

    usable = catalog.usable_columns_by_dataset(project_id)

    if base_df is not None:
        if base_column not in base_df.columns:
            raise JoinError(f"'{base_column}' isn't a column of the given base frame.")
        base_usable = set(base_df.columns)  # any of the frame's own columns is a valid per-candidate override target
        # Defense in depth: base_column_provenance already names every real
        # dataset folded into this frame so far - reject re-joining any of
        # them, not just the literal (here, largely nominal) base_dataset
        # label, in case a caller's own traversal logic has a bug.
        seen_datasets = set((base_column_provenance or {}).values()) | {base_dataset}
    else:
        base_usable = usable.get(base_dataset, set())
        if base_column not in base_usable:
            raise JoinError(f"'{base_dataset}.{base_column}' isn't a usable column.")
        seen_datasets = {base_dataset}

    for spec in candidates:
        if spec.dataset == base_dataset:
            raise JoinError(f"Candidate dataset '{spec.dataset}' can't be the same as the base dataset.")
        if spec.dataset in seen_datasets:
            raise JoinError(f"Candidate dataset '{spec.dataset}' was specified more than once.")
        seen_datasets.add(spec.dataset)
        if spec.column not in usable.get(spec.dataset, set()):
            raise JoinError(f"'{spec.dataset}.{spec.column}' isn't a usable column.")
        if spec.base_column is not None and spec.base_column not in base_usable:
            raise JoinError(f"'{spec.base_column}' isn't a usable base column.")

    con = duckdb.connect(":memory:")
    try:
        if base_df is None:
            base_df = data_loader.read_preprocessed(base_dataset)
        _materialize(con, "base_table", base_df)

        result_columns = list(base_df.columns)
        select_parts = [f"base_table.{quote_ident(c)}" for c in base_df.columns]
        join_clauses = []
        aggregated_datasets = []
        renamed_columns = {}
        column_provenance = dict(base_column_provenance) if base_column_provenance is not None else {c: base_dataset for c in base_df.columns}
        original_columns = dict(base_original_columns) if base_original_columns is not None else {c: c for c in base_df.columns}

        for i, spec in enumerate(candidates):
            try:
                cand_df = data_loader.read_preprocessed(spec.dataset)
            except data_loader.DatasetNotFoundError as e:
                raise JoinError(str(e)) from e
            if spec.column not in cand_df.columns:
                raise JoinError(f"Column '{spec.column}' not found in '{spec.dataset}'")

            table_name = f"cand{i}"
            _materialize(con, table_name, cand_df)

            other_cols = [c for c in cand_df.columns if c != spec.column]
            row_count = len(cand_df)
            distinct_count = int(cand_df[spec.column].nunique(dropna=True))
            needs_agg = distinct_count != row_count

            key_q = quote_ident(spec.column)
            prepared_name = f"{table_name}_prepared"

            if needs_agg:
                aggregated_datasets.append(spec.dataset)
                select_cols = [f"{key_q} AS join_key"]
                for col in other_cols:
                    is_num = _is_numeric(cand_df[col])
                    func = spec.aggregations.get(col) or DEFAULT_AGGREGATION["numeric" if is_num else "string"]
                    _validate_aggregation(func, col, is_num)
                    select_cols.append(f"{_agg_sql(func, quote_ident(col))} AS {quote_ident(col)}")
                sql = (
                    f"CREATE TABLE {prepared_name} AS "
                    f"SELECT {', '.join(select_cols)} FROM {table_name} GROUP BY {key_q}"
                )
            else:
                select_cols = [f"{key_q} AS join_key"] + [quote_ident(c) for c in other_cols]
                sql = f"CREATE TABLE {prepared_name} AS SELECT {', '.join(select_cols)} FROM {table_name}"
            con.execute(sql)

            for col in other_cols:
                out_name = col
                if out_name in result_columns:
                    out_name = f"{col}__{spec.dataset}"
                    renamed_columns[f"{spec.dataset}.{col}"] = out_name
                result_columns.append(out_name)
                column_provenance[out_name] = spec.dataset
                original_columns[out_name] = col
                select_parts.append(f"{prepared_name}.{quote_ident(col)} AS {quote_ident(out_name)}")

            base_col_q = quote_ident(spec.base_column or base_column)
            join_clauses.append(f"LEFT JOIN {prepared_name} ON base_table.{base_col_q} = {prepared_name}.join_key")

        final_sql = (
            f"CREATE VIEW joined AS SELECT {', '.join(select_parts)} FROM base_table {' '.join(join_clauses)}"
        )
        con.execute(final_sql)
        total_rows = con.execute("SELECT COUNT(*) FROM joined").fetchone()[0]

        return PreparedJoin(
            con=con,
            columns=result_columns,
            total_rows=total_rows,
            aggregated_datasets=aggregated_datasets,
            renamed_columns=renamed_columns,
            column_provenance=column_provenance,
            original_columns=original_columns,
        )
    except Exception:
        con.close()
        raise


def to_dataframe(prepared: PreparedJoin) -> pd.DataFrame:
    return prepared.con.execute("SELECT * FROM joined").fetch_df()


def attach_prepared_candidate(
    base_dataset: str,
    base_column: str,
    base_df: pd.DataFrame,
    base_column_provenance: dict[str, str],
    base_original_columns: dict[str, str],
    candidate_key_column: str,
    candidate_df: pd.DataFrame,
    candidate_column_provenance: dict[str, str],
    candidate_original_columns: dict[str, str],
    aggregations: dict[str, str] | None = None,
) -> PreparedJoin:
    """Like prepare_join, but for attaching a *candidate* that's already an
    arbitrary, possibly multi-dataset frame (its own column_provenance/
    original_columns already tracked) rather than one named raw dataset
    loaded fresh from disk - prepare_join's own candidate loop always does
    the latter (`data_loader.read_preprocessed(spec.dataset)`), which is
    exactly what a caller needs when merging two already-built assets
    together (app/ad_hoc_layer.py's growth algorithm) rather than
    attaching one fresh leaf.

    Same two things handled automatically as prepare_join: aggregation if
    candidate_key_column isn't unique within candidate_df (using each
    column's own true source, from candidate_column_provenance, to pick a
    sensible default aggregation function), and collision-renaming
    (`col__<real source dataset>`, same convention as prepare_join,
    generalized from "the one candidate dataset" to "whichever real
    dataset that specific column actually came from").

    base_df is always required here (never loaded fresh) - this function
    exists specifically for the two-composites-already-exist case, where
    there's no single "base_dataset" name to load."""
    if candidate_key_column not in candidate_df.columns:
        raise JoinError(f"'{candidate_key_column}' isn't a column of the given candidate frame.")

    con = duckdb.connect(":memory:")
    try:
        _materialize(con, "base_table", base_df)
        result_columns = list(base_df.columns)
        select_parts = [f"base_table.{quote_ident(c)}" for c in base_df.columns]
        column_provenance = dict(base_column_provenance)
        original_columns = dict(base_original_columns)

        # Unlike prepare_join (which drops the candidate's own key column -
        # redundant with the base's matching column in a curated result),
        # the key column is kept here as a normal output column: this
        # function's only caller, ad_hoc_layer.py's growth algorithm, needs
        # every (dataset, column) it ever attached to stay resolvable later
        # (_resolve_real_column) - a later turn can genuinely rediscover
        # the exact same underlying relationship (e.g. two different fact
        # tables both referencing the same dimension's key) after the first
        # attach already consumed that column as a join key, and dropping
        # it would make it permanently unresolvable, crashing instead of
        # correctly attaching. Any actually-redundant copy this leaves
        # behind is exactly what declutter_columns already exists to strip
        # back out during final curation - see _declutter_asset.
        other_cols = list(candidate_df.columns)
        row_count = len(candidate_df)
        distinct_count = int(candidate_df[candidate_key_column].nunique(dropna=True))
        needs_agg = distinct_count != row_count

        _materialize(con, "cand", candidate_df)
        key_q = quote_ident(candidate_key_column)
        aggregations = aggregations or {}
        aggregated_datasets: list[str] = []

        if needs_agg:
            aggregated_datasets = sorted({
                candidate_column_provenance[c] for c in other_cols if c != candidate_key_column
            })
            select_cols = [f"{key_q} AS join_key"]
            for col in other_cols:
                if col == candidate_key_column:
                    # The group's own key - deterministic per group, no
                    # aggregation function needed.
                    select_cols.append(f"{key_q} AS {quote_ident(col)}")
                    continue
                is_num = _is_numeric(candidate_df[col])
                func = aggregations.get(col) or DEFAULT_AGGREGATION["numeric" if is_num else "string"]
                _validate_aggregation(func, col, is_num)
                select_cols.append(f"{_agg_sql(func, quote_ident(col))} AS {quote_ident(col)}")
            sql = f"CREATE TABLE cand_prepared AS SELECT {', '.join(select_cols)} FROM cand GROUP BY {key_q}"
        else:
            select_cols = [f"{key_q} AS join_key"] + [quote_ident(c) for c in other_cols]
            sql = f"CREATE TABLE cand_prepared AS SELECT {', '.join(select_cols)} FROM cand"
        con.execute(sql)

        renamed_columns: dict[str, str] = {}
        for col in other_cols:
            source = candidate_column_provenance[col]
            out_name = col
            if out_name in result_columns:
                out_name = f"{col}__{source}"
                renamed_columns[f"{source}.{candidate_original_columns[col]}"] = out_name
            result_columns.append(out_name)
            column_provenance[out_name] = source
            original_columns[out_name] = candidate_original_columns[col]
            select_parts.append(f"cand_prepared.{quote_ident(col)} AS {quote_ident(out_name)}")

        base_col_q = quote_ident(base_column)
        final_sql = (
            f"CREATE VIEW joined AS SELECT {', '.join(select_parts)} FROM base_table "
            f"LEFT JOIN cand_prepared ON base_table.{base_col_q} = cand_prepared.join_key"
        )
        con.execute(final_sql)
        total_rows = con.execute("SELECT COUNT(*) FROM joined").fetchone()[0]

        return PreparedJoin(
            con=con,
            columns=result_columns,
            total_rows=total_rows,
            aggregated_datasets=aggregated_datasets,
            renamed_columns=renamed_columns,
            column_provenance=column_provenance,
            original_columns=original_columns,
        )
    except Exception:
        con.close()
        raise


@dataclass
class ChainStep:
    """One candidate to attach, plus *where* it attaches from - unlike
    JoinCandidateSpec's base_column (a column of whatever single base this
    call already has in hand), base_column/base_column_dataset here name a
    column by its real, original identity (dataset + column, before any
    collision-renaming) so build_chain can resolve it in the accumulated
    frame however many steps deep it already is - the same identity pair
    app/gold_layer.py's JoinCandidate carries for its own incremental
    group-building (_build_group_dataframe), just without a discovery
    `score` (a chain step is user-picked, not ranked)."""
    dataset: str
    column: str
    base_column: str
    base_column_dataset: str
    aggregations: dict[str, str] = field(default_factory=dict)


def build_chain(
    project_id: str, root: str, steps: list[ChainStep]
) -> tuple[pd.DataFrame, dict[str, str], dict[str, str], list[str], dict[str, str], list[str]]:
    """One join, built from an arbitrary sequence of attach steps rather
    than a single shared base column: each step names its own
    (base_column_dataset, base_column) to attach from, which may be the
    root itself (any number of steps, each via a different root column -
    "multi-attribute" join) or any dataset a *previous* step already
    folded in (an "iterative" join, one hop further from what's been built
    so far). Every step is applied one at a time via prepare_join, each
    time resolving its base_column_dataset/base_column against whatever
    the frame currently looks like (resolving to the root's own column
    verbatim for the very first step, or via column_provenance/
    original_columns - carried forward step to step - for every step
    after) and threading the result forward as the next step's base_df.
    This is deliberately never batched (even when several consecutive
    steps all attach from the root): prepare_join already supports several
    candidates with different base_column values in one call, which would
    save a few round trips for that specific case, but processing one step
    at a time keeps this function's logic uniform regardless of where in
    the chain a step attaches from, and produces an identical result -
    every step is a LEFT JOIN against whichever base_column it names,
    which never changes the base's own row count, so the order and
    batching of steps can't change what any later step sees.

    Returns (df, column_provenance, original_columns, aggregated_datasets,
    renamed_columns, protected) - protected is every base_column actually
    used, sorted - the set a caller (main.py) should pass to DIADA/declutter
    as columns that must never be dropped, since they're what the chain
    itself was built on.

    Raises JoinError if a step's base_column_dataset/base_column doesn't
    resolve in the frame as it stands when that step is reached (e.g. the
    first step naming something other than `root`, or a later step naming
    a dataset/column no earlier step actually attached)."""
    if not steps:
        df = data_loader.read_preprocessed(root)
        return df, {c: root for c in df.columns}, {c: c for c in df.columns}, [], {}, []

    df: pd.DataFrame | None = None
    column_provenance: dict[str, str] = {}
    original_columns: dict[str, str] = {}
    aggregated_datasets: list[str] = []
    renamed_columns: dict[str, str] = {}
    protected: set[str] = set()

    for step in steps:
        spec = JoinCandidateSpec(dataset=step.dataset, column=step.column, aggregations=step.aggregations)
        if df is None:
            if step.base_column_dataset != root:
                raise JoinError(
                    f"The first join step must attach from the root dataset '{root}', not '{step.base_column_dataset}'."
                )
            current_base_column = step.base_column
            prepared = prepare_join(project_id, root, current_base_column, [spec])
        else:
            try:
                current_base_column = next(
                    col for col in df.columns
                    if column_provenance[col] == step.base_column_dataset and original_columns[col] == step.base_column
                )
            except StopIteration:
                raise JoinError(
                    f"'{step.base_column_dataset}.{step.base_column}' isn't part of the join built so far."
                ) from None
            prepared = prepare_join(
                project_id, step.base_column_dataset, current_base_column, [spec],
                base_df=df, base_column_provenance=column_provenance, base_original_columns=original_columns,
            )
        aggregated_datasets.extend(prepared.aggregated_datasets)
        renamed_columns.update(prepared.renamed_columns)
        try:
            df = to_dataframe(prepared)
            column_provenance = prepared.column_provenance
            original_columns = prepared.original_columns
        finally:
            prepared.close()
        protected.add(current_base_column)

    return df, column_provenance, original_columns, aggregated_datasets, renamed_columns, sorted(protected)


def unused_primary_key_columns(
    prepared: PreparedJoin,
    base_column: str,
    column_uniqueness: dict[str, dict[str, float]],
    string_columns: dict[str, set[str]],
    threshold: float = catalog.PRIMARY_KEY_UNIQUENESS_THRESHOLD,
) -> list[str]:
    """Result columns that are a primary key of their own source dataset
    (>= threshold of non-null values distinct - see
    catalog.PRIMARY_KEY_UNIQUENESS_THRESHOLD) but weren't the column the
    join was actually performed on - e.g. a second country-code column that
    rode along on a join performed through country name. A genuine primary
    key of its own dataset, just not the one anything was joined on, so it
    identifies rows without adding any information a model could use to
    predict with; the UI offers hiding/downloading a joined result without
    these.

    Only string-typed columns are eligible (string_columns - same
    dataset/main_type shape as catalog.usable_columns_by_dataset(...,
    main_type="string")), matching the same restriction join discovery's
    own model applies to candidates. Without this, a highly unique
    *numeric* column - revenue, income, a size in square feet, anything
    that's a continuous measurement rather than a label - clears the
    uniqueness bar just as easily as a genuine ID does, but it's exactly
    the kind of predictive signal a join is meant to bring in, not an
    identifier to discard (confirmed empirically against retail_join_demo:
    avg_household_income and marketing_budget_monthly are both >=96% unique
    across 150 stores purely by being continuous values, not because
    they're identifiers).

    The base's own join column is always exempt (it's guaranteed to still
    be a primary key of the base dataset - the point here is excluding
    *unused* keys, not the join key itself). A candidate's own join key
    never needs excluding: prepare_join never includes it in the output at
    all (see other_cols above), only the columns it brought along."""
    result = []
    for col in prepared.columns:
        if col == base_column:
            continue
        dataset = prepared.column_provenance[col]
        original = prepared.original_columns[col]
        if original not in string_columns.get(dataset, set()):
            continue
        if column_uniqueness.get(dataset, {}).get(original, 0.0) >= threshold:
            result.append(col)
    return result


# Below this cardinality, two columns matching up is more likely coincidence
# than real redundancy (e.g. two unrelated boolean flags that happen to
# agree in a small sample) - redundant_column_groups ignores columns this
# low-cardinality entirely, the same way discovery/curation treat "few
# distinct values" as its own regime (see MAX_UNIQUE_FOR_CATEGORICAL in
# app/data_type.py) rather than meaningful identity information.
MIN_REDUNDANCY_CARDINALITY = 3


def _is_near_bijection(a: pd.Series, b: pd.Series, a_nunique: int, threshold: float) -> bool:
    pair_count = pd.DataFrame({"a": a, "b": b}).dropna().drop_duplicates().shape[0]
    if pair_count == 0:
        return False
    # a_nunique == b_nunique by construction (same cardinality bucket), so
    # this ratio is symmetric: it's simultaneously "how consistently does
    # each a-value map to one b-value" and vice versa. 1.0 == a strict
    # bijection; every extra (a, b) combination beyond a_nunique pulls it down.
    return (a_nunique / pair_count) >= threshold


def redundant_column_groups(
    df: pd.DataFrame,
    column_provenance: dict[str, str],
    original_columns: dict[str, str],
    base_column: str,
    string_columns: dict[str, set[str]],
    tolerance: float = catalog.COLUMN_REDUNDANCY_TOLERANCE,
) -> list[dict[str, Any]]:
    """Groups of result columns that are (near-)bijective with each other -
    a currency's code and its name, or a country's 2-letter and 3-letter ISO
    codes, all say the same thing under a different encoding. Having every
    member of a group adds nothing a model could use beyond having just one
    of them; the UI offers hiding/downloading a joined result down to one
    representative per group. Not the same problem unused_primary_key_columns
    solves: that flags a column that identifies rows on its own but wasn't
    the join key; this flags columns that duplicate *each other*, primary
    key or not.

    Restricted to string-typed columns of the *same* source dataset (the
    same reasoning as unused_primary_key_columns: a numeric column matching
    another by chance is a coincidence, not a redundant encoding, and
    redundancy is a property of one dataset's own columns, not something
    that emerges from a join). Detection is two stages, cheap first:
      1. Bucket eligible columns by (source dataset, exact cardinality) -
         a (near-)bijection requires matching cardinality, so only columns
         landing in the same bucket can possibly qualify. In practice this
         eliminates the overwhelming majority of column pairs for free.
      2. Only within a bucket, check each pair's actual values: the number
         of distinct (a, b) combinations must be within `tolerance` of the
         shared cardinality (see _is_near_bijection) - one dedup pass per
         candidate pair, not a full pairwise scan of every column in the
         dataset.
    Pairs that pass are unioned (via union-find) into groups, so a group of
    more than two mutually-redundant columns - e.g. currency name, currency
    name (local), 2-letter code, 3-letter code - is detected as one group
    rather than several overlapping pairs.

    Within a group, base_column is always the kept representative if it's a
    member (never hide the join key); otherwise the alphabetically-first
    column name is kept, for the same reproducibility reason ties are
    broken alphabetically elsewhere in this app rather than arbitrarily."""
    eligible = [
        col for col in df.columns
        if original_columns[col] in string_columns.get(column_provenance[col], set())
    ]

    nunique = {col: int(df[col].nunique(dropna=True)) for col in eligible}
    buckets: dict[tuple[str, int], list[str]] = {}
    for col in eligible:
        if nunique[col] < MIN_REDUNDANCY_CARDINALITY:
            continue
        buckets.setdefault((column_provenance[col], nunique[col]), []).append(col)

    parent = {col: col for cols in buckets.values() for col in cols}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cols in buckets.values():
        if len(cols) < 2:
            continue
        for i, a in enumerate(cols):
            for b in cols[i + 1 :]:
                if find(a) == find(b):
                    continue
                if _is_near_bijection(df[a], df[b], nunique[a], tolerance):
                    union(a, b)

    groups: dict[str, list[str]] = {}
    for col in parent:
        groups.setdefault(find(col), []).append(col)

    result = []
    for cols in groups.values():
        if len(cols) < 2:
            continue
        cols_sorted = sorted(cols)
        kept = base_column if base_column in cols_sorted else cols_sorted[0]
        result.append({"columns": cols_sorted, "kept": kept})
    return sorted(result, key=lambda g: g["kept"])


def mostly_null_columns(
    df: pd.DataFrame, base_column: str, threshold: float = catalog.MOSTLY_NULL_THRESHOLD
) -> dict[str, float]:
    """{column name -> null fraction} for every result column (except
    base_column, never dropped) that's at least `threshold` null. Unlike
    unused_primary_key_columns/redundant_column_groups, this isn't scoped
    to one source dataset or one type: the most common real cause is a
    left-joined candidate whose key column only matched a small fraction
    of base rows, leaving nearly every one of its other columns null in
    the joined result regardless of how complete that candidate's own data
    was - a property of *this join*, not of any one dataset, so every
    column is checked. threshold is deliberately not the same number
    quality.py flags a source dataset's own column at (0.95) - see
    catalog.MOSTLY_NULL_THRESHOLD."""
    return {
        col: null_fraction
        for col in df.columns
        if col != base_column and (null_fraction := float(df[col].isna().mean())) >= threshold
    }


def subset_redundant_groups(
    groups: list[dict[str, Any]], present_columns: list[str], base_column: str
) -> list[dict[str, Any]]:
    """Adapts a redundant-group list computed on a full join down to
    whichever of those columns actually survived into one particular output
    (e.g. a CF1/CF2 curation result, which only keeps a subset of the
    joined columns) - drops any group down to its still-present members,
    drops the group entirely if fewer than 2 remain (nothing left to
    deduplicate), and re-picks `kept` if the original one didn't survive."""
    present = set(present_columns)
    result = []
    for group in groups:
        cols = [c for c in group["columns"] if c in present]
        if len(cols) < 2:
            continue
        kept = group["kept"] if group["kept"] in cols else (base_column if base_column in cols else sorted(cols)[0])
        result.append({"columns": cols, "kept": kept})
    return result
