import { useEffect, useRef, useState } from 'react'
import mermaid from 'mermaid'

let mermaidInitialized = false

function ensureMermaidInitialized() {
  if (!mermaidInitialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
    })
    mermaidInitialized = true
  }
}

interface Props {
  jobId: string
  artifactId: string
  fetchArtifactText: (jobId: string, artifactId: string) => Promise<string>
  onDiagramTextLoaded?: (text: string) => void
}

/**
 * Renders a Mermaid .mmd artifact with securityLevel:'strict' and interactive pan/zoom.
 * Never uses dangerouslySetInnerHTML on untrusted text; invalid diagrams show plain-text errors.
 */
export function DiagramViewer({ jobId, artifactId, fetchArtifactText, onDiagramTextLoaded }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scale, setScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const draggingRef = useRef(false)
  const lastPosRef = useRef({ x: 0, y: 0 })

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
          // Clear previous content and insert the strict-mode SVG from Mermaid.
          el.replaceChildren()
          const parser = new DOMParser()
          const doc = parser.parseFromString(svg, 'image/svg+xml')
          const svgEl = doc.documentElement
          if (svgEl && svgEl.nodeName.toLowerCase() === 'svg') {
            el.appendChild(document.importNode(svgEl, true))
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
  }, [jobId, artifactId, fetchArtifactText, onDiagramTextLoaded])

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

  function handleReset() {
    setScale(1)
    setOffset({ x: 0, y: 0 })
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
          onClick={handleReset}
          aria-label="Fit and reset view"
          style={toolbarBtnStyle}
        >
          Reset view
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
          minHeight: '380px',
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
            padding: '1rem',
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
