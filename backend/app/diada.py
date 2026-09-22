"""
Column relationship curation via DIADA - a Java tool (backend/tools/DIADA-0.8.jar,
vendored from the arclo/benchmarks_generation project) that scores every pair of
columns in a dataset with a "soundness" value. Given a materialized join (see
app/join_builder.py), this module turns those pairwise scores into two cleanup
actions:

  - "Remove univariate noise" (CF1): drop any column that has no relationship
    >= SOUNDNESS_THRESHOLD with any other column - a column that doesn't relate
    to anything else is treated as noise.
  - "Structure dataset" (CF2): group columns into clusters of mutually-related
    features (connected components of the >= SOUNDNESS_THRESHOLD relationship
    graph) and split the dataset into one output dataset per cluster. Unlike
    the reference tool's benchmark use (which centers a single cluster on a
    known target column), there's no target here, so every cluster is
    returned; columns with no qualifying relationship to anything - members of
    no cluster - are collected into one shared "unclustered" dataset rather
    than being dropped or split out one-by-one.

Ported from arclo/benchmarks_generation (diada.py + clean_benchmarks.py), with
deliberate differences from that reference:
  - the graph threshold is inclusive (>= 4) here, not the reference's strict > 4
  - runs in an isolated per-request temp directory instead of the reference's
    fixed relative paths, so concurrent curate requests can't collide
  - CF1 and CF2 share a single DIADA invocation (soundness only needs
    computing once) rather than running the tool - which launches a JVM and is
    not cheap - twice
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import networkx as nx
import pandas as pd

from .core.config import DIADA_JAR_PATH

SOUNDNESS_THRESHOLD = 4.0
NUM_BUCKETS = 10  # matches the reference pipeline's standard invoke_diada(..., 10) call
KURTOSIS_SENSIBILITY = 2
DIADA_EPSILON = "0.000001"

_TYPE_SUFFIXES = ("(String)", "(Integer)", "(Double)")


class DiadaError(Exception):
    pass


def _prepare_diada_input(df: pd.DataFrame) -> pd.DataFrame:
    """Labels every column with its DIADA type suffix and buckets numeric
    columns into NUM_BUCKETS bins - the num_buckets>1 branch of the reference
    invoke_diada(), which is the only mode the original pipeline ever used."""
    data = df.copy()

    for col in data.columns:
        if pd.api.types.is_float_dtype(data[col]):
            non_null = data[col].dropna()
            if len(non_null) > 0 and non_null.apply(float.is_integer).all():
                data[col] = data[col].astype("Int64")

    def infer_type(col: pd.Series) -> str:
        if pd.api.types.is_float_dtype(col):
            return "(Double)"
        elif pd.api.types.is_integer_dtype(col):
            return "(Integer)" if col.nunique() > 30 else "(String)"
        else:
            return "(String)"

    data.columns = [f"{col}{infer_type(data[col])}" for col in data.columns]

    for col in data.columns:
        if "(Integer)" in col or "(Double)" in col:
            # A constant or entirely-null column (e.g. a left-joined
            # candidate column where few/no rows matched - routine for
            # app/join_builder.py's joins) has no min/max spread to bin:
            # pd.cut would compute all-NaN bin edges and raise. Leave it
            # unbucketed - DIADA still sees it (as a string of its one
            # repeated value, or of nulls), it just can't be usefully binned.
            if data[col].nunique(dropna=True) <= 1:
                continue
            kurtosis = data[col].kurt()
            heavy_tailed = abs(kurtosis) > KURTOSIS_SENSIBILITY
            if heavy_tailed:  # heavy-tailed: equi-depth (quantile) bins
                data[col] = pd.qcut(data[col], q=NUM_BUCKETS, duplicates="drop")
            else:  # light-tailed: standard equal-width bins
                data[col] = pd.cut(data[col], bins=NUM_BUCKETS)
    data.columns = [c.replace("(Integer)", "(String)").replace("(Double)", "(String)") for c in data.columns]

    return data


def compute_soundness(df: pd.DataFrame) -> pd.DataFrame:
    """Runs DIADA on df and returns one (column_1, column_2, soundness) row
    per related pair. Each call launches a fresh JVM in an isolated temp
    directory - not cheap, so callers should run this once per dataset and
    derive every result from it rather than calling it per-mode (see
    curate())."""
    prepared = _prepare_diada_input(df)

    with tempfile.TemporaryDirectory(prefix="diada-demo-") as tmp:
        tmp_path = Path(tmp)
        input_csv = tmp_path / "diada_input.csv"
        prepared.to_csv(input_csv, index=False)

        cmd = ["java", "-jar", str(DIADA_JAR_PATH), str(input_csv), DIADA_EPSILON, str(len(prepared))]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path)
        except FileNotFoundError as e:
            raise DiadaError("Java runtime not found - DIADA requires a Java 11+ runtime on PATH") from e

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise DiadaError(f"DIADA failed: {detail}")

        output_path = tmp_path / "output.txt"
        if not output_path.exists():
            raise DiadaError("DIADA did not produce an output file")

        if output_path.stat().st_size == 0:
            # DIADA writes nothing at all when it finds zero qualifying
            # pairs - routine for a dataset with too few columns to compare,
            # not a failure. pd.read_csv on a 0-byte file raises
            # EmptyDataError, so short-circuit before that.
            return pd.DataFrame(columns=["column_1", "column_2", "soundness"])

        rel_df = pd.read_csv(output_path, sep=r"\s+", header=None, names=["col_id_1", "col_id_2", "soundness"])

    rel_df = rel_df[rel_df["soundness"] > 0]
    if rel_df.empty:
        # Nothing scored above 0 at all (routine for a dataset with few
        # columns or little cross-column structure) - col_id_1/col_id_2
        # stay numeric-dtype on an empty slice, so .map() below never runs
        # and the columns never actually become strings; the .str.replace()
        # calls further down would then fail with "Can only use .str
        # accessor with string values!" on a dtype that's still numeric.
        # Every caller already treats "no qualifying relationship" as valid
        # input (see _relationship_graph/remove_univariate_noise/
        # structure_dataset), so short-circuit before that ever engages.
        return pd.DataFrame(columns=["column_1", "column_2", "soundness"])

    col_names = prepared.columns.tolist()
    rel_df["column_1"] = rel_df["col_id_1"].map(lambda i: col_names[i])
    rel_df["column_2"] = rel_df["col_id_2"].map(lambda i: col_names[i])

    out = rel_df[["column_1", "column_2", "soundness"]].copy()
    for suffix in _TYPE_SUFFIXES:
        out["column_1"] = out["column_1"].str.replace(suffix, "", regex=False)
        out["column_2"] = out["column_2"].str.replace(suffix, "", regex=False)
    return out


def compute_soundness_for_curation(df: pd.DataFrame, protected_columns=()) -> pd.DataFrame:
    """compute_soundness(df), but with protected_columns (e.g. the column(s)
    a join was actually performed on) excluded from what gets scored -
    curate() and every direct caller of compute_soundness() ahead of
    remove_univariate_noise/structure_dataset should go through this
    instead of scoring the full df.

    A join key isn't analytical content - it's the mechanism that pasted
    every other attached column on in the first place - so it necessarily
    "predicts" those columns perfectly, by construction, regardless of
    whether they have any real relationship to each other. Left in, that
    trivial self-relationship acts as a hub connecting otherwise unrelated
    groups of columns into one another, since DIADA's score reflects
    multivariate dependency, not an isolated pairwise correlation.
    app/gold_layer.py's _curate_group carries the full investigation that
    found this (a table joined at a non-unique grain collapsed nearly
    every column into one giant cluster with the key column left in, and
    split cleanly into the correct, real groups with it excluded) - this
    is the same fix, for app/main.py's manual "Perform a join"/"Curate
    data" flow, which used to score the join key(s) right along with
    everything else.

    remove_univariate_noise/structure_dataset still receive the full,
    all-columns df to slice their own *output* rows/columns from, so a
    protected column is always kept in the result regardless of this
    exclusion - only the scoring itself is narrowed."""
    non_protected = [c for c in df.columns if c not in protected_columns]
    if not non_protected:
        # Nothing left to score once the join key(s) are excluded (e.g. a
        # base with no other columns at all) - DIADA has no pair to
        # compare with fewer than two columns anyway, so this is the same
        # "zero qualifying pairs" outcome compute_soundness itself returns.
        return pd.DataFrame(columns=["column_1", "column_2", "soundness"])
    return compute_soundness(df[non_protected])


def _relationship_graph(soundness_df: pd.DataFrame) -> nx.Graph:
    """Undirected graph of column-name -> column-name relationships scoring
    >= SOUNDNESS_THRESHOLD. Self-loops and sub-threshold pairs are dropped;
    when a pair appears more than once, only the highest score survives."""
    df = soundness_df.copy()
    df["column_1"] = df["column_1"].astype(str)
    df["column_2"] = df["column_2"].astype(str)
    df["soundness"] = pd.to_numeric(df["soundness"], errors="coerce")
    df = df.dropna(subset=["soundness"])
    df = df[df["column_1"] != df["column_2"]]
    df = df[df["soundness"] >= SOUNDNESS_THRESHOLD]

    df["_pair"] = df.apply(lambda r: frozenset((r["column_1"], r["column_2"])), axis=1)
    df = df.sort_values("soundness", ascending=False).drop_duplicates("_pair")

    graph = nx.Graph()
    for _, row in df.iterrows():
        graph.add_edge(row["column_1"], row["column_2"], weight=float(row["soundness"]))
    return graph


def _graph_data(graph: nx.Graph, df: pd.DataFrame, cluster_of: dict[str, str]) -> dict:
    """Nodes (every column, tagged with its natural cluster/"unclustered"
    membership) and edges (the actual >= SOUNDNESS_THRESHOLD relationships,
    unmodified by any protected-column injection) - for the UI's "Structure
    dataset" graph visualization."""
    nodes = [{"id": c, "cluster": cluster_of.get(c, "unclustered")} for c in df.columns]
    edges = [
        {"source": u, "target": v, "soundness": float(data["weight"])}
        for u, v, data in graph.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


def remove_univariate_noise(df: pd.DataFrame, soundness_df: pd.DataFrame, protected_columns=()) -> dict:
    """CF1: keep only columns that have >= SOUNDNESS_THRESHOLD relationship
    with at least one other column in df; everything else is dropped as
    univariate noise. protected_columns (e.g. the column a join was
    performed on) are always kept, regardless of soundness - splitting a
    join apart shouldn't cost you the ability to identify which row is
    which."""
    graph = _relationship_graph(soundness_df)
    related = {c for c in graph.nodes() if c in df.columns}
    protected = {c for c in protected_columns if c in df.columns}
    keep = related | protected
    kept = [c for c in df.columns if c in keep]
    removed = [c for c in df.columns if c not in keep]
    return {"columns": kept, "removed_columns": removed, "dataframe": df[kept]}


def structure_dataset(df: pd.DataFrame, soundness_df: pd.DataFrame, protected_columns=()) -> dict:
    """CF2: split df into one dataset per connected component of the
    >= SOUNDNESS_THRESHOLD relationship graph. Columns with no qualifying
    relationship at all - members of no cluster - are collected into one
    shared "unclustered" dataset instead of being dropped or split out
    one-by-one. Clusters are ordered largest-first (ties broken
    alphabetically by their first column) so results are deterministic.

    protected_columns (e.g. the column a join was performed on) are added
    to every output dataset - each cluster and "unclustered" alike - even
    where DIADA found no relationship for them, so every split-out piece
    stays individually joinable back to the others via that shared key.
    This only affects which columns land in each *output dataset*; the
    "graph" data below still reflects each column's real, undecorated
    cluster membership."""
    graph = _relationship_graph(soundness_df)
    components = [c & set(df.columns) for c in nx.connected_components(graph)]
    components = [c for c in components if c]

    clustered_columns = set().union(*components) if components else set()
    unclustered = [c for c in df.columns if c not in clustered_columns]

    ordered = sorted(components, key=lambda c: (-len(c), sorted(c)[0]))
    protected = {c for c in protected_columns if c in df.columns}

    cluster_of = {c: "unclustered" for c in unclustered}
    clusters = []
    for i, cols in enumerate(ordered, start=1):
        name = f"cluster_{i}"
        for c in cols:
            cluster_of[c] = name
        full_cols = [c for c in df.columns if c in cols or c in protected]
        clusters.append({"name": name, "columns": full_cols, "dataframe": df[full_cols]})
    if unclustered:
        full_unclustered = [c for c in df.columns if c in unclustered or c in protected]
        clusters.append({"name": "unclustered", "columns": full_unclustered, "dataframe": df[full_unclustered]})

    return {"clusters": clusters, "graph": _graph_data(graph, df, cluster_of)}


def curate(df: pd.DataFrame, protected_columns=()) -> dict:
    """Runs DIADA once and returns both the CF1 (remove univariate noise) and
    CF2 (structure dataset) results derived from the same soundness scores.
    soundness_pairs is that same raw pairwise DataFrame, unfiltered by
    SOUNDNESS_THRESHOLD - CF1/CF2 only ever see the >= 4 subset (via
    _relationship_graph), but a caller wanting the full picture (e.g. an
    export of every pair DIADA actually scored) needs the unfiltered one.

    Scoring excludes protected_columns (see compute_soundness_for_curation)
    - they still come back in cf1/cf2's own output columns regardless,
    via remove_univariate_noise/structure_dataset's own protected_columns
    handling, but never as a candidate to relate other columns *through*."""
    soundness_df = compute_soundness_for_curation(df, protected_columns)
    return {
        "cf1": remove_univariate_noise(df, soundness_df, protected_columns),
        "cf2": structure_dataset(df, soundness_df, protected_columns),
        "soundness_pairs": soundness_df,
    }
