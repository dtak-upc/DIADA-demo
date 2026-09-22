const BASE = '/api'

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const errorBody = await res.json().catch(() => ({}))
    throw new Error(errorBody.detail || `Request failed: ${res.status}`)
  }
  return res.json()
}

// For POST endpoints that stream back a CSV file rather than JSON.
async function downloadRequest(path, body, fallbackFilename) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const errorBody = await res.json().catch(() => ({}))
    throw new Error(errorBody.detail || `Request failed: ${res.status}`)
  }
  const disposition = res.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  return { blob: await res.blob(), filename: match ? match[1] : fallbackFilename }
}

export const api = {
  listDatasets: () => request('/datasets'),
  getProfile: (name) => request(`/datasets/${encodeURIComponent(name)}`),
  getPreview: (name, { limit = 50, offset = 0, raw = false } = {}) =>
    request(
      `/datasets/${encodeURIComponent(name)}/preview?limit=${limit}&offset=${offset}&raw=${raw}`
    ),
  getColumnProfiles: (name) => request(`/datasets/${encodeURIComponent(name)}/column-profiles`),
  getCatalog: () => request('/catalog'),
  getUsableCatalog: () => request('/catalog/usable'),
  getJoinDiscovery: (dataset, column) =>
    request(`/join-discovery?dataset=${encodeURIComponent(dataset)}&column=${encodeURIComponent(column)}`),
  joinPreview: (joinRequest, { limit = 50, offset = 0 } = {}) =>
    request(`/join/preview?limit=${limit}&offset=${offset}`, { method: 'POST', body: joinRequest }),
  joinDownload: (joinRequest) => downloadRequest('/join/download', joinRequest, 'join.csv'),

  curateJoin: (joinRequest) => request('/join/curate', { method: 'POST', body: joinRequest }),
  curateCf1Download: (joinRequest) =>
    downloadRequest('/join/curate/cf1/download', joinRequest, 'cf1_cleaned.csv'),
  curateCf2Download: (joinRequest, cluster) =>
    downloadRequest('/join/curate/cf2/download', { ...joinRequest, cluster }, `cf2_${cluster}.csv`),

  // mode is one of "every_table" | "ad_hoc" - the two gold-layer
  // algorithms build and persist completely independently of one another
  // (see backend/app/gold_layer.py's module docstring).
  runGoldLayer: (mode, config) => request('/gold-layer/run', { method: 'POST', body: { mode, ...config } }),
  getGoldLayerProgress: (mode) => request(`/gold-layer/progress?mode=${mode}`),
  getGoldLayer: (mode) => request(`/gold-layer?mode=${mode}`),
  goldTablePreview: (tableId, { limit = 50, offset = 0 } = {}) =>
    request(`/gold-layer/preview?limit=${limit}&offset=${offset}`, { method: 'POST', body: { table_id: tableId } }),
  goldTableDownload: (tableId) =>
    downloadRequest('/gold-layer/download', { table_id: tableId }, `${tableId.replace(/::/g, '_')}.csv`),

  listProjects: () => request('/projects'),
  createProject: (name, folder) => request('/projects', { method: 'POST', body: { name, folder } }),
  activateProject: (id) => request(`/projects/${encodeURIComponent(id)}/activate`, { method: 'POST' }),
  deleteProject: (id) => request(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  clearProjectData: (id) => request(`/projects/${encodeURIComponent(id)}/data`, { method: 'DELETE' }),
  getProcessingProgress: () => request('/projects/progress'),
}
