"""
Autonomous gold-layer construction: given a project, automatically finds
which datasets are worth joining together, joins them, and curates each
result with DIADA - no human picking a base dataset/column or candidate
list by hand. Nothing about how a join or a curation is actually computed
changes here - app/join_builder.py and app/diada.py are used exactly as
they are for the manual "Perform a join" / "Curate data" UI. What's new is
how the *inputs* to those two steps get chosen.

Every usable dataset is a base - there's no ranking/capping step (an
earlier version picked a top-N subset by score; dropped for now in favor
of processing everything, revisit if that stops being affordable on a
large data lake).

For a given base dataset, discovery isn't run against a single "best"
column - EVERY one of the base dataset's own string-typed usable columns
(main_type == "string" - see app/data_type.py) is tried as a join key in
turn, and the ranked candidates from each run are kept separately. Only
after all of a base's own columns have been tried are the results merged:
if the same candidate dataset turns up under more than one of the base's
columns, it's only actually joined once, through whichever base column
scored it highest (see _candidates_for_base). This is deliberately
different from trying only the single globally-best base column - a
dataset can plausibly be the join key for two unrelated candidate datasets
via two different columns (e.g. customer_id joins one table, product_sku
joins another), and picking only one column up front would silently miss
the other.

The base column and the candidate column were originally held to
different standards. The base column can be anything, unique or not - a
country row's "continent" column is a perfectly good base column even
though many countries share the same continent, because the point of the
join is to look up per-continent data for each country row. The
*candidate* column was, for a while, required to actually be that
dimension's key: if it isn't (near-)unique in its own dataset, "joining"
through it doesn't look anything up on its own - it just attaches some
aggregate over a pile of candidate rows to every base row that happens to
share that value. join_builder.py already aggregates a non-unique
candidate down to one row per key rather than fanning out duplicate base
rows, which makes the join *mechanically* valid regardless; the open
question was whether that aggregate is *meaningful*.

candidate_uniqueness_threshold (DEFAULT_CANDIDATE_UNIQUENESS_THRESHOLD)
used to default to 0.90 to gate this - a candidate had to look
primary-key-like in its own dataset before autonomous mode would touch
it. That default is now 0.0 (no bar at all): a genuinely one-to-many
candidate - several reviews per property, several transactions per
customer - is exactly what aggregation is *for*, and excluding it
entirely by default meant the autonomous pipeline routinely missed real
signal a human would have included by hand (confirmed against
urban_join_demo's resident_reviews - true, non-trivial signal, unique-
per-row-only-a-third-of-the-time - which a 0.90 bar excluded outright).
The risk the old default guarded against - a coincidentally non-unique
candidate averaging together rows that have nothing to do with each other
- hasn't gone away, it's just no longer assumed to be more likely than
the false negative. The config field remains (a project that wants the
old, more conservative behavior back can still set
candidate_uniqueness_threshold explicitly), and this is very much a
first cut at the tradeoff, not a settled one - flag it if autonomous runs
start pulling in aggregates that don't hold up.

  1. For each base dataset, run join discovery (app/joinability_model.py)
     once per string-typed usable column, filter every run's candidates to
     score >= discovery_threshold AND to a candidate column with
     non-null-uniqueness >= candidate_uniqueness_threshold (see
     catalog.usable_column_uniqueness and _candidates_for_base - a no-op
     by default now, see above), and dedupe by candidate dataset (best
     score wins; ties broken alphabetically by column name - deterministic,
     unlike the manual UI's Math.random(), which is fine for a human
     clicking around but not for something meant to be reproducible). Cap
     at max_candidates_per_join.
  2. If nothing cleared the threshold, the dataset goes into curation
     unmodified - "no join" is a valid outcome, not a skip.
  3. Before building anything, a base whose own join would duplicate an
     already-kept base's - the exact same set of datasets joined together
     *at the same granularity* (the base's own row count - see
     _deduplicate_bases for why that's the right, cheap proxy), just
     viewed from a different one's own column - is skipped entirely (see
     GoldLayerRun.redundant_datasets). Same datasets at a *different*
     granularity are kept as separate tables - they're not duplicates,
     e.g. one row per property vs. one row per review touching the exact
     same set of datasets are two genuinely different tables.
  4. Otherwise build the multi-way join (each candidate against whichever
     of the base's columns discovered it - see
     join_builder.JoinCandidateSpec.base_column) and curate it with DIADA:
     every base column that was actually used to join something stays
     protected from removal, identical in spirit to the manual flow. Only
     CF2 ("structure dataset") is ever turned into a gold table - CF1
     ("remove univariate noise") is an intermediate cleaning step, not
     something worth surfacing on its own, and an all-"unclustered"
     CF2 result (DIADA found no relationship among any of the table's
     columns at all) is suppressed too rather than shown as a table with
     nothing structured about it (see _visible_cf2_clusters).
  5. Every table already carries its own explanation: join_candidates says
     which datasets were joined in and through which columns/scores, and
     column_provenance says which source dataset every one of its own
     columns actually came from (sliced straight from
     join_builder.PreparedJoin.column_provenance, the same provenance the
     manual join/curate UI uses) - no separate whole-run graph needed to
     answer "why does this table look like this," just that one table's
     own record.

The default threshold (0.001) is deliberately permissive: it's the same
number the join discovery UI uses as its default "hide obviously
irrelevant candidates" display filter, not a "this is definitely a good
join" bar. Signed off on deliberately: DIADA curation is fast and thorough
enough that a wrong or coincidental candidate mostly just produces a table
(or a cluster within one) curation immediately isolates or drops - being
too permissive here is lower-risk than being too strict and silently
missing real relationships.

Every CF2 cluster is labeled plainly - "Cluster 1", "Cluster 2", ... in
DIADA's own size-descending order, or "Unclustered" for the shared
leftover bucket - see _cf2_tables(). An earlier version tried to invent a
descriptive name from each cluster's own column names; see
table_naming.py's module docstring for why that was dropped rather than
kept as a not-quite-reliable nicety.

Two algorithms, two independent entry points (run_every_table,
run_ad_hoc), each with its own config and its own persisted result (see
storage.GOLD_LAYER_MODES) - building one never touches whatever the
other last produced, and each runs its own discovery pass rather than
sharing one. An earlier version also offered a third view of this same
discovery ("create gold tables"/run_grouped: group mutually-joinable
datasets into connected components and let a caller pick which member
should be the join's base, materializing that specific choice on demand)
- dropped once "Manual composition" covered the same need directly,
letting a human actually walk through join_builder.py themselves rather
than picking from a pre-grouped list.
"""
from __future__ import annotations

