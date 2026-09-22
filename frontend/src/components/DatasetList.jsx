import { useMemo, useState } from 'react'

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function DatasetList({ datasets, selected, onSelect, loading }) {
  const [search, setSearch] = useState('')

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return datasets
    return datasets.filter((d) => d.name.toLowerCase().includes(q))
  }, [datasets, search])

  return (
    <aside className="sidebar">
      <h2>Datasets</h2>
      {loading && <p className="muted">Loading...</p>}
      {!loading && datasets.length === 0 && (
        <p className="muted">No CSV files found in /data.</p>
      )}
      {!loading && datasets.length > 0 && (
        <input
          type="text"
          className="source-input dataset-search"
          placeholder={`Search ${datasets.length} datasets...`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      )}
      <div className="dataset-list-scroll">
        {!loading && datasets.length > 0 && filtered.length === 0 && (
          <p className="muted">No datasets match "{search}".</p>
        )}
        <ul className="dataset-list">
          {filtered.map((d) => (
            <li key={d.name}>
              <button
                className={d.name === selected ? 'dataset-btn active' : 'dataset-btn'}
                onClick={() => onSelect(d.name)}
              >
                <span className="dataset-name">{d.name}</span>
                <span className="dataset-meta">
                  {d.row_count.toLocaleString()} rows &middot; {d.column_count} cols &middot;{' '}
                  {formatBytes(d.size_bytes)}
                </span>
                {d.duplicate_row_count > 0 && (
                  <span className="badge badge-warn">{d.duplicate_row_count} dup rows</span>
                )}
                {d.flags.map((f) => (
                  <span key={f.type} className="badge badge-warn" title={f.message}>
                    {f.type.replace(/_/g, ' ')}
                  </span>
                ))}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  )
}
