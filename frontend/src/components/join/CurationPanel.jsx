import { useState } from 'react'
import { api } from '../../api/client'
import { declutteredColumnSet } from '../../utils/declutter'
import { columnColorsFromProvenance } from '../../utils/provenanceColors'
import ColumnsBySource from './ColumnsBySource'
import CurationGraph from './CurationGraph'
import DeclutterSection from './DeclutterSection'

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

function csvField(value) {
  const s = String(value)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function downloadSoundnessPairs(pairs, filename) {
  const header = 'column_1,column_2,soundness'
  const lines = pairs.map((p) => [p.column_1, p.column_2, p.soundness].map(csvField).join(','))
  const blob = new Blob([[header, ...lines].join('\n')], { type: 'text/csv' })
  triggerDownload(blob, filename)
}

function visibleColumns(columns, hiddenSet) {
  return columns.filter((c) => !hiddenSet.has(c))
}

function visibleProvenance(columnProvenance, hiddenSet) {
  return Object.fromEntries(Object.entries(columnProvenance).filter(([c]) => !hiddenSet.has(c)))
}

function RawPreviewTable({ columns, rows, totalRows, columnColors }) {
  return (
    <div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c} style={columnColors?.[c] ? { boxShadow: `inset 0 -3px 0 ${columnColors[c]}` } : undefined}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c}>
                    {row[c] === null || row[c] === undefined ? (
                      <span className="null-cell">null</span>
                    ) : (
                      String(row[c])
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted curation-preview-note">
        Showing {rows.length} of {totalRows.toLocaleString()} row{totalRows === 1 ? '' : 's'} - download for
        the full result.
      </p>
    </div>
  )
}

// One dataset's own declutter state (button + gated removed-columns
// summary) plus the derived hidden-column set - used identically for CF1
// and for each CF2 cluster below, each getting its own independent
// instance rather than sharing one global toggle.
function useSectionDeclutter() {
  const [hideUnusedPk, setHideUnusedPk] = useState(false)
  const [hideRedundant, setHideRedundant] = useState(false)
  const [hideMostlyNull, setHideMostlyNull] = useState(false)
  return { hideUnusedPk, setHideUnusedPk, hideRedundant, setHideRedundant, hideMostlyNull, setHideMostlyNull }
}

// One CF2 cluster's table, own declutter section, and own download button -
// a plain component (not inlined in the .map() below) purely so each
// cluster can call its own useSectionDeclutter() hook; hooks can't be
// called conditionally or in a variable number of times inside a loop
// body directly.
function ClusterSection({ cluster, result, colors, payload }) {
  const toggles = useSectionDeclutter()
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)

  const hidden = declutteredColumnSet(
    {
      unusedPrimaryKeys: cluster.unused_primary_keys,
      redundantGroups: cluster.redundant_column_groups,
      mostlyNullColumns: cluster.mostly_null_columns,
    },
    toggles
  )

  function download() {
    setDownloading(true)
    setDownloadError(null)
    api
      .curateCf2Download(
        {
          ...payload,
          exclude_unused_primary_keys: toggles.hideUnusedPk,
          exclude_redundant_columns: toggles.hideRedundant,
          exclude_mostly_null_columns: toggles.hideMostlyNull,
        },
        cluster.name
      )
      .then(({ blob, filename }) => triggerDownload(blob, filename))
      .catch((e) => setDownloadError(e.message))
      .finally(() => setDownloading(false))
  }

  return (
    <div className="curation-cluster">
      <h5>
        {cluster.name === 'unclustered' ? 'Unclustered' : cluster.name.replace('_', ' ')}{' '}
        <span className="muted">({cluster.columns.length} columns)</span>
      </h5>

      <RawPreviewTable
        columns={visibleColumns(cluster.columns, hidden)}
        rows={cluster.rows}
        totalRows={result.row_count}
        columnColors={columnColorsFromProvenance(visibleProvenance(cluster.column_provenance, hidden), colors)}
      />

      <div className="curation-actions">
        <button onClick={download} disabled={downloading} className="secondary">
          {downloading ? 'Preparing CSV...' : 'Download CSV'}
        </button>
      </div>
      {downloadError && <div className="error-banner">{downloadError}</div>}

      <div className="join-builder-info-columns">
        <div className="join-builder-info-left">
          <p className="provenance-heading muted">Columns by source</p>
          <ColumnsBySource columnProvenance={visibleProvenance(cluster.column_provenance, hidden)} colors={colors} />
        </div>
        <DeclutterSection
          toggles={toggles}
          unusedPrimaryKeys={cluster.unused_primary_keys}
          redundantGroups={cluster.redundant_column_groups}
          mostlyNullColumns={cluster.mostly_null_columns}
        />
      </div>
    </div>
  )
}