import dataclasses
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from .. import diada, join_builder
from ..catalog import catalog, joinability_model
from ..core import data_loader, storage


class GoldLayerRecomputeError(Exception):
    """Raised by materialize_table_df when a persisted gold table can no
    longer be reproduced as-is (e.g. a recorded CF2 cluster no longer
    appears because the underlying data changed since the run)."""

DEFAULT_DISCOVERY_THRESHOLD = 0.001
DEFAULT_MAX_CANDIDATES_PER_JOIN = 4
DEFAULT_MAX_WORKERS = 8
# No uniqueness bar by default - a candidate that clears discovery_threshold
# is included regardless of how unique its own join column is, relying on
# join_builder's existing aggregation to fold a one-to-many candidate down
# to one row per key. Used to default to catalog.PRIMARY_KEY_UNIQUENESS_THRESHOLD
# (0.90, the same "what counts as a primary key" bar declutter's
# unused-primary-key signal uses) - see module docstring for why that was
# loosened and what it traded away. Still a real config field: pass a
# higher value in GoldLayerConfig to restore the old, more conservative
# behavior for a given run.
DEFAULT_CANDIDATE_UNIQUENESS_THRESHOLD = 0.0

# Ad hoc mode's own defaults - kept separate from the three fields above
# rather than reusing them, because ad hoc mode's growth algorithm
# (app/ad_hoc_layer.py) uses candidate_uniqueness_threshold for a genuinely
# different purpose than "every table" mode does. That one only uses it
# to decide whether a one-to-many candidate is worth aggregating and
# including at all - a question where 0.0 (no bar) was deliberately chosen,
# see this module's own docstring. Ad hoc mode keeps a real bar here as a
# general "don't attach on a candidate column that barely looks like a key"
# quality guard - see ad_hoc_layer.py's module docstring and
# docs/ad_hoc_gold_layer.md for the fuller history (this used to also be
# load-bearing for hub misclassification before hub detection was removed).
# max_columns_per_asset/min_match_rate are ad_hoc_layer's own tuning knobs
# (see AdHocConfig); defined here rather than in ad_hoc_layer.py, alongside
# every other DEFAULT_*, so GoldLayerConfig can reference them without a
# circular import - ad_hoc_layer.py imports them back the same way it
# already imports DEFAULT_DISCOVERY_THRESHOLD.
DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD = 0.9
DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET = 80
DEFAULT_AD_HOC_MIN_MATCH_RATE = 0.1


@dataclass
class GoldLayerConfig:
    discovery_threshold: float = DEFAULT_DISCOVERY_THRESHOLD
    max_candidates_per_join: int = DEFAULT_MAX_CANDIDATES_PER_JOIN
    max_workers: int = DEFAULT_MAX_WORKERS
    candidate_uniqueness_threshold: float = DEFAULT_CANDIDATE_UNIQUENESS_THRESHOLD
    # Ad hoc mode only (see the constants' own comment above) - every other
    # field on this dataclass is shared across both gold-layer views.
    ad_hoc_candidate_uniqueness_threshold: float = DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD
    ad_hoc_max_columns_per_asset: int = DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET
    ad_hoc_min_match_rate: float = DEFAULT_AD_HOC_MIN_MATCH_RATE


@dataclass
class JoinScore:
    """One raw (base column, candidate column) pair joinability_model ever
    scored during a run, before gold_layer.py's own discovery_threshold/
    candidate_uniqueness_threshold/max_candidates_per_join/best-per-dataset
    filtering narrows that down to what actually gets used to build
    anything (see _candidates_for_base) - the full, unfiltered discovery
    result GoldLayerRun.all_join_scores carries, so a consumer that wants
    to apply its own threshold (the "Joinability graph" catalog view) can,
    without re-running discovery itself. Directional, not deduped against
    its own reverse pair - scoring (A, a) against B can differ from
    scoring (B, b) against A (the underlying distance features aren't all
    symmetric - see joinability_model.compute_distances' "containment"
    pattern), so both directions are kept as independent, equally valid
    findings rather than one being discarded."""
    base_dataset: str
    base_column: str
    candidate_dataset: str
    candidate_column: str
    score: float


@dataclass
class JoinCandidate:
    dataset: str
    column: str
    score: float
    base_column: str  # which of base_column_dataset's own string columns discovered (and joins) this candidate
    # Which dataset base_column actually belongs to. For "every_table" mode
    # this is always the table's own base_dataset (the only dataset a
    # candidate could ever be discovered through there); "ad hoc" mode
    # needs it explicitly, since a member can be attached via a column that
    # belongs to any already-included dataset in the composite, not just
    # the original base - see ad_hoc_layer.py's _grow_assets.
    base_column_dataset: str = ""


