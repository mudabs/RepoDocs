import { useCallback, useEffect, useRef, useState } from 'react'
import type { Job, JobState } from '../api/types'
import { StatusBadge, BudgetPreview } from './StatusBadge'

const ACTIVE_STATES: JobState[] = ['queued', 'preflight', 'running']
const TERMINAL_STATES: JobState[] = [
  'succeeded', 'partial', 'budget-exhausted', 'cancelled', 'failed', 'interrupted', 'stale',
]

function isActive(state: JobState): boolean {
  return ACTIVE_STATES.includes(state)
}

function isTerminal(state: JobState): boolean {
  return TERMINAL_STATES.includes(state)
}

interface Props {
  initialJob: Job
  mode: string
  onComplete: (job: Job) => void
  getJob: (jobId: string) => Promise<Job>
  cancelJob: (jobId: string) => Promise<Job>
}

/**
 * Polls for job state with 1s to 5s backoff. Cancel button is shown during active states.
 * Refresh/reconnect does not resubmit; it only reads current state.
 */
export function JobPoller({ initialJob, mode, onComplete, getJob, cancelJob }: Props) {
  const [job, setJob] = useState<Job>(initialJob)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const sequenceRef = useRef(initialJob.sequence)
  const delayRef = useRef(1000)

  const poll = useCallback(async () => {
    try {
      const updated = await getJob(job.job_id)
      setJob(updated)
      sequenceRef.current = updated.sequence
      if (isTerminal(updated.state)) {
        onComplete(updated)
        return
      }
      // Increase poll interval up to 5s.
      delayRef.current = Math.min(delayRef.current * 1.5, 5000)
      intervalRef.current = setTimeout(poll, delayRef.current)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Polling failed')
    }
  }, [job.job_id, getJob, onComplete])

  useEffect(() => {
    if (isTerminal(initialJob.state)) {
      onComplete(initialJob)
      return
    }
    delayRef.current = 1000
    intervalRef.current = setTimeout(poll, delayRef.current)
    return () => {
      if (intervalRef.current !== null) {
        clearTimeout(intervalRef.current)
      }
    }
  }, [initialJob, onComplete, poll])

  async function handleCancel() {
    setCancelling(true)
    try {
      const cancelled = await cancelJob(job.job_id)
      setJob(cancelled)
      if (isTerminal(cancelled.state)) {
        if (intervalRef.current !== null) {
          clearTimeout(intervalRef.current)
        }
        onComplete(cancelled)
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Cancel failed')
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div
      role="region"
      aria-label="Job progress"
      style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
        <StatusBadge state={job.state} stage={job.stage} />
        <span style={{ fontSize: '0.82rem', color: '#6b7280' }}>
          Job {job.job_id.slice(0, 8)}
        </span>
      </div>

      {job.error && (
        <div
          role="alert"
          style={{
            fontSize: '0.85rem',
            color: '#dc2626',
            background: '#fef2f2',
            border: '1px solid #fca5a5',
            borderRadius: '0.4rem',
            padding: '0.5rem 0.75rem',
          }}
        >
          {job.error.message}
          {job.error.retryable && <span style={{ color: '#6b7280' }}> (retryable)</span>}
        </div>
      )}

      {job.result?.warnings && job.result.warnings.length > 0 && (
        <ul
          aria-label="Warnings"
          style={{
            fontSize: '0.82rem',
            color: '#92400e',
            background: '#fffbeb',
            border: '1px solid #f59e0b',
            borderRadius: '0.4rem',
            padding: '0.5rem 0.75rem 0.5rem 1.5rem',
            margin: 0,
          }}
        >
          {job.result.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      <BudgetPreview budget={job.budget} mode={mode} />

      {isActive(job.state) && (
        <button
          type="button"
          onClick={handleCancel}
          disabled={cancelling}
          aria-label="Cancel this job"
          style={{
            padding: '0.4rem 0.9rem',
            background: cancelling ? '#e5e7eb' : '#fee2e2',
            color: cancelling ? '#9ca3af' : '#dc2626',
            border: '1px solid #fca5a5',
            borderRadius: '0.4rem',
            fontWeight: 600,
            fontSize: '0.85rem',
            cursor: cancelling ? 'not-allowed' : 'pointer',
            alignSelf: 'flex-start',
          }}
        >
          {cancelling ? 'Cancelling...' : 'Cancel job'}
        </button>
      )}

      {error && (
        <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>
      )}
    </div>
  )
}
