// Shared logic for the "Declutter" feature: three independent ways a
// joined result's columns can be safely trimmed down without losing
// predictive information - see backend/app/join_builder.py's
// unused_primary_key_columns (an identifier that wasn't the join key),
// redundant_column_groups (several columns that are just different
// encodings of the same information, like a currency's code and its
// name), and mostly_null_columns (a left-joined candidate that barely
// matched, leaving one of its columns almost entirely null).

// Every non-kept column across every redundant group, flattened - the set
// of columns "hide redundant columns" would remove.
export function redundantHiddenColumns(groups) {
  const hidden = []
  for (const g of groups) {
    for (const c of g.columns) {
      if (c !== g.kept) hidden.push(c)
    }
  }
  return hidden
}

// info: {unusedPrimaryKeys: string[], redundantGroups: {columns,kept}[],
// mostlyNullColumns: {[column]: nullFraction}}
// toggles: {hideUnusedPk, hideRedundant, hideMostlyNull} booleans
//
// The full set of columns currently hidden by the declutter toggles, for
// filtering a preview's columns/rows/provenance client-side.
export function declutteredColumnSet(info, toggles) {
  const hidden = new Set()
  if (toggles.hideUnusedPk) for (const c of info.unusedPrimaryKeys) hidden.add(c)
  if (toggles.hideRedundant) for (const c of redundantHiddenColumns(info.redundantGroups)) hidden.add(c)
  if (toggles.hideMostlyNull) for (const c of Object.keys(info.mostlyNullColumns)) hidden.add(c)
  return hidden
}
