import { useMemo, useState } from 'react'
import type { SelectedEdge } from './DiagramViewer'
import type { RepositoryRevision } from './RevisionBadge'

export interface EvidenceCitation {
  id?: string
  key?: string
  repository_id: string
  path: string
  line_start: number
  line_end: number
  content_hash?: string
  link?: string | null
}

export interface FileRef {
  path: string
  link: string | null
}

export interface ComponentEvidenceEntry {
  files: FileRef[]
  evidence: EvidenceCitation[]
  support: string
  support_reasons: string[]
}

export interface ConnectionEvidenceEntry {
  evidence: EvidenceCitation[]
  support: string
  support_reasons: string[]
}

export interface AlternativeEvidenceEntry {
  about: string[]
  evidence: EvidenceCitation[]
  statement?: string
}

export interface EvidenceMapData {
  map_version: string
  context_hash: string
  source_ir_hash: string
  repositories: RepositoryRevision[]
  components: Record<string, ComponentEvidenceEntry>
  connections: Record<string, ConnectionEvidenceEntry>
  alternatives: AlternativeEvidenceEntry[]
}

// Mirrors repodocs.architecture.navigation.SOURCE_LINK and viewer_script.py SOURCE_LINK.
export const SOURCE_LINK_RE =
  /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/blob\/[0-9a-f]{40}\/[A-Za-z0-9._~%\/+@!$&'()*,;=:-]+(?:#L[0-9]+(?:-L[0-9]+)?)?$/

export function isAllowedSourceLink(url: string | null | undefined): url is string {
  return typeof url === 'string' && SOURCE_LINK_RE.test(url)
}

export interface MermaidCommentEntry {
  subject: string
  kind: 'entity' | 'relationship'
  evidenceIds: string[]
}

export function parseMermaidEvidenceComments(mmdText: string): MermaidCommentEntry[] {
  const entries: MermaidCommentEntry[] = []
  for (const rawLine of mmdText.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line.startsWith('%% evidence ')) continue
    const rest = line.slice('%% evidence '.length)
    const colonIdx = rest.indexOf(':')
    if (colonIdx === -1) continue
    const subject = rest.slice(0, colonIdx).trim()
    const ids = rest
      .slice(colonIdx + 1)
      .split(/[,\s]+/)
      .map(s => s.trim())
      .filter(Boolean)
    if (subject && ids.length > 0) {
      entries.push({
        subject,
        kind: subject.includes('->') ? 'relationship' : 'entity',
        evidenceIds: ids,
      })
    }
  }
  return entries
}

function formatCitationLabel(c: EvidenceCitation): string {
  const span = c.line_start
    ? `:${c.line_start}${c.line_end && c.line_end > c.line_start ? `-${c.line_end}` : ''}`
    : ''
  return `${c.repository_id}:${c.path}${span}`
}

