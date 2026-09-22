export function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined) return ''
  return seconds < 10 ? `${seconds.toFixed(2)}s` : `${seconds.toFixed(1)}s`
}

export default function ProcessingProgress({ progress }) {
  if (!progress) return null

  const { total, completed, current_dataset: currentDataset, elapsed_seconds: elapsedSeconds } = progress
  const pct = total > 0 ? Math.round((completed / total) * 100) : 100

  return (
    <div className="processing-panel">
      <h2>Processing datasets&hellip;</h2>
      <div className="progress-bar-track">
        <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <p className="muted">
        {completed} of {total} datasets processed
        {currentDataset ? ` · profiling "${currentDataset}"` : ''}
        {elapsedSeconds !== null ? ` · ${formatSeconds(elapsedSeconds)} elapsed` : ''}
      </p>
    </div>
  )
}
