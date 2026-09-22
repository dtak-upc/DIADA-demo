// Assigns a stable color per source dataset within one join - the same
// dataset should read as the same color everywhere on one card/panel (a
// join diagram, DataTable's column header tinting, "columns by source").
// Used by both the autonomous gold-layer tab (GoldLayer.jsx, one join per
// gold table) and the manual join-discovery flow (IterationPanel.jsx/
// CurationPanel.jsx, one join per chain iteration).
const PALETTE = [
  '#f97316', // orange
  '#0ea5e9', // sky
  '#22c55e', // green
  '#e11d48', // rose
  '#a855f7', // purple
  '#eab308', // amber
  '#14b8a6', // teal
  '#ec4899', // pink
  '#84cc16', // lime
]

const BASE_COLOR = '#6366f1' // indigo - matches the "base" role color used elsewhere (CatalogGraph, CurationGraph)

// baseDataset always gets BASE_COLOR; every other dataset (candidates, in
// the order given - typically join_candidates' own best-score order) gets
// the next unused palette color, cycling if there are more than PALETTE.length.
export function colorsForJoin(baseDataset, otherDatasets) {
  const colors = { [baseDataset]: BASE_COLOR }
  let i = 0
  for (const dataset of otherDatasets) {
    if (dataset in colors) continue
    colors[dataset] = PALETTE[i % PALETTE.length]
    i += 1
  }
  return colors
}

// {dataset -> [column, ...]} grouping of a column_provenance map ({column
// -> dataset}), for the "columns by source" breakdown shown alongside a
// JoinGraph. Preserves first-appearance order of both the outer (dataset)
// and inner (column) entries, which for a join_builder-produced
// column_provenance means the base dataset's own columns come first.
export function columnsBySource(columnProvenance) {
  const bySource = {}
  for (const [col, dataset] of Object.entries(columnProvenance)) {
    ;(bySource[dataset] ||= []).push(col)
  }
  return bySource
}

// {column -> color} for DataTable's columnColors prop, from a
// column_provenance map ({column -> dataset}) and a colorsForJoin() result.
export function columnColorsFromProvenance(columnProvenance, colors) {
  return Object.fromEntries(
    Object.entries(columnProvenance).map(([col, dataset]) => [col, colors[dataset] || '#9ca3af'])
  )
}
