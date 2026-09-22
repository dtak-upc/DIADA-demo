import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { forceCollide, forceX, forceY } from 'd3-force'

const GROUP_ANCHOR_RADIUS = 260
const ANCHOR_FORCE_STRENGTH = 0.1
const HIGHLIGHT_COLOR = '#f59e0b'
const MAX_SUGGESTIONS = 20

// Every node's group id, from `groupOf` (supplied by the caller - e.g.
// "which dataset does this belong to"), plus a couple of standing rules
// every one of this app's graph views happens to share: a `has_flag` edge
// always means "same group as whatever it came from" (a quality-flag node
// has no group field of its own), so it's inherited through that edge
// regardless of which view is showing it.
function groupOwnership(nodes, edges, groupOf) {
  const owner = {}
  for (const n of nodes) {
    const g = groupOf(n)
    if (g) owner[n.id] = g
  }
  for (const e of edges) {
    if (e.relation === 'has_flag' && owner[e.source]) owner[e.target] = owner[e.source]
  }
  return owner
}

// Beyond maxGroups, rendering every group makes the graph an unreadable
// hairball - instead sample an evenly-spread subset (alphabetically, every
// Nth one) as a representative example. A selected search result overrides
// this: its own group is always force-included, and so is any node
// directly connected to it (even one whose own group didn't make the cut)
// - "search, pick it, and it appears alongside what it's connected to"
// shouldn't depend on the sample size.
function sampleByGroup(nodes, edges, groupOf, maxGroups, selectedId) {
  const owner = groupOwnership(nodes, edges, groupOf)
  const groupIds = [...new Set(Object.values(owner))]

  const matchedIds = new Set(selectedId ? [selectedId] : [])
  const matchedGroupIds = new Set([...matchedIds].map((id) => owner[id]).filter(Boolean))

  let chosenGroupIds
  let truncated = false
  if (groupIds.length <= maxGroups) {
    chosenGroupIds = new Set(groupIds)
  } else {
    truncated = true
    const sorted = [...groupIds].sort()
    const step = sorted.length / maxGroups
    chosenGroupIds = new Set()
    for (let i = 0; i < maxGroups; i++) chosenGroupIds.add(sorted[Math.floor(i * step)])
    for (const g of matchedGroupIds) chosenGroupIds.add(g)
  }

  const keepIds = new Set(nodes.filter((n) => chosenGroupIds.has(owner[n.id])).map((n) => n.id))
  for (const id of matchedIds) keepIds.add(id)
  for (const e of edges) {
    if (matchedIds.has(e.source)) keepIds.add(e.target)
    if (matchedIds.has(e.target)) keepIds.add(e.source)
  }

  const outNodes = nodes.filter((n) => keepIds.has(n.id))
  const outEdges = edges.filter((e) => keepIds.has(e.source) && keepIds.has(e.target))
  return {
    nodes: outNodes,
    edges: outEdges,
    truncated,
    shown: chosenGroupIds.size,
    total: groupIds.length,
    matchedIds,
    searched: matchedIds.size > 0,
  }
}

function groupAnchors(groupIds) {
  const anchors = {}
  groupIds.forEach((id, i) => {
    const angle = (i / groupIds.length) * 2 * Math.PI
    anchors[id] = {
      x: groupIds.length > 1 ? Math.cos(angle) * GROUP_ANCHOR_RADIUS : 0,
      y: groupIds.length > 1 ? Math.sin(angle) * GROUP_ANCHOR_RADIUS : 0,
    }
  })
  return anchors
}

