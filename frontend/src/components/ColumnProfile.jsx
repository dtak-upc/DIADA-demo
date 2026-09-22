import { useEffect, useState } from 'react'

const METRIC_GROUPS = [
  {
    title: 'Cardinality',
    fields: [
      ['num_rows', 'Rows', 'int'],
      ['cardinality', 'Distinct values', 'int'],
      ['uniqueness', 'Uniqueness', 'pct'],
      ['incompleteness', 'Incompleteness', 'pct'],
      ['entropy', 'Entropy', 'float'],
    ],
  },
  {
    title: 'Distribution',
    fields: [
      ['frequency_avg', 'Freq. avg', 'float'],
      ['frequency_min', 'Freq. min', 'float'],
      ['frequency_max', 'Freq. max', 'float'],
      ['frequency_sd', 'Freq. std dev', 'float'],
      ['skewness', 'Skewness', 'float'],
      ['kurtosis', 'Kurtosis', 'float'],
      ['val_pct_min', 'Value % min', 'pct'],
      ['val_pct_max', 'Value % max', 'pct'],
      ['val_pct_avg', 'Value % avg', 'pct'],
    ],
  },
  {
    title: 'Coverage',
    fields: [
      ['top_1_coverage', 'Top 1 value', 'pct'],
      ['top_5_coverage', 'Top 5 values', 'pct'],
      ['top_10_coverage', 'Top 10 values', 'pct'],
    ],
  },
  {
    title: 'Boundary',
    fields: [
      ['first_word', 'Min / first', 'raw'],
      ['last_word', 'Max / last', 'raw'],
    ],
  },
  {
    title: 'Flags',
    fields: [
      ['is_empty', 'Empty column', 'bool'],
      ['is_binary', 'Binary (2 distinct values)', 'bool'],
    ],
  },
  {
    title: 'Length',
    stringOnly: true,
    fields: [
      ['len_avg_full', 'Avg length', 'float'],
      ['len_min_full', 'Min length', 'float'],
      ['len_max_full', 'Max length', 'float'],
      ['len_avg_word', 'Avg word length', 'float'],
      ['len_min_word', 'Min word length', 'float'],
      ['len_max_word', 'Max word length', 'float'],
    ],
  },
  {
    title: 'Word count',
    stringOnly: true,
    fields: [
      ['words_cnt_avg', 'Avg words', 'float'],
      ['words_cnt_min', 'Min words', 'int'],
      ['words_cnt_max', 'Max words', 'int'],
      ['words_cnt_sd', 'Std dev words', 'float'],
      ['number_words', 'Total words', 'int'],
    ],
  },
  {
    title: 'Numerical',
    numericOnly: true,
    fields: [
      ['percentage_negative', '% negative', 'pct'],
      ['number_zero', '% zero', 'pct'],
      ['outliers_below_iqr', 'Outliers below IQR', 'int'],
      ['outliers_above_iqr', 'Outliers above IQR', 'int'],
      ['percentage_outliers', '% outliers', 'pct'],
    ],
  },
]

function formatValue(value, kind) {
  if (value === null || value === undefined) return '—'
  if (kind === 'pct') return `${(value * 100).toFixed(2)}%`
  if (kind === 'int') return Number(value).toLocaleString()
  if (kind === 'float') return Number(value).toFixed(3)
  if (kind === 'bool') return value ? 'Yes' : 'No'
  return String(value)
}

export default function ColumnProfile({ profiles, loading, error }) {
  const [selectedColumn, setSelectedColumn] = useState(null)

  useEffect(() => {
    if (profiles && profiles.length > 0) setSelectedColumn(profiles[0].column)
  }, [profiles])

  if (loading) return <p className="muted">Computing column profiles...</p>
  if (error) return <div className="error-banner">{error}</div>
  if (!profiles || profiles.length === 0) return null

  const active = profiles.find((p) => p.column === selectedColumn) || profiles[0]
  const groups = METRIC_GROUPS.filter(
    (g) =>
      (!g.stringOnly || active.main_type === 'string') &&
      (!g.numericOnly || active.main_type === 'numeric')
  )

  return (
    <div className="column-profile">
      <div className="column-profile-picker">
        {profiles.map((p) => (
          <button
            key={p.column}
            className={p.column === active.column ? 'column-chip active' : 'column-chip'}
            onClick={() => setSelectedColumn(p.column)}
          >
            {p.column}
          </button>
        ))}
      </div>

      <div className="column-profile-subtitle">
        <span className={`type-pill type-pill-${active.main_type}`}>
          {active.secondary_type.replace(/_/g, ' ')}
        </span>
        <span className="muted">({active.main_type})</span>
      </div>

      <div className="column-profile-groups">
        {groups.map((group) => (
          <div key={group.title} className="metric-card">
            <h4>{group.title}</h4>
            <dl>
              {group.fields.map(([key, label, kind]) => (
                <div key={key} className="metric-row">
                  <dt>{label}</dt>
                  <dd>{formatValue(active[key], kind)}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}

        <div className="metric-card">
          <h4>Most common values</h4>
          {active.freq_word_containment && active.freq_word_containment.length > 0 ? (
            <table className="common-values-table">
              <thead>
                <tr>
                  <th>Value</th>
                  <th>Soundex</th>
                </tr>
              </thead>
              <tbody>
                {active.freq_word_containment.map((val, i) => (
                  <tr key={i}>
                    <td>{val === null ? <span className="null-cell">null</span> : String(val)}</td>
                    <td>{active.freq_word_soundex_containment?.[i] ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">No values.</p>
          )}
        </div>
      </div>
    </div>
  )
}
