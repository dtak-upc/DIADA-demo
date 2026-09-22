export default function SchemaPanel({ profile }) {
  if (!profile) return null
  return (
    <div className="schema-panel">
      <div className="table-wrap">
        <table className="schema-table">
          <thead>
            <tr>
              <th>Column</th>
              <th>Type</th>
              <th>Semantic type</th>
              <th>Nulls</th>
              <th>Unique</th>
              <th>Sample values</th>
              <th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {profile.columns.map((c) => (
              <tr key={c.name}>
                <td className="col-name">{c.name}</td>
                <td>
                  <span className="type-pill">{c.dtype}</span>
                </td>
                <td>
                  <span
                    className={`type-pill type-pill-${c.main_type}`}
                    title={`Main type: ${c.main_type}`}
                  >
                    {c.secondary_type.replace(/_/g, ' ')}
                  </span>
                </td>
                <td className={c.null_count > 0 ? 'warn' : ''}>
                  {c.null_count} ({c.null_pct}%)
                </td>
                <td>{c.unique_count}</td>
                <td className="samples">{c.sample_values.join(', ')}</td>
                <td>
                  {c.flags.map((f) => (
                    <span key={f.type} className="badge badge-warn" title={f.message}>
                      {f.type.replace(/_/g, ' ')}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