function CitationList({ citations }: { citations: EvidenceCitation[] }) {
  if (citations.length === 0) return null
  return (
    <ul style={{ margin: '0.25rem 0 0 1.1rem', padding: 0, display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
      {citations.map((c, idx) => {
        const label = formatCitationLabel(c)
        return (
          <li key={`${label}-${idx}`} style={{ fontSize: '0.8rem' }}>
            {isAllowedSourceLink(c.link) ? (
              <a href={c.link} target="_blank" rel="noopener noreferrer" style={{ color: '#2563eb' }}>
                {label}
              </a>
            ) : (
              <span>{label}</span>
            )}
            {c.content_hash && (
              <code title={c.content_hash} style={{ marginLeft: '0.4rem', fontSize: '0.72rem', color: '#6b7280' }}>
                {c.content_hash.slice(0, 10)}
              </code>
            )}
          </li>
        )
      })}
    </ul>
  )
}

interface Props {
  evidenceMap: EvidenceMapData | null
  diagramText: string
  selectedKey: string | null
  selectedEdge?: SelectedEdge | null
  onSelectKey: (key: string) => void
}

export function EvidencePanel({ evidenceMap, diagramText, selectedKey, selectedEdge, onSelectKey }: Props) {
  const [selectedAlternativeIdx, setSelectedAlternativeIdx] = useState<number | null>(null)
  const mermaidEntries = useMemo(() => parseMermaidEvidenceComments(diagramText), [diagramText])

  // Map Mermaid node keys (c_<hex> / subject) to component/connection/comment details.
  const componentIds = useMemo(
    () => (evidenceMap ? Object.keys(evidenceMap.components) : []),
    [evidenceMap],
  )
  const connectionIds = useMemo(
    () => (evidenceMap ? Object.keys(evidenceMap.connections) : []),
    [evidenceMap],
  )

  // Match selectedKey against exact component/connection ID, or a diagram comment subject's evidence IDs.
  const resolvedComponent = useMemo(() => {
    if (!evidenceMap || !selectedKey) return null
    if (evidenceMap.components[selectedKey]) {
      return { id: selectedKey, entry: evidenceMap.components[selectedKey] }
    }
    const comment = mermaidEntries.find(e => e.subject === selectedKey)
    if (comment) {
      for (const cid of componentIds) {
        const entry = evidenceMap.components[cid]
        if (entry.evidence.some(ev => {
          const evId = ev.id ?? ev.key
          return Boolean(evId && comment.evidenceIds.includes(evId))
        })) {
          return { id: cid, entry }
        }
      }
    }
    // Fallback: prefix/substring match for projected node keys (c_<12-hex>)
    const hexMatch = /([0-9a-f]{12,16})$/.exec(selectedKey)
    if (hexMatch) {
      const matchedId = componentIds.find(cid => cid.includes(hexMatch[1]))
      if (matchedId) return { id: matchedId, entry: evidenceMap.components[matchedId] }
    }
    return null
  }, [evidenceMap, selectedKey, mermaidEntries, componentIds])

  const resolvedConnection = useMemo(() => {
    if (!evidenceMap) return null
    if (selectedKey && evidenceMap.connections[selectedKey]) {
      return { id: selectedKey, entry: evidenceMap.connections[selectedKey] }
    }
    if (selectedEdge && selectedEdge.evidenceIds.length > 0) {
      const targetIds = new Set(selectedEdge.evidenceIds)
      let best: { id: string; entry: ConnectionEvidenceEntry; score: number } | null = null
      for (const cid of connectionIds) {
        const entry = evidenceMap.connections[cid]
        const keys = entry.evidence.map(ev => ev.id ?? ev.key).filter((k): k is string => Boolean(k))
        const overlap = keys.filter(k => targetIds.has(k)).length
        if (overlap > 0 && (!best || overlap > best.score)) {
          best = { id: cid, entry, score: overlap }
        }
      }
      if (best) return { id: best.id, entry: best.entry }
    }
    return null
  }, [evidenceMap, selectedKey, selectedEdge, connectionIds])

  const selectedComment = useMemo(() => {
    if (!selectedKey) return null
    return mermaidEntries.find(e => e.subject === selectedKey) ?? null
  }, [selectedKey, mermaidEntries])

  const hasAnyData =
    componentIds.length > 0 ||
    connectionIds.length > 0 ||
    (evidenceMap?.alternatives.length ?? 0) > 0 ||
    mermaidEntries.length > 0

  return (
    <aside
      role="region"
      aria-label="Evidence panel"
      style={{
        border: '1px solid #e5e7eb',
        borderRadius: '0.5rem',
        background: '#fff',
        padding: '1rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        maxHeight: '560px',
        overflowY: 'auto',
      }}
    >
      <h2 style={{ fontSize: '1rem', fontWeight: 600, margin: 0 }}>Evidence &amp; Claims</h2>

      {!hasAnyData && (
        <p style={{ fontSize: '0.85rem', color: '#6b7280', margin: 0 }}>
          No evidence data available
        </p>
      )}

      {/* Selected item details */}
      {resolvedComponent && (
        <div
          role="region"
          aria-label="Selected component evidence"
          style={{ background: '#f8fafc', border: '1px solid #cbd5e1', borderRadius: '0.4rem', padding: '0.65rem' }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.88rem' }}>{resolvedComponent.id}</div>
          <div style={{ fontSize: '0.78rem', color: '#475569', marginTop: '0.2rem' }}>
            Support: <strong>{resolvedComponent.entry.support}</strong>
            {resolvedComponent.entry.support_reasons.length > 0 &&
              ` (${resolvedComponent.entry.support_reasons.join(', ')})`}
          </div>
          {resolvedComponent.entry.files.length > 0 && (
            <div style={{ marginTop: '0.4rem', fontSize: '0.8rem' }}>
              <strong>Files:</strong>
              <ul style={{ margin: '0.2rem 0 0 1.1rem', padding: 0 }}>
                {resolvedComponent.entry.files.map((f, i) => (
                  <li key={i}>
                    {isAllowedSourceLink(f.link) ? (
                      <a href={f.link} target="_blank" rel="noopener noreferrer" style={{ color: '#2563eb' }}>
                        {f.path}
                      </a>
                    ) : (
                      <code>{f.path}</code>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div style={{ marginTop: '0.4rem', fontSize: '0.8rem' }}>
            <strong>Evidence:</strong>
            <CitationList citations={resolvedComponent.entry.evidence} />
          </div>
        </div>
      )}

      {resolvedConnection && (
        <div
          role="region"
          aria-label="Selected connection evidence"
          style={{ background: '#f8fafc', border: '1px solid #cbd5e1', borderRadius: '0.4rem', padding: '0.65rem' }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.88rem' }}>{resolvedConnection.id}</div>
          <div style={{ fontSize: '0.78rem', color: '#475569', marginTop: '0.2rem' }}>
            Support: <strong>{resolvedConnection.entry.support}</strong>
            {resolvedConnection.entry.support_reasons.length > 0 &&
              ` (${resolvedConnection.entry.support_reasons.join(', ')})`}
          </div>
          <div style={{ marginTop: '0.4rem', fontSize: '0.8rem' }}>
            <strong>Evidence:</strong>
            <CitationList citations={resolvedConnection.entry.evidence} />
          </div>
        </div>
      )}

      {!resolvedComponent && !resolvedConnection && selectedEdge && (
        <div
          role="region"
          aria-label="Selected edge evidence"
          style={{ background: '#f8fafc', border: '1px solid #cbd5e1', borderRadius: '0.4rem', padding: '0.65rem' }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.88rem' }}>
            {selectedEdge.source} -&gt; {selectedEdge.target}
            {selectedEdge.label ? `: ${selectedEdge.label}` : ''}
          </div>
          <div style={{ fontSize: '0.78rem', fontFamily: 'monospace', color: '#475569', marginTop: '0.25rem' }}>
            Evidence IDs: {selectedEdge.evidenceIds.join(', ') || 'none'}
          </div>
        </div>
      )}

      {!resolvedComponent && !resolvedConnection && !selectedEdge && selectedComment && (
        <div
          role="region"
          aria-label="Selected diagram element evidence"
          style={{ background: '#f8fafc', border: '1px solid #cbd5e1', borderRadius: '0.4rem', padding: '0.65rem' }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.88rem' }}>
            {selectedComment.subject} ({selectedComment.kind})
          </div>
          <div style={{ fontSize: '0.78rem', fontFamily: 'monospace', color: '#475569', marginTop: '0.25rem' }}>
            Evidence IDs: {selectedComment.evidenceIds.join(', ')}
          </div>
        </div>
      )}

      {selectedAlternativeIdx !== null && evidenceMap?.alternatives[selectedAlternativeIdx] && (
        <div
          role="region"
          aria-label="Alternative interpretation"
          style={{ background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: '0.4rem', padding: '0.65rem' }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.86rem' }}>Alternative reading</div>
          {evidenceMap.alternatives[selectedAlternativeIdx].statement && (
            <p style={{ margin: '0.25rem 0', fontSize: '0.8rem' }}>
              {evidenceMap.alternatives[selectedAlternativeIdx].statement}
            </p>
          )}
          <div style={{ fontSize: '0.78rem', color: '#475569' }}>
            About: {evidenceMap.alternatives[selectedAlternativeIdx].about.join(', ') || 'the whole view'}
          </div>
          <CitationList citations={evidenceMap.alternatives[selectedAlternativeIdx].evidence} />
        </div>
      )}

      {/* Component selector list */}
      {componentIds.length > 0 && (
        <div>
          <h3 style={subHeadingStyle}>Components ({componentIds.length})</h3>
          <ul style={listStyle}>
            {componentIds.map(id => {
              const item = evidenceMap!.components[id]
              const active = selectedKey === id || resolvedComponent?.id === id
              return (
                <li key={id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedAlternativeIdx(null)
                      onSelectKey(id)
                    }}
                    style={itemBtnStyle(active)}
                  >
                    <span>{id}</span>
                    <span style={{ color: '#6b7280', fontSize: '0.74rem' }}> ({item.support})</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* Connection selector list */}
      {connectionIds.length > 0 && (
        <div>
          <h3 style={subHeadingStyle}>Connections ({connectionIds.length})</h3>
          <ul style={listStyle}>
            {connectionIds.map(id => {
              const item = evidenceMap!.connections[id]
              const active = selectedKey === id
              return (
                <li key={id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedAlternativeIdx(null)
                      onSelectKey(id)
                    }}
                    style={itemBtnStyle(active)}
                  >
                    <span>{id}</span>
                    <span style={{ color: '#6b7280', fontSize: '0.74rem' }}> ({item.support})</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* Alternatives */}
      {evidenceMap && evidenceMap.alternatives.length > 0 && (
        <div>
          <h3 style={subHeadingStyle}>Alternatives ({evidenceMap.alternatives.length})</h3>
          <ul style={listStyle}>
            {evidenceMap.alternatives.map((alt, idx) => (
              <li key={idx}>
                <button
                  type="button"
                  onClick={() => setSelectedAlternativeIdx(idx)}
                  style={itemBtnStyle(selectedAlternativeIdx === idx)}
                >
                  Alternative {idx + 1}: {alt.about.join(', ') || 'view'}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Diagram-embedded evidence comments (for ERD / deterministic diagrams) */}
      {mermaidEntries.length > 0 && (
        <div>
          <h3 style={subHeadingStyle}>Diagram Evidence ({mermaidEntries.length})</h3>
          <ul style={listStyle}>
            {mermaidEntries.map((entry, idx) => (
              <li key={`${entry.subject}-${idx}`}>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedAlternativeIdx(null)
                    onSelectKey(entry.subject)
                  }}
                  style={itemBtnStyle(selectedKey === entry.subject)}
                >
                  <span>{entry.subject}</span>
                  <span style={{ color: '#6b7280', fontSize: '0.74rem' }}> ({entry.evidenceIds.join(', ')})</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  )
}

const subHeadingStyle: React.CSSProperties = {
  fontSize: '0.84rem',
  fontWeight: 600,
  color: '#374151',
  margin: '0 0 0.35rem',
}

const listStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: '0.25rem',
}

function itemBtnStyle(active: boolean): React.CSSProperties {
  return {
    width: '100%',
    textAlign: 'left',
    padding: '0.3rem 0.5rem',
    borderRadius: '0.3rem',
    border: active ? '1px solid #2563eb' : '1px solid #e5e7eb',
    background: active ? '#eff6ff' : '#fff',
    color: '#111827',
    fontSize: '0.8rem',
    cursor: 'pointer',
  }
}
