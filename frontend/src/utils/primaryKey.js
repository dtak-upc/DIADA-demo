// Mirrors catalog.PRIMARY_KEY_UNIQUENESS_THRESHOLD (backend, app/catalog.py)
// - keep the two numbers in sync by hand. A column counts as a primary key
// if it's string-typed (matches the same restriction join discovery's own
// model applies to candidates - a highly unique *numeric* column, like
// revenue or income, is exactly the predictive signal a join is meant to
// bring in, not an identifier to discard) and at least this fraction of its
// non-null values are distinct.
export const PRIMARY_KEY_UNIQUENESS_THRESHOLD = 0.9

// Builds a fast (dataset, column) -> {isPrimaryKey, uniqueness} lookup from
// the usable-catalog payload (GET /api/catalog/usable), already loaded by
// JoinDiscovery - same formula as catalog.usable_column_uniqueness on the
// backend, computed here instead of over the wire since the catalog payload
// already has everything needed (unique_count/null_count/main_type per
// column, row_count per dataset).
export function buildPrimaryKeyLookup(usableCatalog) {
  if (!usableCatalog) return () => ({ isPrimaryKey: false, uniqueness: 0 })

  const rowCounts = {}
  for (const node of usableCatalog.nodes) {
    if (node.kind === 'dataset') rowCounts[node.name] = node.row_count
  }

  const info = {}
  for (const node of usableCatalog.nodes) {
    if (node.kind !== 'column') continue
    const key = `${node.dataset}.${node.name}`
    if (node.main_type !== 'string') {
      info[key] = { isPrimaryKey: false, uniqueness: 0 }
      continue
    }
    const nonNull = (rowCounts[node.dataset] || 0) - node.null_count
    const uniqueness = nonNull > 0 ? node.unique_count / nonNull : 0
    info[key] = { isPrimaryKey: uniqueness >= PRIMARY_KEY_UNIQUENESS_THRESHOLD, uniqueness }
  }

  return (dataset, column) => info[`${dataset}.${column}`] || { isPrimaryKey: false, uniqueness: 0 }
}
