import { useCallback, useEffect, useRef, useState } from 'react'
import mermaid from 'mermaid'

let mermaidInitialized = false

function ensureMermaidInitialized() {
  if (!mermaidInitialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      htmlLabels: false,
      theme: 'default',
      flowchart: { htmlLabels: false },
    })
    mermaidInitialized = true
  }
}

export interface SelectedEdge {
  source: string
  target: string
  label: string
  occurrence: number
  evidenceIds: string[]
  connectionIds: string[]
}

interface ParsedEdge {
  source: string
  target: string
  label: string
  occurrence: number
  evidenceIds: string[]
  connectionIds: string[]
}

function parseCommentList(line: string, prefix: string, map: Map<string, string[][]>): boolean {
  if (!line.startsWith(prefix)) return false
  const rest = line.slice(prefix.length)
  const colonIdx = rest.indexOf(':')
  if (colonIdx === -1) return true
  const subject = rest.slice(0, colonIdx).trim()
  if (!subject.includes('->')) return true
  const ids = rest
    .slice(colonIdx + 1)
    .split(/[,\s]+/)
    .map(s => s.trim())
    .filter(Boolean)
  const existing = map.get(subject) ?? []
  existing.push(ids)
  map.set(subject, existing)
  return true
}

function parseDiagramEdges(mmdText: string): ParsedEdge[] {
  const edgeDeclarations: Array<{ source: string; target: string; label: string }> = []
  const evidenceByPair = new Map<string, string[][]>()
  const connectionsByPair = new Map<string, string[][]>()

  for (const rawLine of mmdText.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (parseCommentList(line, '%% evidence ', evidenceByPair)) continue
    if (parseCommentList(line, '%% connection ', connectionsByPair)) continue

    const edgeMatch = /^([A-Za-z0-9_.-]+)\s+(?:-->|-.->|==>)\|"?([^"|]*)"?\|\s+([A-Za-z0-9_.-]+)$/.exec(line)
    if (edgeMatch) {
      edgeDeclarations.push({
        source: edgeMatch[1],
        target: edgeMatch[3],
        label: edgeMatch[2],
      })
    }
  }

  const pairCounts = new Map<string, number>()
  return edgeDeclarations.map(edge => {
    const pairKey = `${edge.source}->${edge.target}`
    const occurrence = pairCounts.get(pairKey) ?? 0
    pairCounts.set(pairKey, occurrence + 1)
    const pairEvidenceLists = evidenceByPair.get(pairKey) ?? []
    const evidenceIds = pairEvidenceLists[occurrence] ?? []
    const pairConnectionLists = connectionsByPair.get(pairKey) ?? []
    const connectionIds = pairConnectionLists[occurrence] ?? []
    return { ...edge, occurrence, evidenceIds, connectionIds }
  })
}

interface Props {
  jobId: string
  artifactId: string
  fetchArtifactText: (jobId: string, artifactId: string) => Promise<string>
  onDiagramTextLoaded?: (text: string) => void
  onSelectNode?: (nodeKey: string) => void
  onSelectEdge?: (edge: SelectedEdge) => void
}

/**
 * Renders a Mermaid .mmd artifact with securityLevel:'strict' and htmlLabels:false,
 * interactive pan/zoom/fit, and clickable + keyboard-accessible node and edge selection.
 */
/** Set the SVG's viewBox, width and height to its drawn content plus a margin. Exported for tests. */
export function fitViewBoxToContent(svg: SVGSVGElement, margin = 8): void {
  const content = svg.querySelector(':scope > g') as SVGGraphicsElement | null
  if (!content || typeof content.getBBox !== 'function') return
  let box: DOMRect
  try {
    box = content.getBBox()
  } catch {
    return
  }
  if (!(box.width > 0 && box.height > 0)) return
  const x = box.x - margin
  const y = box.y - margin
  const width = box.width + 2 * margin
  const height = box.height + 2 * margin
  svg.setAttribute('viewBox', `${x} ${y} ${width} ${height}`)
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
}

