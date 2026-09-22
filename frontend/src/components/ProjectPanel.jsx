import { useState } from 'react'
import ProjectSetup from './ProjectSetup'

export default function ProjectPanel({
  projects,
  activeProjectId,
  onActivate,
  onDelete,
  onClearData,
  onCreate,
  actionLoading,
  actionError,
  samplePath,
  onClose,
}) {
  const [creating, setCreating] = useState(false)

  return (
    <div className="project-panel">
      <div className="project-panel-header">
        <h3>Projects</h3>
        {onClose && (
          <button className="secondary" onClick={onClose}>
            Close
          </button>
        )}
      </div>

      {projects.length === 0 && <p className="muted">No projects yet.</p>}

      <ul className="project-list">
        {projects.map((p) => (
          <li key={p.id} className={p.id === activeProjectId ? 'project-row active' : 'project-row'}>
            <div className="project-row-info">
              <span className="project-row-name">{p.name}</span>
              <span className="muted project-row-folder" title={p.folder}>
                {p.folder}
              </span>
            </div>
            <div className="project-row-actions">
              {p.id === activeProjectId ? (
                <span className="badge badge-active">active</span>
              ) : (
                <button className="secondary" disabled={actionLoading} onClick={() => onActivate(p.id)}>
                  Activate
                </button>
              )}
              <button
                className="secondary"
                disabled={actionLoading}
                onClick={() => {
                  if (window.confirm(`Clear all computed data for "${p.name}"? It will be recomputed from the source folder.`)) {
                    onClearData(p.id)
                  }
                }}
              >
                Clear data
              </button>
              <button
                className="secondary danger"
                disabled={actionLoading}
                onClick={() => {
                  if (window.confirm(`Delete project "${p.name}"? This removes it and all its computed data. The source folder itself is untouched.`)) {
                    onDelete(p.id)
                  }
                }}
              >
                Delete
              </button>
            </div>
          </li>
        ))}
      </ul>

      {!creating && (
        <button className="secondary" onClick={() => setCreating(true)}>
          + New project
        </button>
      )}
      {creating && (
        <ProjectSetup
          onSubmit={(name, folder) => onCreate(name, folder)}
          loading={actionLoading}
          error={actionError}
          samplePath={samplePath}
          onCancel={() => setCreating(false)}
        />
      )}
    </div>
  )
}
