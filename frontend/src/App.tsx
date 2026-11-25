import { useState } from 'react'
import { WorkflowPage } from './pages/WorkflowPage'

export default function App() {
  const [page, setPage] = useState<{ name: 'workflow' } | { name: 'viewer'; jobId: string }>({
    name: 'workflow',
  })

  if (page.name === 'viewer') {
    return (
      <div style={{ fontFamily: 'system-ui, sans-serif', padding: '1rem' }}>
        <button
          type="button"
          onClick={() => setPage({ name: 'workflow' })}
          style={{
            marginBottom: '1rem',
            background: 'none',
            border: 'none',
            color: '#2563eb',
            cursor: 'pointer',
            fontSize: '0.9rem',
            textDecoration: 'underline',
          }}
        >
          Back to workflow
        </button>
        <p style={{ color: '#374151' }}>
          Diagram viewer for job <code>{page.jobId}</code>.
          The interactive viewer is delivered by issue #75.
        </p>
      </div>
    )
  }

  return <WorkflowPage onViewJob={(jobId) => setPage({ name: 'viewer', jobId })} />
}