export function DiagramViewer({
  jobId,
  artifactId,
  fetchArtifactText,
  onDiagramTextLoaded,
  onSelectNode,
  onSelectEdge,
}: Props) {
  const stageRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scale, setScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const draggingRef = useRef(false)
  const lastPosRef = useRef({ x: 0, y: 0 })

  const computeFit = useCallback((readableFloor = false) => {
    const stage = stageRef.current
    const container = containerRef.current
    const svg = container?.querySelector('svg')
    if (!stage || !svg) {
      setScale(1)
      setOffset({ x: 0, y: 0 })
      return
    }
    const vb = svg.viewBox?.baseVal
    const attrW = Number(svg.getAttribute('width'))
    const attrH = Number(svg.getAttribute('height'))
    const svgW = (vb && vb.width > 0 ? vb.width : Number.isFinite(attrW) && attrW > 0 ? attrW : svg.getBoundingClientRect().width / scale) || 1
    const svgH = (vb && vb.height > 0 ? vb.height : Number.isFinite(attrH) && attrH > 0 ? attrH : svg.getBoundingClientRect().height / scale) || 1
    const pad = 24
    const availW = Math.max(100, stage.clientWidth - pad)
    const availH = Math.max(100, stage.clientHeight - pad)
    const fitScale = Math.max(0.15, Math.min(2, Number(Math.min(availW / svgW, availH / svgH, 1).toFixed(2))))
    if (readableFloor && fitScale < 0.55) {
      // Too wide to read when fitted: start readable and centred on the drawing; Fit still shows all of it.
      setScale(0.6)
      setOffset({ x: Math.round((stage.clientWidth - svgW * 0.6) / 2), y: 16 })
      return
    }
    const dx = Math.max(12, Math.round((stage.clientWidth - svgW * fitScale) / 2))
    const dy = Math.max(12, Math.round((stage.clientHeight - svgH * fitScale) / 2))
    setScale(fitScale)
    setOffset({ x: dx, y: dy })
  }, [scale])

  const fitToStage = useCallback(() => {
    computeFit(false)
  }, [computeFit])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setScale(1)
    setOffset({ x: 0, y: 0 })

    async function loadAndRender() {
      try {
        const text = await fetchArtifactText(jobId, artifactId)
        if (cancelled) return
        onDiagramTextLoaded?.(text)
        const parsedEdges = parseDiagramEdges(text)

        ensureMermaidInitialized()
        const renderId = `repodocs-mermaid-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
        const { svg } = await mermaid.render(renderId, text)
        if (cancelled) return

        const el = containerRef.current
        if (el) {
          el.replaceChildren()
          const parser = new DOMParser()
          const doc = parser.parseFromString(svg, 'image/svg+xml')
          const svgEl = doc.documentElement
          if (svgEl && svgEl.nodeName.toLowerCase() === 'svg') {
            const imported = document.importNode(svgEl, true)
            // Normalize SVG dimensions to its viewBox coordinate space so CSS transform scaling never double-shrinks wide diagrams.
            const vbAttr = imported.getAttribute('viewBox')
            if (vbAttr) {
              const parts = vbAttr.trim().split(/[\s,]+/).map(Number)
              if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
                imported.setAttribute('width', String(parts[2]))
                imported.setAttribute('height', String(parts[3]))
              }
            }
            imported.style.maxWidth = 'none'
            imported.style.width = ''
            imported.style.height = ''
            // Strip any external url(...) references from embedded SVG style tags.
            imported.querySelectorAll('style').forEach(styleEl => {
              if (styleEl.textContent && /url\s*\(/i.test(styleEl.textContent)) {
                styleEl.textContent = styleEl.textContent.replace(/url\s*\([^)]*\)/gi, 'none')
              }
            })

            // Wire clickable + keyboard-accessible node selection
            imported.querySelectorAll('g.node').forEach(node => {
              const idAttr = node.getAttribute('id') || ''
              const match = /(?:^|-)((?:c|g)_[0-9a-f]{12,16})(?:-|$)/.exec(idAttr)
              const fallbackMatch = /^flowchart-(.+?)-\d+$/.exec(idAttr)
              const key = match ? match[1] : fallbackMatch ? fallbackMatch[1] : null
              if (key && onSelectNode) {
                ;(node as SVGElement).style.cursor = 'pointer'
                node.setAttribute('tabindex', '0')
                node.setAttribute('role', 'button')
                node.setAttribute('aria-label', `Select node ${key}`)
                node.addEventListener('click', e => {
                  e.stopPropagation()
                  onSelectNode(key)
                })
                node.addEventListener('keydown', e => {
                  if ((e as KeyboardEvent).key === 'Enter' || (e as KeyboardEvent).key === ' ') {
                    e.preventDefault()
                    onSelectNode(key)
                  }
                })
              }
            })

            // Wire clickable + keyboard-accessible edge selection (including parallel edges)
            if (onSelectEdge && parsedEdges.length > 0) {
              const edgePaths = Array.from(imported.querySelectorAll('g.edgePaths path'))
              const edgeLabels = Array.from(imported.querySelectorAll('g.edgeLabels g.edgeLabel'))

              const bindEdgeElement = (targetEl: Element, edge: ParsedEdge) => {
                ;(targetEl as SVGElement).style.cursor = 'pointer'
                targetEl.setAttribute('tabindex', '0')
                targetEl.setAttribute('role', 'button')
                targetEl.setAttribute(
                  'aria-label',
                  `Select edge ${edge.source} to ${edge.target}: ${edge.label || 'connection'} (${edge.occurrence + 1})`,
                )
                targetEl.setAttribute('data-edge-source', edge.source)
                targetEl.setAttribute('data-edge-target', edge.target)
                targetEl.setAttribute('data-edge-index', String(edge.occurrence))
                if (edge.connectionIds.length > 0) {
                  targetEl.setAttribute('data-edge-connections', edge.connectionIds.join(','))
                }
                const activate = (e: Event) => {
                  e.stopPropagation()
                  onSelectEdge(edge)
                }
                targetEl.addEventListener('click', activate)
                targetEl.addEventListener('keydown', e => {
                  if ((e as KeyboardEvent).key === 'Enter' || (e as KeyboardEvent).key === ' ') {
                    e.preventDefault()
                    activate(e)
                  }
                })
              }

              edgePaths.forEach((pathEl, idx) => {
                const edge = parsedEdges[idx]
                if (!edge) return
                bindEdgeElement(pathEl, edge)
                if (edgeLabels[idx]) {
                  bindEdgeElement(edgeLabels[idx], edge)
                }
              })
            }

            el.appendChild(imported)
            // Mermaid sizes the viewBox while rendering off-screen. If the laid-out drawing ends up larger than that
            // box, its edges are clipped, so size the viewBox from the drawing as it is actually laid out here.
            fitViewBoxToContent(imported as unknown as SVGSVGElement)
            requestAnimationFrame(() => {
              if (!cancelled) computeFit(true)
            })
          } else {
            setError('Diagram rendered an unexpected non-SVG result.')
          }
        }
      } catch (err: unknown) {
        if (!cancelled) {
          if (containerRef.current) {
            containerRef.current.replaceChildren()
          }
          setError(err instanceof Error ? err.message : 'Failed to render diagram')
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    loadAndRender()
    return () => {
      cancelled = true
    }
  }, [jobId, artifactId, fetchArtifactText, onDiagramTextLoaded, onSelectNode, onSelectEdge])

  function handleWheel(e: React.WheelEvent) {
    e.preventDefault()
    const delta = e.deltaY < 0 ? 0.1 : -0.1
    setScale(prev => Math.max(0.25, Math.min(4, Number((prev + delta).toFixed(2)))))
  }

  function handleMouseDown(e: React.MouseEvent) {
    draggingRef.current = true
    lastPosRef.current = { x: e.clientX, y: e.clientY }
  }

  function handleMouseMove(e: React.MouseEvent) {
    if (!draggingRef.current) return
    const dx = e.clientX - lastPosRef.current.x
    const dy = e.clientY - lastPosRef.current.y
    lastPosRef.current = { x: e.clientX, y: e.clientY }
    setOffset(prev => ({ x: prev.x + dx, y: prev.y + dy }))
  }

  function handleMouseUp() {
    draggingRef.current = false
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div
        role="toolbar"
        aria-label="Diagram zoom controls"
        style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}
      >
        <button
          type="button"
          onClick={() => setScale(s => Math.min(4, Number((s + 0.2).toFixed(2))))}
          aria-label="Zoom in"
          style={toolbarBtnStyle}
        >
          Zoom +
        </button>
        <button
          type="button"
          onClick={() => setScale(s => Math.max(0.25, Number((s - 0.2).toFixed(2))))}
          aria-label="Zoom out"
          style={toolbarBtnStyle}
        >
          Zoom -
        </button>
        <button
          type="button"
          onClick={fitToStage}
          aria-label="Fit diagram to view"
          style={toolbarBtnStyle}
        >
          Fit
        </button>
        <span style={{ fontSize: '0.8rem', color: '#6b7280' }}>
          {Math.round(scale * 100)}% (drag to pan, scroll to zoom)
        </span>
      </div>

      {loading && (
        <div role="status" style={{ padding: '1rem', fontSize: '0.9rem', color: '#6b7280' }}>
          Loading diagram {artifactId}...
        </div>
      )}

      {error && (
        <div
          role="alert"
          style={{
            padding: '0.75rem 1rem',
            background: '#fef2f2',
            border: '1px solid #fca5a5',
            borderRadius: '0.4rem',
            color: '#dc2626',
            fontSize: '0.85rem',
            whiteSpace: 'pre-wrap',
          }}
        >
          {error}
        </div>
      )}

      <div
        ref={stageRef}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        aria-label={`Diagram canvas for ${artifactId}`}
        style={{
          border: '1px solid #e5e7eb',
          borderRadius: '0.5rem',
          background: '#fafafa',
          height: 'min(70vh, 640px)',
          minHeight: '420px',
          overflow: 'hidden',
          cursor: 'grab',
          position: 'relative',
          display: error ? 'none' : 'block',
        }}
      >
        <div
          ref={containerRef}
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
            transformOrigin: 'top left',
            padding: '0.5rem',
            display: 'inline-block',
          }}
        />
      </div>
    </div>
  )
}

const toolbarBtnStyle: React.CSSProperties = {
  padding: '0.3rem 0.65rem',
  fontSize: '0.8rem',
  background: '#fff',
  border: '1px solid #d1d5db',
  borderRadius: '0.3rem',
  cursor: 'pointer',
  color: '#374151',
}
