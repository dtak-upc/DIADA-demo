import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'

// Mirrors join_builder.py's NUMERIC_AGGREGATIONS/STRING_AGGREGATIONS - kept
// in sync by hand, same convention as JoinBuilder.jsx's own copy used to be.
const NUMERIC_AGGREGATIONS = ['mean', 'sum', 'min', 'max', 'median']
const STRING_AGGREGATIONS = ['most_common', 'first', 'count_distinct']
const DEFAULT_AGGREGATION = { numeric: 'mean', string: 'most_common' }

// One entry per dataset, best-scoring column only - a multi-way join needs
// one key column per candidate dataset, so duplicate-dataset entries
// wouldn't make sense to offer here regardless of the discovery tab's own
// display mode.
function bestPerDataset(rows) {
  const byDataset = {}
  for (const r of rows) {
    ;(byDataset[r.dataset] ||= []).push(r)
  }
  const best = Object.values(byDataset).map((group) => {
    const maxScore = Math.max(...group.map((r) => r.score))
    const tied = group.filter((r) => r.score === maxScore)
    return tied[Math.floor(Math.random() * tied.length)]
  })
  return best.sort((a, b) => b.score - a.score)
}

// Picks which of a search's ranked candidates to attach, and how to
// aggregate each if it needs to be (key column not unique) - the same
// top-K/manual/aggregation-override UI the old single-search JoinBuilder
// had, just scoped to *one search's* results and handing its pick back to
// a caller (IterationPanel) instead of running the join itself: an
// iteration can run several searches (one per base column it wants to
// attach through - see the module docstring on IterationPanel), so
// "select candidates" and "commit to a join" are now two separate steps.
export default function CandidatePicker({ baseDataset, baseColumn, results, excludeDatasets, onAdd }) {
  const pool = useMemo(() => {
    const filtered = excludeDatasets ? results.filter((r) => !excludeDatasets.has(r.dataset)) : results
    return bestPerDataset(filtered)
  }, [results, excludeDatasets])

  const [count, setCount] = useState(Math.min(3, pool.length))
  const [countInput, setCountInput] = useState(String(Math.min(3, pool.length)))
  useEffect(() => {
    const next = Math.min(3, pool.length)
    setCount(next)
    setCountInput(String(next))
  }, [pool.length])

  const [selectionMode, setSelectionMode] = useState('top-k')
  const [manualSelected, setManualSelected] = useState(() => new Set())
  useEffect(() => {
    setSelectionMode('top-k')
    setManualSelected(new Set())
  }, [results])

  function enableManualSelection() {
    if (selectionMode === 'manual') return
    setManualSelected(new Set(pool.slice(0, count).map((c) => c.dataset)))
    setSelectionMode('manual')
  }

  function enableTopKSelection() {
    setSelectionMode('top-k')
  }

  function toggleManualDataset(dataset) {
    setManualSelected((prev) => {
      const next = new Set(prev)
      if (next.has(dataset)) next.delete(dataset)
      else next.add(dataset)
      return next
    })
  }

  const selected =
    selectionMode === 'manual' ? pool.filter((c) => manualSelected.has(c.dataset)) : pool.slice(0, count)

  const [profiles, setProfiles] = useState({}) // dataset -> columns[]
  useEffect(() => {
    for (const c of selected) {
      if (profiles[c.dataset]) continue
      api
        .getProfile(c.dataset)
        .then((p) => setProfiles((prev) => ({ ...prev, [c.dataset]: p.columns })))
        .catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected.map((c) => c.dataset).join(',')])

  const [overrides, setOverrides] = useState({}) // "dataset.column" -> func
  function setOverride(dataset, column, func) {
    setOverrides((prev) => ({ ...prev, [`${dataset}.${column}`]: func }))
  }

  function add() {
    onAdd(
      selected.map((c) => {
        const aggregations = {}
        for (const [key, func] of Object.entries(overrides)) {
          const [ds, col] = key.split('.')
          if (ds === c.dataset) aggregations[col] = func
        }
        return { dataset: c.dataset, column: c.column, aggregations }
      })
    )
  }

  if (pool.length === 0) {
    return (
      <p className="muted">
        Every candidate dataset here is already part of this join - nothing left to add through{' '}
        {baseDataset}.{baseColumn}.
      </p>
    )
  }

  return (
    <div className="candidate-picker">
      <p className="muted">
        Attach the top-scoring candidate column from each chosen dataset to {baseDataset}.{baseColumn} -
        by default the top-scoring {count} of {pool.length}, or pick specific datasets yourself below. If
        a candidate's key column isn't unique, that dataset is aggregated first - numeric columns default
        to <strong>mean</strong>, string columns to <strong>most common value</strong>, both overridable
        below.
      </p>

      <div className="join-display-toggle">
        <button className={selectionMode === 'top-k' ? 'tab active' : 'tab'} onClick={enableTopKSelection}>
          Top-scoring
        </button>
        <button className={selectionMode === 'manual' ? 'tab active' : 'tab'} onClick={enableManualSelection}>
          Choose manually
        </button>
      </div>

      {selectionMode === 'top-k' ? (
        <label className="join-builder-count">
          Number of candidate datasets to attach
          <input
            type="number"
            min={1}
            max={pool.length}
            value={countInput}
            onChange={(e) => {
              const raw = e.target.value
              setCountInput(raw)
              if (raw === '') return
              const v = Number(raw)
              if (!Number.isNaN(v) && v >= 1) setCount(Math.min(pool.length, Math.floor(v)))
            }}
            onBlur={() => {
              const v = Number(countInput)
              if (countInput === '' || Number.isNaN(v) || v < 1) {
                setCountInput(String(count))
              } else {
                const clamped = Math.max(1, Math.min(pool.length, Math.floor(v)))
                setCount(clamped)
                setCountInput(String(clamped))
              }
            }}
          />
        </label>
      ) : (
        <div className="join-manual-picker">
          {pool.map((c, i) => (
            <label key={c.dataset} className="join-manual-picker-row">
              <input
                type="checkbox"
                checked={manualSelected.has(c.dataset)}
                onChange={() => toggleManualDataset(c.dataset)}
              />
              <span className="join-manual-picker-rank">#{i + 1}</span>
              <span className="join-manual-picker-name">{c.dataset}</span>
              <span className="muted">.{c.column}</span>
            </label>
          ))}
        </div>
      )}

      {selected.map((c) => {
        const columns = (profiles[c.dataset] || []).filter((col) => col.name !== c.column)
        if (columns.length === 0) return null
        return (
          <details key={c.dataset} className="join-agg-settings">
            <summary>Aggregation settings for "{c.dataset}" (used only if it needs to be aggregated)</summary>
            <table className="join-agg-table">
              <thead>
                <tr>
                  <th>Column</th>
                  <th>Type</th>
                  <th>Aggregation</th>
                </tr>
              </thead>
              <tbody>
                {columns.map((col) => {
                  const isNumeric = col.main_type === 'numeric'
                  const options = isNumeric ? NUMERIC_AGGREGATIONS : STRING_AGGREGATIONS
                  const current =
                    overrides[`${c.dataset}.${col.name}`] || DEFAULT_AGGREGATION[isNumeric ? 'numeric' : 'string']
                  return (
                    <tr key={col.name}>
                      <td className="col-name">{col.name}</td>
                      <td>
                        <span className={`type-dot type-dot-${col.main_type}`} /> {col.main_type}
                      </td>
                      <td>
                        <select value={current} onChange={(e) => setOverride(c.dataset, col.name, e.target.value)}>
                          {options.map((opt) => (
                            <option key={opt} value={opt}>
                              {opt}
                            </option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </details>
        )
      })}

      <div className="join-builder-actions">
        <button onClick={add} disabled={selected.length === 0}>
          Add {selected.length} dataset{selected.length === 1 ? '' : 's'} to this iteration
        </button>
      </div>
    </div>
  )
}
