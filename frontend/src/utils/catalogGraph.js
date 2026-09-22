// Shared node styling for every catalog-graph subtab (see
// CatalogGraphTabs.jsx / GraphCanvas.jsx) - node kind/shape/coloring/
// tooltip conventions are the same regardless of which subtab is showing,
// only the actual set of nodes/edges and how edges are styled differs.

export const RADIUS = { dataset: 13, column: 7, quality_flag: 6, gold_table: 11 }

export function nodeRadius(node) {
  return RADIUS[node.kind] ?? 8
}

export function nodeColor(node) {
  if (node.kind === 'dataset') return '#6366f1'
  if (node.kind === 'gold_table') return '#ec4899'
  if (node.kind === 'quality_flag') return '#d97706'
  return node.main_type === 'numeric' ? '#3b82f6' : '#22c55e'
}

export function nodeLabelText(node) {
  return node.name || node.type || node.id
}

// Defensive: an uncaught exception in here throws inside react-force-
// graph-2d's own mouse-move handler, which corrupts its internal
// interaction state and silently breaks all further dragging/hovering.
// Missing fields should degrade the tooltip, never throw.
export function nodeTooltip(node) {
  if (node.kind === 'dataset') {
    const rows = node.row_count != null ? node.row_count.toLocaleString() : '?'
    return `${node.name ?? node.id}\n${rows} rows · ${node.column_count ?? '?'} cols`
  }
  if (node.kind === 'gold_table') {
    const rows = node.row_count != null ? node.row_count.toLocaleString() : '?'
    const kindLabel = (node.table_kind ?? '').replace(/_/g, ' ')
    return `${node.name ?? node.id}\n${kindLabel}\n${rows} rows · ${node.column_count ?? '?'} cols`
  }
  if (node.kind === 'column') {
    return `${node.name ?? node.id}\n${(node.secondary_type ?? '').replace(/_/g, ' ')}`
  }
  return (node.type ?? '').replace(/_/g, ' ')
}

// Which "gravity point" a node is pulled toward for layout (see
// GraphCanvas's groupOwnership) - always a real dataset, whether the node
// itself is that dataset, one of its own columns, or a gold table. A gold
// table anchors on root_dataset, not base_dataset: for "every table" mode
// those are the same, but "ad hoc" tables' base_dataset is a
// synthetic display name (e.g. "a+b+c::anchor=b"), not a real dataset node
// - anchoring on that would invent a phantom group with no dataset node in
// it. root_dataset is always the real, loadable dataset backing the table
// (see app/gold_layer.py's GoldTable.root_dataset docstring). quality_flag
// nodes return null on purpose: they have no group field of their own, and
// inherit one instead through their has_flag edge (GraphCanvas handles
// that generically).
export function groupOf(node) {
  if (node.kind === 'dataset') return node.id
  if (node.kind === 'column') return `dataset:${node.dataset}`
  if (node.kind === 'gold_table') return `dataset:${node.root_dataset}`
  return null
}
