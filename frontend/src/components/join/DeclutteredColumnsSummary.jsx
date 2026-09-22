// Lists exactly which columns Declutter actually removed and why - only
// the categories currently toggled on, not every category that could be
// removed. One line per reason, columns that share a reason grouped onto
// it together, in the "<col, col, ...>: <reason>" shape rather than
// describing the kept column and leaving the reader to infer what
// happened to the others. Shared by IterationPanel.jsx's raw preview (where
// the toggles live, next to its own Declutter button) and
// CurationPanel.jsx's CF1/CF2 results (which inherit the same toggles as
// read-only booleans - curation has no Declutter button of its own).
export default function DeclutteredColumnsSummary({
  unusedPrimaryKeys,
  redundantGroups,
  mostlyNullColumns,
  hideUnusedPk,
  hideRedundant,
  hideMostlyNull,
}) {
  const lines = []
  if (hideUnusedPk && unusedPrimaryKeys.length > 0) {
    lines.push({
      key: 'unused-pk',
      columns: unusedPrimaryKeys,
      reason: "primary key of its own dataset, but not the column the join was performed on",
    })
  }
  if (hideRedundant) {
    for (const g of redundantGroups) {
      const hidden = g.columns.filter((c) => c !== g.kept)
      if (hidden.length > 0) lines.push({ key: `redundant-${g.kept}`, columns: hidden, reason: `same information as ${g.kept}` })
    }
  }
  if (hideMostlyNull) {
    for (const [col, fraction] of Object.entries(mostlyNullColumns)) {
      lines.push({ key: `null-${col}`, columns: [col], reason: `${(fraction * 100).toFixed(1)}% null` })
    }
  }
  if (lines.length === 0) return null

  return (
    <div className="declutter-removed-summary">
      {lines.map((l) => (
        <p key={l.key} className="declutter-removed-line">
          <strong>{l.columns.join(', ')}</strong>: {l.reason}
        </p>
      ))}
    </div>
  )
}
