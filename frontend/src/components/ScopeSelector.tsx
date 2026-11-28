export type DiagramScope = 'overview' | 'components' | 'erd'

export function scopeOfArtifact(artifactId: string): DiagramScope {
  if (artifactId.startsWith('architecture-view')) return 'components'
  if (artifactId.startsWith('erd')) return 'erd'
  return 'overview'
}

export function labelOfArtifact(artifactId: string): string {
  if (artifactId === 'architecture-view.mmd') return 'Architecture overview'
  if (artifactId.startsWith('architecture-view-')) return `Subsystem: ${artifactId.replace(/^architecture-view-|\.mmd$/g, '')}`
  if (artifactId.startsWith('erd')) return 'Database ERD'
  if (artifactId === 'overview.mmd') return 'Static overview'
  if (artifactId === 'architecture.mmd') return 'Repository overview'
  return artifactId
}

interface Props {
  artifactIds: string[]
  selectedArtifactId: string | null
  onSelect: (artifactId: string) => void
}

/**
 * Tabs/buttons for switching between available Mermaid diagram artifacts by scope.
 */
export function ScopeSelector({ artifactIds, selectedArtifactId, onSelect }: Props) {
  const mmdArtifacts = artifactIds.filter(id => id.endsWith('.mmd'))
  if (mmdArtifacts.length === 0) {
    return null
  }

  return (
    <div role="tablist" aria-label="Diagram scope selector" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
      {mmdArtifacts.map(id => {
        const selected = id === selectedArtifactId
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onSelect(id)}
            style={{
              padding: '0.4rem 0.85rem',
              borderRadius: '0.4rem',
              border: selected ? '1px solid #2563eb' : '1px solid #d1d5db',
              background: selected ? '#eff6ff' : '#fff',
              color: selected ? '#1d4ed8' : '#374151',
              fontWeight: selected ? 600 : 400,
              fontSize: '0.85rem',
              cursor: 'pointer',
            }}
          >
            {labelOfArtifact(id)}
            {labelOfArtifact(id) !== id && <span style={{ fontWeight: 400, color: '#4b5563' }}> ({id})</span>}
          </button>
        )
      })}
    </div>
  )
}
