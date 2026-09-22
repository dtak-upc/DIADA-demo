import { useState } from 'react'

export default function ProjectSetup({ onSubmit, loading, error, samplePath, initialName, initialFolder, onCancel }) {
  const [name, setName] = useState(initialName || '')
  const [folder, setFolder] = useState(initialFolder || '')

  return (
    <div className="source-setup">
      <h2>{onCancel ? 'New project' : 'Create your first project'}</h2>
      <p className="muted">
        A project is a name bound to a folder of CSV files on the machine
        running the backend. Every *.csv file in it becomes a dataset, and
        everything computed about it is saved under this project so it
        loads instantly next time.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit(name, folder)
        }}
      >
        <div className="source-fields">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Project name"
            className="source-input"
            autoFocus
          />
          <input
            type="text"
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="C:\path\to\data or /path/to/data"
            className="source-input"
          />
        </div>
        <div className="source-actions">
          <button type="submit" disabled={loading || !name.trim() || !folder.trim()}>
            {loading ? 'Creating...' : 'Create project'}
          </button>
          {samplePath && (
            <button
              type="button"
              className="secondary"
              disabled={loading}
              onClick={() => {
                const sampleName = name.trim() || 'Sample Data'
                setName(sampleName)
                setFolder(samplePath)
                onSubmit(sampleName, samplePath)
              }}
            >
              Use bundled sample data
            </button>
          )}
          {onCancel && (
            <button type="button" className="secondary" disabled={loading} onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      </form>
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
