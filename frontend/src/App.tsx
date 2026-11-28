import { useCallback, useEffect, useState } from 'react'
import { client } from './api/client'
import { rememberProvider } from './api/providerMemory'
import { HomePage } from './pages/HomePage'
import { ViewerPage } from './pages/ViewerPage'
import { WorkflowPage } from './pages/WorkflowPage'

type RouteState =
  | { name: 'home' }
  | { name: 'workflow'; draftId?: string }
  | { name: 'viewer'; jobId: string; draftId?: string }

function parseLocation(): RouteState {
  const path = window.location.pathname
  const viewerMatch = /^\/(?:viewer|jobs)\/([^/]+)$/.exec(path)
  if (viewerMatch) {
    return { name: 'viewer', jobId: decodeURIComponent(viewerMatch[1]) }
  }
  const draftMatch = /^\/workflow\/([^/]+)$/.exec(path)
  if (draftMatch) {
    return { name: 'workflow', draftId: decodeURIComponent(draftMatch[1]) }
  }
  if (path === '/workflow') {
    return { name: 'workflow' }
  }
  return { name: 'home' }
}

function urlOf(route: RouteState): string {
  if (route.name === 'viewer') return `/viewer/${encodeURIComponent(route.jobId)}`
  if (route.name === 'workflow') return route.draftId ? `/workflow/${encodeURIComponent(route.draftId)}` : '/workflow'
  return '/'
}

export default function App() {
  const [page, setPage] = useState<RouteState>(() => parseLocation())
  const [sessionReady, setSessionReady] = useState(() => client.hasSession())
  const [authNeeded, setAuthNeeded] = useState(false)
  const [authToken, setAuthToken] = useState('')
  const [authError, setAuthError] = useState<string | null>(null)
  const [signedOut, setSignedOut] = useState(false)
  const [confirmSignOut, setConfirmSignOut] = useState(false)

  const navigate = useCallback((next: RouteState, replace = false) => {
    if (replace && next.name === 'workflow' && window.location.pathname.startsWith('/viewer/')) {
      return
    }
    const url = urlOf(next)
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
    const onPopState = () => setPage(parseLocation())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // Home, results and saved drafts reconnect a loopback session automatically. The step-by-step page keeps its
  // explicit session step when opened directly.
  const autoSession = page.name === 'home' || page.name === 'viewer' || (page.name === 'workflow' && Boolean(page.draftId))
  useEffect(() => {
    if (sessionReady || signedOut || !autoSession) return
    client
      .openSession()
      .then(() => {
        setSessionReady(true)
        setAuthNeeded(false)
      })
      .catch(() => setAuthNeeded(true))
  }, [autoSession, sessionReady, signedOut])

  async function endSession() {
    try {
      await client.endSession()
    } catch {
      // The session may already have expired; the local state is cleared either way.
    }
    rememberProvider(null)
    setSessionReady(false)
    setSignedOut(true)
    navigate({ name: 'home' })
  }

  const header = (
    <header className="rd-header">
      <a
        className="rd-brand"
        href="/"
        onClick={e => {
          e.preventDefault()
          navigate({ name: 'home' })
        }}
      >
        RepoDocs
      </a>
      <nav className="rd-nav" aria-label="Main">
        <button type="button" className="rd-btn" onClick={() => navigate({ name: 'home' })}
          aria-current={page.name === 'home' ? 'page' : undefined}>
          New diagram
        </button>
        <button type="button" className="rd-btn" onClick={() => navigate({ name: 'workflow' })}
          aria-current={page.name === 'workflow' ? 'page' : undefined}>
          Advanced
        </button>
        {sessionReady && (
          confirmSignOut ? (
            <span className="rd-row" role="group" aria-label="Confirm sign out">
              <span className="rd-muted">Signing out hides your saved projects from new sessions.</span>
              <button type="button" className="rd-btn rd-btn-danger" onClick={() => { setConfirmSignOut(false); void endSession() }}>
                Sign out anyway
              </button>
              <button type="button" className="rd-btn" onClick={() => setConfirmSignOut(false)}>Stay signed in</button>
            </span>
          ) : (
            <button type="button" className="rd-btn" onClick={() => setConfirmSignOut(true)}>
              Sign out
            </button>
          )
        )}
      </nav>
    </header>
  )

  let body: JSX.Element
  if (page.name !== 'workflow' && !sessionReady) {
    body = (
      <main id="main" className="rd-card" style={{ maxWidth: '520px', margin: '0 auto' }}>
        <h1 className="rd-h2">{signedOut ? 'Signed out' : 'Connect to RepoDocs'}</h1>
        {signedOut && (
          <p className="rd-muted">
            The session ended and provider keys and consent were forgotten. Saved projects stay on this server's disk,
            but a new session starts a new local profile and does not list them; they are not deleted.
          </p>
        )}
        {authNeeded || signedOut ? (
          <form
            className="rd-stack"
            onSubmit={e => {
              e.preventDefault()
              setAuthError(null)
              client
                .openSession(authToken.trim() || undefined)
                .then(() => {
                  setAuthToken('')
                  setSessionReady(true)
                  setAuthNeeded(false)
                  setSignedOut(false)
                })
                .catch(err => setAuthError(err instanceof Error ? err.message : String(err)))
            }}
          >
            <div className="rd-field">
              <label className="rd-label" htmlFor="auth-token">Operator auth token</label>
              <input id="auth-token" className="rd-input" type="password" value={authToken}
                onChange={e => setAuthToken(e.target.value)} aria-label="Session auth token" autoComplete="off" />
              <span className="rd-muted">Only needed for servers exposed beyond this machine.</span>
            </div>
            {authError && <div role="alert" className="rd-alert rd-alert-error">{authError}</div>}
            <div className="rd-row">
              <button type="submit" className="rd-btn rd-btn-primary">Open session</button>
            </div>
          </form>
        ) : (
          <p role="status" className="rd-muted">Connecting...</p>
        )}
      </main>
    )
  } else if (page.name === 'viewer') {
    body = (
      <div id="main">
        <ViewerPage
          jobId={page.jobId}
          onBack={() => (window.history.length > 1 ? window.history.back() : navigate({ name: 'home' }))}
        />
      </div>
    )
  } else if (page.name === 'home') {
    body = (
      <main id="main">
        <HomePage
          onViewJob={jobId => navigate({ name: 'viewer', jobId })}
          onAdvanced={() => navigate({ name: 'workflow' })}
        />
      </main>
    )
  } else {
    body = (
      <div id="main">
        <WorkflowPage
          draftId={page.draftId}
          onSessionReady={() => setSessionReady(true)}
          onDraftRoute={(id, replace) => navigate({ name: 'workflow', draftId: id }, replace)}
          onViewJob={(jobId, draftId) => navigate({ name: 'viewer', jobId, draftId })}
        />
      </div>
    )
  }

  return (
    <div className="rd-shell">
      <a className="rd-skip" href="#main">Skip to content</a>
      {header}
      {body}
    </div>
  )
}
