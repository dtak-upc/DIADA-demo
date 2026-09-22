import { useEffect, useState } from 'react'
import { api } from './api/client'
import DatasetList from './components/DatasetList'
import SchemaPanel from './components/SchemaPanel'
import DataTable from './components/DataTable'
import ProjectSetup from './components/ProjectSetup'
import ProjectPanel from './components/ProjectPanel'
import CatalogGraphTabs from './components/CatalogGraphTabs'
import ColumnProfile from './components/ColumnProfile'
import JoinDiscovery from './components/join/JoinDiscovery'
import GoldLayer from './components/GoldLayer'
import ProcessingProgress, { formatSeconds } from './components/ProcessingProgress'
import './App.css'

const PAGE_SIZE = 25

export default function App() {
  // undefined = still checking on mount, null = no active project, string = active project id
  const [activeProjectId, setActiveProjectId] = useState(undefined)
  const [projectsList, setProjectsList] = useState([])
  const [samplePath, setSamplePath] = useState(null)
  const [projectActionLoading, setProjectActionLoading] = useState(false)
  const [projectActionError, setProjectActionError] = useState(null)
  const [showProjectPanel, setShowProjectPanel] = useState(false)

  const [processingState, setProcessingState] = useState(null)
  const [lastProcessingSummary, setLastProcessingSummary] = useState(null)

  const [view, setView] = useState('browse') // 'browse' | 'graph' | 'joins'

  const [datasets, setDatasets] = useState([])
  const [datasetsLoading, setDatasetsLoading] = useState(false)
  const [error, setError] = useState(null)

  const [selected, setSelected] = useState(null)
  const [profile, setProfile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [rowsLoading, setRowsLoading] = useState(false)
  const [page, setPage] = useState(0)

  const [columnProfiles, setColumnProfiles] = useState(null)
  const [columnProfilesLoading, setColumnProfilesLoading] = useState(false)
  const [columnProfilesError, setColumnProfilesError] = useState(null)

  const activeProject = projectsList.find((p) => p.id === activeProjectId) || null
  const projectPanelVisible = showProjectPanel || (activeProjectId === null && projectsList.length > 0)

  useEffect(() => {
    api
      .listProjects()
      .then((data) => {
        setProjectsList(data.projects)
        setSamplePath(data.sample_path)
        if (!data.active_project_id) {
          setActiveProjectId(null)
          return
        }
        // Resolve processing state *before* activeProjectId so both land in
        // the same React batch - otherwise the dataset-fetch effect below
        // could fire once for a stale "not processing" state before we've
        // learned a backend-restart warm-up is already under way.
        return api.getProcessingProgress().then((p) => {
          applyProcessingState(p)
          setActiveProjectId(data.active_project_id)
        })
      })
      .catch((e) => {
        setActiveProjectId(null)
        setError(e.message)
      })
  }, [])

  useEffect(() => {
    if (!activeProjectId || processingState?.running) return
    setDatasetsLoading(true)
    api
      .listDatasets()
      .then((data) => {
        setDatasets(data.datasets)
        setSelected(data.datasets.length > 0 ? data.datasets[0].name : null)
        setError(null)
      })
      .catch((e) => setError(e.message))
      .finally(() => setDatasetsLoading(false))
  }, [activeProjectId, processingState?.running])

  useEffect(() => {
    if (!processingState?.running) return
    const id = setInterval(() => {
      api
        .getProcessingProgress()
        .then(applyProcessingState)
        .catch((e) => setError(e.message))
    }, 300)
    return () => clearInterval(id)
  }, [processingState?.running])

  function applyProcessingState(p) {
    setProcessingState(p)
    if (p.error) setError(p.error)
    if (!p.running) {
      setLastProcessingSummary({ total: p.total, completed: p.completed, elapsedSeconds: p.elapsed_seconds })
    }
  }

  useEffect(() => {
    if (!selected) return
    setPage(0)
    api
      .getProfile(selected)
      .then((p) => {
        setProfile(p)
        setError(null)
      })
      .catch((e) => setError(e.message))
  }, [selected])

  useEffect(() => {
    if (!selected) return
    setColumnProfiles(null)
    setColumnProfilesLoading(true)
    setColumnProfilesError(null)
    api
      .getColumnProfiles(selected)
      .then((data) => setColumnProfiles(data.columns))
      .catch((e) => setColumnProfilesError(e.message))
      .finally(() => setColumnProfilesLoading(false))
  }, [selected])

  useEffect(() => {
    if (!selected) return
    setRowsLoading(true)
    api
      .getPreview(selected, { limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then((p) => {
        setPreview(p)
        setError(null)
      })
      .catch((e) => setError(e.message))
      .finally(() => setRowsLoading(false))
  }, [selected, page])

  function resetDatasetView() {
    setSelected(null)
    setProfile(null)
    setPreview(null)
    setColumnProfiles(null)
    setDatasets([])
    setView('browse')
    setLastProcessingSummary(null)
  }

  function refreshProjects() {
    return api.listProjects().then((data) => {
      setProjectsList(data.projects)
      setSamplePath(data.sample_path)
      return data
    })
  }

  function handleCreateProject(name, folder) {
    setProjectActionLoading(true)
    setProjectActionError(null)
    api
      .createProject(name, folder)
      .then((project) => {
        resetDatasetView()
        setShowProjectPanel(false)
        return refreshProjects()
          .then(() => api.getProcessingProgress())
          .then((p) => {
            applyProcessingState(p)
            setActiveProjectId(project.id)
          })
      })
      .catch((e) => setProjectActionError(e.message))
      .finally(() => setProjectActionLoading(false))
  }

  function handleActivateProject(id) {
    setProjectActionLoading(true)
    setProjectActionError(null)
    api
      .activateProject(id)
      .then(() => {
        resetDatasetView()
        setShowProjectPanel(false)
        return api.getProcessingProgress().then((p) => {
          applyProcessingState(p)
          setActiveProjectId(id)
        })
      })
      .catch((e) => setProjectActionError(e.message))
      .finally(() => setProjectActionLoading(false))
  }

  function handleDeleteProject(id) {
    setProjectActionLoading(true)
    setProjectActionError(null)
    api
      .deleteProject(id)
      .then(() => refreshProjects())
      .then(() => {
        if (id === activeProjectId) {
          resetDatasetView()
          setProcessingState(null)
          setActiveProjectId(null)
        }
      })
      .catch((e) => setProjectActionError(e.message))
      .finally(() => setProjectActionLoading(false))
  }

  function handleClearProjectData(id) {
    setProjectActionLoading(true)
    setProjectActionError(null)
    api
      .clearProjectData(id)
      .then(() => {
        if (id === activeProjectId) {
          resetDatasetView()
          return api.getProcessingProgress().then(applyProcessingState)
        }
      })
      .catch((e) => setProjectActionError(e.message))
      .finally(() => setProjectActionLoading(false))
  }

  const pageCount = preview ? Math.max(1, Math.ceil(preview.total_rows / PAGE_SIZE)) : 1

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Demo for DIADA</h1>
        </div>
        {activeProjectId !== undefined && (
          <div className="source-status">
            {activeProject && (
              <>
                <span className="muted" title={activeProject.folder}>
                  {activeProject.name}
                </span>
                {lastProcessingSummary && !processingState?.running && (
                  <span className="muted processing-summary">
                    Loaded {lastProcessingSummary.total} dataset
                    {lastProcessingSummary.total === 1 ? '' : 's'} in{' '}
                    {formatSeconds(lastProcessingSummary.elapsedSeconds)}
                  </span>
                )}
              </>
            )}
            {projectsList.length > 0 && (
              <button className="secondary" onClick={() => setShowProjectPanel((v) => !v)}>
                Projects
              </button>
            )}
          </div>
        )}
      </header>

      {error && <div className="error-banner">{error}</div>}

      {activeProjectId !== undefined && projectPanelVisible && (
        <ProjectPanel
          projects={projectsList}
          activeProjectId={activeProjectId}
          onActivate={handleActivateProject}
          onDelete={handleDeleteProject}
          onClearData={handleClearProjectData}
          onCreate={handleCreateProject}
          actionLoading={projectActionLoading}
          actionError={projectActionError}
          samplePath={samplePath}
          onClose={
            activeProject
              ? () => {
                  setShowProjectPanel(false)
                  setProjectActionError(null)
                }
              : null
          }
        />
      )}

      {activeProjectId !== undefined && !projectPanelVisible && activeProjectId === null && (
        <ProjectSetup
          onSubmit={handleCreateProject}
          loading={projectActionLoading}
          error={projectActionError}
          samplePath={samplePath}
        />
      )}

      {activeProjectId !== undefined && !projectPanelVisible && activeProject && processingState?.running && (
        <ProcessingProgress progress={processingState} />
      )}

      {activeProjectId !== undefined && !projectPanelVisible && activeProject && !processingState?.running && (
        <>
          <nav className="view-tabs">
            <button
              className={view === 'browse' ? 'tab active' : 'tab'}
              onClick={() => setView('browse')}
            >
              Datasets
            </button>
            <button
              className={view === 'graph' ? 'tab active' : 'tab'}
              onClick={() => setView('graph')}
            >
              Catalog graph
            </button>
            <button
              className={view === 'joins' ? 'tab active' : 'tab'}
              onClick={() => setView('joins')}
            >
              Manual composition
            </button>
            <button
              className={view === 'gold' ? 'tab active' : 'tab'}
              onClick={() => setView('gold')}
            >
              Automatic composition
            </button>
          </nav>

          {view === 'browse' && (
            <div className="layout">
              <DatasetList
                datasets={datasets}
                selected={selected}
                onSelect={setSelected}
                loading={datasetsLoading}
              />

              <main className="content">
                {selected && (
                  <>
                    <div className="content-header">
                      <h2>{selected}</h2>
                    </div>

                    {profile && profile.flags.length > 0 && (
                      <div className="dataset-flags">
                        {profile.flags.map((f) => (
                          <span key={f.type} className="badge badge-warn" title={f.message}>
                            {f.type.replace(/_/g, ' ')}
                          </span>
                        ))}
                      </div>
                    )}

                    {profile && (
                      <>
                        <h3>Schema &amp; data quality</h3>
                        <SchemaPanel profile={profile} />
                      </>
                    )}

                    <h3>Column profile</h3>
                    <ColumnProfile
                      profiles={columnProfiles}
                      loading={columnProfilesLoading}
                      error={columnProfilesError}
                    />

                    <h3>Preview</h3>
                    <DataTable
                      preview={preview}
                      loading={rowsLoading}
                      page={page}
                      pageCount={pageCount}
                      onPrev={() => setPage((p) => Math.max(0, p - 1))}
                      onNext={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                    />
                  </>
                )}
                {!datasetsLoading && datasets.length === 0 && (
                  <p className="muted">No CSV files found in this project's folder anymore.</p>
                )}
              </main>
            </div>
          )}

          {view === 'graph' && <CatalogGraphTabs />}

          {/* Kept mounted (not conditionally rendered) so the last discovery
              search / built join / curation result survives switching to
              another tab and back - only its visibility toggles. Keyed on
              the active project so switching projects still remounts it
              fresh instead of showing another project's stale state (this
              block doesn't reliably unmount on its own for a project switch
              - warm-up can finish before the next poll even observes
              processingState.running go true). */}
          <div hidden={view !== 'joins'} key={activeProjectId}>
            <JoinDiscovery />
          </div>

          {view === 'gold' && <GoldLayer />}
        </>
      )}
    </div>
  )
}
