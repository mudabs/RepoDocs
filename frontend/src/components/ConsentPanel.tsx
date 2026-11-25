import { useState } from 'react'

interface Props {
  workspaceId: string
  providerSessionId: string
  contextHash: string
  policyVersion: string
  redactedContext: string
  onConsent: (receiptId: string) => void
  onError: (msg: string) => void
  onRevoke?: () => void
  receiptId: string | null
  grantConsent: (opts: {
    workspace_id: string
    provider_session_id: string
    context_hash: string
    kinds: string[]
  }) => Promise<{ consent_receipt_id: string }>
}

/**
 * Separate consent checkboxes for external provider use and source-excerpt disclosure.
 * Consent is submitted only when both required kinds are checked.
 */
export function ConsentPanel({
  workspaceId,
  providerSessionId,
  contextHash,
  policyVersion,
  redactedContext,
  onConsent,
  onError,
  onRevoke,
  receiptId,
  grantConsent,
}: Props) {
  const [consentExternal, setConsentExternal] = useState(false)
  const [consentExcerpts, setConsentExcerpts] = useState(false)
  const [busy, setBusy] = useState(false)
  const [showContext, setShowContext] = useState(false)

  const canSubmit = consentExternal && consentExcerpts && !receiptId

  if (receiptId) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <span style={{ color: '#16a34a', fontWeight: 500, fontSize: '0.9rem' }}>
          Consent granted
        </span>
        {onRevoke && (
          <button
            type="button"
            onClick={onRevoke}
            style={{ fontSize: '0.82rem', color: '#6b7280', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
          >
            Revoke
          </button>
        )}
      </div>
    )
  }

  async function handleGrant() {
    if (!canSubmit) return
    setBusy(true)
    try {
      const kinds: string[] = []
      if (consentExternal) kinds.push('external')
      if (consentExcerpts) kinds.push('source-excerpts')
      const result = await grantConsent({
        workspace_id: workspaceId,
        provider_session_id: providerSessionId,
        context_hash: contextHash,
        kinds,
      })
      onConsent(result.consent_receipt_id)
    } catch (err: unknown) {
      onError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      role="region"
      aria-label="Consent"
      style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}
    >
      <div
        style={{
          fontSize: '0.82rem',
          color: '#374151',
          background: '#fef3c7',
          border: '1px solid #f59e0b',
          borderRadius: '0.4rem',
          padding: '0.6rem 0.75rem',
        }}
      >
        <strong>Before submitting, review what will be sent.</strong>
        <ul style={{ margin: '0.4rem 0 0 1rem', padding: 0 }}>
          <li>Source code excerpts will be sent to your selected provider.</li>
          <li>Your provider is external to this service. Its terms apply.</li>
          <li>Policy version: <code>{policyVersion}</code></li>
          <li>Context fingerprint: <code>{contextHash.slice(0, 12)}...</code></li>
        </ul>
        <button
          type="button"
          onClick={() => setShowContext(v => !v)}
          style={{ marginTop: '0.5rem', fontSize: '0.78rem', color: '#2563eb', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
        >
          {showContext ? 'Hide redacted context' : 'Show redacted context'}
        </button>
        {showContext && (
          <pre
            aria-label="Redacted context preview"
            style={{
              marginTop: '0.5rem',
              maxHeight: '12rem',
              overflowY: 'auto',
              fontSize: '0.75rem',
              background: '#f9fafb',
              border: '1px solid #e5e7eb',
              borderRadius: '0.3rem',
              padding: '0.5rem',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {redactedContext}
          </pre>
        )}
      </div>

      <label style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem', cursor: 'pointer', fontSize: '0.9rem' }}>
        <input
          type="checkbox"
          checked={consentExternal}
          onChange={e => setConsentExternal(e.target.checked)}
          style={{ marginTop: '0.15rem' }}
          aria-label="I consent to sending data to an external provider"
        />
        <span>
          I understand that source excerpts will be sent to an external AI provider under my own account and at my own cost.
        </span>
      </label>

      <label style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem', cursor: 'pointer', fontSize: '0.9rem' }}>
        <input
          type="checkbox"
          checked={consentExcerpts}
          onChange={e => setConsentExcerpts(e.target.checked)}
          style={{ marginTop: '0.15rem' }}
          aria-label="I consent to sharing source excerpts"
        />
        <span>
          I consent to sharing the source-code excerpts shown above with the selected provider for this job.
        </span>
      </label>

      <button
        type="button"
        onClick={handleGrant}
        disabled={!canSubmit || busy}
        style={{
          padding: '0.5rem 1rem',
          background: canSubmit && !busy ? '#2563eb' : '#e5e7eb',
          color: canSubmit && !busy ? '#fff' : '#9ca3af',
          border: 'none',
          borderRadius: '0.4rem',
          fontWeight: 600,
          fontSize: '0.9rem',
          cursor: canSubmit && !busy ? 'pointer' : 'not-allowed',
          alignSelf: 'flex-start',
        }}
        aria-disabled={!canSubmit || busy}
      >
        {busy ? 'Granting consent...' : 'Grant consent and continue'}
      </button>
    </div>
  )
}
