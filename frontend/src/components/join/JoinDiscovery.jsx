import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import { buildPrimaryKeyLookup } from '../../utils/primaryKey'
import IterationPanel from './IterationPanel'

function describeSteps(steps) {
  if (steps.length === 0) return null
  return [...new Set(steps.map((s) => s.dataset))].join(', ')
}

// Every column of `preview` (a prior iteration's /join/preview result) that
// still maps back to a genuinely *usable*, catalogued (dataset, column)
// pair - i.e. one a fresh /join-discovery search can actually run against.
// A joined result can also carry columns that aren't independently usable
// (flagged, or simply not the type join-discovery's model was scored for)
// - those ride along in the table just fine but can't themselves be
// searched from.
function searchableColumnsOf(preview, columnsByDataset) {
  const out = []
  for (const col of preview.columns) {
    const dataset = preview.column_provenance[col]
    const original = preview.original_columns[col]
    const info = (columnsByDataset[dataset] || []).find((c) => c.name === original)
    if (info) out.push({ dataset, column: original, mainType: info.mainType })
  }
  return out
}

// Orchestrates a *sequence* of join iterations, one subtab each: iteration
// 0 starts from a real, picked root dataset; every iteration after that
// starts from whatever the previous one's join produced, letting the user
// keep attaching more datasets arbitrarily many hops out. Within any one
// iteration, IterationPanel itself lets the user attach through more than
// one of that iteration's own base columns before running the join at all
// - that's the "multi-attribute" half of this (join through a1 *and* a2
// at once); this component's own job is just the "iterative" half
// (iteration N+1 attaches onto iteration N's already-joined result).
//
// Nothing here is persisted - same "recompute on demand, nothing saved
// server-side" approach as the rest of this app's manual join flow. The
// whole chain lives in this component's own state and is gone on refresh;
// each iteration's own already-run result is cached in `chain` purely so
// switching back to an earlier subtab doesn't need to re-run its join.
export default function JoinDiscovery() {
  const [usable, setUsable] = useState(null)
  const [usableLoading, setUsableLoading] = useState(true)
  const [usableError, setUsableError] = useState(null)

  const [rootDataset, setRootDataset] = useState('')
  // One entry per *finalized* iteration (steps actually run and then
  // explicitly carried forward via "Join further from this result") - the
  // always-present, still-editable iteration in progress is never in here,
  // it's activeTab === chain.length.
  const [chain, setChain] = useState([])
  const [activeTab, setActiveTab] = useState(0)

  useEffect(() => {
    api
      .getUsableCatalog()
      .then(setUsable)
      .catch((e) => setUsableError(e.message))
      .finally(() => setUsableLoading(false))
  }, [])

  const columnsByDataset = useMemo(() => {
    const map = {}
    if (usable) {
      for (const node of usable.nodes) {
        if (node.kind !== 'column') continue
        ;(map[node.dataset] ||= []).push({ name: node.name, mainType: node.main_type })
      }
      for (const cols of Object.values(map)) cols.sort((a, b) => a.name.localeCompare(b.name))
    }
    return map
  }, [usable])

  const datasetNames = useMemo(() => Object.keys(columnsByDataset).sort(), [columnsByDataset])
  const getPkInfo = useMemo(() => buildPrimaryKeyLookup(usable), [usable])

  useEffect(() => {
    if (datasetNames.length === 0) return
    if (!datasetNames.includes(rootDataset)) setRootDataset(datasetNames[0])
  }, [datasetNames, rootDataset])

  function changeRoot(next) {
    setRootDataset(next)
    setChain([])
    setActiveTab(0)
  }

  function handleComposed(index, steps, preview) {
    setChain((prev) => [...prev.slice(0, index), { steps, preview }])
    setActiveTab(index + 1)
  }

  function rollBackTo(index) {
    setChain((prev) => prev.slice(0, index))
    setActiveTab(index)
  }

  if (usableLoading) return <p className="muted">Loading usable columns...</p>
  if (usableError) return <div className="error-banner">{usableError}</div>
  if (datasetNames.length === 0) {
    return (
      <p className="muted">
        No usable datasets/columns yet - everything in this project was filtered out by a
        quality flag. Check the "Datasets" tab.
      </p>
    )
  }

  const tabCount = chain.length + 1

  return (
    <div className="join-discovery">
      <p className="muted">
        Build a join one or more iterations at a time. Each iteration can attach through more
        than one of its base's own columns at once (table A's a1 to reach B, a2 to reach C; a
        "multi-attribute" join), and once run, its result can itself become the base for a
        further iteration ("join further from this result"), any number of hops deep.
      </p>
      <p className="muted pk-legend-note">
        <span className="pk-badge">🔑 Key</span> marks a column that's actually a primary key of
        its own dataset (&ge;90% of its values are distinct). This represents the kind of column a join can
        meaningfully look information up through, rather than fanning out duplicate rows together.
      </p>

      <label className="join-root-picker">
        Root dataset
        <select
          className="source-input join-select"
          value={rootDataset}
          onChange={(e) => changeRoot(e.target.value)}
        >
          {datasetNames.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        {chain.length > 0 && (
          <span className="muted">Changing this starts a fresh chain - every iteration so far will be discarded.</span>
        )}
      </label>

      {tabCount > 1 && (
        <div className="join-display-toggle iteration-tabs">
          {chain.map((it, i) => (
            <button key={i} className={activeTab === i ? 'tab active' : 'tab'} onClick={() => setActiveTab(i)}>
              Iteration {i + 1}
              {describeSteps(it.steps) ? ` — ${describeSteps(it.steps)}` : ''}
              <span
                className="iteration-tab-close"
                title="Remove this and every later iteration"
                onClick={(e) => {
                  e.stopPropagation()
                  rollBackTo(i)
                }}
              >
                &times;
              </span>
            </button>
          ))}
          <button className={activeTab === chain.length ? 'tab active' : 'tab'} onClick={() => setActiveTab(chain.length)}>
            {chain.length === 0 ? 'Iteration 1' : `Iteration ${chain.length + 1} (new)`}
          </button>
        </div>
      )}

      {Array.from({ length: tabCount }, (_, i) => i)
        .filter((i) => i === activeTab)
        .map((i) => {
          const isLast = i === chain.length
          const priorChain = chain.slice(0, isLast ? i : i + 1)
          const priorSteps = priorChain.flatMap((it) => it.steps)
          const availableColumns =
            i === 0
              ? (columnsByDataset[rootDataset] || []).map((c) => ({ dataset: rootDataset, column: c.name, mainType: c.mainType }))
              : searchableColumnsOf(chain[i - 1].preview, columnsByDataset)
          const excludeDatasets = new Set([rootDataset, ...priorSteps.map((s) => s.dataset)])

          return (
            <IterationPanel
              // chain.length (not just i) in the key: rolling back to an
              // earlier, previously-frozen iteration re-activates the same
              // index i as editable again, with a fresh, empty pendingSteps
              // - without chain.length here, React would reuse the same
              // component instance across that transition and its stale
              // preview/search state (from when it was still frozen) would
              // leak into the "editable again" render instead of resetting.
              key={`${rootDataset}::${i}::${chain.length}`}
              label={`Iteration ${i + 1}${i === 0 ? ` — starting from ${rootDataset}` : ''}`}
              rootDataset={rootDataset}
              priorSteps={priorSteps}
              availableColumns={availableColumns}
              excludeDatasets={excludeDatasets}
              getPkInfo={getPkInfo}
              isLast={isLast}
              frozenPreview={isLast ? null : chain[i].preview}
              onComposed={(steps, preview) => handleComposed(i, steps, preview)}
            />
          )
        })}
    </div>
  )
}
