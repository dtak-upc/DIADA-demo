import { useState } from 'react'
import { columnsBySource } from '../../utils/provenanceColors'

// Beyond this a source dataset's column list gets long enough to dominate
// the summary rather than just identify it - truncate with an expand
// toggle instead of either always showing everything or hard-capping it.
const MAX_COLUMNS_SHOWN = 10

// "Columns by source" breakdown: which dataset each output column came
// from, tinting the dataset's own name (rather than a separate color
// swatch next to it) so the color reads as "this text is about that
// dataset" instead of a decoration that can drift out of alignment with
// the name once lines wrap. Shared by IterationPanel.jsx's raw preview and
// CurationPanel.jsx's CF1/CF2 results, so the same look and truncation
// behavior applies everywhere a join's provenance is shown.
export default function ColumnsBySource({ columnProvenance, colors }) {
  const [expanded, setExpanded] = useState({}) // dataset -> bool
  const bySource = columnsBySource(columnProvenance)

  return (
    <div className="provenance-summary">
      {Object.entries(bySource).map(([dataset, cols]) => {
        const isExpanded = expanded[dataset]
        const shown = isExpanded ? cols : cols.slice(0, MAX_COLUMNS_SHOWN)
        const hiddenCount = cols.length - shown.length
        return (
          <p key={dataset} className="provenance-line">
            <strong style={{ color: colors[dataset] || '#6b7280' }}>{dataset}</strong>: {shown.join(', ')}
            {hiddenCount > 0 && (
              <button
                className="link-button provenance-expand"
                onClick={() => setExpanded((prev) => ({ ...prev, [dataset]: true }))}
              >
                +{hiddenCount} more
              </button>
            )}
            {isExpanded && cols.length > MAX_COLUMNS_SHOWN && (
              <button
                className="link-button provenance-expand"
                onClick={() => setExpanded((prev) => ({ ...prev, [dataset]: false }))}
              >
                show less
              </button>
            )}
          </p>
        )
      })}
    </div>
  )
}
