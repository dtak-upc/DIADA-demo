"""
Metadata catalog: a graph over datasets, columns, data-quality flags, and -
once an autonomous gold-layer run exists (app/gold_layer.py) - the gold
tables built from them.

  (dataset) --has_column--> (column) --has_flag--> (quality_flag)
  (gold_table) --built_from--> (dataset)
  (gold_table) --derived_from--> (column)          [output column -> its real source column]
  (gold_table) --joined_via--> (dataset)            [discovery record: column pair, score]
  (column) --statistically_related--> (column)      [DIADA soundness, translated back onto source columns]
  (column) --joinable--> (column)                   [every raw discovery score this run found, unfiltered]

Assembling the ingestion half of the graph is cheap (it's just iterating
already-persisted profiles - see data_loader.profile_dataset - no CSV
parsing or DuckDB involved), but per the "the graph should be stored too"
requirement it's persisted as its own artifact (app/storage.py) rather than
rebuilt on every GET /api/catalog: app/processing.py rebuilds and saves it
once, as the last step after profiling every dataset in a project, so
viewing the catalog is a disk read like everything else.

This is meant to be the one place that accumulates cross-cutting metadata
as later stages add more to reason about, rather than each stage inventing
its own separate explanation mechanism. The gold-layer edges above are that
in practice: app/gold_layer.py's own tables already carry their full
recipe (join_candidates, column_provenance, DIADA's soundness graph - see
its module docstring), but that per-table record only answers "why does
*this* table look like this." Layering the same information onto the
catalog graph (see _add_gold_layer_governance, called from
rebuild_and_store whenever ad hoc has a persisted run) makes it answer the
wider question too - "everything this column ever ended up in, across
every gold table" - by tracing edges outward from one node instead of
grepping every table's recipe by hand.

Deliberately ad hoc only, of the two gold-layer algorithms (see
storage.GOLD_LAYER_MODES) - ad hoc is the primary one, and "every table"
exists mainly to compare against it (see rebuild_and_store's own
docstring), so its output never feeds the catalog graph even though each
has its own independent build/storage.
Nothing here is incremental: a rebuild always starts from a fresh
ingestion-time graph and re-layers whatever ad hoc's own last run
currently persists on top of it, which means a stale gold table from a
run that's since been superseded or a dataset that's since been deleted
simply doesn't appear next time, rather than needing to be explicitly
retracted.

build_usable_graph() derives a second, filtered graph from the ingestion
half above only: every dataset/column that carries a quality flag (see
app/quality.py) is dropped, leaving only the "trusted" backbone -
(dataset) --has_column--> (column), nothing else - that downstream stages
(join discovery first) should build on top of instead of re-deriving their
own notion of "usable" from the raw flags every time. It's persisted the
same way as the full catalog, as its own artifact, so it's a disk read
too. Gold-layer governance edges are deliberately layered on *after* this
filtered graph is derived (see rebuild_and_store) - they describe output
that already exists, not eligibility for future joins, so they have no
business feeding back into "usable".
"""
from __future__ import annotations

from typing import Any

import networkx as nx

from ..core import data_loader, storage

# What counts as a "primary key" throughout the app: at least this fraction
# of a column's non-null values must be distinct (see usable_column_uniqueness
# below). Not a strict 1.0 - real data has a few incidental duplicates. The
# one canonical definition, used by: app/gold_layer.py (a join candidate has
# to clear this to be eligible at all), the manual join-discovery UI
# (highlights columns that clear it), and the "hide/download without unused
# primary keys" feature on a joined result (app/join_builder.py's
# unused_primary_key_columns).
PRIMARY_KEY_UNIQUENESS_THRESHOLD = 0.90

# Two columns (of the same source dataset) count as "redundant" - carrying
# the same information under a different encoding, like a currency's code
# vs. its name - if at least this fraction of one column's distinct values
# map to a single value of the other (a value of 1.0 would mean a strict
# bijection; real data can have a few inconsistent rows without the
# relationship stopping being meaningful). See
# app/join_builder.py's redundant_column_groups.
COLUMN_REDUNDANCY_TOLERANCE = 0.98

# A joined-result column counts as "mostly null" - worth dropping, since a
# handful of non-null values can't carry real predictive signal - once at
# least this fraction of its values are null. Deliberately a different
# (stricter) number than quality.py's own INCOMPLETENESS_THRESHOLD (0.95):
# that one flags a *source* dataset's own column before any join; this one
# is about a joined *result* column, where the far more common cause is a
# left-joined candidate whose key only matched a small fraction of base
# rows - a column that was 0% null in its own dataset can end up mostly
# null here through no fault of the data itself, so the bar for calling it
# unusable is set higher. See app/join_builder.py's mostly_null_columns.
MOSTLY_NULL_THRESHOLD = 0.96


