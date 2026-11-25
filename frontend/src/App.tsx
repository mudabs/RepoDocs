import { useCallback, useEffect, useState } from 'react'
import { client } from './api/client'
import { ViewerPage } from './pages/ViewerPage'
import { WorkflowPage } from './pages/WorkflowPage'

type RouteState =
  | { name: 'workflow'; draftId?: string }
  | { name: 'viewer'; jobId: string; draftId?: string }

function parseLocation(): RouteState {
  const path = window.location.pathname
  const viewerMatch = /^\/viewer\/([^/]+)$/.exec(path)
  if (viewerMatch) {
    return { name: 'viewer', jobId: decodeURIComponent(viewerMatch[1]) }
  }
  const jobMatch = /^\/jobs\/([^/]+)$/.exec(path)
  if (jobMatch) {
    return { name: 'viewer', jobId: decodeURIComponent(jobMatch[1]) }
  }
  const draftMatch = /^\/workflow\/([^/]+)$/.exec(path)
  if (draftMatch) {
    return { name: 'workflow', draftId: decodeURIComponent(draftMatch[1]) }
  }
  return { name: 'workflow' }
}

export default function App() {
  const [page, setPage] = useState<RouteState>(() => parseLocation())
  const [sessionReady, setSessionReady] = useState(() => client.hasSession())
  const [authNeeded, setAuthNeeded] = useState(false)
  const [authToken, setAuthToken] = useState('')
  const [authError, setAuthError] = useState<string | null>(null)

  const navigate = useCallback((next: RouteState, replace = false) => {
    const url =
      next.name === 'viewer'
        ? `/viewer/${encodeURIComponent(next.jobId)}`
        : next.draftId
          ? `/workflow/${encodeURIComponent(next.draftId)}`
          : '/workflow'
    if (window.location.pathname !== url) {
      if (replace) {
        window.history.replaceState(next, '', url)
      } else {
        window.history.pushState(next, '', url)
      }
    }
    setPage(next)
  }, [])

  useEffect(() => {
    const onPopState = () => {
      setPage(parseLocation())
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // On deep link or refresh to /viewer/:jobId or /workflow/:draftId, attempt silent loopback session reconnect
  useEffect(() => {
    if (sessionReady) return
    if (page.name === 'viewer' || page.draftId) {
      client
        .openSession()
        .then(() => {
          setSessionReady(true)
          setAuthNeeded(false)
        })
        .catch(() => {
          setAuthNeeded(true)
        })
    }
  }, [page, sessionReady])

  if (page.name === 'viewer' && !sessionReady) {
    return (
      <main style={{ maxWidth: '520px', margin: '3rem auto', padding: '0 1rem', fontFamily: 'system-ui, sans-serif' }}>
        <h1 style={{ fontSize: '1.3rem', marginBottom: '0.75rem' }}>Reconnect session to view result</h1>
        {authNeeded ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
            <input
              type="password"
              value={authToken}
              onChange={e => setAuthToken(e.target.value)}
              placeholder="Operator auth token (if exposed)"
              aria-label="Session auth token"
              style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem' }}
            />
            {authError && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{authError}</div>}
            <button
              type="button"
              onClick={() => {
                setAuthError(null)
                client
                  .openSession(authToken.trim() || undefined)
                  .then(() => {
                    setAuthToken('')
                    setSessionReady(true)
                    setAuthNeeded(false)
                  })
                  .catch(err => setAuthError(err instanceof Error ? err.message : String(err)))
              }}
              style={{ padding: '0.5rem 1rem', background: '#2563eb', color: '#fff', border: 'none', borderRadius: '0.4rem', fontWeight: 600, cursor: 'pointer', alignSelf: 'flex-start' }}
            >
              Open session
            </button>
          </div>
        ) : (
          <p role="status">Reconnecting session...</p>
        )}
      </main>
    )
  }

  if (page.name === 'viewer') {
    return (
      <ViewerPage
        jobId={page.jobId}
        onBack={() => navigate({ name: 'workflow', draftId: page.draftId })}
      />
    )
  }

  return (
    <WorkflowPage
      draftId={page.draftId}
      onSessionReady={() => setSessionReady(true)}
      onDraftRoute={(id, replace) => navigate({ name: 'workflow', draftId: id }, replace)}
      onViewJob={(jobId, draftId) => navigate({ name: 'viewer', jobId, draftId })}
    />
  )
}
