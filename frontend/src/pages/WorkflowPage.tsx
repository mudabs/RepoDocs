import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, client } from '../api/client'
import type {
  AnySource,
  DraftData,
  Job,
  JobLimits,
  JobMode,
  JobView,
  PreviewResponse,
  ProviderResponse,
  WorkspaceHistoryItem,
} from '../api/types'
import { ProviderForm } from '../components/ProviderForm'
import { ConsentPanel } from '../components/ConsentPanel'
import { JobPoller } from '../components/JobPoller'
import { BudgetPreview, StatusBadge } from '../components/StatusBadge'

type Step =
  | 'source'
  | 'workspace'
  | 'preview'
  | 'mode'
  | 'provider'
  | 'limits'
  | 'consent'
  | 'submit'
  | 'poll'
  | 'done'

function generateIdempotencyKey(): string {
  // RFC 4122 v4 UUID from Web Crypto
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

interface StepHeaderProps {
  number: number
  title: string
  active: boolean
  done: boolean
}

function StepHeader({ number, title, active, done }: StepHeaderProps) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.6rem',
        marginBottom: '0.5rem',
        opacity: active || done ? 1 : 0.4,
      }}
    >
      <span
        style={{
          width: '1.6rem',
          height: '1.6rem',
          borderRadius: '50%',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '0.8rem',
          fontWeight: 700,
          background: done ? '#16a34a' : active ? '#2563eb' : '#e5e7eb',
          color: done || active ? '#fff' : '#6b7280',
          flexShrink: 0,
        }}
        aria-hidden="true"
      >
        {done ? '✓' : number}
      </span>
      <span style={{ fontWeight: 600, fontSize: '1rem' }}>{title}</span>
    </div>
  )
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        background: '#fff',
        border: '1px solid #e5e7eb',
        borderRadius: '0.5rem',
        padding: '1.25rem 1.5rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
      }}
    >
      {children}
    </div>
  )
}

interface WorkflowPageProps {
  draftId?: string
  onSessionReady?: () => void
  onDraftRoute?: (draftId: string, replace?: boolean) => void
  onViewJob: (jobId: string, draftId?: string) => void
}

