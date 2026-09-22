import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { colorsForJoin, columnColorsFromProvenance, columnsBySource } from '../utils/provenanceColors'
import CurationGraph from './join/CurationGraph'
import DataTable from './DataTable'
import { formatSeconds } from './ProcessingProgress'

// Mirrors gold_layer.py's DEFAULT_* constants - kept in sync by hand, same
// convention as CandidatePicker.jsx's aggregation options.
//
// Two independent shapes, one per algorithm tab (see GoldLayer's own
// activeTab state) - each tab's inputs are its own, remembered
// independently of whichever tab you look at next.
const DEFAULT_EVERY_TABLE_CONFIG = {
  discoveryThreshold: 0.001,
  maxCandidatesPerJoin: 4,
  maxWorkers: 8,
  candidateUniquenessThreshold: 0, // no uniqueness bar by default - see gold_layer.py's module docstring
}
// Ad hoc is the primary algorithm (see App.jsx's tab ordering), so its
// defaults favor actually finishing a real build rather than mirroring
// the other tab's more conservative one:
//  - adHocCandidateUniquenessThreshold: 0.9, not "every table"'s 0 - see
//    gold_layer.py's DEFAULT_AD_HOC_* comment for why ad hoc holds
//    candidates to a higher bar.
const DEFAULT_AD_HOC_CONFIG = {
  discoveryThreshold: 0.001,
  maxWorkers: 8,
  adHocCandidateUniquenessThreshold: 0.9,
  adHocMaxColumnsPerAsset: 80,
  adHocMinMatchRate: 0.1,
}

const PAGE_SIZE = 50

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function tableKindLabel(kind) {
  if (kind === 'cf1') return 'Cleaned (univariate noise removed)'
  if (kind === 'cf2_unclustered') return 'Unclustered'
  if (kind === 'cf2_cluster') return 'Cluster'
  if (kind === 'joined_uncurated') return 'Joined (composition unavailable)'
  return kind
}

// A GoldTable's id is "{base}::cf2::{cluster_name}" (see gold_layer.py's
// _cf2_tables) - the part after "::cf2::" is exactly the raw cluster name
// diada.structure_dataset used to tag graph.nodes[].cluster, so this
// recovers a clusterOrder for CurationGraph that's guaranteed to match.
function clusterNameFromId(id) {
  const marker = '::cf2::'
  const idx = id.indexOf(marker)
  return idx === -1 ? null : id.slice(idx + marker.length)
}

// One shared cluster/attribute graph per base (not per gold table - every
// cluster produced from the same base/group shares the identical `graph`,
// see gold_layer.py's GoldTable.graph), shown instead of a per-table join
// topology diagram. Collapsed by default and only mounted once opened
// (`open && ...`, not just CSS-hidden inside a closed <details>): a gold
// layer run can produce many bases at once, and CurationGraph runs a real
// react-force-graph-2d physics simulation - running many of those
// simultaneously is wasteful and risks the same "gets stuck" class of bug
// worked through for the whole-run graphs elsewhere in this app.
function BaseClusterGraph({ tables }) {
  const [open, setOpen] = useState(false)
  const graph = tables[0]?.graph
  if (!graph) return null
  const clusterOrder = tables.map((t) => clusterNameFromId(t.id)).filter(Boolean)

  return (
    <details className="curation-graph-toggle" onToggle={(e) => setOpen(e.target.open)}>
      <summary>Show attribute clusters</summary>
      {open && <CurationGraph graph={graph} clusterOrder={clusterOrder} />}
    </details>
  )
}


