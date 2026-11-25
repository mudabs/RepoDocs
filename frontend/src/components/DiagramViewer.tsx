import { useCallback, useEffect, useRef, useState } from 'react'
import mermaid from 'mermaid'

let mermaidInitialized = false

function ensureMermaidInitialized() {
  if (!mermaidInitialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      htmlLabels: false,
      flowchart: { htmlLabels: false },
    })
    mermaidInitialized = true
  }
}

interface Props {
  jobId: string
  artifactId: string
  fetchArtifactText: (jobId: string, artifactId: string) => Promise<string>
  onDiagramTextLoaded?: (text: string) => void
  onSelectNode?: (nodeKey: string) => void
}

/**
 * Renders a Mermaid .mmd artifact with securityLevel:'strict' and htmlLabels:false,
 * interactive pan/zoom/fit, and clickable diagram node selection.
 */
export function DiagramViewer({
  jobId,
  artifactId,
  fetchArtifactText,
  onDiagramTextLoaded,
  onSelectNode,
}: Props) {
  const stageRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scale, setScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const draggingRef = useRef(false)
  const lastPosRef = useRef({ x: 0, y: 0 })

  const fitToStage = useCallback(() => {
    const stage = stageRef.current
    const container = containerRef.current
    const svg = container?.querySelector('svg')
    if (!stage || !svg) {
      setScale(1)
      setOffset({ x: 0, y: 0 })
      return
    }
    const vb = svg.viewBox?.baseVal
    const svgW = (vb && vb.width > 0 ? vb.width : svg.getBoundingClientRect().width / scale) || 1
    const svgH = (vb && vb.height > 0 ? vb.height : svg.getBoundingClientRect().height / scale) || 1
    const pad = 24
    const availW = Math.max(100, stage.clientWidth - pad)
    const availH = Math.max(100, stage.clientHeight - pad)
    const nextScale = Math.max(0.25, Math.min(2, Number(Math.min(availW / svgW, availH / svgH, 1).toFixed(2))))
    const dx = Math.max(0, Math.round((stage.clientWidth - svgW * nextScale) / 2))
    const dy = Math.max(0, Math.round((stage.clientHeight - svgH * nextScale) / 2))
    setScale(nextScale)
    setOffset({ x: dx, y: dy })
  }, [scale])

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
            // Wire clickable node IDs (c_<hex> / g_<hex> or entity IDs)
            imported.querySelectorAll('g.node').forEach(node => {
              const idAttr = node.getAttribute('id') || ''
              const match = /(?:^|-)((?:c|g)_[0-9a-f]{12,16})(?:-|$)/.exec(idAttr)
              const fallbackMatch = /^flowchart-(.+?)-\d+$/.exec(idAttr)
              const key = match ? match[1] : fallbackMatch ? fallbackMatch[1] : null
              if (key && onSelectNode) {
                ;(node as SVGElement).style.cursor = 'pointer'
                node.setAttribute('tabindex', '0')
                node.setAttribute('role', 'button')
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
            el.appendChild(imported)
            requestAnimationFrame(() => {
              if (!cancelled) fitToStage()
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
  }, [jobId, artifactId, fetchArtifactText, onDiagramTextLoaded, onSelectNode])

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
          height: '420px',
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
