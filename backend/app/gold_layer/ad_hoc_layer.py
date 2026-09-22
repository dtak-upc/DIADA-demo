"""
Ad-hoc gold-layer construction: grows composite assets bottom-up across the
whole lake, rather than one star join per base (gold_layer.py's "every
table as gold layer"). Every usable dataset gets
exactly one "turn", visited in descending row-count order; each turn tries
every one of that dataset's own discovered candidates (no top-N cap, just
a score threshold - see AdHocConfig), and when a candidate already belongs
to some other growing asset, the two assets merge - so a join can chain
through already-joined tables, not just raw ones (e.g. A+B already merged,
C+D already merged, C discovers A -> the whole thing becomes A+B+C+D, see
_attempt_attach/_process_candidate). Growth for a given asset simply stops
once the next attach would push it past max_columns_per_asset (see
AdHocConfig) - there's no other stopping condition and no attempt to
recognize when a dataset is shared by many others (a "hub"); see
docs/ad_hoc_gold_layer.md for why that was deliberately left out of this
first version and what a future version would need to add it back
correctly.

Two things this needs that gold_layer.py's existing machinery doesn't
already provide:

  - Attaching an already-joined, multi-dataset frame as a *candidate*
    (join_builder.attach_prepared_candidate) - prepare_join/build_chain
    only ever attach one raw dataset as a candidate, or extend a
    multi-dataset *base*, never both sides multi-dataset at once.

  - Deciding, at every attach or merge, which side keeps its own row count
    (the finer-grained one - more rows - stays base) and which one
    aggregates down to fit, regardless of which side "discovered" the
    other, and regardless of whether either side is a single raw dataset
    or an already-grown composite - see _attempt_attach.

Two-stage attach when a discovered candidate is a non-anchor member of
another asset: try attaching that asset's *whole* current composite first
(more columns, but its non-anchor columns might have been aggregated by an
earlier join, diluting one specific column's values without necessarily
showing up as an obvious problem); if the resulting match rate is too low,
retry with a fresh, untouched copy of just the discovered dataset alone
(fewer columns, but always safe values); reject only if neither works.
This is deliberately empirical rather than trying to reason in advance
about *why* a column might be unsafe (was it aggregated, by what
function) - the two-stage retry finds out empirically instead, and only
pays for the extra join in the cases that are actually uncertain, which
also means a genuinely safe column (the common case) always succeeds on
the first, cheaper try.

Without hub handling, a dataset that many others reference (e.g. a shared
"stores" or "regions" dimension) is simply claimed by whichever asset's
turn discovers it first (largest row count first, ties broken
alphabetically - see _grow_assets' `order`); every other dataset that later
discovers the same already-claimed dataset goes through the ordinary
two-stage attach/merge path in _process_candidate, competing on row count
like anything else. That's a real behavioral limitation, not just an
implementation gap - see docs/ad_hoc_gold_layer.md.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .. import diada, join_builder
from ..core import data_loader, table_naming
from .gold_layer import (
    DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD,
    DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET,
    DEFAULT_AD_HOC_MIN_MATCH_RATE,
    DEFAULT_DISCOVERY_THRESHOLD,
    DEFAULT_MAX_WORKERS,
    GoldLayerConfig,
    GoldTable,
    JoinCandidate,
    _cf2_tables,
    _curate_group,
    _discover_edges,
    _visible_cf2_clusters,
    declutter_columns,
)


@dataclass
class AdHocConfig:
    discovery_threshold: float = DEFAULT_DISCOVERY_THRESHOLD
    max_columns_per_asset: int = DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET
    min_match_rate: float = DEFAULT_AD_HOC_MIN_MATCH_RATE


@dataclass
class _Asset:
    anchor: str  # real, loadable dataset - whichever side won the most recent row-count comparison
    df: pd.DataFrame
    column_provenance: dict[str, str]
    original_columns: dict[str, str]
    aggregated_datasets: list[str]
    renamed_columns: dict[str, str]
    members: list[str]  # every real dataset whose columns appear here, display order - includes fallback-duplicated datasets
    canonical_members: set[str] = field(default_factory=set)  # subset of members whose *own* future turn should redirect here - excludes fallback duplicates (their canonical home is elsewhere)
    join_candidates: list[JoinCandidate] = field(default_factory=list)  # every attach/merge step that built this, in order - explains the asset the same way GoldTable.join_candidates does elsewhere


def _standalone_asset(dataset: str, canonical: bool = True) -> _Asset:
    """canonical=False is for the two-stage attach's fallback copy (see
    _process_candidate): it's real member data (shows up in `members`),
    but it must never end up in `canonical_members` - if it did, _commit
    would repoint `dataset`'s own future turn at whatever it just got
    attached to here, when its real canonical home is the asset it already
    belongs to."""
    df = data_loader.read_preprocessed(dataset)
    return _Asset(
        anchor=dataset, df=df,
        column_provenance={c: dataset for c in df.columns},
        original_columns={c: c for c in df.columns},
        aggregated_datasets=[], renamed_columns={},
        members=[dataset], canonical_members={dataset} if canonical else set(),
    )


def _resolve_real_column(asset: _Asset, dataset: str, column: str) -> str:
    """The result column of `asset` that currently represents
    (dataset, column)'s real identity - resolved via provenance/
    original_columns exactly like join_builder.build_chain does, so this
    works whether `asset` is a single raw dataset or something several
    merges deep."""
    return next(
        result_col for result_col in asset.df.columns
        if asset.column_provenance[result_col] == dataset and asset.original_columns[result_col] == column
    )


def _match_rate(merged_df: pd.DataFrame, new_columns: list[str]) -> float:
    """Fraction of base rows for which *at least one* newly-attached
    column is non-null - the best-case reading among the new columns
    (rather than an average, which an incidentally-sparse source column
    could drag down even though the join itself found a real match)."""
    if not new_columns:
        return 1.0
    return float(merged_df[new_columns].notna().any(axis=1).mean())


def _attempt_attach(
    current: _Asset, current_column: tuple[str, str],
    other: _Asset, other_column: tuple[str, str],
    config: AdHocConfig,
    force_current_as_base: bool = False,
) -> _Asset | None:
    """The one operation everything else in this module is built from:
    attach `other` onto `current` (or the reverse - whichever has more
    rows stays base, see the module docstring), via the given real
    (dataset, column) identity on each side. Returns the merged asset, or
    None if it should be rejected (too wide, or too few real matches) -
    rejection never raises, since "this particular attach isn't worth
    keeping" is an expected, common outcome here, not an error.

    force_current_as_base=True skips the row-count comparison and keeps
    `current` as base no matter what - used only by _process_candidate's
    fallback (non-canonical, extra_member) attach, where `other` is a
    disposable fresh re-read of one dataset being tacked on as an extra
    member, never meant to take over the anchor. Without this, a big
    enough fresh copy could win the row-count comparison and become base
    instead - reassigning the result's anchor to `other`'s own dataset
    name. Since that exact fallback can fire independently from more than
    one turn (any asset with `d` as a non-canonical member can try
    re-reading it fresh), two unrelated fallback attaches can both anchor
    at the same name, and the second one to commit silently overwrites
    the first in the shared `assets` dict - orphaning every membership
    entry that still pointed at the one that just got clobbered (a later
    lookup for one of its real members then fails with StopIteration,
    since assets[that name] is now a completely different asset)."""
    if force_current_as_base or len(current.df) >= len(other.df):
        base, base_id, cand, cand_id = current, current_column, other, other_column
    else:
        base, base_id, cand, cand_id = other, other_column, current, current_column

    prospective_width = len(base.df.columns) + (len(cand.df.columns) - 1)
    if prospective_width > config.max_columns_per_asset:
        return None

    base_column = _resolve_real_column(base, *base_id)
    cand_column = _resolve_real_column(cand, *cand_id)
    base_columns_before = set(base.df.columns)

    prepared = join_builder.attach_prepared_candidate(
        base_dataset=base.anchor, base_column=base_column, base_df=base.df,
        base_column_provenance=base.column_provenance, base_original_columns=base.original_columns,
        candidate_key_column=cand_column, candidate_df=cand.df,
        candidate_column_provenance=cand.column_provenance, candidate_original_columns=cand.original_columns,
    )
    try:
        merged_df = join_builder.to_dataframe(prepared)
    finally:
        prepared.close()

    new_columns = [c for c in prepared.columns if c not in base_columns_before]
    if _match_rate(merged_df, new_columns) < config.min_match_rate:
        return None

    return _Asset(
        anchor=base.anchor, df=merged_df,
        column_provenance=prepared.column_provenance, original_columns=prepared.original_columns,
        aggregated_datasets=base.aggregated_datasets + prepared.aggregated_datasets,
        renamed_columns={**base.renamed_columns, **prepared.renamed_columns},
        members=base.members + [m for m in cand.members if m not in base.members],
        canonical_members=base.canonical_members | cand.canonical_members,
        join_candidates=base.join_candidates,
    )


def _process_candidate(
    current: _Asset, cand: JoinCandidate,
    membership: dict[str, str], assets: dict[str, _Asset],
    config: AdHocConfig,
) -> _Asset:
    """One discovered candidate, from the current turn's own base dataset.
    Dispatches on the discovered dataset's current status - see the module
    docstring for the two-stage attach."""
    d = cand.dataset
    if d in current.members:
        return current  # already part of this exact asset - nothing to do

    current_id = (cand.base_column_dataset, cand.base_column)
    other_id = (d, cand.column)

    if d not in membership:
        raw = _standalone_asset(d)
        result = _attempt_attach(current, current_id, raw, other_id, config)
        return _commit(result, current, [raw.anchor], membership, assets) if result else current

    other = assets[membership[d]]
    if other.anchor == current.anchor:
        return current

    if d == other.anchor or len(other.canonical_members) == 1:
        # d anchors its own asset, or that asset is just d alone (single-
        # dataset "asset", nothing to distinguish) - always safe, no
        # aggregation risk regardless of which column was discovered, and
        # nothing smaller to retry if this fails.
        result = _attempt_attach(current, current_id, other, other_id, config)
        return _commit(result, current, [other.anchor], membership, assets) if result else current

    # d is a non-anchor member of a real multi-dataset asset - two-stage.
    result = _attempt_attach(current, current_id, other, other_id, config)
    if result is not None:
        return _commit(result, current, [other.anchor], membership, assets)

    # canonical=False: `other` stays d's real home. Its own future turn
    # (if not already past) must still redirect there, not here - getting
    # this wrong would silently corrupt `membership` for a dataset whose
    # turn hasn't happened yet, depending on processing order.
    # force_current_as_base=True: `raw` is a disposable fresh re-read of
    # `d` alone, tacked on as an extra member - it must never win the
    # anchor away from `current` (see _attempt_attach's own docstring for
    # the anchor-collision bug that let it clobber a different asset).
    raw = _standalone_asset(d, canonical=False)
    result = _attempt_attach(current, current_id, raw, other_id, config, force_current_as_base=True)
    if result is not None:
        # Fallback: only `d` itself joins in, as a fresh copy - `other`
        # is untouched, and d's *canonical* home stays `other` (its own
        # future turn still redirects there, not here) - see _commit.
        return _commit(result, current, [], membership, assets, extra_member=d)
    return current


def _commit(
    result: _Asset, previously: _Asset, absorbed: list[str],
    membership: dict[str, str], assets: dict[str, _Asset],
    extra_member: str | None = None,
) -> _Asset:
    """After a successful attach/merge, retires whichever assets got
    folded into `result` and repoints every canonical member's own future
    turn at the new anchor. `absorbed` is the anchor name of any asset
    that was fully merged in (its own dict entry disappears); a plain
    fallback attach (extra_member set, nothing in `absorbed`) only adds
    one dataset as a non-canonical member, and leaves its real owner's
    entry untouched."""
    assets.pop(previously.anchor, None)
    for anchor in absorbed:
        assets.pop(anchor, None)
    if extra_member is not None:
        result.members = result.members + [extra_member] if extra_member not in result.members else result.members
    assets[result.anchor] = result
    for member in result.canonical_members:
        membership[member] = result.anchor
    return result


def discover_and_run(
    project_id: str,
    base_datasets: list[str],
    string_usable: dict[str, set[str]],
    row_counts: dict[str, int],
    column_uniqueness: dict[str, dict[str, float]],
    discovery_threshold: float,
    max_workers: int,
    candidate_uniqueness_threshold: float = DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD,
    max_columns_per_asset: int = DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET,
    min_match_rate: float = DEFAULT_AD_HOC_MIN_MATCH_RATE,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[list[GoldTable], list, float, float]:
    """Runs its own, uncapped discovery pass (every candidate above
    threshold, no top-N limit - see the module docstring for why this
    view needs that where the other two don't) and grows it into gold
    tables. Called by gold_layer.run_ad_hoc() (the live build) - the only
    caller; a single already-built table's preview/download instead goes
    through rebuild_asset below, which only needs one asset, not a full
    build - so this is where "how ad hoc gets its edges" lives for the
    build path specifically.

    candidate_uniqueness_threshold has its own, higher default here than
    the other two views' shared DEFAULT_CANDIDATE_UNIQUENESS_THRESHOLD -
    see gold_layer.py's DEFAULT_AD_HOC_* comment. max_columns_per_asset/
    min_match_rate configure the growth algorithm itself - see
    AdHocConfig.

    Returns (tables, all_scores, discovery_seconds, composition_seconds):
    all_scores exists because, now that each of the two gold-layer
    algorithms can be built completely independently (see gold_layer.py's
    module docstring), ad hoc can no longer assume "every table" already
    captured all_join_scores from its own, identically-shaped discovery
    pass in the same run - it might be the only algorithm ever built for
    this project, so it captures and returns its own copy rather than
    discarding it. discovery_seconds/
    composition_seconds split the wall time either side of the one clean
    boundary this view has between "find candidates" and "grow + curate
    assets from them" - see gold_layer.GoldLayerRun's own field comments
    for why the other two views don't get an equivalent split.

    on_progress(label, completed, total) is called at each phase
    boundary - "Finding join candidates..." fires once with
    completed=total=0 before any base's own discovery has finished, then
    once per base as its own discovery finishes (see _discover_edges,
    real completed/total count); "Growing composite assets..." likewise
    fires once with completed=total=0 before growth's first turn, then
    once per dataset as its own turn finishes (see _grow_assets, real
    completed/total count); then once per asset as curation finishes them
    (see run_ad_hoc below) with a real completed/total count."""
    if on_progress:
        on_progress("Finding join candidates across the lake", 0, 0)
    discover_config = GoldLayerConfig(
        discovery_threshold=discovery_threshold,
        max_candidates_per_join=len(base_datasets) or 1,
        max_workers=max_workers,
        candidate_uniqueness_threshold=candidate_uniqueness_threshold,
    )
    discover_started = time.monotonic()
    edges, all_scores = _discover_edges(
        project_id, base_datasets, string_usable, discover_config, column_uniqueness, on_progress=on_progress,
    )
    discovery_seconds = round(time.monotonic() - discover_started, 2)

    if on_progress:
        on_progress("Growing composite assets", 0, 0)
    compose_started = time.monotonic()
    tables = run_ad_hoc(
        project_id, base_datasets, edges, row_counts, string_usable, column_uniqueness,
        AdHocConfig(
            discovery_threshold=discovery_threshold,
            max_columns_per_asset=max_columns_per_asset, min_match_rate=min_match_rate,
        ),
        max_workers=max_workers,
        on_progress=on_progress,
    )
    composition_seconds = round(time.monotonic() - compose_started, 2)

    return tables, all_scores, discovery_seconds, composition_seconds


def _grow_assets(
    base_datasets: list[str],
    edges: dict[str, list[JoinCandidate]],
    row_counts: dict[str, int],
    config: AdHocConfig,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, _Asset]:
    """The growth algorithm itself, with no curation attached - one turn
    per dataset (descending row count), attaching/merging every one of
    that turn's own candidates. See the module docstring.

    Deliberately its own function, separate from run_ad_hoc's curation
    step: this alone is also what materialize_table_df needs to recompute
    one already-built ad-hoc table's data on demand (a preview or
    download request) - unlike the other two views, an ad-hoc table's
    build isn't a simple linear recipe (root + ordered attach steps) that
    can be replayed directly, since which side of a merge keeps its own
    anchor depends on row counts decided fresh at replay time - so the
    only correct way to reproduce one table is to rerun the whole growth
    pass and pick out the matching asset, same as the live build did.

    on_progress(label, completed, total), if given, fires once per base
    dataset as its own turn finishes - single-threaded and strictly
    ordered (each turn depends on assets earlier turns already built, so
    this can never be parallelized the way discovery/curation are), but
    every dataset still gets exactly one turn, so a real completed/total
    count is just as meaningful here as the per-item ticks those other
    two phases report. rebuild_asset's one-off recompute passes no
    on_progress - it never drives a progress bar."""
    order = sorted(base_datasets, key=lambda d: (-row_counts.get(d, 0), d))

    assets: dict[str, _Asset] = {}
    membership: dict[str, str] = {}

    for i, tbase in enumerate(order, start=1):
        if tbase not in membership:
            asset = _standalone_asset(tbase)
            assets[asset.anchor] = asset
            membership[tbase] = asset.anchor
        current = assets[membership[tbase]]

        for cand in edges.get(tbase, []):
            before = current
            current = _process_candidate(current, cand, membership, assets, config)
            if current is not before:
                # A rejection (width cap, or both attach stages failing
                # the match-rate gate) always returns the same object it
                # was given - see _process_candidate/_attempt_attach -
                # only a genuine attach/merge produces a new one, so this
                # identity check is what actually distinguishes "recorded
                # because it was used" from "recorded just because it was
                # tried". Recording every attempt regardless (the bug this
                # replaced) meant a rejected candidate's base_column could
                # still count as "protected" in _curate_asset, and an
                # asset that never actually joined anything could still
                # look like it had - see _curate_asset's own handling of
                # a genuinely empty join_candidates.
                current.join_candidates = current.join_candidates + [cand]

        if on_progress:
            on_progress(f'Growing composite assets ({i} of {len(order)} datasets)', i, len(order))

    return assets


def run_ad_hoc(
    project_id: str,
    base_datasets: list[str],
    edges: dict[str, list[JoinCandidate]],
    row_counts: dict[str, int],
    string_columns: dict[str, set[str]],
    column_uniqueness: dict[str, dict[str, float]],
    config: AdHocConfig | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[GoldTable]:
    """Runs the whole growth algorithm and curates every resulting asset -
    see the module docstring. `edges` should be uncapped (every candidate
    above discovery_threshold, no top-N limit - gold_layer.py's other two
    views cap candidates per base, which this deliberately doesn't).

    The growth pass itself (_grow_assets) is a single-threaded, strictly
    ordered algorithm - one turn per dataset, each turn's outcome
    depending on assets earlier turns already built - so max_workers has
    no role there, but on_progress still fires once per dataset as its
    own turn finishes (see _grow_assets), same real completed/total shape
    as curation's own ticks below just single-threaded instead of
    parallel. Curation is where max_workers matters: each finished asset's _curate_asset call is fully
    self-contained (its own DIADA subprocess, diada.py's own isolated-
    temp-dir guarantee), exactly like gold_layer.py's _build_and_curate
    for "every table" - so it's parallelized here the same way, rather
    than curating every asset strictly one at a time regardless of how
    many CPUs are available, and on_progress(label, completed, total)
    fires once per asset as it finishes, same shape as that view's own
    progress calls."""
    config = config or AdHocConfig()
    assets = _grow_assets(base_datasets, edges, row_counts, config, on_progress=on_progress)
    tables: list[GoldTable] = []
    total = len(assets)
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(_curate_asset, project_id, asset, string_columns, column_uniqueness): asset
            for asset in assets.values()
        }
        for future in as_completed(futures):
            tables.extend(future.result())
            completed += 1
            if on_progress:
                asset = futures[future]
                on_progress(f'Curating "{asset.anchor}" ({completed} of {total} assets)', completed, total)
    return tables


def rebuild_asset(
    project_id: str,
    base_datasets: list[str],
    string_usable: dict[str, set[str]],
    row_counts: dict[str, int],
    column_uniqueness: dict[str, dict[str, float]],
    discovery_threshold: float,
    max_workers: int,
    anchor: str,
    candidate_uniqueness_threshold: float = DEFAULT_AD_HOC_CANDIDATE_UNIQUENESS_THRESHOLD,
    max_columns_per_asset: int = DEFAULT_AD_HOC_MAX_COLUMNS_PER_ASSET,
    min_match_rate: float = DEFAULT_AD_HOC_MIN_MATCH_RATE,
) -> _Asset | None:
    """Reruns discovery and growth (not curation) and returns just the one
    asset currently anchored at `anchor` - what
    gold_layer.materialize_table_df needs to recompute one persisted
    ad-hoc table's real data (df/protected columns) on demand, without
    curating every other asset in the lake along the way. None if no
    asset is anchored there any more (the underlying data changed since
    the run that produced it).

    Every one of these must match whatever the original run used, or a
    different set of candidates/attaches could happen this time and
    rebuild a different asset than the one actually persisted under this
    anchor - see discover_and_run's docstring."""
    discover_config = GoldLayerConfig(
        discovery_threshold=discovery_threshold,
        max_candidates_per_join=len(base_datasets) or 1,
        max_workers=max_workers,
        candidate_uniqueness_threshold=candidate_uniqueness_threshold,
    )
    # Raw scores discarded (not `_`-unused - genuinely thrown away): this is
    # a one-off recompute for a single preview/download, not a persisted
    # build - nothing here ever gets saved, so there's nothing for a
    # captured score list to usefully feed into.
    edges, _all_scores = _discover_edges(project_id, base_datasets, string_usable, discover_config, column_uniqueness)
    assets = _grow_assets(
        base_datasets, edges, row_counts,
        AdHocConfig(
            discovery_threshold=discovery_threshold,
            max_columns_per_asset=max_columns_per_asset, min_match_rate=min_match_rate,
        ),
    )
    return assets.get(anchor)


def _declutter_asset(
    asset: _Asset, string_columns: dict[str, set[str]], column_uniqueness: dict[str, dict[str, float]],
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Every real column that was ever used as a join key while this asset
    grew, from whichever member it belonged to - not just the anchor's
    own, since a later attach/merge step can key off a non-anchor member's
    column too (see _process_candidate) - then the same unconditional
    declutter gold_layer.py's other views apply. Also doubles as the set
    _curate_group excludes from DIADA's soundness scoring (every one of
    these is a join key, not analytical content - see _curate_group's own
    docstring for why that exclusion matters).

    Returns (df, protected, declutter_removed) - shared by _curate_asset
    (the live build) and gold_layer.materialize_table_df (recomputing one
    already-built table's data on demand), so there's exactly one place
    this logic lives."""
    df = asset.df
    protected = sorted({c.base_column for c in asset.join_candidates if c.base_column in df.columns})

    drop: set[str] = set()
    if protected:
        drop = declutter_columns(
            df,
            join_builder.PreparedJoin(
                con=None, columns=list(df.columns), total_rows=len(df),
                aggregated_datasets=asset.aggregated_datasets, renamed_columns=asset.renamed_columns,
                column_provenance=asset.column_provenance, original_columns=asset.original_columns,
            ),
            protected, string_columns, column_uniqueness,
        )
    declutter_removed: list[str] = []
    if drop:
        df = df.drop(columns=list(drop))
        declutter_removed = sorted(drop)
    return df, protected, declutter_removed


def _curate_asset(
    project_id: str, asset: _Asset,
    string_columns: dict[str, set[str]], column_uniqueness: dict[str, dict[str, float]],
) -> list[GoldTable]:
    """Declutters and curates one finished asset into GoldTables - the
    same shape and steps as gold_layer.py's _build_and_curate (unconditional
    declutter, DIADA curation with join keys excluded from soundness
    scoring, keyword-named CF2 clusters), so this view's tables render with
    the exact same frontend components as the other two."""
    df, protected, declutter_removed = _declutter_asset(asset, string_columns, column_uniqueness)

    display_name = f"{table_naming.group_display_name(sorted(asset.canonical_members))} — ad hoc"
    group_key = "+".join(sorted(asset.members)) + f"::anchor={asset.anchor}"
    common = dict(
        base_dataset=display_name,
        root_dataset=asset.anchor,
        row_count=len(df),
        join_candidates=asset.join_candidates,
        aggregated_datasets=asset.aggregated_datasets,
        renamed_columns=asset.renamed_columns,
        declutter_removed_columns=declutter_removed,
        group_members=asset.members,
        ad_hoc=True,
    )

    try:
        result = _curate_group(df, protected)
    except diada.DiadaError:
        columns = list(df.columns)
        return [GoldTable(
            id=f"{group_key}::joined", name=f"{display_name}_joined",
            kind="joined_uncurated", columns=columns,
            column_provenance={c: asset.column_provenance[c] for c in columns},
            original_columns={c: asset.original_columns[c] for c in columns},
            **common,
        )]

    visible_clusters = _visible_cf2_clusters(result["cf2"]["clusters"])
    if not visible_clusters:
        if asset.join_candidates:
            # A real join that produced no structure worth showing - same
            # suppression the other two views apply to this outcome.
            return []
        # Never actually joined with anything (every candidate either
        # didn't clear discovery, or was tried and rejected) - unlike a
        # join that came up empty, there's no "did this add value" call
        # to make here: the asset just *is* the raw dataset, which is
        # still a legitimate member of the redistribution and shouldn't
        # vanish because DIADA - unsurprisingly - found no relationships
        # within a single untouched table's own columns. Shown as one
        # table under its own name rather than "Unclustered", which
        # would misleadingly suggest a join happened and just didn't
        # cluster.
        columns = list(df.columns)
        return [GoldTable(
            id=f"{group_key}::cf2::unclustered", name=asset.anchor,
            kind="cf2_unclustered", columns=columns,
            column_provenance={c: asset.column_provenance[c] for c in columns},
            original_columns={c: asset.original_columns[c] for c in columns},
            graph=result["cf2"]["graph"],
            **common,
        )]
    return _cf2_tables(
        visible_clusters, result["cf2"]["graph"], group_key,
        asset.column_provenance, asset.original_columns, common,
    )
