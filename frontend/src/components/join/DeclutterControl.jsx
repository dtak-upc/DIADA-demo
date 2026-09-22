import { declutteredColumnSet, redundantHiddenColumns } from '../../utils/declutter'

// One master "Declutter" toggle for all three cleaning tasks (see
// backend/app/join_builder.py: unused_primary_key_columns,
// redundant_column_groups, mostly_null_columns), plus a dropdown giving
// full control over each independently - clicking the button flips all
// three together; the checkboxes underneath override that per-task. Each
// displayed dataset (the raw join, CF1, and every CF2 cluster) gets its
// own instance of this with its own toggle state - one dataset's declutter
// choice has no bearing on any other's.
export default function DeclutterControl({
  unusedPrimaryKeys,
  redundantGroups,
  mostlyNullColumns,
  hideUnusedPk,
  setHideUnusedPk,
  hideRedundant,
  setHideRedundant,
  hideMostlyNull,
  setHideMostlyNull,
}) {
  const unusedPrimaryKeyCount = unusedPrimaryKeys.length
  const redundantHiddenCount = redundantHiddenColumns(redundantGroups).length
  const mostlyNullCount = Object.keys(mostlyNullColumns).length
  if (unusedPrimaryKeyCount === 0 && redundantHiddenCount === 0 && mostlyNullCount === 0) return null

  const info = { unusedPrimaryKeys, redundantGroups, mostlyNullColumns }
  // A column can qualify under more than one task at once (e.g. an unused
  // primary key that's also mostly null) - union the sets rather than
  // adding their counts, or it'd be double-counted in the label.
  const hiddenCount = declutteredColumnSet(info, { hideUnusedPk, hideRedundant, hideMostlyNull }).size
  const allOn = hideUnusedPk && hideRedundant && hideMostlyNull

  function toggleAll() {
    const next = !allOn
    setHideUnusedPk(next)
    setHideRedundant(next)
    setHideMostlyNull(next)
  }

  return (
    <div className="declutter-control">
      <button className={hiddenCount > 0 ? 'declutter-btn active' : 'declutter-btn'} onClick={toggleAll}>
        🧹 Declutter{hiddenCount > 0 ? ` (${hiddenCount} hidden)` : ''}
      </button>
      <details className="declutter-options">
        <summary>Customize</summary>
        <label className="toggle">
          <input
            type="checkbox"
            checked={hideUnusedPk}
            disabled={unusedPrimaryKeyCount === 0}
            onChange={(e) => setHideUnusedPk(e.target.checked)}
          />
          Hide primary keys not used in the join ({unusedPrimaryKeyCount})
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={hideRedundant}
            disabled={redundantHiddenCount === 0}
            onChange={(e) => setHideRedundant(e.target.checked)}
          />
          Hide redundant columns ({redundantHiddenCount})
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={hideMostlyNull}
            disabled={mostlyNullCount === 0}
            onChange={(e) => setHideMostlyNull(e.target.checked)}
          />
          Hide mostly-null columns ({mostlyNullCount})
        </label>
      </details>
    </div>
  )
}