function drawNode(node, ctx, globalScale, config) {
  const r = config.nodeRadius(node)
  ctx.beginPath()
  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI)
  ctx.fillStyle = node.color
  ctx.fill()
  ctx.lineWidth = node.matched ? 3 : node.kind === 'dataset' ? 2 : 1.5
  ctx.strokeStyle = node.matched ? HIGHLIGHT_COLOR : '#ffffff'
  ctx.stroke()

  const fontSize = Math.max(node.kind === 'dataset' ? 11 : 9, (node.kind === 'dataset' ? 13 : 10) / globalScale)
  ctx.font = `${node.kind === 'dataset' ? '600' : '400'} ${fontSize}px "Segoe UI", sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  ctx.fillStyle = node.matched ? HIGHLIGHT_COLOR : '#1f2430'
  ctx.fillText(node.label, node.x, node.y + r + 2)
}

function paintNodePointerArea(node, color, ctx, config) {
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(node.x, node.y, config.nodeRadius(node) + 2, 0, 2 * Math.PI)
  ctx.fill()
}

// Autocomplete combobox over this view's own dataset/column nodes - typing
// filters a dropdown of real names (never a free-text graph filter on its
// own); only an actual pick (click, or Enter on the highlighted option)
// changes what's shown. `onSelect(id | null)` fires with the chosen node's
// id, or null when the selection is cleared.
function SearchCombobox({ options, placeholder, onSelect }) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const boxRef = useRef(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return []
    return options.filter((o) => o.label.toLowerCase().includes(q)).slice(0, MAX_SUGGESTIONS)
  }, [options, query])

  useEffect(() => setActiveIndex(0), [filtered])

  function pick(option) {
    setQuery(option.label)
    setOpen(false)
    onSelect(option.id)
  }

  function clear() {
    setQuery('')
    setOpen(false)
    onSelect(null)
  }

  function handleKeyDown(e) {
    if (!open || filtered.length === 0) {
      if (e.key === 'Escape') clear()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      pick(filtered[activeIndex])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="graph-search-combobox" ref={boxRef}>
      <input
        type="text"
        className="graph-canvas-search"
        placeholder={placeholder}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value)
          setOpen(true)
          onSelect(null)
        }}
        onFocus={() => query && setOpen(true)}
        onKeyDown={handleKeyDown}
      />
      {query && (
        <button type="button" className="graph-search-clear" onClick={clear} aria-label="Clear search">
          &times;
        </button>
      )}
      {open && filtered.length > 0 && (
        <ul className="graph-search-suggestions">
          {filtered.map((o, i) => (
            <li
              key={o.id}
              className={i === activeIndex ? 'active' : ''}
              // onMouseDown (not onClick) fires before the input's onBlur
              // would otherwise close this list first and swallow the pick.
              onMouseDown={(e) => {
                e.preventDefault()
                pick(o)
              }}
              onMouseEnter={() => setActiveIndex(i)}
            >
              <span className={`graph-search-kind graph-search-kind-${o.kind}`}>
                {o.kind === 'dataset' ? 'dataset' : 'column'}
              </span>
              {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Generic force-directed (react-force-graph-2d, canvas) graph view shared
// by every catalog-graph subtab (see CatalogGraphTabs.jsx): each caller
// hands in its own already-filtered nodes/edges plus a `config` describing
// how to color/size/label/group them, and this component owns everything
// else - layout, group-anchoring, the "N of M groups shown" truncation
// control, dataset/column search-and-highlight, and the actual canvas
// rendering.
//
// `config` must be a REFERENTIALLY STABLE object (define it at module
// scope, not inline in a render) - react-force-graph-2d owns a canvas +
// its own imperative animation/simulation loop, and a *new* prop reference
// on a render that doesn't actually change the graph - even one for
// something as unrelated as a callback function - can disrupt its
// internal interaction state (confirmed the hard way: that's what was
// previously causing this graph to render fine and then lock up after a
// few seconds whenever something elsewhere in the app re-rendered this
// component's parent). Wrapped in React.memo for the same reason.
function GraphCanvas({ nodes, edges, config, loading }) {
  const fgRef = useRef()
  // State, not a plain useRef: the wrapper div below doesn't exist yet on
  // the first render (still "Loading..." until data arrives) - a useRef +
  // useEffect(..., []) would run its one-and-only time against that
  // div-less first render, see a null ref, and never retry once the node
  // actually mounts. A callback ref stored in state makes the effect below
  // correctly re-run exactly when the node attaches, whenever that is.
  const [containerNode, setContainerNode] = useState(null)
  const [width, setWidth] = useState(900)
  const [maxGroups, setMaxGroups] = useState(config.defaultMaxGroups)
  const [selectedId, setSelectedId] = useState(null)

  useEffect(() => {
    if (!containerNode) return
    setWidth((prev) => {
      const w = containerNode.getBoundingClientRect().width
      return w && Math.abs(w - prev) > 2 ? w : prev
    })
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width
      setWidth((prev) => (w && Math.abs(w - prev) > 2 ? w : prev))
    })
    observer.observe(containerNode)
    return () => observer.disconnect()
  }, [containerNode])

  const searchOptions = useMemo(() => {
    return nodes
      .filter((n) => n.kind === 'dataset' || n.kind === 'column')
      .map((n) => ({
        id: n.id,
        kind: n.kind,
        label: n.kind === 'column' ? `${n.dataset}.${n.name}` : n.name,
      }))
      .sort((a, b) => a.label.localeCompare(b.label))
  }, [nodes])

  const { renderNodes, renderLinks, anchors, owner, sample } = useMemo(() => {
    if (!nodes || nodes.length === 0) {
      return { renderNodes: [], renderLinks: [], anchors: {}, owner: {}, sample: null }
    }
    const sample = sampleByGroup(nodes, edges, config.groupOf, maxGroups, selectedId)
    const owner = groupOwnership(sample.nodes, sample.edges, config.groupOf)
    const anchors = groupAnchors([...new Set(Object.values(owner))])

    const renderNodes = sample.nodes.map((n) => ({
      ...n,
      color: config.nodeColor(n),
      label: config.nodeLabelText(n),
      matched: sample.matchedIds.has(n.id),
    }))
    const renderLinks = sample.edges.map((e) => ({ ...e }))
    return { renderNodes, renderLinks, anchors, owner, sample }
  }, [nodes, edges, config, maxGroups, selectedId])

  const graphData = useMemo(() => ({ nodes: renderNodes, links: renderLinks }), [renderNodes, renderLinks])

  const handleNodeCanvasObject = useCallback((node, ctx, scale) => drawNode(node, ctx, scale, config), [config])
  const handleNodePointerArea = useCallback((node, color, ctx) => paintNodePointerArea(node, color, ctx, config), [config])
  const handleLinkColor = useCallback((link) => config.edgeColor(link), [config])
  const handleLinkWidth = useCallback((link) => config.edgeWidth(link), [config])
  const handleLinkLabel = useCallback((link) => config.edgeTooltip(link), [config])
  const handleNodeLabel = useCallback((node) => config.nodeTooltip(node), [config])

  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('x', forceX((n) => anchors[owner[n.id]]?.x ?? 0).strength(ANCHOR_FORCE_STRENGTH))
    fg.d3Force('y', forceY((n) => anchors[owner[n.id]]?.y ?? 0).strength(ANCHOR_FORCE_STRENGTH))
    fg.d3Force('collide', forceCollide((n) => config.nodeRadius(n) + 10))
    fg.d3ReheatSimulation()

    // One fixed-delay auto-frame, decoupled from the simulation's cooldown/
    // stop event, so the view always ends up framed even if the sim
    // settles before or after that event would have fired.
    const timer = setTimeout(() => fg.zoomToFit(400, 40), 700)
    return () => clearTimeout(timer)
  }, [anchors, owner, renderNodes, config])

  if (loading) return <p className="muted">Loading...</p>
  if (!nodes || nodes.length === 0) {
    return <p className="muted">{config.emptyMessage}</p>
  }

  return (
    <div>
      <div className="graph-canvas-controls">
        <SearchCombobox options={searchOptions} placeholder={config.searchPlaceholder} onSelect={setSelectedId} />
        {sample.total > 1 && (
          <label className="catalog-graph-max-datasets">
            {config.groupLabel} shown
            <input
              type="number"
              min={1}
              max={sample.total}
              value={maxGroups}
              onChange={(e) => {
                const v = Number(e.target.value)
                setMaxGroups(Math.max(1, Math.min(sample.total, Number.isNaN(v) ? 1 : v)))
              }}
            />
            <span className="muted">of {sample.total}</span>
          </label>
        )}
      </div>
      {sample.truncated && (
        <p className="catalog-graph-sample-note">
          Showing an example subset - {sample.shown} of {sample.total} {config.groupLabel.toLowerCase()}.
          {sample.searched ? ' The selected search result is always included, along with whatever it connects to.' : ''}
        </p>
      )}
      <div className="catalog-graph-wrap" ref={setContainerNode}>
        <ForceGraph2D
          ref={fgRef}
          width={width}
          height={640}
          graphData={graphData}
          cooldownTicks={100}
          cooldownTime={4000}
          linkWidth={handleLinkWidth}
          linkColor={handleLinkColor}
          linkDirectionalArrowLength={4}
          linkDirectionalArrowRelPos={1}
          linkLabel={handleLinkLabel}
          nodeLabel={handleNodeLabel}
          nodeCanvasObject={handleNodeCanvasObject}
          nodePointerAreaPaint={handleNodePointerArea}
        />
      </div>
    </div>
  )
}

export default memo(GraphCanvas)
