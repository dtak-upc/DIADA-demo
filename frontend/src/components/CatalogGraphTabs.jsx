import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { formatNormalizedScore, formatRawScore, normalizeScore } from '../utils/score'
import { groupOf, nodeColor, nodeLabelText, nodeRadius, nodeTooltip } from '../utils/catalogGraph'
import GraphCanvas from './GraphCanvas'

const DEFAULT_MAX_GROUPS = 10

function totalDatasetsOf(nodes) {
  return nodes.filter((n) => n.kind === 'dataset').length
}

function defaultMaxGroupsFor(nodes) {
  const total = totalDatasetsOf(nodes)
  return total > 0 ? Math.min(DEFAULT_MAX_GROUPS, total) : DEFAULT_MAX_GROUPS
}

// ---------------------------------------------------------------------
// "Initial data": the ingestion-time graph exactly as it was right after
// data was loaded - no gold-layer content, ever, in either mode. "Usable
// only" and "Full" are two different already-persisted graphs (see
// app/catalog.py:build_usable_graph), not a client-side filter of one
// another - a flagged dataset's columns genuinely never entered the usable
// graph, so "usable" isn't just "full with flags hidden".
// ---------------------------------------------------------------------

function stripGoldLayer(source) {
  const nodes = source.nodes.filter((n) => n.kind === 'dataset' || n.kind === 'column' || n.kind === 'quality_flag')
  const edges = source.edges.filter((e) => e.relation === 'has_column' || e.relation === 'has_flag')
  return { nodes, edges }
}

function initialEdgeColor(link) {
  return link.relation === 'has_flag' ? 'rgba(217, 119, 6, 0.45)' : 'rgba(99, 102, 241, 0.3)'
}
function constantEdgeWidth() {
  return 1.5
}
function relationTooltip(link) {
  return (link.relation || '').replace(/_/g, ' ')
}

function InitialDataGraph({ full, usable }) {
  const [mode, setMode] = useState('full')
  const source = mode === 'usable' ? usable : full
  const { nodes, edges } = useMemo(() => (source ? stripGoldLayer(source) : { nodes: [], edges: [] }), [source])
  const defaultMaxGroups = useMemo(() => defaultMaxGroupsFor(nodes), [nodes])

  const config = useMemo(
    () => ({
      groupOf,
      groupLabel: 'Datasets',
      defaultMaxGroups,
      nodeColor,
      nodeRadius,
      nodeLabelText,
      nodeTooltip,
      edgeColor: initialEdgeColor,
      edgeWidth: constantEdgeWidth,
      edgeTooltip: relationTooltip,
      emptyMessage: 'No datasets loaded yet.',
      searchPlaceholder: 'Search datasets...',
    }),
    [defaultMaxGroups],
  )

  return (
    <div>
      <p className="muted">
        The metadata graph built right after data was ingested. It contains every dataset, its columns, and any
        data quality flags found on them.
      </p>
      <div className="catalog-mode-toggle">
        <button className={mode === 'full' ? 'tab active' : 'tab'} onClick={() => setMode('full')}>
          Full
        </button>
        <button className={mode === 'usable' ? 'tab active' : 'tab'} onClick={() => setMode('usable')}>
          Usable only
        </button>
        <span className="muted catalog-mode-hint">
          {mode === 'usable'
            ? 'Datasets/columns with any quality flag are filtered out entirely - the basis for join discovery.'
            : 'Everything, including flagged datasets/columns.'}
        </span>
      </div>
      <GraphCanvas nodes={nodes} edges={edges} config={config} loading={!source} />
    </div>
  )
}

// ---------------------------------------------------------------------
// "Joinability graph": every (column, column) pair join discovery scored
// during the ad hoc algorithm's last build (app/gold_layer.JoinScore,
// layered onto the catalog graph as "joinable" edges - see app/
// catalog.py; deliberately ad hoc only, not "every table"/"create gold
// tables" - see catalog.py's module docstring for why), with a
// user-controlled threshold applied entirely client-side - the backend
// stores every score it ever computed, unfiltered, precisely so this
// threshold has something to actually reveal as it's lowered.
// ---------------------------------------------------------------------

// Two directions can both be scored for the same unordered column pair -
// see gold_layer.JoinScore's docstring (the model's own distance features
// aren't all symmetric). Keep only the higher-scoring direction per pair
// so the graph draws one line, not two nearly-overlapping ones.
function dedupeJoinable(edges) {
  const best = new Map()
  for (const e of edges) {
    const key = [e.source, e.target].sort().join('||')
    const existing = best.get(key)
    if (!existing || e.score > existing.score) best.set(key, e)
  }
  return [...best.values()]
}