@dataclass
class GoldTable:
    id: str  # "{base_dataset}::cf1" | "{base_dataset}::cf2::{cluster_name}" | "{base_dataset}::joined"
    name: str  # display name, always "{base_dataset}_something" - never a bare cluster name
    kind: str  # "cf1" | "cf2_cluster" | "cf2_unclustered" | "joined_uncurated"
    base_dataset: str  # display identity - for "ad hoc" tables this is a composite display name (see table_naming.group_display_name), not a literal loadable dataset; use root_dataset for that
    columns: list[str]
    row_count: int
    join_candidates: list[JoinCandidate]  # empty if no candidate cleared the threshold - table is the base, unjoined
    aggregated_datasets: list[str]
    renamed_columns: dict[str, str]
    column_provenance: dict[str, str] = field(default_factory=dict)  # column name -> source dataset (sliced from join_builder.PreparedJoin.column_provenance)
    removed_columns: list[str] = field(default_factory=list)  # only meaningful for kind == "cf1"
    declutter_removed_columns: list[str] = field(default_factory=list)  # unused-PK/redundant/mostly-null columns dropped before DIADA ever saw them - see declutter_columns
    original_columns: dict[str, str] = field(default_factory=dict)  # column name -> its original name in that source dataset, before collision-renaming (sliced from join_builder.PreparedJoin.original_columns) - lets a consumer (e.g. app/catalog/catalog.py's governance-edge layering) look up a renamed column's true uniqueness/type in its real source dataset
    root_dataset: str = ""  # the real, loadable anchor dataset - "every_table" mode: always equals base_dataset; "ad hoc" mode: the original seed dataset the composite grew from (see ad_hoc_layer.py's _grow_assets). materialize_table_df's no-join-candidates branch must load this, never base_dataset, since only this field is guaranteed to be a real dataset name.
    group_members: list[str] | None = None  # full, untruncated list of every dataset folded into this table - "ad hoc" mode only; also doubles as "is this an ad-hoc-mode table" for callers (e.g. the frontend)
    ad_hoc: bool = False  # built by app/ad_hoc_layer.py's growth algorithm - a multi-dataset composite, same as "every table" mode's single-dataset tables never are, but grown bottom-up rather than star-joined off one base
    graph: dict[str, Any] | None = None  # diada.structure_dataset's {"nodes": [{id, cluster}], "edges": [{source, target, soundness}]} for CF2 tables - the *whole* base/group's relationship graph (every cluster, not just this one table's own columns), shared verbatim across every cluster produced from the same curation pass, for the frontend's attribute-cluster graph (CurationGraph). None for cf1/joined_uncurated tables, which have no CF2 pass to draw from.


@dataclass
class GoldLayerRun:
    config: GoldLayerConfig
    base_datasets: list[str]
    skipped_datasets: list[str]  # no usable string column at all - can't be tried as a base
    tables: list[GoldTable]  # this algorithm's own gold tables - see storage.GOLD_LAYER_MODES for why this never mixes tables from a different algorithm's run
    build_seconds: float
    redundant_datasets: dict[str, str] = field(default_factory=dict)  # "every table" view only - {skipped_base: earlier_base_it_duplicated} for bases whose own join would repeat an already-kept one, see _deduplicate_bases
    all_join_scores: list[JoinScore] = field(default_factory=list)  # every raw (base column, candidate column) pair discovery ever scored this run, unfiltered - see JoinScore. Layered onto app/catalog.py's graph as column-to-column "joinable" edges for the "Joinability graph" catalog view, which applies its own user-chosen threshold client-side rather than being stuck with config.discovery_threshold's.
    # How much of build_seconds was spent finding candidates
    # (discovery_seconds) vs. building/curating tables from them
    # (composition_seconds) - see run_every_table/run_ad_hoc, both of
    # which have one clean boundary between "score every candidate" and
    # "build from the ones that qualified" to time separately.
    discovery_seconds: float | None = None
    composition_seconds: float | None = None


def _candidates_for_base(
    project_id: str,
    base_dataset: str,
    string_columns: set[str],
    config: GoldLayerConfig,
    column_uniqueness: dict[str, dict[str, float]],
) -> tuple[list[JoinCandidate], list[JoinScore]]:
    """Tries every one of base_dataset's own string-typed usable columns as
    a join key - not just the single best one, see module docstring - and
    keeps, per candidate dataset, whichever base column scored it highest.
    Restricted to score >= discovery_threshold AND to candidate columns that
    behave like a primary key in their own dataset (>=
    candidate_uniqueness_threshold of non-null values distinct - see module
    docstring for why only the candidate side is held to this), capped at
    max_candidates_per_join.

    Returns (chosen, all_scores): `chosen` is that filtered/capped list,
    used to actually build tables; `all_scores` is every (base_column,
    candidate) pair joinability_model scored at all, completely unfiltered
    - see JoinScore. Both are derived from the exact same
    find_join_candidates() calls, so capturing all_scores here costs
    nothing extra - it would otherwise just be discarded."""
    best_per_dataset: dict[str, JoinCandidate] = {}
    all_scores: list[JoinScore] = []
    for base_column in sorted(string_columns):
        try:
            _, ranked = joinability_model.find_join_candidates(project_id, base_dataset, base_column)
        except joinability_model.NotUsableColumnError:
            continue
        for r in ranked:
            all_scores.append(JoinScore(
                base_dataset=base_dataset, base_column=base_column,
                candidate_dataset=r["dataset"], candidate_column=r["column"], score=r["score"],
            ))
            if r["score"] < config.discovery_threshold:
                continue
            uniqueness = column_uniqueness.get(r["dataset"], {}).get(r["column"], 0.0)
            if uniqueness < config.candidate_uniqueness_threshold:
                continue
            candidate = JoinCandidate(
                dataset=r["dataset"], column=r["column"], score=r["score"],
                base_column=base_column, base_column_dataset=base_dataset,
            )
            existing = best_per_dataset.get(candidate.dataset)
            if existing is None or candidate.score > existing.score or (
                candidate.score == existing.score and candidate.column < existing.column
            ):
                best_per_dataset[candidate.dataset] = candidate

    chosen = sorted(best_per_dataset.values(), key=lambda c: (-c.score, c.dataset))
    return chosen[: config.max_candidates_per_join], all_scores