function GoldTableRow({ table }) {
  const [preview, setPreview] = useState(null)
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)

  function loadPage(nextPage) {
    setLoading(true)
    setError(null)
    api
      .goldTablePreview(table.id, { limit: PAGE_SIZE, offset: nextPage * PAGE_SIZE })
      .then((data) => {
        setPreview(data)
        setPage(nextPage)
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  function handleToggle(e) {
    if (e.target.open && !preview && !loading) loadPage(0)
  }

  function download() {
    setDownloading(true)
    setDownloadError(null)
    api
      .goldTableDownload(table.id)
      .then(({ blob, filename }) => triggerDownload(blob, filename))
      .catch((e) => setDownloadError(e.message))
      .finally(() => setDownloading(false))
  }

  const pageCount = preview ? Math.max(1, Math.ceil(preview.total_rows / PAGE_SIZE)) : 1

  // An "ad hoc" mode table's base_dataset is a display name (e.g. "a+b+c"),
  // not a real dataset - and join_candidates only lists the attach steps,
  // never the original seed (see ad_hoc_layer.py's _grow_assets) - so
  // anchoring colorsForJoin on base_dataset/join_candidates the way
  // "every table" mode tables do would leave the seed's own columns with
  // no assigned color at all. root_dataset/group_members (both real
  // dataset names, seed included) are the right anchor/member-list for an
  // ad-hoc composite table.
  const isComposite = !!table.group_members
  const colors = isComposite
    ? colorsForJoin(table.root_dataset, table.group_members.filter((m) => m !== table.root_dataset))
    : colorsForJoin(table.base_dataset, table.join_candidates.map((c) => c.dataset))
  const columnColors = columnColorsFromProvenance(table.column_provenance, colors)

  return (
    <details className="gold-table-row" onToggle={handleToggle}>
      <summary>
        <strong className="gold-table-name">{table.name}</strong>
        <span className="gold-table-kind">{tableKindLabel(table.kind)}</span>
        <span className="muted">
          {table.column_count ?? table.columns?.length} cols &middot; {table.row_count.toLocaleString()} rows
        </span>
      </summary>

      <div className="gold-table-detail">
        {isComposite && <p className="join-note">Composed from: {table.group_members.join(', ')}</p>}

        <p className="provenance-heading muted">Columns by source</p>
        <div className="provenance-summary">
          {Object.entries(columnsBySource(table.column_provenance)).map(([dataset, cols]) => (
            <p key={dataset} className="provenance-line">
              <span className="join-graph-swatch" style={{ background: colors[dataset] || '#9ca3af' }} />
              <strong>{dataset}</strong>: {cols.join(', ')}
            </p>
          ))}
        </div>

        {table.aggregated_datasets.length > 0 && (
          <p className="join-note">Aggregated: {table.aggregated_datasets.join(', ')}</p>
        )}
        {table.declutter_removed_columns?.length > 0 && (
          <p className="join-note">
            Decluttered (unused primary key, redundant, or mostly-null): {table.declutter_removed_columns.join(', ')}
          </p>
        )}
        {table.removed_columns.length > 0 && (
          <p className="join-note">Removed as univariate noise: {table.removed_columns.join(', ')}</p>
        )}

        {error && <div className="error-banner">{error}</div>}
        {loading && !preview && <p className="muted">Loading preview...</p>}
        {preview && (
          <>
            <p className="provenance-heading muted">Preview</p>
            <DataTable
              preview={preview}
              loading={loading}
              page={page}
              pageCount={pageCount}
              onPrev={() => loadPage(Math.max(0, page - 1))}
              onNext={() => loadPage(Math.min(pageCount - 1, page + 1))}
              columnColors={columnColors}
            />
          </>
        )}

        <div className="curation-actions">
          <button onClick={download} disabled={downloading}>
            {downloading ? 'Preparing CSV...' : 'Download CSV'}
          </button>
        </div>
        {downloadError && <div className="error-banner">{downloadError}</div>}
      </div>
    </details>
  )
}

const GROUP_PAGE_SIZE = 20

// Renders one algorithm's results as base/anchor-named groups, with a
// text filter and "load more" pagination - a run over a real data lake
// can produce hundreds of groups (each with its own BaseClusterGraph and
// one <details> per table), and mounting all of them at once is what was
// making the page sluggish. Filtering narrows `groups` first, then
// pagination slices *that* - so searching for one dataset never requires
// paging through everything ahead of it alphabetically/build-order first.
// Resets to the first page whenever the query changes, not when `groups`
// itself changes (e.g. a rebuild while staying on the same tab) - the
// component instance persists across a rebuild, and there's no harm in
// leaving pagination/search exactly as the user left it; the slice below
// is always safe even if the new result has fewer groups than before.
function GoldTableGroupList({ groups, renderGroupExtra }) {
  const [query, setQuery] = useState('')
  const [visibleCount, setVisibleCount] = useState(GROUP_PAGE_SIZE)

  const normalized = query.trim().toLowerCase()
  const filtered = normalized
    ? groups.filter(
        ([key, tables]) =>
          key.toLowerCase().includes(normalized) || tables.some((t) => t.name.toLowerCase().includes(normalized))
      )
    : groups
  const visible = filtered.slice(0, visibleCount)

  return (
    <>
      <input
        type="search"
        className="gold-layer-table-search"
        placeholder="Search tables or datasets..."
        value={query}
        onChange={(e) => {
          setQuery(e.target.value)
          setVisibleCount(GROUP_PAGE_SIZE)
        }}
      />

      {filtered.length === 0 && <p className="muted">No tables match &ldquo;{query}&rdquo;.</p>}

      {visible.map(([key, tables]) => (
        <div key={key} className="gold-layer-base-group">
          <h5>{key}</h5>
          {renderGroupExtra?.(key, tables)}
          <BaseClusterGraph tables={tables} />
          {tables.map((t) => (
            <GoldTableRow key={t.id} table={t} />
          ))}
        </div>
      ))}

      {visibleCount < filtered.length && (
        <button className="secondary gold-layer-load-more" onClick={() => setVisibleCount((n) => n + GROUP_PAGE_SIZE)}>
          Load more ({filtered.length - visibleCount} remaining)
        </button>
      )}
    </>
  )
}

// Polls independently per mode, so both algorithms can build concurrently
// without one's polling loop stepping on the other's state.
function usePollProgress(mode, progress, setProgress, setRuns, setErrors) {
  useEffect(() => {
    if (!progress?.running) return
    const id = setInterval(() => {
      api
        .getGoldLayerProgress(mode)
        .then((p) => {
          setProgress((prev) => ({ ...prev, [mode]: p }))
          if (!p.running) {
            if (p.error) {
              setErrors((prev) => ({ ...prev, [mode]: p.error }))
            } else {
              api.getGoldLayer(mode).then((data) => setRuns((prev) => ({ ...prev, [mode]: data.run })))
            }
          }
        })
        .catch((e) => setErrors((prev) => ({ ...prev, [mode]: e.message })))
    }, 400)
    return () => clearInterval(id)
  }, [mode, progress?.running, setProgress, setRuns, setErrors])
}

export default function GoldLayer() {
  const [everyTableConfig, setEveryTableConfig] = useState(DEFAULT_EVERY_TABLE_CONFIG)
  const [adHocConfig, setAdHocConfig] = useState(DEFAULT_AD_HOC_CONFIG)
  // Every algorithm's own last persisted run/progress/error, keyed by
  // mode - each builds and persists completely independently of the
  // other (see backend/app/gold_layer.py's module docstring), so
  // building one must never overwrite what the other last produced.
  const [runs, setRuns] = useState({ every_table: null, ad_hoc: null })
  const [progress, setProgress] = useState({ every_table: null, ad_hoc: null })
  const [errors, setErrors] = useState({ every_table: null, ad_hoc: null })
  const [initialLoading, setInitialLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  // Which algorithm is selected - drives both which parameters are shown
  // (each tab only edits its own independently-stored config above) and
  // which one's results are on screen. Ad hoc first/default: it's the
  // primary algorithm.
  const [activeTab, setActiveTab] = useState('ad_hoc')

  useEffect(() => {
    Promise.all(
      ['every_table', 'ad_hoc'].map((mode) => api.getGoldLayer(mode).then((data) => [mode, data.run]))
    )
      .then((pairs) => setRuns(Object.fromEntries(pairs)))
      .catch((e) => setLoadError(e.message))
      .finally(() => setInitialLoading(false))
  }, [])

  usePollProgress('every_table', progress.every_table, setProgress, setRuns, setErrors)
  usePollProgress('ad_hoc', progress.ad_hoc, setProgress, setRuns, setErrors)

  function startRun() {
    setErrors((prev) => ({ ...prev, [activeTab]: null }))
    const configPayload =
      activeTab === 'ad_hoc'
        ? {
            discovery_threshold: adHocConfig.discoveryThreshold,
            max_workers: adHocConfig.maxWorkers,
            ad_hoc_candidate_uniqueness_threshold: adHocConfig.adHocCandidateUniquenessThreshold,
            ad_hoc_max_columns_per_asset: adHocConfig.adHocMaxColumnsPerAsset,
            ad_hoc_min_match_rate: adHocConfig.adHocMinMatchRate,
          }
        : {
            discovery_threshold: everyTableConfig.discoveryThreshold,
            max_candidates_per_join: everyTableConfig.maxCandidatesPerJoin,
            max_workers: everyTableConfig.maxWorkers,
            candidate_uniqueness_threshold: everyTableConfig.candidateUniquenessThreshold,
          }

    api
      .runGoldLayer(activeTab, configPayload)
      .then(() => {
        setProgress((prev) => ({
          ...prev,
          [activeTab]: { running: true, total: 0, completed: 0, current_step: null, elapsed_seconds: 0 },
        }))
      })
      .catch((e) => setErrors((prev) => ({ ...prev, [activeTab]: e.message })))
  }

  if (initialLoading) return <p className="muted">Loading...</p>
  if (loadError) return <div className="error-banner">{loadError}</div>

  const run = runs[activeTab]
  const activeProgress = progress[activeTab]
  const error = errors[activeTab]
  const buildDisabled = activeProgress?.running

  // run.tables is scoped entirely to whichever algorithm produced it now
  // (see backend/app/storage.GOLD_LAYER_MODES) - no more sifting one
  // combined list by group_members/ad_hoc flags to tell the two apart.
  const tablesByBase = {}
  if (run && activeTab === 'every_table') {
    for (const t of run.tables) {
      ;(tablesByBase[t.base_dataset] ||= []).push(t)
    }
  }
  const everyTableCount = Object.values(tablesByBase).reduce((n, ts) => n + ts.length, 0)
  const everyTableBaseCount = Object.keys(tablesByBase).length
  const redundantEntries = run ? Object.entries(run.redundant_datasets || {}) : []

  const adHocByAnchor = {}
  if (run && activeTab === 'ad_hoc') {
    for (const t of run.tables) {
      ;(adHocByAnchor[t.root_dataset] ||= []).push(t)
    }
  }
  const adHocCount = Object.values(adHocByAnchor).reduce((n, ts) => n + ts.length, 0)
  const adHocAssetCount = Object.keys(adHocByAnchor).length

  return (
    <div className="gold-layer">
      <p className="muted">
        Autonomously discovers which datasets are worth joining together, 
        executes the joins and composes the resulting data, all in one single. We provide
        two algorithms to do so. The "ad hoc" algorithms greedily traverses the join space,
        going over each table a single time. The "Every table as base" starts separated join
        processes for each table, simply enriching the original assets.
      </p>

      <div className="join-display-toggle gold-layer-algo-tabs">
        <button className={activeTab === 'ad_hoc' ? 'tab active' : 'tab'} onClick={() => setActiveTab('ad_hoc')}>
          Ad hoc
        </button>
        <button
          className={activeTab === 'every_table' ? 'tab active' : 'tab'}
          onClick={() => setActiveTab('every_table')}
        >
          Every table as base
        </button>
      </div>

      {activeTab === 'ad_hoc' && (
        <p className="muted">
          Grows assets bottom-up instead of one join per base: every dataset gets one turn (largest
          first), tries every candidate above the discovery threshold, and
          when a candidate already belongs to another growing asset, the two merge, so a join can
          chain through already-joined tables, not just raw ones, until an asset hits the max
          columns per asset limit. A dataset joinable to many others is simply claimed by whichever
          asset's turn discovers it first.
        </p>
      )}
      {activeTab === 'every_table' && (
        <p className="muted">
          Builds one composed, DIADA-curated table (or family of tables) per dataset outright. It skips
          datasets whose own join would just repeat a set of datasets another one already
          covered.
        </p>
      )}

      {activeTab === 'ad_hoc' ? (
        <div className="gold-layer-config-detailed">
          <span className="gold-layer-field-label">Discovery score threshold</span>
          <input
            type="number"
            step="0.001"
            min={0}
            value={adHocConfig.discoveryThreshold}
            onChange={(e) => setAdHocConfig((c) => ({ ...c, discoveryThreshold: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              How strong a discovered relationship must score before it's considered a candidate at all.
              This is the model's <strong>raw</strong> score, not the normalized 0-1 value shown in the
              Catalog graph's Joinability tab. The real scores for a typical lake range from roughly -0.0001
              (noise) up to a few tenths for a genuine match, so the default of 0.001 is a generaly valid middle point
              that lets through almost anything above pure noise, since a weak/wrong candidate
              is filtered out cheaply by curation downstream, while a real relationship missed here is
              missed for good.
            </p>
          </details>

          <span className="gold-layer-field-label">Parallel workers</span>
          <input
            type="number"
            min={1}
            value={adHocConfig.maxWorkers}
            onChange={(e) => setAdHocConfig((c) => ({ ...c, maxWorkers: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              Allows the system to profit from parallel threats in several of its tasks.
            </p>
          </details>

          <span className="gold-layer-field-label">Candidate uniqueness threshold</span>
          <input
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={adHocConfig.adHocCandidateUniquenessThreshold}
            onChange={(e) =>
              setAdHocConfig((c) => ({ ...c, adHocCandidateUniquenessThreshold: Number(e.target.value) }))
            }
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              Fraction of a column's non-null values to be unique. This is used to check the likelihood of a column of being a primary key.
              As the algorithm chains merges across many hops with no human review a low-quality
              attach drags everything that merges with it afterward along too, so this a quality gate to prevent that.
            </p>
            <p>
              Example: in this project's demo lake, resident_reviews.property_id is unique only a third
              of the time (9,000 reviews over 3,000 distinct properties - about 3 reviews per property).
              At the default 0.9, ad hoc refuses to attach through that column at all - 0.33 is nowhere
              near key-like. Lower the threshold and it becomes eligible instead: ad hoc would attach it
              by averaging every one of a property's reviews into a single row, rather than a clean
              one-to-one lookup. Whether that's desirable depends entirely on whether that average is a
              meaningful summary for what you're building.
            </p>
          </details>

          <span className="gold-layer-field-label">Max columns per asset</span>
          <input
            type="number"
            min={1}
            value={adHocConfig.adHocMaxColumnsPerAsset}
            onChange={(e) => setAdHocConfig((c) => ({ ...c, adHocMaxColumnsPerAsset: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              Maximum number of columns that a joined dataset can have before it stops growin. Needed to keep the 
              composition process feasible.
            </p>
          </details>

          <span className="gold-layer-field-label">Min match rate</span>
          <input
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={adHocConfig.adHocMinMatchRate}
            onChange={(e) => setAdHocConfig((c) => ({ ...c, adHocMinMatchRate: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              The minimum fraction of the base's own rows a candidate must actually match on to be
              attached. Guards against a technically valid join that in practice barely overlaps.
            </p>
          </details>

          <button onClick={startRun} disabled={buildDisabled} className="gold-layer-build-button">
            {buildDisabled ? 'Building...' : 'Build gold layer'}
          </button>
        </div>
      ) : (
        <div className="gold-layer-config-detailed">
          <span className="gold-layer-field-label">Discovery score threshold</span>
          <input
            type="number"
            step="0.001"
            min={0}
            value={everyTableConfig.discoveryThreshold}
            onChange={(e) => setEveryTableConfig((c) => ({ ...c, discoveryThreshold: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              How strong a discovered relationship must score before it's considered a candidate at all.
              This is the model's <strong>raw</strong> score, not the normalized 0-1 value shown in the
              Catalog graph's Joinability tab - real scores for a typical lake range from roughly -0.0001
              (noise) up to a few tenths for a genuine match, so the default of 0.001 is deliberately
              permissive: it lets through almost anything above pure noise, since a weak/wrong candidate
              is filtered out cheaply by curation downstream, while a real relationship missed here is
              missed for good.
            </p>
          </details>

          <span className="gold-layer-field-label">Max candidates per join</span>
          <input
            type="number"
            min={1}
            value={everyTableConfig.maxCandidatesPerJoin}
            onChange={(e) =>
              setEveryTableConfig((c) => ({ ...c, maxCandidatesPerJoin: Number(e.target.value) }))
            }
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              Caps how many candidate datasets can be joined into a single base, after ranking by score -
              candidates beyond the cap are simply left out. Unlike ad hoc's growth pass, this algorithm
              always builds exactly one star join per base, so the cap is the only thing keeping a base
              with many discoverable relationships from pulling in every one of them at once. In this
              project's demo lake, a base like property_listings clears the discovery threshold against
              several other datasets (parcel/permit records, resident reviews, weather stations, ...) -
              a low cap like the default of 4 keeps the resulting table anchored around its strongest,
              highest-scoring relationships rather than one that joins in everything discovery found,
              most of which score progressively weaker.
            </p>
          </details>

          <span className="gold-layer-field-label">Parallel workers</span>
          <input
            type="number"
            min={1}
            value={everyTableConfig.maxWorkers}
            onChange={(e) => setEveryTableConfig((c) => ({ ...c, maxWorkers: Number(e.target.value) }))}
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              It controls two separate thread pools, one per build phase. Discovery: one thread per base
              dataset, each asking the joinability model to score every candidate column against that
              dataset's own string columns. Composition: one thread per base, each spawning its own DIADA
              subprocess (a JVM) to build and curate that base's table. In both phases the per-item cost
              is dominated by waiting on that external work (the model call, the JVM subprocess), not on
              CPU inside Python, so raising this shortens wall-clock time roughly up to however many bases
              are being processed at once - past that point, extra workers just sit idle with nothing to
              pick up.
            </p>
          </details>

          <span className="gold-layer-field-label">Candidate uniqueness threshold</span>
          <input
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={everyTableConfig.candidateUniquenessThreshold}
            onChange={(e) =>
              setEveryTableConfig((c) => ({ ...c, candidateUniquenessThreshold: Number(e.target.value) }))
            }
          />
          <details className="gold-layer-field-hint">
            <summary aria-label="What does this do?">?</summary>
            <p>
              How primary-key-like a candidate column must be in its own dataset (fraction of its
              non-null values that are distinct) before it's eligible to be joined in at all. Defaults to
              0 (no bar): a genuinely one-to-many candidate - several reviews per property, several
              transactions per customer - is exactly what join_builder's aggregation step is for, and
              excluding it entirely would routinely miss real signal a human would have included by hand.
            </p>
            <p>
              Example: in this project's demo lake, resident_reviews.property_id is unique only a third
              of the time (9,000 reviews over 3,000 distinct properties - about 3 reviews per property).
              At the default of 0, it's still joined in - averaging every one of a property's reviews
              into a single row. Ad hoc mode defaults this field to a much stricter 0.9 instead, because
              it chains merges across many hops with no per-join review, so a low-quality attach early
              compounds; this algorithm builds one star join per base in isolation, so a shaky aggregate
              here only affects that one table, not a whole chain of them.
            </p>
          </details>

          <button onClick={startRun} disabled={buildDisabled} className="gold-layer-build-button">
            {buildDisabled ? 'Building...' : 'Build gold layer'}
          </button>
        </div>
      )}

      {activeProgress?.running && (
        <div className="processing-panel">
          <h2>Building&hellip;</h2>
          <div className="progress-bar-track">
            <div
              className="progress-bar-fill"
              style={{
                width: `${activeProgress.total > 0 ? Math.round((activeProgress.completed / activeProgress.total) * 100) : 5}%`,
              }}
            />
          </div>
          <p className="muted">
            {activeProgress.current_step || 'Working...'}
            {activeProgress.elapsed_seconds != null ? ` · ${formatSeconds(activeProgress.elapsed_seconds)} elapsed` : ''}
          </p>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {run && !activeProgress?.running && (
        <div className="gold-layer-results">
          {run.skipped_datasets.length > 0 && (
            <p className="muted">
              Skipped (no usable string column to join on): {run.skipped_datasets.join(', ')}
            </p>
          )}

          {activeTab === 'every_table' ? (
            <>
              <h3>
                {everyTableCount} final table{everyTableCount === 1 ? '' : 's'} from{' '}
                {everyTableBaseCount} base dataset{everyTableBaseCount === 1 ? '' : 's'}
                <span className="muted build-time">
                  {' '}
                  &middot;{' '}
                  {run.discovery_seconds != null && run.composition_seconds != null
                    ? `discovery ${formatSeconds(run.discovery_seconds)}, composition ${formatSeconds(run.composition_seconds)} (${formatSeconds(run.build_seconds)} total)`
                    : `built in ${formatSeconds(run.build_seconds)}`}
                </span>
              </h3>

              {redundantEntries.length > 0 && (
                <p className="muted">
                  Skipped as redundant (the same datasets are already joined via another base):{' '}
                  {redundantEntries.map(([skipped, via]) => `${skipped} (same as ${via})`).join(', ')}
                </p>
              )}

              <h4>Final tables</h4>
              <GoldTableGroupList groups={Object.entries(tablesByBase)} />
            </>
          ) : (
            <>
              <h3>
                {adHocCount} final table{adHocCount === 1 ? '' : 's'} from{' '}
                {adHocAssetCount} asset{adHocAssetCount === 1 ? '' : 's'}
                <span className="muted build-time">
                  {' '}
                  &middot;{' '}
                  {run.discovery_seconds != null && run.composition_seconds != null
                    ? `discovery ${formatSeconds(run.discovery_seconds)}, composition ${formatSeconds(run.composition_seconds)} (${formatSeconds(run.build_seconds)} total)`
                    : `built in ${formatSeconds(run.build_seconds)}`}
                </span>
              </h3>

              <h4>Final tables</h4>
              <GoldTableGroupList
                groups={Object.entries(adHocByAnchor)}
                renderGroupExtra={(_anchor, tables) => (
                  <p className="join-note">Composed from: {tables[0].group_members.join(', ')}</p>
                )}
              />
            </>
          )}
        </div>
      )}
    </div>
  )
}