function JoinabilityGraph({ catalog }) {
  const scoreBounds = catalog.score_bounds
  const datasetNodes = useMemo(() => catalog.nodes.filter((n) => n.kind === 'dataset'), [catalog])
  const columnNodes = useMemo(() => catalog.nodes.filter((n) => n.kind === 'column'), [catalog])
  const nodes = useMemo(() => [...datasetNodes, ...columnNodes], [datasetNodes, columnNodes])

  // Faint dataset -> its own columns lines, so a dataset doesn't float with
  // no visible structure to its columns - same has_column edges the
  // "Initial data" tab shows, always included regardless of threshold.
  const hasColumnEdges = useMemo(() => catalog.edges.filter((e) => e.relation === 'has_column'), [catalog])

  const allJoinable = useMemo(
    () => dedupeJoinable(catalog.edges.filter((e) => e.relation === 'joinable')),
    [catalog],
  )

  const scoreExtent = useMemo(() => {
    if (allJoinable.length === 0) return { min: 0, max: 0 }
    let min = Infinity
    let max = -Infinity
    for (const e of allJoinable) {
      if (e.score < min) min = e.score
      if (e.score > max) max = e.score
    }
    return { min, max }
  }, [allJoinable])

  // Adaptive default: whatever score the top ~30% of found joins clear, so
  // the first render is neither an empty graph nor an unreadable hairball
  // regardless of how this lake's score distribution happens to be shaped.
  const adaptiveDefault = useMemo(() => {
    if (allJoinable.length === 0) return scoreExtent.min
    const sorted = allJoinable.map((e) => e.score).sort((a, b) => a - b)
    return sorted[Math.floor(0.7 * (sorted.length - 1))]
  }, [allJoinable, scoreExtent])

  const [threshold, setThreshold] = useState(null)
  useEffect(() => {
    if (threshold === null) setThreshold(adaptiveDefault)
  }, [adaptiveDefault, threshold])
  const effectiveThreshold = threshold === null ? adaptiveDefault : threshold

  const edges = useMemo(
    () => [...hasColumnEdges, ...allJoinable.filter((e) => e.score >= effectiveThreshold)],
    [hasColumnEdges, allJoinable, effectiveThreshold],
  )

  const defaultMaxGroups = useMemo(() => defaultMaxGroupsFor(nodes), [nodes])

  const config = useMemo(
    () => ({
      groupOf,
      groupLabel: 'Datasets',
      defaultMaxGroups,
      nodeColor,
      nodeRadius,
      nodeLabelText,
      nodeTooltip,
      edgeColor: (link) =>
        link.relation === 'has_column'
          ? 'rgba(99, 102, 241, 0.25)'
          : `rgba(236, 72, 153, ${0.25 + normalizeScore(link.score, scoreBounds) * 0.6})`,
      edgeWidth: (link) => (link.relation === 'has_column' ? 1 : 1 + normalizeScore(link.score, scoreBounds) * 5),
      edgeTooltip: (link) =>
        link.relation === 'has_column'
          ? 'belongs to dataset'
          : `joinability score: ${formatNormalizedScore(link.score, scoreBounds)} (raw ${formatRawScore(link.score)})`,
      emptyMessage: 'No usable datasets/columns to show.',
      searchPlaceholder: 'Search datasets or columns...',
    }),
    [defaultMaxGroups, scoreBounds],
  )

  if (allJoinable.length === 0) {
    return <p className="muted">The ad hoc algorithm's last build found no join candidates scored above zero.</p>
  }

  return (
    <div>
      <p className="muted">
        Initial data graph extended after join discovery. Every pink line is a
        (column, column) pair the join-discovery model scored during the automatic composition's last build.
        All joinable candidates are stored, and the threshold below is applied purely to what's displayed. Line
        thickness tracks the score, and hovering over a line reveals its exact value.
      </p>
      <div className="join-score-slider">
        <label>
          Only show joins scored &ge; {formatNormalizedScore(effectiveThreshold, scoreBounds)} (raw{' '}
          {formatRawScore(effectiveThreshold)})
        </label>
        <input
          type="range"
          min={scoreExtent.min}
          max={scoreExtent.max}
          step={(scoreExtent.max - scoreExtent.min) / 200 || 0.0001}
          value={effectiveThreshold}
          onChange={(e) => setThreshold(Number(e.target.value))}
        />
        <div className="join-score-slider-ticks">
          <span>{formatRawScore(scoreExtent.min)} (show all found)</span>
          <span>{formatRawScore(scoreExtent.max)} (strictest)</span>
        </div>
      </div>
      <GraphCanvas nodes={nodes} edges={edges} config={config} loading={false} />
    </div>
  )
}

// ---------------------------------------------------------------------
// "Composition graph": which cluster each column ended up in, across every
// gold table the last build produced - gold_table nodes (pink, one per
// cluster) linked to the real source columns DIADA grouped into them, plus
// the DIADA relationships that justified the grouping in the first place.
// ---------------------------------------------------------------------

function normalizeSoundness(soundness) {
  return Math.min(1, (soundness ?? 4) / 10)
}

