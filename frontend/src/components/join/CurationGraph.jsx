import { memo, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { forceCollide, forceX, forceY } from 'd3-force'

const NODE_RADIUS = 9
const CLUSTER_ANCHOR_RADIUS = 100 // how far apart cluster "centers of gravity" are placed
const ANCHOR_FORCE_STRENGTH = 0.12 // pulls a cluster's own nodes toward its anchor, without rigidly fixing them

const CLUSTER_COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ec4899', '#06b6d4', '#a855f7', '#ef4444', '#14b8a6']

function clusterColor(clusterName, order) {
  if (clusterName === 'unclustered') return '#9ca3af'
  const idx = order.indexOf(clusterName)
  return CLUSTER_COLORS[idx % CLUSTER_COLORS.length]
}

function clusterAnchors(clusterOrder) {
  // One anchor point per cluster, spread evenly around a circle - real
  // separation in space rather than a fixed row/column grid. Clusters with
  // actual edges pull their own nodes into a tight little constellation
  // around their anchor; unrelated clusters' anchors sit far apart, so
  // "unclustered" (mutually disconnected) nodes drift toward their own
  // region instead of scattering randomly across the whole canvas.
  const anchors = {}
  clusterOrder.forEach((name, i) => {
    const angle = (i / clusterOrder.length) * 2 * Math.PI
    anchors[name] = {
      x: clusterOrder.length > 1 ? Math.cos(angle) * CLUSTER_ANCHOR_RADIUS : 0,
      y: clusterOrder.length > 1 ? Math.sin(angle) * CLUSTER_ANCHOR_RADIUS : 0,
    }
  })
  return anchors
}

function linkWidth(link) {
  return Math.min(4, 1 + link.soundness / 20)
}

function linkColor() {
  return 'rgba(99, 102, 241, 0.35)'
}

function linkLabel(link) {
  return `soundness ${link.soundness.toFixed(2)}`
}

function nodeLabel(node) {
  return node.id
}

function drawNode(node, ctx, globalScale) {
  ctx.beginPath()
  ctx.arc(node.x, node.y, NODE_RADIUS, 0, 2 * Math.PI)
  ctx.fillStyle = node.color
  ctx.fill()
  ctx.lineWidth = 1.5
  ctx.strokeStyle = '#ffffff'
  ctx.stroke()

  const fontSize = Math.max(10, 12 / globalScale)
  ctx.font = `${fontSize}px "Segoe UI", sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  ctx.fillStyle = '#1f2430'
  ctx.fillText(node.id, node.x, node.y + NODE_RADIUS + 3)
}

function paintNodePointerArea(node, color, ctx) {
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(node.x, node.y, NODE_RADIUS + 2, 0, 2 * Math.PI)
  ctx.fill()
}

// Force-directed graph (react-force-graph-2d, canvas-rendered) rather than
// a fixed row/column grid: nodes are free to settle wherever the physics
// puts them, edges are drawn straight from whatever point the node ends up
// at (no fixed handles like a flowchart library), and clusters naturally
// separate in space because disconnected nodes repel each other while
// connected ones pull together.
//
// Wrapped in React.memo, and every prop handed to ForceGraph2D is either a
// stable module-level function or memoized (graphData below) - see the
// longer explanation in CatalogGraph.jsx, where a new prop reference on an
// unrelated re-render (even just a callback function) turned out to be
// what made the graph lock up after a few seconds.
function CurationGraph({ graph, clusterOrder }) {
  const fgRef = useRef()
  const containerRef = useRef()
  const [width, setWidth] = useState(900)

  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width
      // Guard against a resize feedback loop: without this, a fractional
      // width change caused by the canvas itself repainting could trigger
      // another resize, another re-render, another repaint, forever.
      setWidth((prev) => (w && Math.abs(w - prev) > 2 ? w : prev))
    })
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [])

  const { nodes, links, anchors } = useMemo(() => {
    if (!graph) return { nodes: [], links: [], anchors: {} }
    const anchors = clusterAnchors(clusterOrder)
    const nodes = graph.nodes.map((n) => ({
      id: n.id,
      cluster: n.cluster,
      color: clusterColor(n.cluster, clusterOrder),
    }))
    const links = graph.edges.map((e) => ({ source: e.source, target: e.target, soundness: e.soundness }))
    return { nodes, links, anchors }
  }, [graph, clusterOrder])

  const graphData = useMemo(() => ({ nodes, links }), [nodes, links])

  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('x', forceX((n) => anchors[n.cluster]?.x ?? 0).strength(ANCHOR_FORCE_STRENGTH))
    fg.d3Force('y', forceY((n) => anchors[n.cluster]?.y ?? 0).strength(ANCHOR_FORCE_STRENGTH))
    fg.d3Force('collide', forceCollide(NODE_RADIUS + 14))
    fg.d3ReheatSimulation()

    // One fixed-delay auto-frame, decoupled from the simulation's cooldown/
    // stop event - see the module docstring above / CatalogGraph.jsx for why.
    const timer = setTimeout(() => fg.zoomToFit(400, 50), 700)
    return () => clearTimeout(timer)
  }, [anchors, nodes])

  if (!graph || graph.nodes.length === 0) return null

  return (
    <div>
      <div className="curation-graph-legend">
        {clusterOrder.map((name) => (
          <span key={name} className="curation-graph-legend-item">
            <span className="type-dot" style={{ background: clusterColor(name, clusterOrder) }} />
            {name === 'unclustered' ? 'Unclustered' : name.replace('_', ' ')}
          </span>
        ))}
      </div>
      <div className="curation-graph-wrap" ref={containerRef}>
        <ForceGraph2D
          ref={fgRef}
          width={width}
          height={560}
          graphData={graphData}
          cooldownTicks={100}
          cooldownTime={4000}
          linkWidth={linkWidth}
          linkColor={linkColor}
          linkLabel={linkLabel}
          nodeLabel={nodeLabel}
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={paintNodePointerArea}
        />
      </div>
    </div>
  )
}

export default memo(CurationGraph)
