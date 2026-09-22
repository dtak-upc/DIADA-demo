import { useState } from 'react'
import { api } from '../../api/client'
import { declutteredColumnSet } from '../../utils/declutter'
import { colorsForJoin, columnColorsFromProvenance } from '../../utils/provenanceColors'
import { formatNormalizedScore, formatRawScore, normalizeScore } from '../../utils/score'
import CandidatePicker from './CandidatePicker'
import ColumnsBySource from './ColumnsBySource'
import CurationPanel from './CurationPanel'
import DataTable from '../DataTable'
import DeclutterSection from './DeclutterSection'

const PAGE_SIZE = 50

// Slider positions, strictest (fewest results) to loosest (everything).
// `null` means "all" - no filtering, not even score >= 0 (scores can be
// negative, see the model's own output). Mirrors the thresholds the old
// single-search Join Discovery tab used.
const THRESHOLD_LEVELS = [0.1, 0.01, 0.001, null]
const DEFAULT_THRESHOLD_INDEX = 1 // 0.01

function formatThreshold(threshold) {
  return threshold === null ? 'All' : String(threshold)
}

function columnKey(c) {
  return `${c.dataset}::${c.column}`
}

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

// One iteration's workspace: a join built from an arbitrary number of
// candidates attached through an arbitrary number of the current base's
// own columns ("multi-attribute" - table A's a1 to reach B, a2 to reach C,
// all in this one iteration), run all at once. Once run, its result
// becomes the starting point ("root" of the next search) a *further*
// iteration can attach even more onto - see JoinDiscovery.jsx, which owns
// the sequence of iterations and decides which one is this active,
// editable one vs. an earlier, now-frozen one (rendered read-only, same
// component, just with isLast=false and its own already-composed
// steps/preview instead of live search state).
//
// priorSteps is every step committed by an earlier iteration - always
// included whole in every request this component makes (nothing is
// persisted server-side, see app/join_builder.py's build_chain - nothing
// here is stateful across requests either). pendingSteps is just this
// iteration's own, still-being-assembled contribution.
export default function IterationPanel({
  label,
  rootDataset,
  priorSteps,
  availableColumns, // [{dataset, column, mainType}] - what this iteration may search FROM
  excludeDatasets, // Set<string> - datasets already anywhere in the chain so far, root included
  getPkInfo,
  isLast,
  frozenPreview, // for an earlier, already-composed iteration: its own cached /join/preview result, shown read-only instead of live search state
  onComposed, // (steps, preview) => void
}) {
  const [pendingSteps, setPendingSteps] = useState([])
  const [selectedColumnKey, setSelectedColumnKey] = useState(availableColumns[0] ? columnKey(availableColumns[0]) : '')
  const selectedColumn = availableColumns.find((c) => columnKey(c) === selectedColumnKey)

  const [results, setResults] = useState(null)
  const [resultsBase, setResultsBase] = useState(null)
  const [scoreBounds, setScoreBounds] = useState(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [displayMode, setDisplayMode] = useState('all')
  const [scoreMode, setScoreMode] = useState('normalized')
  const [thresholdIndex, setThresholdIndex] = useState(DEFAULT_THRESHOLD_INDEX)
  const threshold = THRESHOLD_LEVELS[thresholdIndex]

  const [preview, setPreview] = useState(frozenPreview || null)
  const [page, setPage] = useState(0)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState(null)
  const [downloadLoading, setDownloadLoading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)
  const [hideUnusedPk, setHideUnusedPk] = useState(false)
  const [hideRedundant, setHideRedundant] = useState(false)
  const [hideMostlyNull, setHideMostlyNull] = useState(false)

  const editable = isLast && !frozenPreview
  const allSteps = [...priorSteps, ...pendingSteps]
  const pendingExcluded = new Set([...excludeDatasets, ...pendingSteps.map((s) => s.dataset)])

  function buildPayload() {
    return { root_dataset: rootDataset, steps: allSteps }
  }

  function handleSearch() {
    if (!selectedColumn) return
    setSearchLoading(true)
    setSearchError(null)
    setResults(null)
    setResultsBase(null)
    setScoreBounds(null)
    api
      .getJoinDiscovery(selectedColumn.dataset, selectedColumn.column)
      .then((data) => {
        setResults(data.candidates)
        setResultsBase(data.base)
        setScoreBounds(data.score_bounds)
      })
      .catch((e) => setSearchError(e.message))
      .finally(() => setSearchLoading(false))
  }

  function handleAdd(selectedCandidates) {
    const newSteps = selectedCandidates.map((c) => ({
      dataset: c.dataset,
      column: c.column,
      aggregations: c.aggregations,
      base_column: resultsBase.column,
      base_column_dataset: resultsBase.dataset,
    }))
    setPendingSteps((prev) => [...prev, ...newSteps])
    setResults(null)
    setResultsBase(null)
  }

  function removeStep(index) {
    setPendingSteps((prev) => prev.filter((_, i) => i !== index))
    setPreview(null)
  }

  function runJoin(nextPage = 0) {
    setPreviewLoading(true)
    setPreviewError(null)
    api
      .joinPreview(buildPayload(), { limit: PAGE_SIZE, offset: nextPage * PAGE_SIZE })
      .then((data) => {
        setPreview(data)
        setPage(nextPage)
      })
      .catch((e) => setPreviewError(e.message))
      .finally(() => setPreviewLoading(false))
  }

  function download() {
    setDownloadLoading(true)
    setDownloadError(null)
    api
      .joinDownload({
        ...buildPayload(),
        exclude_unused_primary_keys: hideUnusedPk,
        exclude_redundant_columns: hideRedundant,
        exclude_mostly_null_columns: hideMostlyNull,
      })
      .then(({ blob, filename }) => triggerDownload(blob, filename))
      .catch((e) => setDownloadError(e.message))
      .finally(() => setDownloadLoading(false))
  }

  const visibleResults = (() => {
    if (!results) return null
    let rows = results.filter((r) => !pendingExcluded.has(r.dataset))
    rows = threshold === null ? rows : rows.filter((r) => r.score >= threshold)
    if (displayMode === 'best-per-dataset') {
      const byDataset = {}
      for (const r of rows) (byDataset[r.dataset] ||= []).push(r)
      rows = Object.values(byDataset)
        .map((group) => group.reduce((best, r) => (r.score > best.score ? r : best)))
        .sort((a, b) => b.score - a.score)
    }
    return rows
  })()
  const maxScore = visibleResults && visibleResults.length > 0 ? Math.max(...visibleResults.map((r) => r.score), 0) : 0

  const colors = colorsForJoin(rootDataset, allSteps.map((s) => s.dataset))
  const pageCount = preview ? Math.max(1, Math.ceil(preview.total_rows / PAGE_SIZE)) : 1
  const unusedPk = preview?.unused_primary_keys || []
  const redundantGroups = preview?.redundant_column_groups || []
  const mostlyNullColumns = preview?.mostly_null_columns || {}
  const hiddenSet = declutteredColumnSet(
    { unusedPrimaryKeys: unusedPk, redundantGroups, mostlyNullColumns },
    { hideUnusedPk, hideRedundant, hideMostlyNull }
  )
  const displayedColumns = preview ? preview.columns.filter((c) => !hiddenSet.has(c)) : undefined
  const displayedProvenance = preview
    ? Object.fromEntries(Object.entries(preview.column_provenance).filter(([c]) => !hiddenSet.has(c)))
    : undefined

  return (
    <div className="iteration-panel">
      <h3>{label}</h3>

      {allSteps.length > 0 && (
        <ul className="iteration-step-list">
          {priorSteps.map((s, i) => (
            <li key={`prior-${i}`} className="iteration-step iteration-step-frozen">
              {s.dataset} <span className="muted">via {s.base_column_dataset}.{s.base_column}</span>
            </li>
          ))}
          {editable &&
            pendingSteps.map((s, i) => (
              <li key={`pending-${i}`} className="iteration-step">
                {s.dataset} <span className="muted">via {s.base_column_dataset}.{s.base_column}</span>
                <button className="link-button" onClick={() => removeStep(i)}>
                  remove
                </button>
              </li>
            ))}
        </ul>
      )}

      {editable && (
        <div className="iteration-search">
          {availableColumns.length === 0 ? (
            <p className="muted">No further usable columns to search from.</p>
          ) : (
            <>
              <div className="join-column-picker">
                {availableColumns.map((c) => {
                  const pk = getPkInfo(c.dataset, c.column)
                  const key = columnKey(c)
                  return (
                    <button
                      key={key}
                      className={
                        key === selectedColumnKey
                          ? `column-chip join-column-chip join-column-chip-${c.mainType} active`
                          : `column-chip join-column-chip join-column-chip-${c.mainType}`
                      }
                      onClick={() => setSelectedColumnKey(key)}
                      title={pk.isPrimaryKey ? `Primary key - ${Math.round(pk.uniqueness * 100)}% unique` : undefined}
                    >
                      <span className={`type-dot type-dot-${c.mainType}`} />
                      {rootDataset === c.dataset ? c.column : `${c.dataset}.${c.column}`}
                      {pk.isPrimaryKey && <span className="pk-badge">🔑</span>}
                    </button>
                  )
                })}
              </div>

              <div className="join-picker">
                <button onClick={handleSearch} disabled={searchLoading || !selectedColumn}>
                  {searchLoading ? 'Ranking...' : 'Find joins'}
                </button>
              </div>

              {searchError && <div className="error-banner">{searchError}</div>}

              {results && resultsBase && (
                <>
                  <h4>
                    Candidates for {resultsBase.dataset}.{resultsBase.column}
                  </h4>
                  {results.length === 0 ? (
                    <p className="muted">No other usable columns of the same type to compare against.</p>
                  ) : (
                    <>
                      <div className="join-result-controls">
                        <div className="join-score-controls">
                          <label className="join-score-slider">
                            {threshold === null ? 'Showing all scores' : <>Only show score &ge; {formatThreshold(threshold)}</>}
                            <input
                              type="range"
                              min={0}
                              max={THRESHOLD_LEVELS.length - 1}
                              step={1}
                              value={thresholdIndex}
                              onChange={(e) => setThresholdIndex(Number(e.target.value))}
                            />
                            <div className="join-score-slider-ticks">
                              {THRESHOLD_LEVELS.map((t, i) => (
                                <span key={i}>{formatThreshold(t)}</span>
                              ))}
                            </div>
                          </label>
                          <div className="join-display-toggle">
                            <button
                              className={scoreMode === 'normalized' ? 'tab active' : 'tab'}
                              onClick={() => setScoreMode('normalized')}
                            >
                              Normalized score
                            </button>
                            <button className={scoreMode === 'raw' ? 'tab active' : 'tab'} onClick={() => setScoreMode('raw')}>
                              Original score
                            </button>
                          </div>
                        </div>
                        <div className="join-display-toggle">
                          <button className={displayMode === 'all' ? 'tab active' : 'tab'} onClick={() => setDisplayMode('all')}>
                            All columns
                          </button>
                          <button
                            className={displayMode === 'best-per-dataset' ? 'tab active' : 'tab'}
                            onClick={() => setDisplayMode('best-per-dataset')}
                          >
                            Best per dataset
                          </button>
                        </div>
                      </div>

                      {visibleResults.length === 0 ? (
                        <p className="muted">
                          No candidates scored &ge; {formatThreshold(threshold)}.{' '}
                          <button className="link-button" onClick={() => setThresholdIndex(THRESHOLD_LEVELS.length - 1)}>
                            Show all {results.length} candidates instead
                          </button>
                          .
                        </p>
                      ) : (
                        <table className="join-results-table">
                          <thead>
                            <tr>
                              <th>Dataset</th>
                              <th>Column</th>
                              <th></th>
                              <th>{scoreMode === 'normalized' ? 'Score (0-1)' : 'Score (original)'}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {visibleResults.map((r) => {
                              const pk = getPkInfo(r.dataset, r.column)
                              return (
                                <tr key={`${r.dataset}.${r.column}`}>
                                  <td>{r.dataset}</td>
                                  <td className="col-name">{r.column}</td>
                                  <td>
                                    {pk.isPrimaryKey && (
                                      <span className="pk-badge" title={`Primary key - ${Math.round(pk.uniqueness * 100)}% unique`}>
                                        🔑 Key
                                      </span>
                                    )}
                                  </td>
                                  <td>
                                    <div className="score-cell">
                                      <div className="score-bar-track">
                                        <div
                                          className="score-bar-fill"
                                          style={{
                                            width: `${
                                              scoreMode === 'normalized' && scoreBounds
                                                ? normalizeScore(r.score, scoreBounds) * 100
                                                : maxScore > 0
                                                  ? Math.max(0, r.score / maxScore) * 100
                                                  : 0
                                            }%`,
                                          }}
                                        />
                                      </div>
                                      <span className="score-value">
                                        {scoreMode === 'normalized' && scoreBounds
                                          ? formatNormalizedScore(r.score, scoreBounds)
                                          : formatRawScore(r.score)}
                                      </span>
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      )}

                      <CandidatePicker
                        baseDataset={resultsBase.dataset}
                        baseColumn={resultsBase.column}
                        results={results}
                        excludeDatasets={pendingExcluded}
                        onAdd={handleAdd}
                      />
                    </>
                  )}
                </>
              )}
            </>
          )}

          <div className="join-builder-actions">
            <button onClick={() => runJoin(0)} disabled={previewLoading || allSteps.length === 0}>
              {previewLoading ? 'Joining...' : 'Run join'}
            </button>
          </div>
        </div>
      )}

      {previewError && <div className="error-banner">{previewError}</div>}

      {preview && (
        <div className="join-builder-result">
          <h4 className="join-builder-result-title">Joined data</h4>

          <DataTable
            preview={{ ...preview, columns: displayedColumns }}
            loading={previewLoading}
            page={page}
            pageCount={pageCount}
            onPrev={() => runJoin(Math.max(0, page - 1))}
            onNext={() => runJoin(Math.min(pageCount - 1, page + 1))}
            columnColors={columnColorsFromProvenance(displayedProvenance, colors)}
          />

          <div className="join-builder-actions">
            <button onClick={download} disabled={downloadLoading} className="secondary">
              {downloadLoading ? 'Preparing CSV...' : 'Download full table as CSV'}
            </button>
          </div>
          {downloadError && <div className="error-banner">{downloadError}</div>}

          <div className="join-builder-info-columns">
            <div className="join-builder-info-left">
              {preview.aggregated_datasets.length > 0 && (
                <p className="join-note">Aggregated (key column wasn't unique): {preview.aggregated_datasets.join(', ')}</p>
              )}
              {Object.keys(preview.renamed_columns).length > 0 && (
                <p className="join-note">
                  Renamed to avoid name collisions:{' '}
                  {Object.entries(preview.renamed_columns)
                    .map(([from, to]) => `${from} → ${to}`)
                    .join(', ')}
                </p>
              )}

              <p className="provenance-heading muted">Columns by source</p>
              <ColumnsBySource columnProvenance={displayedProvenance} colors={colors} />
            </div>

            <DeclutterSection
              toggles={{ hideUnusedPk, setHideUnusedPk, hideRedundant, setHideRedundant, hideMostlyNull, setHideMostlyNull }}
              unusedPrimaryKeys={unusedPk}
              redundantGroups={redundantGroups}
              mostlyNullColumns={mostlyNullColumns}
            />
          </div>

          <CurationPanel key={JSON.stringify(buildPayload())} payload={buildPayload()} colors={colors} />

          {editable && (
            <div className="iteration-next-actions">
              <button onClick={() => onComposed(allSteps, preview)}>
                Join further from this result &rarr;
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