export default function CurationPanel({ payload, colors }) {
  const [curating, setCurating] = useState(false)
  const [curateError, setCurateError] = useState(null)
  const [result, setResult] = useState(null)

  const [cf1Downloading, setCf1Downloading] = useState(false)
  const [cf1DownloadError, setCf1DownloadError] = useState(null)

  const cf1Toggles = useSectionDeclutter()

  function curate() {
    setCurating(true)
    setCurateError(null)
    api
      .curateJoin(payload)
      .then(setResult)
      .catch((e) => setCurateError(e.message))
      .finally(() => setCurating(false))
  }

  function downloadCf1() {
    setCf1Downloading(true)
    setCf1DownloadError(null)
    api
      .curateCf1Download({
        ...payload,
        exclude_unused_primary_keys: cf1Toggles.hideUnusedPk,
        exclude_redundant_columns: cf1Toggles.hideRedundant,
        exclude_mostly_null_columns: cf1Toggles.hideMostlyNull,
      })
      .then(({ blob, filename }) => triggerDownload(blob, filename))
      .catch((e) => setCf1DownloadError(e.message))
      .finally(() => setCf1Downloading(false))
  }

  const cf1Hidden = result
    ? declutteredColumnSet(
        {
          unusedPrimaryKeys: result.cf1.unused_primary_keys,
          redundantGroups: result.cf1.redundant_column_groups,
          mostlyNullColumns: result.cf1.mostly_null_columns,
        },
        cf1Toggles
      )
    : new Set()

  return (
    <div className="curation-panel">
      <h3>Compose with DIADA</h3>
      <p className="muted">
        Runs DIADA against the joined result above to score every pair of columns by how strongly
        they relate to each other, then offers two independent cleanups built on those scores.
        This can take a few seconds (DIADA is a Java tool run as a subprocess).
      </p>

      <button onClick={curate} disabled={curating}>
        {curating ? 'Running DIADA curation...' : result ? 'Re-run curation' : 'Run DIADA curation'}
      </button>

      {curateError && <div className="error-banner">{curateError}</div>}

      {result && (
        <div className="curation-results">
          <section className="curation-section curation-step-1">
            <h4>
              <span className="curation-step-badge">1</span>
              Remove univariate noise
            </h4>
            <p className="muted">
              Kept {result.cf1.columns.length} of {result.cf1.columns.length + result.cf1.removed_columns.length}{' '}
              columns - every kept column has a meaningful relationship (soundness &ge; 4) with at
              least one other column, or is the column the join was performed on (never removed,
              regardless of its relationships).
            </p>

            <RawPreviewTable
              columns={visibleColumns(result.cf1.columns, cf1Hidden)}
              rows={result.cf1.rows}
              totalRows={result.row_count}
              columnColors={columnColorsFromProvenance(visibleProvenance(result.cf1.column_provenance, cf1Hidden), colors)}
            />

            <div className="curation-actions">
              <button onClick={downloadCf1} disabled={cf1Downloading} className="secondary">
                {cf1Downloading ? 'Preparing CSV...' : 'Download CSV'}
              </button>
            </div>
            {cf1DownloadError && <div className="error-banner">{cf1DownloadError}</div>}

            <div className="join-builder-info-columns">
              <div className="join-builder-info-left">
                {result.cf1.removed_columns.length > 0 ? (
                  <p className="join-note">
                    Removed as univariate noise: {result.cf1.removed_columns.join(', ')}
                  </p>
                ) : (
                  <p className="join-note">No columns were removed - every column relates to at least one other.</p>
                )}
                <p className="provenance-heading muted">Columns by source</p>
                <ColumnsBySource
                  columnProvenance={visibleProvenance(result.cf1.column_provenance, cf1Hidden)}
                  colors={colors}
                />
              </div>
              <DeclutterSection
                toggles={cf1Toggles}
                unusedPrimaryKeys={result.cf1.unused_primary_keys}
                redundantGroups={result.cf1.redundant_column_groups}
                mostlyNullColumns={result.cf1.mostly_null_columns}
              />
            </div>
          </section>

          <section className="curation-section curation-step-2">
            <h4>
              <span className="curation-step-badge">2</span>
              Compose dataset
            </h4>
            <p className="muted">
              Groups columns into clusters of features that relate to each other (soundness &ge;
              4), splitting the joined result into one dataset per cluster. Columns with no
              qualifying relationship to anything are collected into a shared "unclustered"
              dataset. The join column itself is kept in every cluster so each piece stays
              joinable back to the others.
            </p>
            <CurationGraph
              graph={result.cf2.graph}
              clusterOrder={result.cf2.clusters.map((c) => c.name)}
            />
            <div className="curation-actions">
              <button
                onClick={() =>
                  downloadSoundnessPairs(
                    result.soundness_pairs,
                    `${payload.root_dataset}_soundness_pairs.csv`
                  )
                }
                className="secondary"
              >
                Download soundness pairs (CSV)
              </button>
            </div>

            {result.cf2.clusters.map((cluster) => (
              <ClusterSection key={cluster.name} cluster={cluster} result={result} colors={colors} payload={payload} />
            ))}
          </section>
        </div>
      )}
    </div>
  )
}
