import { useEffect, useState } from 'react'

export interface EvidenceEntry {
  name: string
  kind: string
  evidenceIds: string[]
  fileRefs: string[]
}

const GITHUB_BLOB_RE = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/blob\/[0-9a-f]{40}\/[^\s#?]+$/
const LOCAL_SOURCE_RE = /^<source:[a-z0-9][a-z0-9_-]*>(?:\/[^\s<>]*)?$/

/**
 * Validates source references so raw server filesystem paths are never displayed as links.
 */
export function isSafeSourceRef(ref: string): boolean {
  return GITHUB_BLOB_RE.test(ref) || LOCAL_SOURCE_RE.test(ref)
}

export function isGitHubBlobUrl(ref: string): boolean {
  return GITHUB_BLOB_RE.test(ref)
}

/**
 * Extracts evidence annotations from Mermaid `%% evidence <subject>: <ids>` comments.
 */
export function parseMermaidEvidenceComments(mmdText: string): EvidenceEntry[] {
  const entries: EvidenceEntry[] = []
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
        name: subject,
        kind: subject.includes('->') ? 'relationship' : 'entity',
        evidenceIds: ids,
        fileRefs: [],
      })
    }
  }
  return entries
}

interface Props {
  jobId: string
  artifactIds: string[]
  diagramText: string
  fetchArtifactText: (jobId: string, artifactId: string) => Promise<string>
}

export function EvidencePanel({ jobId, artifactIds, diagramText, fetchArtifactText }: Props) {
  const [entries, setEntries] = useState<EvidenceEntry[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      const collected: EvidenceEntry[] = []

      // 1. Look for architecture-view or evidence JSON artifacts
      const jsonArtifact = artifactIds.find(
        id => (id.includes('architecture-view') || id.includes('evidence') || id.includes('diagram-index')) && id.endsWith('.json'),
      )
      if (jsonArtifact) {
        try {
          const raw = await fetchArtifactText(jobId, jsonArtifact)
          if (!cancelled) {
            const parsed = JSON.parse(raw) as Record<string, unknown>
            const items = Array.isArray(parsed['components'])
              ? parsed['components']
              : Array.isArray(parsed['entries'])
                ? parsed['entries']
                : []
            for (const item of items) {
              if (item && typeof item === 'object') {
                const rec = item as Record<string, unknown>
                const name = String(rec['name'] ?? rec['id'] ?? 'unknown')
                const kind = String(rec['kind'] ?? rec['role'] ?? 'component')
                const ev = Array.isArray(rec['evidence']) ? rec['evidence'].map(String) : []
                const refs = Array.isArray(rec['files'])
                  ? rec['files'].map(String).filter(isSafeSourceRef)
                  : []
                collected.push({ name, kind, evidenceIds: ev, fileRefs: refs })
              }
            }
          }
        } catch {
          // Ignore malformed optional JSON artifact; fall back to diagram comments
        }
      }

      // 2. Also include evidence comments embedded in the active Mermaid diagram
      if (diagramText) {
        for (const entry of parseMermaidEvidenceComments(diagramText)) {
          collected.push(entry)
        }
      }

      if (!cancelled) {
        setEntries(collected)
        setLoading(false)
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [jobId, artifactIds, diagramText, fetchArtifactText])

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
        gap: '0.6rem',
        maxHeight: '460px',
        overflowY: 'auto',
      }}
    >
      <h2 style={{ fontSize: '1rem', fontWeight: 600, margin: 0 }}>Evidence</h2>

      {loading && <span style={{ fontSize: '0.85rem', color: '#6b7280' }}>Loading evidence...</span>}

      {!loading && entries.length === 0 && (
        <p style={{ fontSize: '0.85rem', color: '#6b7280', margin: 0 }}>
          No evidence data available
        </p>
      )}

      {entries.map((entry, idx) => (
        <div
          key={`${entry.name}-${idx}`}
          style={{
            borderTop: idx > 0 ? '1px solid #f3f4f6' : 'none',
            paddingTop: idx > 0 ? '0.5rem' : 0,
            fontSize: '0.82rem',
          }}
        >
          <div style={{ fontWeight: 600, color: '#111827' }}>
            {entry.name}{' '}
            <span style={{ fontWeight: 400, color: '#6b7280' }}>({entry.kind})</span>
          </div>
          {entry.evidenceIds.length > 0 && (
            <div style={{ color: '#4b5563', fontFamily: 'monospace', fontSize: '0.76rem', marginTop: '0.15rem' }}>
              {entry.evidenceIds.join(', ')}
            </div>
          )}
          {entry.fileRefs.length > 0 && (
            <ul style={{ margin: '0.25rem 0 0 1rem', padding: 0 }}>
              {entry.fileRefs.map((ref, rIdx) => (
                <li key={rIdx}>
                  {isGitHubBlobUrl(ref) ? (
                    <a href={ref} target="_blank" rel="noreferrer" style={{ color: '#2563eb' }}>
                      {ref}
                    </a>
                  ) : (
                    <code>{ref}</code>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </aside>
  )
}