function compositionEdgeColor(link) {
  if (link.relation === 'has_column') return 'rgba(99, 102, 241, 0.25)'
  return link.relation === 'derived_from' ? 'rgba(236, 72, 153, 0.45)' : 'rgba(34, 197, 94, 0.4)'
}
function compositionEdgeWidth(link) {
  if (link.relation === 'has_column') return 1
  return link.relation === 'derived_from' ? 1.5 : 1 + normalizeSoundness(link.soundness) * 3
}
function compositionEdgeTooltip(link) {
  if (link.relation === 'has_column') return 'belongs to dataset'
  return link.relation === 'derived_from'
    ? `derived into "${link.output_column}"`
    : `statistically related · soundness ${link.soundness != null ? link.soundness.toFixed(2) : '?'}`
}

function CompositionGraph({ catalog }) {
  const datasetNodes = useMemo(() => catalog.nodes.filter((n) => n.kind === 'dataset'), [catalog])
  const columnNodes = useMemo(() => catalog.nodes.filter((n) => n.kind === 'column'), [catalog])
  const goldTableNodes = useMemo(() => catalog.nodes.filter((n) => n.kind === 'gold_table'), [catalog])
  const nodes = useMemo(
    () => [...datasetNodes, ...columnNodes, ...goldTableNodes],
    [datasetNodes, columnNodes, goldTableNodes],
  )

  const edges = useMemo(
    () =>
      catalog.edges.filter(
        (e) => e.relation === 'has_column' || e.relation === 'derived_from' || e.relation === 'statistically_related',
      ),
    [catalog],
  )

  const defaultMaxGroups = useMemo(() => defaultMaxGroupsFor(nodes), [nodes])

  const config = useMemo(
    () => ({
      groupOf,
      groupLabel: 'Datasets',
      defaultMaxGroups,
      nodeColor,
      nodeRadius,
      nodeLabelText,
      nodeTooltip,
      edgeColor: compositionEdgeColor,
      edgeWidth: compositionEdgeWidth,
      edgeTooltip: compositionEdgeTooltip,
      emptyMessage: 'No usable datasets/columns to show.',
      searchPlaceholder: 'Search datasets or final tables...',
    }),
    [defaultMaxGroups],
  )

  if (goldTableNodes.length === 0) {
    return <p className="muted">The ad hoc algorithm's last build produced no clustered tables to show.</p>
  }

  return (
    <div>
      <p className="muted">
        Initial data graph extended with the computed components. Pink circles are the composition's 
        own final tables, one per cluster DIADA's curation step produced. Pink lines track
        one of a final table's own columns back to the real source column it was derived from. A
        green line shows a statistical relationship DIADA found between two source columns.
      </p>
      <GraphCanvas nodes={nodes} edges={edges} config={config} loading={false} />
    </div>
  )
}

// ---------------------------------------------------------------------

function NotBuiltNote({ children }) {
  return <p className="catalog-graph-sample-note">{children}</p>
}

export default function CatalogGraphTabs() {
  const [subtab, setSubtab] = useState('initial') // 'initial' | 'joinability' | 'composition'
  const [full, setFull] = useState(null)
  const [usable, setUsable] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    Promise.all([api.getCatalog(), api.getUsableCatalog()])
      .then(([fullResult, usableResult]) => {
        setFull(fullResult)
        setUsable(usableResult)
        setError(null)
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  if (error) return <div className="error-banner">{error}</div>
  if (loading || !full) return <p className="muted">Loading catalog...</p>

  return (
    <div>
      <div className="catalog-mode-toggle">
        <button className={subtab === 'initial' ? 'tab active' : 'tab'} onClick={() => setSubtab('initial')}>
          Initial data
        </button>
        <button className={subtab === 'joinability' ? 'tab active' : 'tab'} onClick={() => setSubtab('joinability')}>
          Joinability graph
        </button>
        <button className={subtab === 'composition' ? 'tab active' : 'tab'} onClick={() => setSubtab('composition')}>
          Composition graph
        </button>
      </div>

      {subtab === 'initial' && <InitialDataGraph full={full} usable={usable} />}

      {subtab === 'joinability' &&
        (full.has_gold_layer_run ? (
          <JoinabilityGraph catalog={full} />
        ) : (
          <NotBuiltNote>
            The ad hoc algorithm hasn't been built yet - this view is scoped to its own results only
            ("Starting from every table" exists mainly to compare against it, and stays visible only on
            its own tab). Start an ad hoc build from the "Automatic composition" tab, then come back here
            to see the joins it found.
          </NotBuiltNote>
        ))}

      {subtab === 'composition' &&
        (full.has_gold_layer_run ? (
          <CompositionGraph catalog={full} />
        ) : (
          <NotBuiltNote>
            The ad hoc algorithm hasn't been built yet - this view is scoped to its own results only
            ("Starting from every table" exists mainly to compare against it, and stays visible only on
            its own tab). Start an ad hoc build from the "Automatic composition" tab, then come back here
            to see how it grouped columns.
          </NotBuiltNote>
        ))}
    </div>
  )
}
