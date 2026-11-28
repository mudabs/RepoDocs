import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, client } from '../api/client'
import { liveProvider, rememberProvider } from '../api/providerMemory'
import type {
  AnySource,
  Job,
  JobLimits,
  JobMode,
  JobView,
  PreviewResponse,
  ProviderResponse,
  WorkspaceHistoryItem,
} from '../api/types'
import { ProviderForm } from '../components/ProviderForm'
import { StatusBadge } from '../components/StatusBadge'

/**
 * URL-first entry (#99): paste a repository, choose AI or local-only, review what would be disclosed,
 * follow progress, open the result. Every internal step reuses the same HTTP API as the step-by-step
 * workflow, which stays available under Advanced settings.
 */

type Phase =
  | { name: 'idle' }
  | { name: 'provider' }
  | { name: 'working'; stage: number }
  | { name: 'review'; preview: PreviewResponse; workspaceId: string; provider: ProviderResponse }
  | { name: 'blocked'; title: string; detail: string }
  | { name: 'progress'; job: Job }
  | { name: 'finished'; job: Job }

const PREP_STAGES = ['Resolve the repository revision', 'Create the workspace', 'Scan and prepare context'] as const
const JOB_STAGES: Array<{ key: string; label: string }> = [
  { key: 'queued', label: 'Queued' },
  { key: 'scan', label: 'Scan sources' },
  { key: 'context', label: 'Prepare context' },
  { key: 'discover', label: 'Ask the model' },
  { key: 'generate', label: 'Generate diagrams' },
  { key: 'validate', label: 'Validate' },
  { key: 'finished', label: 'Finished' },
]
const TERMINAL = ['succeeded', 'partial', 'budget-exhausted', 'cancelled', 'failed', 'interrupted', 'stale']

