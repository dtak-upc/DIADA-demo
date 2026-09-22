export default function DataTable({ preview, loading, page, pageCount, onPrev, onNext, columnColors }) {
  if (loading) return <p className="muted">Loading rows...</p>
  if (!preview) return null

  return (
    <div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              {preview.columns.map((c) => (
                <th key={c} style={columnColors?.[c] ? { boxShadow: `inset 0 -3px 0 ${columnColors[c]}` } : undefined}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row, i) => (
              <tr key={i}>
                {preview.columns.map((c) => (
                  <td key={c}>{row[c] === null || row[c] === undefined ? (
                    <span className="null-cell">null</span>
                  ) : (
                    String(row[c])
                  )}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pagination">
        <button onClick={onPrev} disabled={page === 0}>
          &laquo; Prev
        </button>
        <span>
          Page {page + 1} of {pageCount} &middot; {preview.total_rows.toLocaleString()} total rows
        </span>
        <button onClick={onNext} disabled={page + 1 >= pageCount}>
          Next &raquo;
        </button>
      </div>
    </div>
  )
}