def _dataset_node(name: str) -> str:
    return f"dataset:{name}"


def _column_node(dataset: str, column: str) -> str:
    return f"column:{dataset}.{column}"


def _flag_node(dataset: str, column: str, flag_type: str) -> str:
    return f"flag:{dataset}.{column}.{flag_type}"


def _dataset_flag_node(dataset: str, flag_type: str) -> str:
    return f"flag:{dataset}.{flag_type}"


def _gold_table_node(table_id: str) -> str:
    return f"gold_table:{table_id}"


def build_catalog() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for name in data_loader.dataset_names():
        try:
            profile = data_loader.profile_dataset(name)
        except data_loader.DatasetNotFoundError:
            continue

        ds_node = _dataset_node(name)
        graph.add_node(
            ds_node,
            kind="dataset",
            name=name,
            row_count=profile.row_count,
            column_count=profile.column_count,
        )

        for flag in profile.flags:
            flag_node = _dataset_flag_node(name, flag["type"])
            graph.add_node(flag_node, kind="quality_flag", **flag)
            graph.add_edge(ds_node, flag_node, relation="has_flag")

        for col in profile.columns:
            col_node = _column_node(name, col["name"])
            graph.add_node(
                col_node,
                kind="column",
                dataset=name,
                name=col["name"],
                dtype=col["dtype"],
                main_type=col["main_type"],
                secondary_type=col["secondary_type"],
                null_count=col["null_count"],
                null_pct=col["null_pct"],
                unique_count=col["unique_count"],
            )
            graph.add_edge(ds_node, col_node, relation="has_column")

            for flag in col["flags"]:
                flag_node = _flag_node(name, col["name"], flag["type"])
                graph.add_node(flag_node, kind="quality_flag", **flag)
                graph.add_edge(col_node, flag_node, relation="has_flag")

    return graph