def declutter_columns(
    df: pd.DataFrame,
    prepared: join_builder.PreparedJoin,
    protected: list[str],
    string_columns: dict[str, set[str]],
    column_uniqueness: dict[str, dict[str, float]],
) -> set[str]:
    """The same three signals the manual join UI offers as an opt-in toggle
    (app/main.py's _declutter_info) - unused_primary_key_columns,
    redundant_column_groups, mostly_null_columns - applied unconditionally
    here instead, since nobody reviews an individual gold table before it's
    produced: there's no user in the loop to flip a checkbox, so a column
    that adds no predictive value is just dropped outright.

    unused_primary_key_columns/redundant_column_groups/mostly_null_columns
    each only know how to exempt a single `base_column`, but a gold-layer
    base can protect more than one of its own columns at once (every
    string column that actually discovered a candidate - see module
    docstring and `protected` in _build_and_curate). One of them is picked
    as the representative passed into each function (matching
    prepare_join's own positional-fallback choice below), and every other
    protected column is filtered back out of whatever that function
    flagged, so nothing actually used to join anything is ever dropped."""
    representative = protected[0]
    drop: set[str] = set()
    drop.update(
        c
        for c in join_builder.unused_primary_key_columns(prepared, representative, column_uniqueness, string_columns)
        if c not in protected
    )
    for group in join_builder.redundant_column_groups(
        df, prepared.column_provenance, prepared.original_columns, representative, string_columns
    ):
        drop.update(c for c in group["columns"] if c != group["kept"] and c not in protected)
    drop.update(c for c in join_builder.mostly_null_columns(df, representative) if c not in protected)
    return drop


def _visible_cf2_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """DIADA's CF2 clusters, filtered for display. An "unclustered" bucket
    with no real cluster alongside it means DIADA found no relationship
    among any of this table's columns at all - showing "here's everything,
    ungrouped" on its own isn't informative enough to surface as its own
    gold table, so it's suppressed in that case. A real cluster is always
    shown; "unclustered" is only shown alongside at least one real
    cluster, where it's useful supplementary context (what didn't fit
    anywhere)."""
    if not any(c["name"] != "unclustered" for c in clusters):
        return []
    return clusters


def _cf2_tables(
    visible_clusters: list[dict[str, Any]],
    graph: dict[str, Any],
    id_prefix: str,
    column_provenance: dict[str, str],
    original_columns: dict[str, str],
    common: dict[str, Any],
) -> list[GoldTable]:
    """Turns DIADA's CF2 clusters into GoldTable entries - one per visible
    cluster, plainly labeled ("Cluster 1", "Cluster 2", ... in the same
    size-descending order diada.structure_dataset assigns them, or
    "Unclustered" for the shared leftover bucket) rather than named from
    its own columns - see table_naming.py's module docstring for why an
    invented name was dropped in favor of this (every local, deterministic
    attempt either produced an unreadable word-mashup or, once fixed, just
    concatenated real column names without ever being an actually coherent
    name). The columns shown alongside each table are what convey its
    content instead. Every table gets the same `graph` (the whole
    base/group's relationship graph, not sliced per cluster) so the
    frontend can render one shared attribute-cluster graph per base rather
    than one per cluster. Shared by _build_and_curate ("every table" view)
    and ad_hoc_layer.py's _curate_asset ("ad hoc" view) - the only
    difference between them is what's already baked into
    `id_prefix`/`common` (a bare dataset name vs. a composite display
    name/root)."""
    tables = []
    for cluster in visible_clusters:
        is_unclustered = cluster["name"] == "unclustered"
        display_name = "Unclustered" if is_unclustered else cluster["name"].replace("cluster_", "Cluster ")
        tables.append(GoldTable(
            id=f"{id_prefix}::cf2::{cluster['name']}",
            name=display_name,
            kind="cf2_unclustered" if is_unclustered else "cf2_cluster",
            columns=cluster["columns"],
            column_provenance={c: column_provenance[c] for c in cluster["columns"]},
            original_columns={c: original_columns[c] for c in cluster["columns"]},
            graph=graph,
            **common,
        ))
    return tables