export function WorkflowPage({ draftId, onSessionReady, onDraftRoute, onViewJob }: WorkflowPageProps) {
  const [step, setStep] = useState<Step>('source')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [activeDraftId, setActiveDraftId] = useState<string>(() => draftId || `draft_${generateIdempotencyKey().slice(0, 12)}`)
  const draftVersionRef = useRef<number | undefined>(undefined)
  const initializedDraftRef = useRef<string | null>(null)
  const [draftConflict, setDraftConflict] = useState<string | null>(null)
  const [savedWorkspaces, setSavedWorkspaces] = useState<WorkspaceHistoryItem[]>([])

  // Session auth (transient, never stored in browser storage)
  const [authToken, setAuthToken] = useState('')

  // Source selection (supports single or multiple repositories)
  const [workspaceName, setWorkspaceName] = useState('')
  const [sources, setSources] = useState<AnySource[]>([])
  const [sourceKind, setSourceKind] = useState<'local' | 'github'>('local')
  const [localName, setLocalName] = useState('')
  const [ghUrl, setGhUrl] = useState('')
  const [ghOwner, setGhOwner] = useState('')
  const [ghRepo, setGhRepo] = useState('')
  const [ghRevision, setGhRevision] = useState('')
  const [resolvedBranch, setResolvedBranch] = useState<string | null>(null)

  useEffect(() => {
    return () => {
      setAuthToken('')
    }
  }, [])

  // Workspace
  const [workspaceId, setWorkspaceId] = useState<string | null>(null)

  // Preview
  const [preview, setPreview] = useState<PreviewResponse | null>(null)

  // Mode
  const [mode, setMode] = useState<JobMode>('deterministic')

  // Provider
  const [providerSession, setProviderSession] = useState<ProviderResponse | null>(null)

  // Views
  const [selectedViews, setSelectedViews] = useState<JobView[]>(['overview', 'erd'])

  // Limits
  const [maxRequests, setMaxRequests] = useState(3)
  const [maxTokens, setMaxTokens] = useState<string>('100000')
  const [maxCostDollars, setMaxCostDollars] = useState<string>('')

  // Consent
  const [consentReceiptId, setConsentReceiptId] = useState<string | null>(null)

  // Job
  const [submittedJob, setSubmittedJob] = useState<Job | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState(() => generateIdempotencyKey())
  const [completedJob, setCompletedJob] = useState<Job | null>(null)

  const persistDraft = useCallback(
    async (overrides: Partial<DraftData> = {}, targetDraftId?: string) => {
      if (!client.hasSession()) return
      const id = targetDraftId || activeDraftId
      const data: DraftData = {
        workspace_id: overrides.workspace_id !== undefined ? overrides.workspace_id : workspaceId,
        workspace_name: overrides.workspace_name !== undefined ? overrides.workspace_name : workspaceName,
        sources: overrides.sources !== undefined ? overrides.sources : sources,
        mode: overrides.mode !== undefined ? overrides.mode : mode,
        views: overrides.views !== undefined ? overrides.views : selectedViews,
        max_requests: overrides.max_requests !== undefined ? overrides.max_requests : maxRequests,
        max_tokens: overrides.max_tokens !== undefined ? overrides.max_tokens : maxTokens,
        max_cost_dollars: overrides.max_cost_dollars !== undefined ? overrides.max_cost_dollars : maxCostDollars,
        step: overrides.step !== undefined ? overrides.step : step,
        active_job_id:
          overrides.active_job_id !== undefined
            ? overrides.active_job_id
            : (completedJob?.job_id ?? submittedJob?.job_id ?? null),
        pending_idempotency_key:
          overrides.pending_idempotency_key !== undefined ? overrides.pending_idempotency_key : null,
      }
      try {
        const saved = await client.saveDraft(id, data, draftVersionRef.current)
        draftVersionRef.current = saved.version
        initializedDraftRef.current = id
        setDraftConflict(null)
        onDraftRoute?.(id, true)
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 409) {
          setDraftConflict('A newer draft was saved in another tab. Load latest before overwriting.')
        }
      }
    },
    [
      activeDraftId,
      workspaceId,
      workspaceName,
      sources,
      mode,
      selectedViews,
      maxRequests,
      maxTokens,
      maxCostDollars,
      step,
      completedJob,
      submittedJob,
      onDraftRoute,
    ],
  )

  const restoreFromDraft = useCallback(async (id: string) => {
    const rec = await client.getDraft(id)
    draftVersionRef.current = rec.version
    setActiveDraftId(rec.draft_id)
    const d = rec.data
    if (d.workspace_name !== undefined) setWorkspaceName(d.workspace_name)
    if (d.sources) setSources(d.sources)
    if (d.workspace_id !== undefined) setWorkspaceId(d.workspace_id ?? null)
    if (d.mode) setMode(d.mode)
    if (d.views && d.views.length > 0) setSelectedViews(d.views)
    if (d.max_requests !== undefined) setMaxRequests(d.max_requests)
    if (d.max_tokens !== undefined) setMaxTokens(d.max_tokens)
    if (d.max_cost_dollars !== undefined) setMaxCostDollars(d.max_cost_dollars)

    if (d.active_job_id) {
      const job = await client.getJob(d.active_job_id)
      const terminal = ['succeeded', 'partial', 'budget-exhausted', 'cancelled', 'failed', 'interrupted', 'stale']
      if (terminal.includes(job.state)) {
        setCompletedJob(job)
        setSubmittedJob(job)
        setStep('done')
      } else {
        setSubmittedJob(job)
        setStep('poll')
      }
      return
    }
    if (d.pending_idempotency_key) {
      setIdempotencyKey(d.pending_idempotency_key)
    }
    if (d.workspace_id) {
      const p = await client.preview(d.workspace_id)
      setPreview(p)
      const restoredStep = (d.step as Step | undefined) || 'mode'
      // Never treat restored draft as having live provider credentials or consent
      if (restoredStep === 'consent' || restoredStep === 'submit' || restoredStep === 'limits') {
        setStep(d.mode === 'architecture' ? 'provider' : restoredStep)
      } else {
        setStep(restoredStep === 'source' ? 'workspace' : restoredStep)
      }
    } else {
      setStep('workspace')
    }
  }, [])

  useEffect(() => {
    if (!draftId || initializedDraftRef.current === draftId) return
    initializedDraftRef.current = draftId
    let cancelled = false
    async function reconnectDraft() {
      try {
        if (!client.hasSession()) {
          await client.openSession()
          onSessionReady?.()
        }
        if (cancelled) return
        const workspaces = await client.listWorkspaces().catch(() => [])
        if (!cancelled) setSavedWorkspaces(workspaces)
        await restoreFromDraft(draftId!)
      } catch {
        // Session may require explicit auth token on exposed server; remain on session step
      }
    }
    void reconnectDraft()
    return () => {
      cancelled = true
    }
  }, [draftId, onSessionReady, restoreFromDraft])

  function clearError() {
    setError(null)
  }

  async function handleSession() {
    clearError()
    setBusy(true)
    try {
      const token = authToken.trim() || undefined
      await client.openSession(token)
      setAuthToken('')
      onSessionReady?.()
      const workspaces = await client.listWorkspaces().catch(() => [])
      setSavedWorkspaces(workspaces)
      if (draftId) {
        initializedDraftRef.current = draftId
        try {
          await restoreFromDraft(draftId)
          return
        } catch {
          // Fall back to workspace selection if draft is missing
        }
      }
      initializedDraftRef.current = activeDraftId
      setStep('workspace')
      onDraftRoute?.(activeDraftId, true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function buildPendingSource(): AnySource | null {
    if (sourceKind === 'local') {
      if (!localName.trim()) return null
      return { kind: 'local', name: localName.trim() }
    }
    if (!ghOwner.trim() || !ghRepo.trim() || !ghRevision.trim()) return null
    if (!/^[0-9a-f]{40}$/i.test(ghRevision.trim())) {
      setError('Enter owner, repository, and a full 40-character commit SHA')
      return null
    }
    return {
      kind: 'github',
      owner: ghOwner.trim(),
      repository: ghRepo.trim(),
      revision: ghRevision.trim().toLowerCase(),
    }
  }

  function handleAddSource() {
    clearError()
    const pending = buildPendingSource()
    if (!pending) {
      if (!error) setError('Enter a valid source before adding')
      return
    }
    setSources(prev => [...prev, pending])
    setLocalName('')
    setGhUrl('')
    setGhOwner('')
    setGhRepo('')
    setGhRevision('')
    setResolvedBranch(null)
  }

  async function handleResolveGitHubUrl(): Promise<AnySource | null> {
    clearError()
    if (!ghUrl.trim()) {
      setError('Enter a public https://github.com/owner/repository URL')
      return null
    }
    setBusy(true)
    try {
      const resolved = await client.resolveGitHubUrl(ghUrl.trim())
      setGhOwner(resolved.owner)
      setGhRepo(resolved.repository)
      setGhRevision(resolved.revision)
      setResolvedBranch(resolved.default_branch)
      return { kind: 'github', owner: resolved.owner, repository: resolved.repository, revision: resolved.revision }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
      return null
    } finally {
      setBusy(false)
    }
  }

  function handleRemoveSource(index: number) {
    setSources(prev => prev.filter((_, i) => i !== index))
  }

  async function handleCreateWorkspace() {
    clearError()
    setBusy(true)
    try {
      let finalSources = [...sources]
      let pending = buildPendingSource()
      if (!pending && sourceKind === 'github' && ghUrl.trim() && !ghRevision.trim()) {
        const resolved = await client.resolveGitHubUrl(ghUrl.trim())
        setGhOwner(resolved.owner)
        setGhRepo(resolved.repository)
        setGhRevision(resolved.revision)
        setResolvedBranch(resolved.default_branch)
        pending = { kind: 'github', owner: resolved.owner, repository: resolved.repository, revision: resolved.revision }
      }
      if (pending) {
        finalSources.push(pending)
      }
      if (finalSources.length === 0) {
        setError('Add or enter at least one source repository')
        return
      }
      const first = finalSources[0]
      const defaultName = first.kind === 'local' ? first.name : `${first.owner}-${first.repository}`
      const wsName = workspaceName.trim() || defaultName
      const ws = await client.createWorkspace(wsName, finalSources)
      setSources(finalSources)
      setWorkspaceId(ws.workspace_id)
      setStep('preview')
      await persistDraft({ workspace_id: ws.workspace_id, workspace_name: wsName, sources: finalSources, step: 'preview' })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function handlePreview() {
    if (!workspaceId) return
    clearError()
    setBusy(true)
    try {
      const p = await client.preview(workspaceId, providerSession?.provider_session_id)
      setPreview(p)
      setStep('mode')
      await persistDraft({ step: 'mode' })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function handleProviderConnect(opts: {
    provider: string
    host: string
    model: string
    api_key?: string
  }) {
    if (!workspaceId) return
    clearError()
    // The api_key is passed through; it is not stored in any state field here.
    const sess = await client.connectProvider({
      provider: opts.provider,
      host: opts.host,
      model: opts.model,
      ...(opts.api_key ? { api_key: opts.api_key } : {}),
    })
    setProviderSession(sess)
    const updatedPreview = await client.preview(workspaceId, sess.provider_session_id)
    setPreview(updatedPreview)
  }

  async function handleProviderDisconnect() {
    if (!providerSession) return
    clearError()
    try {
      await client.disconnectProvider(providerSession.provider_session_id)
    } catch {
      // Best-effort disconnect
    }
    setProviderSession(null)
  }

  function handleModeNext() {
    clearError()
    if (mode === 'deterministic') {
      setStep('limits')
    } else {
      setStep('provider')
    }
  }

  function handleProviderNext() {
    if (!providerSession) {
      setError('Connect a provider before continuing')
      return
    }
    clearError()
    setStep('limits')
  }

  function handleLimitsNext() {
    clearError()
    if (mode === 'architecture' && preview?.provider_is_external) {
      setStep('consent')
    } else {
      setStep('submit')
    }
  }

  async function handleSubmit() {
    if (!workspaceId || !preview) return
    clearError()
    setBusy(true)
    try {
      const limits: JobLimits = mode === 'deterministic'
        ? { max_requests: 0, max_total_tokens: null, max_cost_microunits: 0, currency: 'USD' }
        : {
            max_requests: maxRequests,
            max_total_tokens: Math.max(1, parseInt(maxTokens, 10) || 100000),
            max_cost_microunits: maxCostDollars ? Math.round(parseFloat(maxCostDollars) * 1_000_000) : null,
            currency: maxCostDollars ? 'USD' : null,
          }
      const jobReq = {
        contract_version: '1.0' as const,
        kind: 'request' as const,
        workspace_id: workspaceId,
        idempotency_key: idempotencyKey,
        mode,
        provider_session_id: mode === 'architecture' ? (providerSession?.provider_session_id ?? null) : null,
        context_hash: mode === 'architecture' ? (preview.context_hash ?? null) : null,
        consent_receipt_id: mode === 'architecture' ? consentReceiptId : null,
        views: selectedViews,
        limits,
      }
      // Persist pending idempotency key before POST so a lost response recovers without duplicate submission.
      await persistDraft({ step: 'submit', pending_idempotency_key: idempotencyKey })
      const { job } = await client.submitJob(jobReq)
      setSubmittedJob(job)
      setStep('poll')
      await persistDraft({ step: 'poll', active_job_id: job.job_id, pending_idempotency_key: null })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function handleJobComplete(job: Job) {
    setCompletedJob(job)
    setStep('done')
    void persistDraft({ step: 'done', active_job_id: job.job_id, pending_idempotency_key: null })
  }

  async function handleRetryWithSources() {
    if (!workspaceId) return
    clearError()
    setBusy(true)
    try {
      const updated = await client.preview(workspaceId, providerSession?.provider_session_id)
      setPreview(updated)
      setConsentReceiptId(null)
      setSubmittedJob(null)
      setCompletedJob(null)
      setIdempotencyKey(generateIdempotencyKey())
      setStep('limits')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function handleNarrowScope() {
    clearError()
    setWorkspaceId(null)
    setPreview(null)
    setConsentReceiptId(null)
    setSubmittedJob(null)
    setCompletedJob(null)
    setIdempotencyKey(generateIdempotencyKey())
    setStep('workspace')
  }

  const doneSteps: Step[] = []
  const stepsOrder: Step[] = ['source', 'workspace', 'preview', 'mode', 'provider', 'limits', 'consent', 'submit', 'poll', 'done']
  const currentIdx = stepsOrder.indexOf(step)
  for (let i = 0; i < currentIdx; i++) {
    doneSteps.push(stepsOrder[i])
  }

  function isDone(s: Step) { return doneSteps.includes(s) }
  function isActive(s: Step) { return step === s }

  const viewOptions: JobView[] = ['overview', 'components', 'erd']

  return (
    <main
      style={{ maxWidth: '660px', margin: '2rem auto', padding: '0 1rem', fontFamily: 'system-ui, sans-serif' }}
    >
      <h1 style={{ fontSize: '1.5rem', fontWeight: 700, marginBottom: '1.5rem' }}>
        RepoDocs: Generate repository diagrams
      </h1>

      {draftConflict && (
        <div role="alert" style={{ marginBottom: '1rem', padding: '0.6rem 0.8rem', background: '#fef3c7', border: '1px solid #f59e0b', borderRadius: '0.4rem', fontSize: '0.85rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem' }}>
          <span>{draftConflict}</span>
          <button
            type="button"
            onClick={() => { void restoreFromDraft(activeDraftId) }}
            style={{ padding: '0.3rem 0.7rem', background: '#fff', border: '1px solid #d97706', borderRadius: '0.3rem', cursor: 'pointer', fontWeight: 600 }}
          >
            Load latest draft
          </button>
        </div>
      )}

      {/* Step 1: Open session (optional auth token for self-hosted / non-loopback) */}
      {step === 'source' && !workspaceId && (
        <Card>
          <StepHeader number={1} title="Open session" active={true} done={false} />
          <p style={{ fontSize: '0.9rem', color: '#374151', margin: 0 }}>
            Open a session with the RepoDocs service. Loopback servers need no token; self-hosted
            non-loopback servers require the operator auth token.
          </p>
          <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Auth token (optional for loopback)</span>
            <input
              type="password"
              value={authToken}
              onChange={e => setAuthToken(e.target.value)}
              autoComplete="off"
              placeholder="Required only for exposed self-hosted servers"
              style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
              aria-label="Session auth token"
            />
          </label>
          {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}
          <button
            type="button"
            onClick={handleSession}
            disabled={busy}
            style={{
              padding: '0.5rem 1rem',
              background: busy ? '#93c5fd' : '#2563eb',
              color: '#fff',
              border: 'none',
              borderRadius: '0.4rem',
              fontWeight: 600,
              cursor: busy ? 'not-allowed' : 'pointer',
              alignSelf: 'flex-start',
            }}
          >
            {busy ? 'Opening...' : 'Open session'}
          </button>
        </Card>
      )}

      {step === 'workspace' && savedWorkspaces.length > 0 && (
        <Card>
          <strong style={{ fontSize: '0.95rem' }}>Saved workspaces</strong>
          <ul aria-label="Saved workspaces" style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            {savedWorkspaces.map(ws => (
              <li key={ws.workspace_id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem', fontSize: '0.85rem', padding: '0.4rem 0.6rem', background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: '0.35rem' }}>
                <span>
                  <strong>{ws.name}</strong> ({ws.workspace_id.slice(0, 10)}...)
                  {ws.latest_job ? ` - ${ws.latest_job.state}` : ''}
                </span>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  {ws.latest_job && ws.latest_job.result && ws.latest_job.result.artifact_ids.length > 0 && (
                    <button
                      type="button"
                      onClick={() => onViewJob(ws.latest_job!.job_id, activeDraftId)}
                      style={{ fontSize: '0.8rem', color: '#2563eb', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
                    >
                      Open result
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      void client.deleteWorkspace(ws.workspace_id).then(() => {
                        setSavedWorkspaces(prev => prev.filter(item => item.workspace_id !== ws.workspace_id))
                      })
                    }}
                    style={{ fontSize: '0.8rem', color: '#dc2626', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* After session opened, show source selection (single or multiple repositories) */}
      {step === 'workspace' ? (
        <Card>
          <StepHeader number={2} title="Select source repositories" active={isActive('workspace')} done={isDone('workspace')} />
          <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Workspace name (optional)</span>
            <input
              type="text"
              value={workspaceName}
              onChange={e => setWorkspaceName(e.target.value)}
              placeholder="Defaults to first source name"
              style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
              aria-label="Workspace name"
            />
          </label>
          {sources.length > 0 && (
            <ul aria-label="Selected sources" style={{ margin: 0, padding: '0.5rem 0.75rem', background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: '0.35rem', listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
              {sources.map((src, idx) => (
                <li key={idx} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: '0.85rem' }}>
                  <span>
                    {src.kind === 'local'
                      ? `local: ${src.name}`
                      : `github: ${src.owner}/${src.repository}@${src.revision.slice(0, 8)}`}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRemoveSource(idx)}
                    aria-label={`Remove source ${idx + 1}`}
                    style={{ fontSize: '0.78rem', color: '#dc2626', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div role="group" aria-label="Source type" style={{ display: 'flex', gap: '1rem' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer' }}>
              <input type="radio" checked={sourceKind === 'local'} onChange={() => setSourceKind('local')} />
              <span>Local source (allowlisted name)</span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer' }}>
              <input type="radio" checked={sourceKind === 'github'} onChange={() => setSourceKind('github')} />
              <span>GitHub (pinned commit)</span>
            </label>
          </div>
          {sourceKind === 'local' ? (
            <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
              <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Source name</span>
              <input
                type="text"
                value={localName}
                onChange={e => setLocalName(e.target.value)}
                placeholder="e.g. my-service"
                style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
                aria-label="Local source name"
              />
              <span style={{ fontSize: '0.78rem', color: '#6b7280' }}>
                Must match a name configured by the RepoDocs operator.
              </span>
            </label>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>GitHub repository URL</span>
                <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                  <input
                    type="url"
                    value={ghUrl}
                    onChange={e => { setGhUrl(e.target.value); setResolvedBranch(null) }}
                    placeholder="https://github.com/mudabs/EventShare"
                    aria-label="GitHub repository URL"
                    style={{ flex: 1, minWidth: '220px', padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
                  />
                  <button
                    type="button"
                    onClick={() => { void handleResolveGitHubUrl() }}
                    disabled={busy}
                    style={{
                      padding: '0.4rem 0.8rem',
                      background: '#f3f4f6',
                      color: '#1f2937',
                      border: '1px solid #d1d5db',
                      borderRadius: '0.3rem',
                      fontWeight: 600,
                      cursor: busy ? 'not-allowed' : 'pointer',
                    }}
                  >
                    Resolve URL
                  </button>
                </div>
              </label>
              {resolvedBranch && ghOwner && ghRepo && ghRevision && (
                <div role="status" style={{ fontSize: '0.82rem', color: '#166534', background: '#f0fdf4', border: '1px solid #86efac', borderRadius: '0.35rem', padding: '0.4rem 0.6rem' }}>
                  Pinned <strong>{ghOwner}/{ghRepo}</strong> default branch <code>{resolvedBranch}</code> to commit{' '}
                  <code>{ghRevision.slice(0, 12)}</code>
                </div>
              )}
              <details>
                <summary style={{ fontSize: '0.82rem', color: '#4b5563', cursor: 'pointer' }}>
                  Advanced: explicit owner, repository, and commit SHA
                </summary>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginTop: '0.5rem' }}>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Owner</span>
                    <input type="text" value={ghOwner} onChange={e => setGhOwner(e.target.value)}
                      placeholder="github-owner" style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }} />
                  </label>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Repository</span>
                    <input type="text" value={ghRepo} onChange={e => setGhRepo(e.target.value)}
                      placeholder="repo-name" style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }} />
                  </label>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Commit SHA (40 hex characters)</span>
                    <input type="text" value={ghRevision} onChange={e => setGhRevision(e.target.value)}
                      placeholder="e.g. a1b2c3d4..." maxLength={40} style={{ padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem', fontFamily: 'monospace' }} />
                  </label>
                </div>
              </details>
            </div>
          )}
          {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}
          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={handleAddSource}
              disabled={busy}
              style={{
                padding: '0.5rem 1rem',
                background: '#fff',
                color: '#2563eb',
                border: '1px solid #2563eb',
                borderRadius: '0.4rem',
                fontWeight: 600,
                cursor: busy ? 'not-allowed' : 'pointer',
              }}
            >
              Add source
            </button>
            <button
              type="button"
              onClick={handleCreateWorkspace}
              disabled={busy}
              style={{
                padding: '0.5rem 1rem',
                background: busy ? '#93c5fd' : '#2563eb',
                color: '#fff',
                border: 'none',
                borderRadius: '0.4rem',
                fontWeight: 600,
                cursor: busy ? 'not-allowed' : 'pointer',
              }}
            >
              {busy ? 'Creating workspace...' : 'Create workspace'}
            </button>
          </div>
        </Card>
      ) : null}

      {/* Preview */}
      {workspaceId && (step === 'preview' || isDone('preview')) && (
        <Card>
          <StepHeader number={3} title="Scan and preview" active={isActive('preview')} done={isDone('preview')} />
          {!preview ? (
            <>
              <p style={{ fontSize: '0.9rem', color: '#374151', margin: 0 }}>
                Scan the source and preview the redacted context that will be used.
              </p>
              {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}
              <button
                type="button"
                onClick={handlePreview}
                disabled={busy}
                style={{
                  padding: '0.5rem 1rem',
                  background: busy ? '#93c5fd' : '#2563eb',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: busy ? 'not-allowed' : 'pointer',
                  alignSelf: 'flex-start',
                }}
              >
                {busy ? 'Scanning...' : 'Scan and preview'}
              </button>
            </>
          ) : (
            <div style={{ fontSize: '0.85rem', color: '#374151' }}>
              <p style={{ margin: '0 0 0.5rem' }}>
                Context hash: <code>{preview.context_hash.slice(0, 16)}...</code> ({preview.documents} documents, {preview.context_bytes.toLocaleString()} bytes, {preview.redactions} redactions)
              </p>
              <p style={{ margin: '0 0 0.5rem' }}>
                Omitted:{' '}
                {Object.keys(preview.omitted).length === 0
                  ? 'none'
                  : Object.entries(preview.omitted)
                      .map(([reason, count]) => `${reason}: ${count}`)
                      .join(', ')}
              </p>
              {preview.snapshots.length > 0 && (
                <ul aria-label="Source snapshots" style={{ margin: '0 0 0.5rem 1.25rem', padding: 0 }}>
                  {preview.snapshots.map((snap, idx) => (
                    <li key={idx}>
                      <strong>{snap.repository_id}</strong>: {snap.revision_kind} <code>{snap.revision.slice(0, 12)}</code> ({snap.working_tree_state})
                    </li>
                  ))}
                </ul>
              )}
              {preview.limitations > 0 && (
                <p style={{ margin: '0 0 0.5rem', color: '#92400e' }}>
                  Scan limitations: {preview.limitations}
                </p>
              )}
              {preview.warnings.length > 0 && (
                <ul style={{ margin: '0 0 0.5rem 1.25rem', padding: 0 }}>
                  {preview.warnings.map((w, i) => <li key={i} style={{ color: '#92400e' }}>{w}</li>)}
                </ul>
              )}
              {preview.budget && (
                <p style={{ margin: 0, color: '#6b7280' }}>
                  Estimated max cost:{' '}
                  {preview.budget.max_estimated_cost_usd !== null
                    ? `~$${preview.budget.max_estimated_cost_usd} USD`
                    : 'unknown'}{' '}
                  (estimate only)
                  {preview.budget.notes.length > 0 ? `: ${preview.budget.notes.join('; ')}` : ''}
                </p>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Mode selection */}
      {preview && (step === 'mode' || isDone('mode')) && (
        <Card>
          <StepHeader number={4} title="Select mode" active={isActive('mode')} done={isDone('mode')} />
          {isActive('mode') && (
            <>
              <div role="group" aria-label="Analysis mode" style={{ display: 'flex', gap: '1rem' }}>
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: '0.4rem', cursor: 'pointer' }}>
                  <input type="radio" checked={mode === 'deterministic'} onChange={() => setMode('deterministic')} style={{ marginTop: '0.2rem' }} />
                  <span>
                    <strong>Deterministic</strong>
                    <span style={{ display: 'block', fontSize: '0.8rem', color: '#6b7280' }}>
                      No AI provider required. Generates diagrams from static analysis only.
                    </span>
                  </span>
                </label>
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: '0.4rem', cursor: 'pointer' }}>
                  <input type="radio" checked={mode === 'architecture'} onChange={() => setMode('architecture')} style={{ marginTop: '0.2rem' }} />
                  <span>
                    <strong>Architecture</strong>
                    <span style={{ display: 'block', fontSize: '0.8rem', color: '#6b7280' }}>
                      Uses an AI provider you configure. Subject to your provider's terms and costs.
                    </span>
                  </span>
                </label>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
                <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Diagram types to include</span>
                <div style={{ display: 'flex', gap: '1rem' }}>
                  {viewOptions.map(v => (
                    <label key={v} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer', fontSize: '0.9rem' }}>
                      <input
                        type="checkbox"
                        checked={selectedViews.includes(v)}
                        onChange={e => {
                          if (e.target.checked) {
                            setSelectedViews(prev => [...prev, v])
                          } else {
                            setSelectedViews(prev => prev.filter(x => x !== v))
                          }
                        }}
                        disabled={v === 'components' && mode === 'deterministic'}
                      />
                      <span style={{ opacity: v === 'components' && mode === 'deterministic' ? 0.4 : 1 }}>
                        {v} {v === 'components' && mode === 'deterministic' ? '(requires architecture mode)' : ''}
                      </span>
                    </label>
                  ))}
                </div>
              </div>

              {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}
              <button
                type="button"
                onClick={handleModeNext}
                disabled={selectedViews.length === 0}
                style={{
                  padding: '0.5rem 1rem',
                  background: selectedViews.length === 0 ? '#e5e7eb' : '#2563eb',
                  color: selectedViews.length === 0 ? '#9ca3af' : '#fff',
                  border: 'none',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: selectedViews.length === 0 ? 'not-allowed' : 'pointer',
                  alignSelf: 'flex-start',
                }}
              >
                Continue
              </button>
            </>
          )}
          {isDone('mode') && (
            <span style={{ fontSize: '0.85rem', color: '#374151' }}>
              Mode: <strong>{mode}</strong>, views: {selectedViews.join(', ')}
            </span>
          )}
        </Card>
      )}

      {/* Provider (architecture only) */}
      {mode === 'architecture' && (step === 'provider' || isDone('provider')) && (
        <Card>
          <StepHeader number={5} title="Connect provider" active={isActive('provider')} done={isDone('provider')} />
          {isActive('provider') && (
            <>
              <ProviderForm
                onConnect={handleProviderConnect}
                onDisconnect={handleProviderDisconnect}
                connected={providerSession !== null}
                error={error}
              />
              {providerSession && (
                <button
                  type="button"
                  onClick={handleProviderNext}
                  style={{
                    padding: '0.5rem 1rem',
                    background: '#2563eb',
                    color: '#fff',
                    border: 'none',
                    borderRadius: '0.4rem',
                    fontWeight: 600,
                    cursor: 'pointer',
                    alignSelf: 'flex-start',
                  }}
                >
                  Continue
                </button>
              )}
            </>
          )}
          {isDone('provider') && providerSession && (
            <span style={{ fontSize: '0.85rem', color: '#374151' }}>
              Provider: <strong>{providerSession.provider}</strong>, model: <strong>{providerSession.model}</strong>
            </span>
          )}
        </Card>
      )}

      {/* Limits */}
      {(step === 'limits' || isDone('limits')) && (
        <Card>
          <StepHeader number={mode === 'architecture' ? 6 : 5} title="Budget limits" active={isActive('limits')} done={isDone('limits')} />
          {isActive('limits') && mode === 'architecture' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              {preview?.budget?.initial_request && (
                <div
                  aria-label="Per-request model capacity and preflight"
                  style={{
                    background: preview.budget.preflight_ok === false ? '#fef2f2' : '#f8fafc',
                    border: `1px solid ${preview.budget.preflight_ok === false ? '#fca5a5' : '#cbd5e1'}`,
                    borderRadius: '0.4rem',
                    padding: '0.75rem',
                    fontSize: '0.84rem',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '0.3rem',
                  }}
                >
                  <strong>Per-request model capacity</strong>
                  <span>
                    Effective context window: <strong>{preview.budget.initial_request.context_window_tokens.toLocaleString()}</strong> tokens{' '}
                    ({preview.budget.context_window_source === 'operator'
                      ? 'configured by operator'
                      : preview.budget.context_window_source === 'provider-default'
                        ? 'provider default, not verified for served model'
                        : 'fallback default (4,096), unverified for served model'})
                  </span>
                  <span>
                    Reserved output per request: <strong>{preview.budget.initial_request.reserved_output_tokens.toLocaleString()}</strong> tokens
                  </span>
                  <span>
                    Initial request estimate: <strong>{preview.budget.initial_request.input_tokens.toLocaleString()}</strong> input (
                    {preview.budget.initial_request.estimate_method}) +{' '}
                    <strong>{preview.budget.initial_request.reserved_output_tokens.toLocaleString()}</strong> output ={' '}
                    <strong>{preview.budget.initial_request.total_tokens.toLocaleString()}</strong> tokens
                  </span>
                  {preview.budget.preflight_error && (
                    <div role="alert" style={{ color: '#b91c1c', marginTop: '0.25rem' }}>
                      {preview.budget.preflight_error.message}
                    </div>
                  )}
                </div>
              )}

              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <strong style={{ fontSize: '0.88rem' }}>Aggregate job allowance</strong>
                <label style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Max AI requests (1-6)</span>
                  <input
                    type="number"
                    min={1}
                    max={6}
                    value={maxRequests}
                    onChange={e => setMaxRequests(Math.max(1, Math.min(6, parseInt(e.target.value, 10) || 1)))}
                    style={{ width: '5rem', padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
                    aria-label="Maximum number of AI requests"
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Aggregate total token cap across all requests</span>
                  <input
                    type="number"
                    min={1}
                    required
                    value={maxTokens}
                    onChange={e => setMaxTokens(e.target.value)}
                    placeholder="e.g. 100000"
                    style={{ width: '10rem', padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
                    aria-label="Maximum total tokens"
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Max cost in USD (optional, requires matching operator price profile)</span>
                  <input
                    type="number"
                    min={0}
                    step={0.01}
                    value={maxCostDollars}
                    onChange={e => setMaxCostDollars(e.target.value)}
                    placeholder="e.g. 0.50"
                    style={{ width: '10rem', padding: '0.4rem 0.6rem', border: '1px solid #d1d5db', borderRadius: '0.3rem', fontSize: '0.9rem' }}
                    aria-label="Maximum cost in USD"
                  />
                  <span style={{ fontSize: '0.78rem', color: '#6b7280' }}>
                    Estimates are upper bounds only. Missing pricing is shown as &quot;unknown&quot;, not zero.
                  </span>
                </label>
              </div>
            </div>
          )}
          {isActive('limits') && mode === 'deterministic' && (
            <p style={{ fontSize: '0.9rem', color: '#374151', margin: 0 }}>
              Deterministic mode uses no AI requests and has no cost.
            </p>
          )}
          {isActive('limits') && (
            <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
              <button
                type="button"
                onClick={handleLimitsNext}
                disabled={mode === 'architecture' && preview?.budget?.preflight_ok === false}
                style={{
                  padding: '0.5rem 1rem',
                  background: mode === 'architecture' && preview?.budget?.preflight_ok === false ? '#93c5fd' : '#2563eb',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: mode === 'architecture' && preview?.budget?.preflight_ok === false ? 'not-allowed' : 'pointer',
                }}
              >
                Continue
              </button>
              {mode === 'architecture' && preview?.budget?.preflight_ok === false && (
                <button
                  type="button"
                  onClick={handleNarrowScope}
                  style={{
                    padding: '0.5rem 1rem',
                    background: '#fff',
                    color: '#374151',
                    border: '1px solid #d1d5db',
                    borderRadius: '0.4rem',
                    fontWeight: 600,
                    cursor: 'pointer',
                  }}
                >
                  Change sources
                </button>
              )}
            </div>
          )}
          {isDone('limits') && mode === 'architecture' && (
            <span style={{ fontSize: '0.85rem', color: '#374151' }}>
              Limits: {maxRequests} requests
              {maxTokens ? `, ${parseInt(maxTokens).toLocaleString()} tokens` : ''}
              {maxCostDollars ? `, $${parseFloat(maxCostDollars).toFixed(2)} max cost` : ''}
            </span>
          )}
        </Card>
      )}

      {/* Consent (architecture + external only) */}
      {mode === 'architecture' && preview?.provider_is_external && providerSession && (step === 'consent' || isDone('consent')) && (
        <Card>
          <StepHeader number={7} title="Review and consent" active={isActive('consent')} done={isDone('consent')} />
          {(isActive('consent') || isDone('consent')) && (
            <ConsentPanel
              workspaceId={workspaceId!}
              providerSessionId={providerSession.provider_session_id}
              contextHash={preview.context_hash}
              policyVersion={preview.policy}
              redactedContext={(preview.excerpts ?? []).map(
                ex => `--- ${ex.repository_id}:${ex.path}:${ex.line_start}-${ex.line_end} (redactions: ${ex.redactions}) ---\n${ex.text}`
              ).join('\n\n') || '(No source excerpts included)'}
              onConsent={id => { setConsentReceiptId(id); setStep('submit') }}
              onError={setError}
              onRevoke={() => { setConsentReceiptId(null); client.revokeConsent(consentReceiptId!).catch(() => {}) }}
              receiptId={consentReceiptId}
              grantConsent={client.grantConsent.bind(client)}
            />
          )}
        </Card>
      )}

      {/* Submit */}
      {(step === 'submit' || isDone('submit')) && (
        <Card>
          <StepHeader number={mode === 'architecture' ? 8 : 6} title="Submit job" active={isActive('submit')} done={isDone('submit')} />
          {isActive('submit') && (
            <>
              <p style={{ fontSize: '0.9rem', color: '#374151', margin: 0 }}>
                Submit the job. The same request is idempotent: re-submitting returns the existing job.
              </p>
              {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}
              <button
                type="button"
                onClick={handleSubmit}
                disabled={busy}
                style={{
                  padding: '0.5rem 1rem',
                  background: busy ? '#93c5fd' : '#2563eb',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: busy ? 'not-allowed' : 'pointer',
                  alignSelf: 'flex-start',
                }}
              >
                {busy ? 'Submitting...' : 'Submit job'}
              </button>
            </>
          )}
        </Card>
      )}

      {/* Poll */}
      {submittedJob && step === 'poll' && (
        <Card>
          <StepHeader number={mode === 'architecture' ? 9 : 7} title="Job progress" active={true} done={false} />
          <JobPoller
            initialJob={submittedJob}
            mode={mode}
            onComplete={handleJobComplete}
            getJob={client.getJob.bind(client)}
            cancelJob={client.cancelJob.bind(client)}
          />
        </Card>
      )}

      {/* Done */}
      {step === 'done' && completedJob && (
        <Card>
          <StepHeader number={mode === 'architecture' ? 10 : 8} title="Result" active={false} done={true} />
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
            <StatusBadge state={completedJob.state} stage={completedJob.stage} />
            <span style={{ fontSize: '0.85rem', color: '#6b7280' }}>
              Job {completedJob.job_id.slice(0, 8)}
            </span>
          </div>

          {completedJob.state !== 'succeeded' && (
            <div
              role="alert"
              style={{
                fontSize: '0.85rem',
                background: completedJob.state === 'failed' ? '#fef2f2' : '#fef3c7',
                border: `1px solid ${completedJob.state === 'failed' ? '#fca5a5' : '#f59e0b'}`,
                borderRadius: '0.4rem',
                padding: '0.6rem 0.75rem',
                color: completedJob.state === 'failed' ? '#b91c1c' : '#92400e',
                display: 'flex',
                flexDirection: 'column',
                gap: '0.35rem',
              }}
            >
              <span>
                This result is not current (not a complete success). It may be partial, stale, cancelled, or failed.
                Inspect details before using any output.
              </span>
              {completedJob.error && (
                <span>
                  <strong>{completedJob.error.code}:</strong> {completedJob.error.message}
                </span>
              )}
            </div>
          )}

          <BudgetPreview budget={completedJob.budget} mode={mode} />

          {error && <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>}

          {completedJob.result && (
            <div style={{ fontSize: '0.85rem', color: '#374151' }}>
              <p style={{ margin: '0 0 0.5rem' }}>
                Completeness: <strong>{completedJob.result.completeness}</strong>
              </p>
              {completedJob.result.artifact_ids.length > 0 && (
                <p style={{ margin: '0 0 0.5rem' }}>
                  Artifacts: {completedJob.result.artifact_ids.join(', ')}
                </p>
              )}
              {completedJob.result.warnings.length > 0 && (
                <ul style={{ margin: '0 0 0.5rem 1.25rem', padding: 0, color: '#92400e' }}>
                  {completedJob.result.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}
            </div>
          )}

          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
            {completedJob.result && completedJob.result.artifact_ids.length > 0 && (
              <button
                type="button"
                onClick={() => onViewJob(completedJob.job_id, activeDraftId)}
                style={{
                  padding: '0.5rem 1rem',
                  background: '#2563eb',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                View diagrams
              </button>
            )}
            {completedJob.state !== 'succeeded' && (
              <button
                type="button"
                onClick={handleRetryWithSources}
                disabled={busy}
                style={{
                  padding: '0.5rem 1rem',
                  background: '#fff',
                  color: '#1d4ed8',
                  border: '1px solid #93c5fd',
                  borderRadius: '0.4rem',
                  fontWeight: 600,
                  cursor: busy ? 'not-allowed' : 'pointer',
                }}
              >
                {busy ? 'Revalidating...' : 'Retry with current sources'}
              </button>
            )}
          </div>
        </Card>
      )}

    </main>
  )
}