function newKey(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function sourceLabel(source: Record<string, unknown>): string {
  if (source.kind === 'github') {
    const rev = typeof source.revision === 'string' ? source.revision.slice(0, 7) : ''
    return `${source.owner}/${source.repository}${rev ? ` @ ${rev}` : ''}`
  }
  const git = source.git as Record<string, unknown> | undefined
  const rev = typeof git?.git_revision === 'string' ? ` @ ${git.git_revision.slice(0, 7)}` : ''
  const dirty = git?.working_tree_state === 'dirty' ? ' (uncommitted changes)' : ''
  return `local: ${String(source.label ?? source.name ?? source.id ?? 'source')}${rev}${dirty}`
}

function sameSource(stored: Array<Record<string, unknown>>, wanted: AnySource): boolean {
  if (stored.length !== 1) return false
  const source = stored[0]
  if (wanted.kind === 'local') return source.kind === 'local' && source.id === wanted.name
  const git = source.git as Record<string, unknown> | undefined
  return source.kind === 'github' && source.label === `${wanted.owner}/${wanted.repository}` && git?.git_revision === wanted.revision
}

function when(seconds: number | undefined): string {
  if (!seconds) return ''
  const date = new Date(seconds * 1000)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}

function usageSummary(job: Job): string {
  const requests = job.budget?.requests ?? 0
  const counts = job.budget?.count_requests ?? 0
  if (requests === 0 && counts === 0) return 'No model request was made.'
  const usage = job.usage
  const tokens = usage && usage.input_tokens !== null && usage.output_tokens !== null
    ? `${(usage.input_tokens + usage.output_tokens).toLocaleString()} tokens${usage.accounting === 'estimated' ? ' (estimated)' : ''}`
    : 'token usage unknown'
  const cost = usage && usage.cost_microunits !== null
    ? `cost ${(usage.cost_microunits / 1_000_000).toFixed(4)} ${usage.currency ?? 'USD'}`
    : 'cost unknown (no price profile)'
  return `Model requests: ${requests}${counts ? `, token-count requests: ${counts}` : ''}; ${tokens}; ${cost}.`
}

function plainOutcome(job: Job): string {
  switch (job.state) {
    case 'succeeded':
      return 'Diagrams are ready.'
    case 'partial':
      return 'Some diagrams were produced, but the result is incomplete. Check the warnings before relying on it.'
    case 'budget-exhausted':
      return 'The run stopped at a usage limit before finishing. Nothing more was sent after the limit.'
    case 'cancelled':
      return 'The run was cancelled. Requests already sent may still have been billed by the provider.'
    case 'interrupted':
      return 'The server stopped during this run. It was not resumed and no request was retried.'
    case 'stale':
      return 'The sources changed after this result was produced, so it no longer matches them.'
    default:
      return 'The run failed and produced no current result.'
  }
}

interface Props {
  onViewJob: (jobId: string) => void
  onAdvanced: () => void
}

export function HomePage({ onViewJob, onAdvanced }: Props) {
  const [repo, setRepo] = useState(() => new URLSearchParams(window.location.search).get('repo') ?? '')
  const [mode, setMode] = useState<JobMode>('architecture')
  const [phase, setPhase] = useState<Phase>({ name: 'idle' })
  const [error, setError] = useState<string | null>(null)
  const [provider, setProvider] = useState<ProviderResponse | null>(null)
  const [consentExternal, setConsentExternal] = useState(false)
  const [consentExcerpts, setConsentExcerpts] = useState(false)
  const [history, setHistory] = useState<WorkspaceHistoryItem[] | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [announce, setAnnounce] = useState('')
  const keyRef = useRef<string>(newKey())
  const headingRef = useRef<HTMLHeadingElement>(null)

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await client.listWorkspaces())
    } catch {
      setHistory([])
    }
  }, [])

  useEffect(() => {
    void refreshHistory()
    void liveProvider().then(setProvider)
  }, [refreshHistory])

  // Move focus to the step heading whenever the phase changes, so keyboard and screen-reader users follow along.
  useEffect(() => {
    if (phase.name !== 'idle') headingRef.current?.focus()
  }, [phase.name])

  function updateRepoQuery(value: string) {
    const url = value ? `/?repo=${encodeURIComponent(value)}` : '/'
    window.history.replaceState(window.history.state, '', url)
  }

  async function prepareSources(): Promise<AnySource[]> {
    const value = repo.trim()
    if (/^https?:\/\//i.test(value) || value.startsWith('github.com/')) {
      const resolved = await client.resolveGitHubUrl(value.startsWith('github.com/') ? `https://${value}` : value)
      return [{ kind: 'github', owner: resolved.owner, repository: resolved.repository, revision: resolved.revision }]
    }
    return [{ kind: 'local', name: value }]
  }

  async function generate(withProvider?: ProviderResponse) {
    setError(null)
    if (!repo.trim()) {
      setError('Enter a GitHub repository URL or an allowlisted local source name.')
      return
    }
    let active: ProviderResponse | null = null
    if (mode === 'architecture') {
      active = withProvider ?? (await liveProvider())
      setProvider(active)
      if (!active) {
        setPhase({ name: 'provider' })
        setAnnounce('Connect an AI provider to continue.')
        return
      }
    }
    updateRepoQuery(repo.trim())
    try {
      setPhase({ name: 'working', stage: 0 })
      setAnnounce(PREP_STAGES[0])
      const sources = await prepareSources()
      setPhase({ name: 'working', stage: 1 })
      setAnnounce(PREP_STAGES[1])
      const first = sources[0]
      const name = first.kind === 'local' ? first.name : `${first.owner}/${first.repository}`
      // Reuse the saved project for the same source and revision; the preview below rescans it either way.
      const saved = (await client.listWorkspaces().catch(() => [] as WorkspaceHistoryItem[]))
        .find(item => sameSource(item.sources, first))
      // A run for this source that is still active (for example after a lost response or a reload) is followed,
      // never submitted a second time.
      if (saved?.latest_job && !TERMINAL.includes(saved.latest_job.state)) {
        follow(saved.latest_job)
        setAnnounce('A run for this repository is already in progress; showing its progress instead of starting another.')
        return
      }
      const workspace = saved ? { workspace_id: saved.workspace_id } : await client.createWorkspace(name, sources)
      setPhase({ name: 'working', stage: 2 })
      setAnnounce(PREP_STAGES[2])
      const preview = await client.preview(workspace.workspace_id, active?.provider_session_id)
      void refreshHistory()
      if (mode === 'architecture' && active) {
        const budget = preview.budget
        if (budget && budget.preflight_ok === false) {
          setPhase({
            name: 'blocked',
            title: 'This request does not fit the configured model limits',
            detail: budget.preflight_error?.message ?? 'The prepared context exceeds the model limits configured by the operator.',
          })
          return
        }
        if (preview.provider_is_external) {
          setConsentExternal(false)
          setConsentExcerpts(false)
          setPhase({ name: 'review', preview, workspaceId: workspace.workspace_id, provider: active })
          setAnnounce('Review what will be sent before generating.')
          return
        }
      }
      await submit(workspace.workspace_id, preview, active, null)
    } catch (err: unknown) {
      setPhase({ name: 'idle' })
      setError(messageOf(err))
      setAnnounce(`Error: ${messageOf(err)}`)
    }
  }

  function limitsFor(preview: PreviewResponse): JobLimits {
    if (mode === 'deterministic') {
      return { max_requests: 0, max_total_tokens: null, max_cost_microunits: 0, currency: 'USD' }
    }
    const limits = preview.budget?.limits ?? {}
    const cost = limits.max_cost_usd
    const costNumber = cost === null || cost === undefined ? null : Number(cost)
    return {
      max_requests: typeof limits.max_requests === 'number' && limits.max_requests > 0 ? limits.max_requests : 3,
      max_total_tokens: typeof limits.max_total_tokens === 'number' ? limits.max_total_tokens : 100000,
      max_cost_microunits: costNumber !== null && Number.isFinite(costNumber) ? Math.round(costNumber * 1_000_000) : null,
      currency: costNumber !== null && Number.isFinite(costNumber) ? 'USD' : null,
    }
  }

  async function submit(workspaceId: string, preview: PreviewResponse, active: ProviderResponse | null, consent: string | null) {
    const views: JobView[] = mode === 'architecture' ? ['overview', 'components', 'erd'] : ['overview', 'erd']
    const { job } = await client.submitJob({
      contract_version: '1.0',
      kind: 'request',
      workspace_id: workspaceId,
      idempotency_key: keyRef.current,
      mode,
      provider_session_id: mode === 'architecture' ? (active?.provider_session_id ?? null) : null,
      context_hash: mode === 'architecture' ? preview.context_hash : null,
      consent_receipt_id: consent,
      views,
      limits: limitsFor(preview),
    })
    keyRef.current = newKey()
    void refreshHistory()
    follow(job)
  }

  async function approveAndGenerate() {
    if (phase.name !== 'review') return
    setError(null)
    const { preview, workspaceId, provider: active } = phase
    try {
      setPhase({ name: 'working', stage: 3 })
      const receipt = await client.grantConsent({
        workspace_id: workspaceId,
        provider_session_id: active.provider_session_id,
        context_hash: preview.context_hash,
        kinds: ['external', 'source-excerpts'],
      })
      await submit(workspaceId, preview, active, receipt.consent_receipt_id)
    } catch (err: unknown) {
      setPhase({ name: 'review', preview, workspaceId, provider: active })
      setError(messageOf(err))
    }
  }

  // ------------------------------------------------------------------ progress
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  function follow(job: Job) {
    if (timerRef.current) clearTimeout(timerRef.current)
    if (TERMINAL.includes(job.state)) {
      setPhase({ name: 'finished', job })
      setAnnounce(plainOutcome(job))
      void refreshHistory()
      return
    }
    setPhase({ name: 'progress', job })
    setAnnounce(`Running: ${job.stage}`)
    let delay = 1000
    const tick = async () => {
      try {
        const next = await client.getJob(job.job_id)
        setError(null)
        if (TERMINAL.includes(next.state)) {
          setPhase({ name: 'finished', job: next })
          setAnnounce(plainOutcome(next))
          void refreshHistory()
          return
        }
        setPhase({ name: 'progress', job: next })
      } catch (err: unknown) {
        const transient = !(err instanceof ApiError) || err.status >= 500 || err.retryable
        if (!transient) {
          setError(messageOf(err))
          return
        }
        setError(`${messageOf(err)}. Retrying...`)
      }
      delay = Math.min(delay * 1.5, 5000)
      timerRef.current = setTimeout(tick, delay)
    }
    timerRef.current = setTimeout(tick, delay)
  }

  async function cancel(job: Job) {
    try {
      follow(await client.cancelJob(job.job_id))
    } catch (err: unknown) {
      setError(messageOf(err))
    }
  }

  async function deleteWorkspace(id: string) {
    setConfirmDelete(null)
    try {
      await client.deleteWorkspace(id)
      setAnnounce('Saved project deleted.')
    } catch (err: unknown) {
      setError(messageOf(err))
    }
    void refreshHistory()
  }

  function reset() {
    if (timerRef.current) clearTimeout(timerRef.current)
    setPhase({ name: 'idle' })
    setError(null)
  }

  const busy = phase.name === 'working'

  return (
    <div className="rd-home">
      <div className="rd-stack">
        <section className="rd-card" aria-labelledby="generate-heading">
          <h1 id="generate-heading" className="rd-title">Generate architecture diagrams</h1>
          <p className="rd-muted">
            Paste a public GitHub repository. RepoDocs pins the exact commit, reads the source without running it,
            and draws evidence-linked diagrams.
          </p>
          <form
            className="rd-stack"
            onSubmit={e => {
              e.preventDefault()
              void generate()
            }}
          >
            <div className="rd-field">
              <label className="rd-label" htmlFor="repo-url">Repository URL</label>
              <input
                id="repo-url"
                className="rd-input rd-input-lg"
                type="text"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                value={repo}
                onChange={e => setRepo(e.target.value)}
                placeholder="https://github.com/owner/repository"
                aria-describedby="repo-help"
                disabled={busy}
              />
              <span id="repo-help" className="rd-muted">
                Public github.com repositories. Operators can also allow local sources by name.
              </span>
            </div>
            <fieldset className="rd-stack" style={{ border: 'none', padding: 0, margin: 0 }} disabled={busy}>
              <legend className="rd-label">How to generate</legend>
              <label className="rd-choice">
                <input type="radio" name="mode" checked={mode === 'architecture'} onChange={() => setMode('architecture')} />
                <span>
                  <strong>AI architecture</strong>
                  <span className="rd-muted" style={{ display: 'block' }}>
                    A model you choose groups the code into components and connections. You review what is sent first.
                  </span>
                </span>
              </label>
              <label className="rd-choice">
                <input type="radio" name="mode" checked={mode === 'deterministic'} onChange={() => setMode('deterministic')} />
                <span>
                  <strong>Local facts only</strong>
                  <span className="rd-muted" style={{ display: 'block' }}>
                    No model and nothing leaves this machine. Shows languages, frameworks and tables, with less architectural depth.
                  </span>
                </span>
              </label>
            </fieldset>
            {mode === 'architecture' && provider && phase.name === 'idle' && (
              <p className="rd-muted">
                Using {provider.provider} model <code>{provider.model}</code> at <code>{provider.host}</code>.{' '}
                <button type="button" className="rd-btn-link" onClick={() => setPhase({ name: 'provider' })}>Change</button>
              </p>
            )}
            <div className="rd-row">
              <button type="submit" className="rd-btn rd-btn-primary rd-btn-lg" disabled={busy || phase.name === 'progress'}>
                {mode === 'architecture' ? 'Generate architecture' : 'Generate local diagrams'}
              </button>
              <button type="button" className="rd-btn-link" onClick={onAdvanced}>
                Advanced settings
              </button>
            </div>
          </form>
          {error && phase.name !== 'review' && (
            <div role="alert" className="rd-alert rd-alert-error">{error}</div>
          )}
        </section>

        {phase.name === 'provider' && (
          <section className="rd-card" aria-labelledby="provider-heading">
            <h2 id="provider-heading" className="rd-h2" tabIndex={-1} ref={headingRef}>Connect an AI provider</h2>
            <p className="rd-muted">
              {provider === null
                ? 'No provider is connected in this browser session. Keys stay in server memory for this session only and are never saved.'
                : 'Choose a different provider or model.'}
            </p>
            <ProviderForm
              connected={false}
              error={null}
              onConnect={async opts => {
                const session = await client.connectProvider(opts)
                rememberProvider(session)
                setProvider(session)
                setPhase({ name: 'idle' })
                await generate(session)
              }}
            />
            <div className="rd-row">
              <button type="button" className="rd-btn" onClick={reset}>Back</button>
            </div>
          </section>
        )}

        {phase.name === 'working' && (
          <section className="rd-card" aria-labelledby="working-heading" aria-busy="true">
            <h2 id="working-heading" className="rd-h2" tabIndex={-1} ref={headingRef}>Preparing</h2>
            <ol className="rd-stages">
              {[...PREP_STAGES, 'Record consent and submit'].map((label, index) => {
                const state = index < phase.stage ? 'done' : index === phase.stage ? 'active' : 'pending'
                if (index === 3 && phase.stage < 3) return null
                return (
                  <li key={label} data-state={state}>
                    <span className="rd-stage-mark" aria-hidden="true">{state === 'done' ? '✓' : state === 'active' ? '•' : '○'}</span>
                    {label}
                    <span className="rd-visually-hidden">{state === 'done' ? ' (done)' : state === 'active' ? ' (in progress)' : ''}</span>
                  </li>
                )
              })}
            </ol>
            <p className="rd-muted">No model request is made in these steps.</p>
          </section>
        )}

        {phase.name === 'review' && (
          <section className="rd-card" aria-labelledby="review-heading">
            <h2 id="review-heading" className="rd-h2" tabIndex={-1} ref={headingRef}>Review what will be sent</h2>
            <p className="rd-muted">
              Generating will send redacted source excerpts to an external provider. Nothing has been sent yet.
            </p>
            <dl className="rd-facts">
              <dt>Provider</dt>
              <dd>{phase.provider.provider}, model <code>{phase.provider.model}</code></dd>
              <dt>Destination</dt>
              <dd><code>{phase.provider.host}</code></dd>
              <dt>Sources</dt>
              <dd>
                {phase.preview.snapshots.map(s => `${s.repository_id} @ ${s.revision.slice(0, 12)}`).join(', ')}
              </dd>
              <dt>Excerpts</dt>
              <dd>
                {phase.preview.excerpts?.length ?? phase.preview.documents} excerpts,{' '}
                {phase.preview.context_bytes.toLocaleString()} bytes, {phase.preview.redactions} redactions
              </dd>
              <dt>Limits</dt>
              <dd>
                {(() => {
                  const l = limitsFor(phase.preview)
                  const cost = phase.preview.budget?.max_estimated_cost_usd
                  return `Up to ${l.max_requests} requests and ${(l.max_total_tokens ?? 0).toLocaleString()} tokens. ` +
                    (cost ? `Estimated worst case $${cost}.` : 'Cost is unknown: no price profile is configured.')
                })()}
              </dd>
            </dl>
            {phase.preview.warnings.length > 0 && (
              <div className="rd-alert rd-alert-warn">
                <strong>Context warnings</strong>
                <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.2rem' }}>
                  {phase.preview.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}
            <details>
              <summary>Show the exact excerpts</summary>
              <div className="rd-excerpts" tabIndex={0} aria-label="Redacted source excerpts">
                {(phase.preview.excerpts ?? []).map(ex =>
                  `--- ${ex.repository_id}:${ex.path}:${ex.line_start}-${ex.line_end} (redactions: ${ex.redactions}) ---\n${ex.text}`,
                ).join('\n\n') || '(No excerpts included)'}
              </div>
            </details>
            <label className="rd-choice">
              <input
                type="checkbox"
                checked={consentExternal}
                onChange={e => setConsentExternal(e.target.checked)}
                aria-label="I consent to sending data to an external provider"
              />
              <span>Send the redacted request to {phase.provider.host}</span>
            </label>
            <label className="rd-choice">
              <input
                type="checkbox"
                checked={consentExcerpts}
                onChange={e => setConsentExcerpts(e.target.checked)}
                aria-label="I consent to sharing source excerpts"
              />
              <span>Include the source excerpts shown above</span>
            </label>
            {error && <div role="alert" className="rd-alert rd-alert-error">{error}</div>}
            <div className="rd-row">
              <button
                type="button"
                className="rd-btn rd-btn-primary"
                disabled={!consentExternal || !consentExcerpts}
                onClick={() => { void approveAndGenerate() }}
              >
                Send and generate
              </button>
              <button type="button" className="rd-btn" onClick={reset}>Cancel</button>
            </div>
          </section>
        )}

        {phase.name === 'blocked' && (
          <section className="rd-card" aria-labelledby="blocked-heading">
            <h2 id="blocked-heading" className="rd-h2" tabIndex={-1} ref={headingRef}>{phase.title}</h2>
            <div className="rd-alert rd-alert-error" role="alert">{phase.detail}</div>
            <p className="rd-muted">
              Nothing was sent to the model. Narrow the sources in Advanced settings, or ask the operator to configure
              the model's real context window and output limits.
            </p>
            <div className="rd-row">
              <button type="button" className="rd-btn rd-btn-primary" onClick={onAdvanced}>Open Advanced settings</button>
              <button type="button" className="rd-btn" onClick={reset}>Back</button>
            </div>
          </section>
        )}

        {(phase.name === 'progress' || phase.name === 'finished') && (
          <section className="rd-card" aria-labelledby="progress-heading">
            <div className="rd-row" style={{ justifyContent: 'space-between' }}>
              <h2 id="progress-heading" className="rd-h2" tabIndex={-1} ref={headingRef}>
                {phase.name === 'progress' ? 'Generating' : 'Result'}
              </h2>
              <StatusBadge state={phase.job.state} stage={phase.job.stage} />
            </div>
            {phase.name === 'progress' && (
              <ol className="rd-stages" aria-label="Job stages">
                {JOB_STAGES.filter(s => mode === 'architecture' || s.key !== 'discover').map(s => {
                  const order = JOB_STAGES.findIndex(x => x.key === s.key)
                  const current = JOB_STAGES.findIndex(x => x.key === phase.job.stage)
                  const state = order < current ? 'done' : order === current ? 'active' : 'pending'
                  return (
                    <li key={s.key} data-state={state}>
                      <span className="rd-stage-mark" aria-hidden="true">{state === 'done' ? '✓' : state === 'active' ? '•' : '○'}</span>
                      {s.label}
                    </li>
                  )
                })}
              </ol>
            )}
            {phase.name === 'finished' && (
              <div className={`rd-alert ${phase.job.state === 'succeeded' ? 'rd-alert-ok' : 'rd-alert-warn'}`}>
                {plainOutcome(phase.job)}
                {phase.job.error && <p>{phase.job.error.message}</p>}
              </div>
            )}
            <p className="rd-muted">{usageSummary(phase.job)}</p>
            {error && <div role="alert" className="rd-alert rd-alert-error">{error}</div>}
            <div className="rd-row">
              {phase.name === 'progress' && (
                <button type="button" className="rd-btn rd-btn-danger" onClick={() => { void cancel(phase.job) }}>
                  Cancel generation
                </button>
              )}
              {phase.name === 'finished' && (phase.job.result?.artifact_ids?.length ?? 0) > 0 && (
                <button type="button" className="rd-btn rd-btn-primary" onClick={() => onViewJob(phase.job.job_id)}>
                  View diagrams
                </button>
              )}
              {phase.name === 'finished' && (
                <button type="button" className="rd-btn" onClick={reset}>Start another</button>
              )}
            </div>
          </section>
        )}
      </div>

      <aside className="rd-card" aria-labelledby="history-heading">
        <div className="rd-row" style={{ justifyContent: 'space-between' }}>
          <h2 id="history-heading" className="rd-h2">Saved projects</h2>
          <button type="button" className="rd-btn-link" onClick={() => { void refreshHistory() }}>Refresh list</button>
        </div>
        <p className="rd-muted">Opening a saved result never runs the model again.</p>
        {history === null && <p className="rd-muted" role="status">Loading...</p>}
        {history !== null && history.length === 0 && <p className="rd-muted">Nothing saved yet.</p>}
        {history !== null && history.length > 0 && (
          <ul className="rd-history" aria-label="Saved projects">
            {history.map(item => {
              const job = item.latest_job
              return (
                <li key={item.workspace_id}>
                  <span className="rd-history-name">{item.name}</span>
                  <span className="rd-muted">{item.sources.map(sourceLabel).join(', ')}</span>
                  {job ? (
                    <div className="rd-row">
                      <StatusBadge state={job.state} stage={job.stage} compact />
                      <span className="rd-muted">{when(item.updated_at)}</span>
                    </div>
                  ) : (
                    <span className="rd-muted">No run yet</span>
                  )}
                  <div className="rd-row">
                    {job && (job.result?.artifact_ids?.length ?? 0) > 0 && (
                      <button type="button" className="rd-btn" onClick={() => onViewJob(job.job_id)}
                        aria-label={`Open result for ${item.name}`}>
                        Open result
                      </button>
                    )}
                    {job && !TERMINAL.includes(job.state) && (
                      <button type="button" className="rd-btn" onClick={() => follow(job)}
                        aria-label={`Show progress for ${item.name}`}>
                        Show progress
                      </button>
                    )}
                    {confirmDelete === item.workspace_id ? (
                      <>
                        <button type="button" className="rd-btn rd-btn-danger" onClick={() => { void deleteWorkspace(item.workspace_id) }}
                          aria-label={`Permanently delete ${item.name}`}>
                          Delete permanently
                        </button>
                        <button type="button" className="rd-btn" onClick={() => setConfirmDelete(null)}>Keep</button>
                      </>
                    ) : (
                      <button type="button" className="rd-btn-link" onClick={() => setConfirmDelete(item.workspace_id)}
                        aria-label={`Delete ${item.name}`}>
                        Delete
                      </button>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </aside>

      <div role="status" aria-live="polite" className="rd-visually-hidden">{announce}</div>
    </div>
  )
}
