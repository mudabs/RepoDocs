interface Props {
  jobId: string
  artifactIds: string[]
  artifactUrl: (jobId: string, artifactId: string) => string
}

function artifactTypeLabel(artifactId: string): string {
  if (artifactId.endsWith('.mmd')) return 'Mermaid'
  if (artifactId.endsWith('.json')) return 'JSON'
  if (artifactId.endsWith('.md')) return 'Markdown'
  if (artifactId.endsWith('.html')) return 'HTML'
  if (artifactId.endsWith('.svg')) return 'SVG'
  return 'File'
}

export function ArtifactDownloads({ jobId, artifactIds, artifactUrl }: Props) {
  if (artifactIds.length === 0) {
    return null
  }

  return (
    <section
      role="region"
      aria-label="Artifact downloads"
      style={{
        border: '1px solid #e5e7eb',
        borderRadius: '0.5rem',
        background: '#fff',
        padding: '1rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.5rem',
      }}
    >
      <h2 style={{ fontSize: '1rem', fontWeight: 600, margin: 0 }}>Artifacts</h2>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        {artifactIds.map(id => (
          <li
            key={id}
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem', fontSize: '0.85rem' }}
          >
            <span>
              <code>{id}</code>{' '}
              <span style={{ color: '#6b7280', fontSize: '0.78rem' }}>({artifactTypeLabel(id)})</span>
            </span>
            <a
              href={artifactUrl(jobId, id)}
              download={id}
              style={{
                color: '#2563eb',
                fontWeight: 500,
                textDecoration: 'none',
                border: '1px solid #bfdbfe',
                background: '#eff6ff',
                padding: '0.2rem 0.6rem',
                borderRadius: '0.3rem',
                fontSize: '0.8rem',
              }}
            >
              Download
            </a>
          </li>
        ))}
      </ul>
    </section>
  )
}
