import { useCallback, useEffect, useState } from 'react'
import { client } from '../api/client'
import type { Job } from '../api/types'
import { ArtifactDownloads } from '../components/ArtifactDownloads'
import { DiagramViewer } from '../components/DiagramViewer'
import { EvidencePanel } from '../components/EvidencePanel'
import { RevisionBadge } from '../components/RevisionBadge'
import { ScopeSelector } from '../components/ScopeSelector'
import { StatusBadge } from '../components/StatusBadge'

interface Props {
  jobId: string
  onBack: () => void
}

export function ViewerPage({ jobId, onBack }: Props) {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null)
  const [diagramText, setDiagramText] = useState<string>('')

  useEffect(() => {
    let cancelled = false
    setError(null)
    client
      .getJob(jobId)
      .then(loaded => {
        if (cancelled) return
        setJob(loaded)
        const firstMmd = loaded.result?.artifact_ids.find(id => id.endsWith('.mmd')) ?? null
        setSelectedArtifact(firstMmd)
      })
      .catch(err => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })
    return () => {
      cancelled = true
    }
  }, [jobId])

  const fetchArtifactText = useCallback(
    (id: string, artifactId: string) => client.fetchArtifactText(id, artifactId),
    [],
  )

  const artifactUrl = useCallback(
    (id: string, artifactId: string) => client.artifactUrl(id, artifactId),
    [],
  )

  return (
    <main
      style={{
        maxWidth: '1120px',
        margin: '1.5rem auto',
        padding: '0 1rem',
        fontFamily: 'system-ui, sans-serif',
        display: 'flex',
        flexDirection: 'column',
        gap: '1rem',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.75rem' }}>
        <button
          type="button"
          onClick={onBack}
          style={{
            background: 'none',
            border: 'none',
            color: '#2563eb',
            cursor: 'pointer',
            fontSize: '0.9rem',
            padding: 0,
            textDecoration: 'underline',
          }}
        >
          Back to workflow
        </button>

        {job && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
            <StatusBadge state={job.state} stage={job.stage} />
            {job.result && <RevisionBadge sourceIrHash={job.result.source_ir_hash} />}
          </div>
        )}
      </div>

      {error && (
        <div
          role="alert"
          style={{
            padding: '0.75rem 1rem',
            background: '#fef2f2',
            border: '1px solid #fca5a5',
            borderRadius: '0.4rem',
            color: '#dc2626',
            fontSize: '0.9rem',
          }}
        >
          {error}
        </div>
      )}

      {!job && !error && (
        <div role="status" style={{ fontSize: '0.9rem', color: '#6b7280' }}>
          Loading job {jobId}...
        </div>
      )}

      {job && job.state !== 'succeeded' && (
        <div
          role="alert"
          style={{
            padding: '0.65rem 1rem',
            background: '#fef3c7',
            border: '1px solid #f59e0b',
            borderRadius: '0.4rem',
            color: '#92400e',
            fontSize: '0.88rem',
          }}
        >
          This result is not current (state: <strong>{job.state}</strong>).
          {job.error ? ` ${job.error.message}` : ''}
        </div>
      )}

      {job?.result?.warnings && job.result.warnings.length > 0 && (
        <ul
          aria-label="Job warnings"
          style={{
            margin: 0,
            padding: '0.6rem 1rem 0.6rem 1.75rem',
            background: '#fffbeb',
            border: '1px solid #fde68a',
            borderRadius: '0.4rem',
            color: '#92400e',
            fontSize: '0.84rem',
          }}
        >
          {job.result.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      {job?.result && (
        <>
          <ScopeSelector
            artifactIds={job.result.artifact_ids}
            selectedArtifactId={selectedArtifact}
            onSelect={id => {
              setDiagramText('')
              setSelectedArtifact(id)
            }}
          />

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 2fr) minmax(260px, 1fr)',
              gap: '1rem',
              alignItems: 'start',
            }}
          >
            <div>
              {selectedArtifact ? (
                <DiagramViewer
                  jobId={job.job_id}
                  artifactId={selectedArtifact}
                  fetchArtifactText={fetchArtifactText}
                  onDiagramTextLoaded={setDiagramText}
                />
              ) : (
                <div
                  style={{
                    padding: '2rem',
                    border: '1px solid #e5e7eb',
                    borderRadius: '0.5rem',
                    color: '#6b7280',
                    fontSize: '0.9rem',
                  }}
                >
                  No Mermaid diagram artifacts were produced for this job.
                </div>
              )}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              <EvidencePanel
                jobId={job.job_id}
                artifactIds={job.result.artifact_ids}
                diagramText={diagramText}
                fetchArtifactText={fetchArtifactText}
              />
              <ArtifactDownloads
                jobId={job.job_id}
                artifactIds={job.result.artifact_ids}
                artifactUrl={artifactUrl}
              />
            </div>
          </div>
        </>
      )}
    </main>
  )
}
