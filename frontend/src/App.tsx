import { useState } from 'react'
import { ViewerPage } from './pages/ViewerPage'
import { WorkflowPage } from './pages/WorkflowPage'

export default function App() {
  const [page, setPage] = useState<{ name: 'workflow' } | { name: 'viewer'; jobId: string }>({
    name: 'workflow',
  })

  if (page.name === 'viewer') {
    return (
      <ViewerPage
        jobId={page.jobId}
        onBack={() => setPage({ name: 'workflow' })}
      />
    )
  }

  return <WorkflowPage onViewJob={(jobId) => setPage({ name: 'viewer', jobId })} />
}