def _build_and_curate(
    project_id: str,
    base_dataset: str,
    candidates: list[JoinCandidate],
    string_columns: dict[str, set[str]],
    column_uniqueness: dict[str, dict[str, float]],
) -> list[GoldTable]:
    declutter_removed: list[str] = []
    if not candidates:
        # No candidate cleared the threshold via any of this base's string
        # columns - the dataset "remains the same": curation still runs,
        # just on the dataset as it already is, nothing joined into it.
        # Nothing was joined, so none of the three declutter signals apply
        # (they're all about *why a join column adds nothing*) - the base
        # dataset's own columns are left exactly as curation would see them
        # in the manual "no candidate selected" case.
        df = data_loader.read_preprocessed(base_dataset)
        aggregated_datasets: list[str] = []
        renamed_columns: dict[str, str] = {}
        column_provenance = {c: base_dataset for c in df.columns}
        original_columns = {c: c for c in df.columns}
        protected: list[str] = []
    else:
        specs = [
            join_builder.JoinCandidateSpec(dataset=c.dataset, column=c.column, base_column=c.base_column)
            for c in candidates
        ]
        # prepare_join's positional base_column is only a fallback for
        # specs that don't set their own - every spec here always does, so
        # any already-tested base column works; it's never actually used as
        # a join key itself.
        prepared = join_builder.prepare_join(project_id, base_dataset, candidates[0].base_column, specs)
        try:
            df = join_builder.to_dataframe(prepared)
            aggregated_datasets = prepared.aggregated_datasets
            renamed_columns = prepared.renamed_columns
            column_provenance = prepared.column_provenance
            original_columns = prepared.original_columns
            protected = sorted({c.base_column for c in candidates})
            drop = declutter_columns(df, prepared, protected, string_columns, column_uniqueness)
            if drop:
                df = df.drop(columns=list(drop))
                declutter_removed = sorted(drop)
        finally:
            prepared.close()

    common = dict(
        base_dataset=base_dataset,
        root_dataset=base_dataset,
        row_count=len(df),
        join_candidates=candidates,
        aggregated_datasets=aggregated_datasets,
        renamed_columns=renamed_columns,
        declutter_removed_columns=declutter_removed,
    )

    try:
        # protected (empty for an unjoined base) is excluded from DIADA's
        # soundness scoring by _curate_group - see its own docstring.
        result = _curate_group(df, protected)
    except diada.DiadaError:
        # Curation failing (e.g. no Java runtime) shouldn't take down the
        # whole run - the join itself (or the bare dataset, if unjoined) is
        # still a valid result worth keeping.
        columns = list(df.columns)
        return [GoldTable(
            id=f"{base_dataset}::joined", name=f"{base_dataset}_joined",
            kind="joined_uncurated", columns=columns,
            column_provenance={c: column_provenance[c] for c in columns},
            original_columns={c: original_columns[c] for c in columns},
            **common,
        )]

    # CF1 ("remove univariate noise") is deliberately never surfaced as its
    # own gold table - it's an intermediate cleaning step, not something
    # worth composing a table around on its own (matching ad hoc mode,
    # which never shows it either). DIADA still computes it as
    # part of the one soundness pass CF2 shares; its dict is just unused here.
    visible_clusters = _visible_cf2_clusters(result["cf2"]["clusters"])
    return _cf2_tables(
        visible_clusters, result["cf2"]["graph"], base_dataset, column_provenance, original_columns, common,
    )


def _discover_common(
    project_id: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, dict[str, float]], list[str], list[str], dict[str, int]]:
    """Setup shared by both algorithms below: which datasets/columns
    are usable at all, which datasets are even eligible as a base (have at
    least one usable string column), and each dataset's row count (already-
    persisted profile stats, no re-scanning). Independent of config - the
    same regardless of which algorithm's own settings are in effect - so
    it's split out rather than duplicated twice."""
    string_usable = catalog.usable_columns_by_dataset(project_id, main_type="string")
    usable = catalog.usable_columns_by_dataset(project_id)
    column_uniqueness = catalog.usable_column_uniqueness(project_id)

    base_datasets: list[str] = []
    skipped: list[str] = []
    for dataset in sorted(usable):
        if string_usable.get(dataset):
            base_datasets.append(dataset)
        else:
            skipped.append(dataset)

    row_counts = {
        n["name"]: n["row_count"] for n in catalog.get_usable_graph(project_id)["nodes"] if n["kind"] == "dataset"
    }
    return string_usable, usable, column_uniqueness, base_datasets, skipped, row_counts


