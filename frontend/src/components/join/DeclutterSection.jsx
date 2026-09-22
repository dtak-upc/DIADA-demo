import DeclutterControl from './DeclutterControl'
import DeclutteredColumnsSummary from './DeclutteredColumnsSummary'

// The right-hand column of a dataset's "columns by source / declutter"
// info row: the toggle button (+ per-task customize checkboxes) and,
// beneath it, exactly which columns it actually hid and why. Shared by
// IterationPanel.jsx's raw preview and CurationPanel.jsx's CF1/CF2 results,
// each passing its own independent `toggles` (see useSectionDeclutter in
// CurationPanel.jsx) so one dataset's declutter choice never affects another's.
export default function DeclutterSection({ toggles, unusedPrimaryKeys, redundantGroups, mostlyNullColumns }) {
  return (
    <div className="join-builder-info-right">
      <DeclutterControl
        unusedPrimaryKeys={unusedPrimaryKeys}
        redundantGroups={redundantGroups}
        mostlyNullColumns={mostlyNullColumns}
        hideUnusedPk={toggles.hideUnusedPk}
        setHideUnusedPk={toggles.setHideUnusedPk}
        hideRedundant={toggles.hideRedundant}
        setHideRedundant={toggles.setHideRedundant}
        hideMostlyNull={toggles.hideMostlyNull}
        setHideMostlyNull={toggles.setHideMostlyNull}
      />
      <DeclutteredColumnsSummary
        unusedPrimaryKeys={unusedPrimaryKeys}
        redundantGroups={redundantGroups}
        mostlyNullColumns={mostlyNullColumns}
        hideUnusedPk={toggles.hideUnusedPk}
        hideRedundant={toggles.hideRedundant}
        hideMostlyNull={toggles.hideMostlyNull}
      />
    </div>
  )
}
