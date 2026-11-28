import type { Job, JobState } from '../api/types'

interface Props {
  state: JobState
  stage?: string
  compact?: boolean
}

const STATE_LABELS: Record<JobState, string> = {
  queued: 'Queued',
  preflight: 'Preflight',
  running: 'Running',
  succeeded: 'Succeeded',
  partial: 'Partial result',
  'budget-exhausted': 'Budget exhausted',
  cancelled: 'Cancelled',
  failed: 'Failed',
  interrupted: 'Interrupted',
  stale: 'Stale',
}

const STATE_COLORS: Record<JobState, string> = {
  queued: '#6b7280',
  preflight: '#6b7280',
  running: '#2563eb',
  succeeded: '#16a34a',
  partial: '#d97706',
  'budget-exhausted': '#d97706',
  cancelled: '#6b7280',
  failed: '#dc2626',
  interrupted: '#dc2626',
  stale: '#d97706',
}

export function StatusBadge({ state, stage, compact }: Props) {
  const label = STATE_LABELS[state] ?? state
  const color = STATE_COLORS[state] ?? '#6b7280'
  const isActive = state === 'queued' || state === 'preflight' || state === 'running'
  const isSuccess = state === 'succeeded'
  const isNonCurrent = state === 'partial' || state === 'budget-exhausted' || state === 'stale'
  const isError = state === 'failed' || state === 'interrupted'

  return (
    <span
      aria-label={`Job state: ${label}${stage && !compact ? `, stage: ${stage}` : ''}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '0.35rem',
        padding: '0.2rem 0.6rem',
        borderRadius: '999px',
        fontSize: '0.78rem',
        fontWeight: 600,
        background: color + '22',
        color,
        border: `1px solid ${color}55`,
        userSelect: 'none',
      }}
    >
      {isActive && (
        <span
          aria-hidden="true"
          style={{
            width: '0.5rem',
            height: '0.5rem',
            borderRadius: '50%',
            background: color,
            animation: 'pulse 1.4s infinite',
          }}
        />
      )}
      {label}
      {stage && !compact && isActive && (
        <span style={{ fontWeight: 400, opacity: 0.8 }}> ({stage})</span>
      )}
      {isNonCurrent && !compact && (
        <span
          title="This result is not current or complete"
          style={{ fontWeight: 400, fontSize: '0.72rem', marginLeft: '0.2rem' }}
        >
          (not current)
        </span>
      )}
      {isSuccess && !compact && (
        <span aria-hidden="true"> ✓</span>
      )}
      {isError && !compact && (
        <span aria-hidden="true"> ✗</span>
      )}
    </span>
  )
}

interface BudgetProps {
  budget: Job['budget']
  mode: string
}

export function BudgetPreview({ budget, mode }: BudgetProps) {
  if (mode === 'deterministic' || budget === null) {
    return null
  }

  function fmt(val: number | null, suffix = ''): string {
    if (val === null) return 'unknown'
    return val.toLocaleString() + suffix
  }

  function fmtCost(val: number | null): string {
    if (val === null) return 'unknown'
    // microunits: 1 USD = 1,000,000 microunits
    const dollars = val / 1_000_000
    return `~$${dollars.toFixed(4)}`
  }

  return (
    <div
      role="region"
      aria-label="Budget usage"
      style={{
        fontSize: '0.82rem',
        color: '#374151',
        background: '#f9fafb',
        border: '1px solid #e5e7eb',
        borderRadius: '0.4rem',
        padding: '0.5rem 0.75rem',
        display: 'grid',
        gridTemplateColumns: 'auto 1fr',
        gap: '0.15rem 0.75rem',
      }}
    >
      <span style={{ color: '#6b7280' }}>Generation requests:</span>
      <span>{budget.requests} (remaining: {budget.remaining_requests})</span>
      <span style={{ color: '#6b7280' }}>Token-count requests:</span>
      <span>{budget.count_requests}</span>
      <span style={{ color: '#6b7280' }}>Tokens remaining:</span>
      <span>{fmt(budget.remaining_total_tokens)}</span>
      <span style={{ color: '#6b7280' }}>Cost remaining:</span>
      <span>{fmtCost(budget.remaining_cost_microunits)}</span>
      <span style={{ color: '#6b7280' }}>Usage accounting:</span>
      <span>{budget.usage_complete ? 'complete' : 'includes reserved/unknown bounds'}</span>
      {budget.stop_reason && (
        <>
          <span style={{ color: '#6b7280' }}>Stop reason:</span>
          <span style={{ color: '#d97706' }}>{budget.stop_reason}</span>
        </>
      )}
    </div>
  )
}