def run_every_table(
    project_id: str,
    config: GoldLayerConfig | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> GoldLayerRun:
    """Builds "every table as gold layer" (aka "starting from every table"):
    one family of gold tables per *kept* base dataset (see
    _deduplicate_bases for why some bases are skipped), from its own
    discovery pass. Runs completely independently of run_ad_hoc - each
    gold-layer algorithm has its own config, its own discovery pass, and
    its own persisted result (see app/storage.py's per-mode save/load
    functions and app/gold_layer_job.py's per-mode background state) -
    building one never touches what the other last produced.

    on_progress(label, completed_count, total_count) fires once at the
    very start (indeterminate, completed=total=0, before any base's own
    discovery has finished), then once per base as its own discovery
    finishes (see _discover_edges) and again once per kept base's table
    family finishes curating - each with a ready-to-display label (e.g.
    'Finding join candidates for "district_properties" (2 of 5
    datasets)', then later 'Joining and composing "district_properties"
    (2 of 5 bases)') rather than a bare dataset name - app/gold_layer_job.py's
    background runner uses this to drive progress polling; a direct call
    (tests, a script) can just leave it out.

    Bases are processed up to config.max_workers at a time (threads, not
    processes): each base's own work - join_builder's own in-memory DuckDB
    connection, diada.compute_soundness's own isolated temp dir and JVM
    subprocess - is already fully self-contained with no shared mutable
    state between bases (diada.py's module docstring calls this out
    explicitly: "concurrent curate requests can't collide"), and the
    dominant per-base cost is waiting on that JVM subprocess, which
    releases the GIL for its whole duration - so threads parallelize the
    wall-clock time well despite the GIL, without needing to pickle
    DuckDB connections or subprocess handles across a process boundary.

    discovery_seconds/composition_seconds on the returned GoldLayerRun
    split the wall time either side of the boundary between "score every
    candidate" (_discover_edges) and "join + curate every kept base"
    (the thread pool below) - the same two-phase split as run_ad_hoc's,
    just within one function instead of across gold_layer.py/
    ad_hoc_layer.py, since this view has no separate growth step in
    between."""
    config = config or GoldLayerConfig()
    started = time.monotonic()

    string_usable, _usable, column_uniqueness, base_datasets, skipped, row_counts = _discover_common(project_id)

    if on_progress:
        on_progress("Finding join candidates across the lake", 0, 0)
    discover_started = time.monotonic()
    edges, all_join_scores = _discover_edges(
        project_id, base_datasets, string_usable, config, column_uniqueness, on_progress=on_progress,
    )
    discovery_seconds = round(time.monotonic() - discover_started, 2)

    # Skip a base whose own join would repeat an already-kept one, at the
    # same granularity - see _deduplicate_bases.
    kept_bases, redundant_datasets = _deduplicate_bases(base_datasets, edges, row_counts)

    def build_one(base_dataset: str) -> list[GoldTable]:
        return _build_and_curate(project_id, base_dataset, edges.get(base_dataset, []), string_usable, column_uniqueness)

    compose_started = time.monotonic()
    tables: list[GoldTable] = []
    with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as pool:
        futures = {pool.submit(build_one, base_dataset): base_dataset for base_dataset in kept_bases}
        completed = 0
        for future in as_completed(futures):
            tables.extend(future.result())
            completed += 1
            if on_progress:
                base_dataset = futures[future]
                on_progress(f'Joining and composing "{base_dataset}" ({completed} of {len(kept_bases)} bases)', completed, len(kept_bases))
    composition_seconds = round(time.monotonic() - compose_started, 2)

    build_seconds = round(time.monotonic() - started, 2)
    return GoldLayerRun(
        config=config, base_datasets=base_datasets, skipped_datasets=skipped, tables=tables,
        redundant_datasets=redundant_datasets, build_seconds=build_seconds, all_join_scores=all_join_scores,
        discovery_seconds=discovery_seconds, composition_seconds=composition_seconds,
    )


def run_ad_hoc(
    project_id: str,
    config: GoldLayerConfig | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> GoldLayerRun:
    """Builds "ad hoc": composite assets grown bottom-up rather than one
    star join per base, from its own uncapped discovery pass (see
    ad_hoc_layer.discover_and_run's docstring). Imported here, not at
    module level: ad_hoc_layer.py imports GoldTable and friends back from
    this module, so a top-level import would cycle.

    on_progress(label, completed, total) fires at each phase boundary
    (discovery, then growth - both indeterminate, completed=total=0) and
    once per asset as curation finishes it (a real completed/total count)
    - see ad_hoc_layer.discover_and_run's own docstring for the exact
    sequence."""
    config = config or GoldLayerConfig()
    started = time.monotonic()

    string_usable, _usable, column_uniqueness, base_datasets, skipped, row_counts = _discover_common(project_id)

    from . import ad_hoc_layer
    tables, all_join_scores, discovery_seconds, composition_seconds = ad_hoc_layer.discover_and_run(
        project_id, base_datasets, string_usable, row_counts, column_uniqueness,
        config.discovery_threshold, config.max_workers, config.ad_hoc_candidate_uniqueness_threshold,
        config.ad_hoc_max_columns_per_asset, config.ad_hoc_min_match_rate,
        on_progress=on_progress,
    )

    build_seconds = round(time.monotonic() - started, 2)
    return GoldLayerRun(
        config=config, base_datasets=base_datasets, skipped_datasets=skipped, tables=tables,
        build_seconds=build_seconds, all_join_scores=all_join_scores,
        discovery_seconds=discovery_seconds, composition_seconds=composition_seconds,
    )


def _discover_edges(
    project_id: str,
    base_datasets: list[str],
    string_usable: dict[str, set[str]],
    config: GoldLayerConfig,
    column_uniqueness: dict[str, dict[str, float]],
    on_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[dict[str, list[JoinCandidate]], list[JoinScore]]:
    """Every base dataset's own candidates (_candidates_for_base, unchanged
    - it's pure/side-effect-free), computed independently and in parallel.
    Called by run_every_table, `edges` used directly as each base's own
    candidate list - ad_hoc_layer.py runs its own, differently-configured
    discovery pass instead of sharing this one (see the module docstring).
    The `all_scores` half is every raw score every base's discovery pass
    produced, unfiltered - see JoinScore and GoldLayerRun.all_join_scores;
    ad_hoc_layer.py's discover_and_run captures and returns its own copy
    of this the same way (its own, differently-configured discovery pass
    over the same base/column pairs), while its rebuild_asset (a one-off
    recompute for a single preview, never persisted) discards it and
    passes no on_progress - it never drives a progress bar.

    on_progress(label, completed, total), if given, fires once per base
    as its own discovery finishes - a real completed/total count, same
    shape as the composition-phase progress both callers already report,
    letting a caller show "found candidates for N of M datasets" instead
    of the discovery phase sitting at an indeterminate 0/0 for however
    long the slowest base's joinability-model calls take."""
    def discover_one(base_dataset: str) -> tuple[list[JoinCandidate], list[JoinScore]]:
        return _candidates_for_base(project_id, base_dataset, string_usable[base_dataset], config, column_uniqueness)

    edges: dict[str, list[JoinCandidate]] = {}
    all_scores: list[JoinScore] = []
    with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as pool:
        futures = {pool.submit(discover_one, base_dataset): base_dataset for base_dataset in base_datasets}
        completed = 0
        for future in as_completed(futures):
            base_dataset = futures[future]
            chosen, scores = future.result()
            edges[base_dataset] = chosen
            all_scores.extend(scores)
            completed += 1
            if on_progress:
                on_progress(
                    f'Finding join candidates for "{base_dataset}" ({completed} of {len(base_datasets)} datasets)',
                    completed, len(base_datasets),
                )
    return edges, all_scores


def _deduplicate_bases(
    base_datasets: list[str], edges: dict[str, list[JoinCandidate]], row_counts: dict[str, int]
) -> tuple[list[str], dict[str, str]]:
    """Detects when two different bases would produce the same "every
    table" gold table in substance - the same set of datasets joined
    together *at the same granularity* - and keeps only the first (bases
    are iterated in their existing sorted, deterministic order, so this is
    reproducible run to run).

    Same dataset set is not enough on its own: join_builder's star join
    always preserves every one of the base's own rows exactly once (a
    LEFT JOIN, with any non-uniquely-keyed candidate pre-aggregated down
    to one row per key before joining, never fanned out from) - so the
    result's row count is always exactly the base dataset's own row
    count, whichever candidates it pulls in. Two bases spanning the exact
    same datasets can still be at completely different granularities (an
    urban_join_demo check found district_properties/permit_applications/
    property_listings/resident_reviews/weather_stations all connected to
    the same component, but rooting at resident_reviews yields one row
    per review - 9000 - while rooting at permit_applications yields one
    row per permit - 20; wildly different tables that happen to touch the
    same datasets) - so both the dataset set *and* the base's own row
    count have to match before two bases count as the same join.

    A base with no candidates at all (an unjoined, single-dataset result)
    is never considered redundant with another - every bare, unjoined
    dataset is its own distinct thing regardless of how many other bare
    datasets there are; only an actual multi-dataset join can duplicate
    another one.

    Returns (kept_bases, redundant_datasets) - redundant_datasets maps
    each skipped base to whichever earlier, still-kept base's join it
    would have duplicated, so the caller can surface *why* a base was
    skipped rather than just that it was."""
    seen: dict[tuple[frozenset[str], int], str] = {}
    kept: list[str] = []
    redundant: dict[str, str] = {}
    for base_dataset in base_datasets:
        candidates = edges.get(base_dataset, [])
        dataset_set = frozenset({base_dataset} | {c.dataset for c in candidates})
        key = (dataset_set, row_counts.get(base_dataset, 0))
        if len(dataset_set) > 1 and key in seen:
            redundant[base_dataset] = seen[key]
            continue
        seen[key] = base_dataset
        kept.append(base_dataset)
    return kept, redundant


def _curate_group(df: pd.DataFrame, protected_columns: list[str]) -> dict[str, Any]:
    """Mirrors diada.curate(df, protected_columns), except protected_columns
    (the real column(s) a join was actually performed on - a join key) are
    excluded from what DIADA's soundness computation scores at all, not
    just exempted from being dropped afterward - remove_univariate_noise/
    structure_dataset still receive the full, all-columns df to slice their
    *output* rows and columns from, so the delivered table keeps every
    protected column regardless.

    A join key isn't analytical content - it's the mechanism that pasted
    every other attached column on in the first place - so it necessarily
    "predicts" those columns perfectly, by construction, regardless of
    whether they have any real relationship to each other. Left in, that
    trivial self-relationship acts as a hub connecting otherwise unrelated
    groups of columns into one another, since DIADA's score reflects
    multivariate dependency, not an isolated pairwise correlation -
    confirmed directly: a table joined at review grain (a non-unique key,
    repeated several times per property) collapsed nearly every column
    into one giant cluster with the key column left in, and split cleanly
    into the correct, real groups with it excluded - matching the same
    table joined at property grain (unique key, nothing repeated) either
    way. See docs/ad_hoc_gold_layer.md for the full investigation.

    An earlier version of this function also de-duplicated the soundness
    basis on the join key(s) before scoring, guarding against a *related*
    but distinct risk: diada.compute_soundness assumes every row is an
    independent observation, which is false whenever a table's root isn't
    uniquely keyed on the column(s) it used to attach the rest of the
    group - repeating the same property's data across 3 reviews doesn't
    add 3 independent data points. That turned out to be the wrong fix for
    the wrong problem: once the join key itself is excluded from scoring
    (above), de-duplication no longer removes any bias - it was never the
    key *column* that caused the reported bias, the key column was already
    the actual cause of the dramatic failure mode this function now
    prevents directly - and it costs DIADA real statistical power for no
    remaining benefit. Confirmed directly: two columns that are both
    constant per property (so de-duplicating can't be "correcting" a real
    difference between them) scored 8.9 on the full, un-deduplicated table
    and only 1.1 after de-duplicating down to one row per property - the
    same real relationship, just harder for DIADA to detect with fewer
    observations of it. See docs/ad_hoc_gold_layer.md.

    One more adjustment: the soundness basis is re-sorted into a canonical,
    content-derived row order (a hash of each row) before scoring, rather
    than whatever incidental order the join happened to produce it in.
    diada.compute_soundness turned out not to be invariant to row order for
    a pair with no real relationship (a genuinely related pair's score
    barely moves regardless of ordering) - confirmed directly: one
    unchanged dataframe, scored three ways with nothing but row order
    varied, gave a >5x range for one null pair while two strongly-related
    pairs stayed within about a point of each other. Sorting canonically
    doesn't remove whatever noise causes that (DIADA's own scoring is
    opaque from here - a vendored external tool), but it does mean the
    exact same data always scores the same way regardless of which code
    path happened to build it, rather than however that path's incidental
    join order landed - which was concretely observed to disagree across
    "every table" mode and ad hoc mode's own build of the same anchor.
    Only reorders the copy handed to DIADA - the delivered table's own row
    order (and everything else about it) is unaffected. See docs/
    ad_hoc_gold_layer.md."""
    non_key_columns = [c for c in df.columns if c not in protected_columns]
    if not non_key_columns:
        # Nothing left to score once the join key(s) are excluded - happens
        # when declutter_columns has already stripped every other column
        # (e.g. a small base dataset whose only other column was an unused
        # primary key, joined against a candidate whose only attached
        # column came back almost entirely null - a low-match-rate join
        # against a much bigger candidate dataset - and got dropped as
        # mostly-null too). pd.util.hash_pandas_object raises "Length of
        # values (0) does not match length of index" on a zero-column
        # frame, and DIADA has no pair to compare with only one column
        # anyway, so skip straight to the same empty result
        # diada.compute_soundness itself returns when it finds zero
        # qualifying pairs.
        soundness_df = pd.DataFrame(columns=["column_1", "column_2", "soundness"])
    else:
        soundness_input = df[non_key_columns]
        canonical_order = pd.util.hash_pandas_object(soundness_input, index=False).argsort()
        soundness_df = diada.compute_soundness(soundness_input.iloc[canonical_order])
    return {
        "cf1": diada.remove_univariate_noise(df, soundness_df, protected_columns),
        "cf2": diada.structure_dataset(df, soundness_df, protected_columns),
    }


def run_to_dict(run_result: GoldLayerRun) -> dict[str, Any]:
    """Plain-dict form for persistence (app/storage.py) and JSON API
    responses - everything in GoldLayerRun is a simple dataclass, so a
    recursive asdict() covers it with no custom (de)serialization needed."""
    return dataclasses.asdict(run_result)


def find_table(project_id: str, table_id: str) -> dict[str, Any]:
    """Looks up one gold table's persisted recipe by id, checking whichever
    of the two algorithms' last runs actually produced it (see
    storage.GOLD_LAYER_MODES) - a bare table id doesn't say which
    algorithm built it. Raises GoldLayerRecomputeError if no algorithm has
    a persisted run at all, or none of them contains a table with this id
    - the caller (app/main.py) translates this to a 404."""
    runs = storage.load_all_gold_layers(project_id)
    if all(stored is None for stored in runs.values()):
        raise GoldLayerRecomputeError("No gold-layer run has been persisted yet for this project")
    for stored in runs.values():
        if stored is None:
            continue
        table = next((t for t in stored["tables"] if t["id"] == table_id), None)
        if table is not None:
            return table
    raise GoldLayerRecomputeError(f"No gold table with id '{table_id}'")


def materialize_table_df(project_id: str, table: dict[str, Any]) -> pd.DataFrame:
    """Rebuilds one gold table's actual row data fresh from its persisted
    recipe (base/join_candidates/kind) - nothing here is persisted, same
    "recompute on demand" approach as the ad-hoc join/curate endpoints.
    Re-applies the same automatic declutter drop this module's own build
    does (see declutter_removed_columns), so a recomputed preview/download
    matches the column set the run actually reported rather than
    resurrecting columns the build already decided were useless.

    Moved here from app/main.py's _recompute_gold_table_df (same logic),
    since this module has no business knowing about HTTP status codes:
    diada.DiadaError propagates unchanged (the caller already handles it),
    and GoldLayerRecomputeError replaces the inline HTTPException this used
    to raise directly."""
    join_candidates = table["join_candidates"]
    is_ad_hoc = bool(table.get("ad_hoc"))

    if is_ad_hoc:
        # Ad hoc's build isn't a simple linear recipe the way "every
        # table" mode's single star join is - which side of a merge keeps
        # its own anchor depends on row counts decided fresh at build
        # time, so a recorded step list alone can't reconstruct it. The
        # only correct way to reproduce one table is to rerun the whole
        # growth pass and pick out the matching asset - see
        # ad_hoc_layer.rebuild_asset.
        from . import ad_hoc_layer
        stored = storage.load_gold_layer(project_id, "ad_hoc")
        run_config = (stored or {}).get("config") or {}
        base_datasets = sorted(catalog.usable_columns_by_dataset(project_id, main_type="string").keys())
        string_columns = catalog.usable_columns_by_dataset(project_id, main_type="string")
        column_uniqueness = catalog.usable_column_uniqueness(project_id)
        row_counts = {
            n["name"]: n["row_count"] for n in catalog.get_usable_graph(project_id)["nodes"] if n["kind"] == "dataset"
        }
        asset = ad_hoc_layer.rebuild_asset(
            project_id, base_datasets, string_columns, row_counts, column_uniqueness,
            run_config.get("discovery_threshold", DEFAULT_DISCOVERY_THRESHOLD),
            run_config.get("max_workers", DEFAULT_MAX_WORKERS),
            table["root_dataset"],
            run_config.get("ad_hoc_candidate_uniqueness_threshold", DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD),
            run_config.get("ad_hoc_max_columns_per_asset", DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET),
            run_config.get("ad_hoc_min_match_rate", DEFAULT_AD_HOC_MIN_MATCH_RATE),
        )
        if asset is None:
            raise GoldLayerRecomputeError(
                f"'{table['root_dataset']}' no longer anchors an ad-hoc asset when this is recomputed - the underlying data may have changed since the gold layer was last built."
            )
        df, protected, _declutter_removed = ad_hoc_layer._declutter_asset(asset, string_columns, column_uniqueness)
    elif not join_candidates:
        # This base had no candidate clear the threshold on any of its own
        # string columns - the table is just the base dataset, unjoined.
        df = data_loader.read_preprocessed(table["root_dataset"])
        protected = []
    else:
        specs = [
            join_builder.JoinCandidateSpec(dataset=c["dataset"], column=c["column"], base_column=c["base_column"])
            for c in join_candidates
        ]
        prepared = join_builder.prepare_join(project_id, table["base_dataset"], join_candidates[0]["base_column"], specs)
        try:
            df = join_builder.to_dataframe(prepared)
            protected = sorted({c["base_column"] for c in join_candidates})
            string_columns = catalog.usable_columns_by_dataset(project_id, main_type="string")
            column_uniqueness = catalog.usable_column_uniqueness(project_id)
            drop = declutter_columns(df, prepared, protected, string_columns, column_uniqueness)
            if drop:
                df = df.drop(columns=list(drop))
        finally:
            prepared.close()

    if table["kind"] == "joined_uncurated":
        return df

    # DiadaError propagates - caller translates.
    result = _curate_group(df, protected)

    if table["kind"] == "cf1":
        return result["cf1"]["dataframe"]

    cluster_name = table["id"].split("::cf2::", 1)[1]
    cluster = next((c for c in result["cf2"]["clusters"] if c["name"] == cluster_name), None)
    if cluster is None:
        raise GoldLayerRecomputeError(
            f"Cluster '{cluster_name}' no longer appears when this join is recomputed - the underlying data may have changed since the gold layer was last built."
        )
    return cluster["dataframe"]