def build_usable_graph(full_graph: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Filters the full catalog down to datasets and columns with zero
    quality flags anywhere on them.

    A dataset with *any* dataset-level flag (too_few_rows, etc.) is dropped
    entirely, including all of its columns - a flagged dataset's shape is
    itself in question, so nothing under it is trustworthy input for
    cross-dataset analysis. Within a dataset that's kept, only its
    individually unflagged columns are kept. Flag nodes themselves don't
    appear here at all: by construction nothing left has one.
    """
    usable = nx.MultiDiGraph()

    def _has_flag_child(node_id: str) -> bool:
        return any(
            full_graph.nodes[target].get("kind") == "quality_flag"
            for _, target, edata in full_graph.out_edges(node_id, data=True)
            if edata.get("relation") == "has_flag"
        )

    for node_id, data in full_graph.nodes(data=True):
        if data.get("kind") != "dataset" or _has_flag_child(node_id):
            continue

        usable.add_node(node_id, **data)
        for _, col_node, edata in full_graph.out_edges(node_id, data=True):
            if edata.get("relation") != "has_column" or _has_flag_child(col_node):
                continue
            usable.add_node(col_node, **full_graph.nodes[col_node])
            usable.add_edge(node_id, col_node, relation="has_column")

    return usable


def _add_gold_layer_governance(graph: nx.MultiDiGraph, run: dict[str, Any]) -> None:
    """Layers persisted gold-layer tables' (app/gold_layer.py) own
    per-table explanation onto the ingestion-time catalog graph, in place -
    see the module docstring for why. `run` only ever needs `tables` and
    `all_join_scores` - the shape storage.load_gold_layer returns for one
    algorithm (== gold_layer.run_to_dict output); rebuild_and_store only
    ever passes it ad hoc's own persisted run, never a merge of both
    algorithms' output (see its own docstring for why). Either way this
    has no dependency on gold_layer.py beyond the dict shape it already
    persists.

    Every edge added here points from the gold table to whatever it's
    explained by (a source dataset, a source column, another column it's
    statistically related to) - `graph.has_node(...)` guards every target
    before adding an edge, so a table referencing a dataset/column that's
    since been renamed or deleted (stale run vs. current files) just loses
    that one edge instead of raising."""
    tables = run.get("tables", [])

    for table in tables:
        table_node = _gold_table_node(table["id"])
        graph.add_node(
            table_node,
            kind="gold_table",
            # NOT "id" - catalog_to_json builds each JSON node as
            # {"id": node_id, **data}, so an "id" attribute here would
            # silently overwrite the real graph node id (the gold_table:-
            # prefixed one edges actually reference) with this bare table
            # id instead, breaking every edge pointing at this node.
            table_id=table["id"],
            name=table["name"],
            table_kind=table["kind"],  # cf1 | cf2_cluster | cf2_unclustered | joined_uncurated - "kind" itself is reserved for the graph node's own type ("gold_table")
            base_dataset=table["base_dataset"],
            root_dataset=table.get("root_dataset") or table["base_dataset"],
            row_count=table["row_count"],
            column_count=len(table["columns"]),
            removed_columns=table.get("removed_columns", []),
            declutter_removed_columns=table.get("declutter_removed_columns", []),
            ad_hoc=bool(table.get("ad_hoc")),
        )

        column_provenance = table.get("column_provenance", {})
        original_columns = table.get("original_columns", {})

        # built_from: every source dataset that actually contributed a
        # surviving column - the real "what is this table made of" signal,
        # narrower than join_candidates below (which also lists candidates
        # that were joined in but later fully decluttered/curated away).
        for source_dataset in sorted(set(column_provenance.values())):
            source_node = _dataset_node(source_dataset)
            if graph.has_node(source_node):
                graph.add_edge(table_node, source_node, relation="built_from")

        # derived_from: per-output-column lineage, straight back into the
        # same column nodes ingestion already built (original_columns
        # undoes any collision-renaming) - rather than minting a parallel
        # set of "gold table column" nodes, a gold table's own columns are
        # represented purely as edges into the catalog's existing ones.
        for out_col, source_dataset in column_provenance.items():
            source_col = original_columns.get(out_col, out_col)
            source_node = _column_node(source_dataset, source_col)
            if graph.has_node(source_node):
                graph.add_edge(table_node, source_node, relation="derived_from", output_column=out_col)

        # joined_via: the discovery record itself - which datasets were
        # tried as candidates, through which column pair, at what score,
        # and whether the candidate side was row-aggregated (one-to-many).
        aggregated = set(table.get("aggregated_datasets", []))
        for candidate in table.get("join_candidates", []):
            source_node = _dataset_node(candidate["dataset"])
            if graph.has_node(source_node):
                graph.add_edge(
                    table_node, source_node, relation="joined_via",
                    base_column=candidate["base_column"], candidate_column=candidate["column"],
                    score=candidate["score"], aggregated=candidate["dataset"] in aggregated,
                )

    # statistically_related: DIADA's own per-curation soundness graph,
    # translated from output-column-name pairs (only meaningful inside the
    # one table/cluster that produced them) onto the shared source column
    # nodes above, so it composes with the rest of the catalog instead of
    # staying a table-scoped artifact only that one table can show.
    #
    # table["graph"] is the *whole* base/group's relationship graph, shared
    # verbatim across every cluster table produced by the same curation
    # pass (see GoldTable.graph's docstring) - but each cluster's own
    # column_provenance/original_columns only covers its own columns, not
    # the other clusters' (or "unclustered"'s) that the shared graph can
    # still reference. Group tables by curation pass (the id prefix before
    # "::cf2::", identical for every table one _cf2_tables() call produced
    # - see gold_layer.py) and merge their provenance maps before
    # resolving edges, rather than resolving against any single table's
    # partial slice.
    passes: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        if not table.get("graph"):
            continue
        pass_key = table["id"].split("::cf2::")[0]
        passes.setdefault(pass_key, []).append(table)

    for members in passes.values():
        table_graph = members[0]["graph"]
        column_provenance: dict[str, str] = {}
        original_columns: dict[str, str] = {}
        for t in members:
            column_provenance.update(t.get("column_provenance", {}))
            original_columns.update(t.get("original_columns", {}))

        def _resolve(col_name: str, _cp=column_provenance, _oc=original_columns) -> str | None:
            source_dataset = _cp.get(col_name)
            if source_dataset is None:
                return None
            node = _column_node(source_dataset, _oc.get(col_name, col_name))
            return node if graph.has_node(node) else None

        for edge in table_graph.get("edges", []):
            a, b = _resolve(edge["source"]), _resolve(edge["target"])
            if a and b and a != b:
                graph.add_edge(a, b, relation="statistically_related", soundness=edge["soundness"])

    # joinable: every raw (base column, candidate column) pair discovery
    # ever scored this run (run.all_join_scores - see gold_layer.JoinScore),
    # completely unfiltered by discovery_threshold or any other build-time
    # bar - straight from joinability_model's own output. This is the one
    # relation in this function that isn't scoped to any particular gold
    # table (there's no per-table id to prefix it with - discovery runs
    # once per base column, independent of which tables ended up using the
    # result), so it's added directly as column-to-column edges. The
    # "Joinability graph" catalog view applies its own user-chosen
    # threshold against these client-side, rather than being stuck with
    # whatever threshold this run happened to be built with.
    for score in run.get("all_join_scores", []):
        source_node = _column_node(score["base_dataset"], score["base_column"])
        target_node = _column_node(score["candidate_dataset"], score["candidate_column"])
        if graph.has_node(source_node) and graph.has_node(target_node):
            graph.add_edge(source_node, target_node, relation="joinable", score=score["score"])


def catalog_to_json(graph: nx.MultiDiGraph) -> dict[str, list[dict[str, Any]]]:
    nodes = [{"id": node_id, **data} for node_id, data in graph.nodes(data=True)]
    edges = [
        {"source": src, "target": dst, **data}
        for src, dst, data in graph.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


def rebuild_and_store(project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Builds the full catalog, the usable graph derived from it, and (if
    ad hoc has a persisted run - see storage.GOLD_LAYER_MODES) layers that
    run's governance/traceability edges onto the full catalog before
    saving it - see _add_gold_layer_governance and the module docstring.
    Called by app/processing.py once every dataset has been (re)profiled,
    and again by app/gold_layer_job.py/app/main.py whenever any gold-layer
    algorithm finishes a build.

    Deliberately ad hoc only, not a merge of both algorithms: ad hoc
    is the primary one (see App.jsx's tab ordering) and its results are
    meant to answer "what's actually in this lake's gold layer" - "every
    table" exists to compare against it, mostly as a way to sanity-check
    the algorithm itself rather than as its own end product, so it stays
    visible only on its own tab and never feeds the catalog graph's
    Joinability/Composition views. Building "every table" alone still
    calls this (so the usable graph and the ingestion half of the catalog
    stay current), it just never contributes any governance edges of its
    own. The usable graph is always derived from
    the plain ingestion graph, before governance edges are added - see the
    module docstring for why gold-layer output shouldn't feed back into
    join-discovery eligibility."""
    graph = build_catalog()

    usable_json = catalog_to_json(build_usable_graph(graph))
    storage.save_usable_graph(project_id, usable_json)

    ad_hoc_run = storage.load_gold_layer(project_id, "ad_hoc")
    if ad_hoc_run is not None:
        _add_gold_layer_governance(graph, ad_hoc_run)

    catalog_json = catalog_to_json(graph)
    storage.save_catalog(project_id, catalog_json)

    return catalog_json


def get_catalog(project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Loads the persisted catalog if present; otherwise builds and
    persists it as a fallback (e.g. requested before processing has run)."""
    stored = storage.load_catalog(project_id)
    if stored is not None:
        return stored
    return rebuild_and_store(project_id)


def get_usable_graph(project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Loads the persisted usable graph if present; otherwise builds (and
    persists both it and the full catalog) as a fallback."""
    stored = storage.load_usable_graph(project_id)
    if stored is not None:
        return stored
    rebuild_and_store(project_id)
    return storage.load_usable_graph(project_id)


def usable_columns_by_dataset(project_id: str, main_type: str | None = None) -> dict[str, set[str]]:
    """{dataset_name: {column_name, ...}} for every unflagged column in the
    project's usable graph, optionally restricted to one main_type (numeric
    vs string - see app/data_type.py). Shared by anything that needs to
    validate "is this (dataset, column) safe to use" - join discovery
    (app/joinability_model.py) and the join builder (app/join_builder.py)
    both do the unfiltered form; app/gold_layer.py uses main_type="string"
    to find which of a dataset's own columns are worth trying as a join
    base (see its module docstring)."""
    usable = get_usable_graph(project_id)
    by_dataset: dict[str, set[str]] = {}
    for node in usable["nodes"]:
        if node["kind"] != "column":
            continue
        if main_type is not None and node.get("main_type") != main_type:
            continue
        by_dataset.setdefault(node["dataset"], set()).add(node["name"])
    return by_dataset


def usable_column_uniqueness(project_id: str) -> dict[str, dict[str, float]]:
    """{dataset_name: {column_name: uniqueness_ratio}} - the fraction of a
    column's non-null values that are actually distinct (1.0 == every
    non-null value is unique, i.e. behaves like a primary key), for every
    unflagged column in the project's usable graph. Derived entirely from
    already-persisted profile stats (unique_count/null_count on each column
    node, row_count on its dataset node) - no re-scanning the data.

    app/gold_layer.py uses this to require a join *candidate's* own column
    look like a primary key before it's eligible to be joined into
    anything - see its module docstring for why (the base column has no
    such requirement; only the candidate side does)."""
    usable = get_usable_graph(project_id)
    row_counts = {n["name"]: n["row_count"] for n in usable["nodes"] if n["kind"] == "dataset"}
    result: dict[str, dict[str, float]] = {}
    for node in usable["nodes"]:
        if node["kind"] != "column":
            continue
        non_null = row_counts.get(node["dataset"], 0) - node["null_count"]
        ratio = (node["unique_count"] / non_null) if non_null > 0 else 0.0
        result.setdefault(node["dataset"], {})[node["name"]] = ratio
    return result
