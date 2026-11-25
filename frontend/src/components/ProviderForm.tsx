import { useEffect, useRef, useState } from 'react'
import type { ProviderKind } from '../api/types'

interface Props {
  onConnect: (opts: {
    provider: ProviderKind
    host: string
    model: string
    api_key?: string
  }) => Promise<void>
  onDisconnect?: () => void
  connected: boolean
  error: string | null
}

/**
 * Ephemeral provider credential form. The api_key field is never written to any storage.
 * Keys are cleared from component state on unmount and after a successful connect.
 */
export function ProviderForm({ onConnect, onDisconnect, connected, error }: Props) {
  const [kind, setKind] = useState<ProviderKind>('ollama')
  const [host, setHost] = useState('http://127.0.0.1:11434')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const apiKeyRef = useRef<HTMLInputElement>(null)

  // Clear the api_key from state on unmount to avoid lingering in memory.
  useEffect(() => {
    return () => {
      setApiKey('')
    }
  }, [])

  function handleKindChange(next: ProviderKind) {
    setKind(next)
    setApiKey('')
    setHost(next === 'ollama' ? 'http://127.0.0.1:11434' : 'https://api.openai.com/v1')
    setModel(next === 'ollama' ? '' : 'gpt-4o-mini')
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      const opts: Parameters<Props['onConnect']>[0] = { provider: kind, host, model }
      if (kind === 'openai' && apiKey) {
        opts.api_key = apiKey
      }
      await onConnect(opts)
      // Clear the key from state immediately after handing it to the caller.
      setApiKey('')
      if (apiKeyRef.current) {
        apiKeyRef.current.value = ''
      }
    } finally {
      setBusy(false)
    }
  }

  if (connected) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <span style={{ color: '#16a34a', fontWeight: 500 }}>Provider connected</span>
        {onDisconnect && (
          <button
            type="button"
            onClick={onDisconnect}
            style={{ fontSize: '0.82rem', color: '#6b7280', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}
          >
            Disconnect
          </button>
        )}
      </div>
    )
  }

  return (
    <form onSubmit={handleSubmit} aria-label="Provider connection form" style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      <div role="group" aria-label="Provider type" style={{ display: 'flex', gap: '1rem' }}>
        {(['ollama', 'openai'] as ProviderKind[]).map(k => (
          <label key={k} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer' }}>
            <input
              type="radio"
              name="provider-kind"
              value={k}
              checked={kind === k}
              onChange={() => handleKindChange(k)}
            />
            <span>
              {k === 'ollama' ? 'Local Ollama' : 'OpenAI (bring your own key)'}
            </span>
          </label>
        ))}
      </div>

      {kind === 'openai' && (
        <div
          role="note"
          style={{
            fontSize: '0.8rem',
            color: '#374151',
            background: '#fffbeb',
            border: '1px solid #f59e0b',
            borderRadius: '0.4rem',
            padding: '0.5rem 0.75rem',
          }}
        >
          Your OpenAI API key is sent directly to this local service only, used for one job,
          and not stored anywhere. You are responsible for any usage charges on your key.
        </div>
      )}

      {kind === 'ollama' && (
        <div
          role="note"
          style={{
            fontSize: '0.8rem',
            color: '#374151',
            background: '#f0fdf4',
            border: '1px solid #86efac',
            borderRadius: '0.4rem',
            padding: '0.5rem 0.75rem',
          }}
        >
          Ollama must be running locally on your machine. Only loopback addresses are supported
          for the web UI. Authenticated remote Ollama gateways require the CLI.
        </div>
      )}

      <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Host</span>
        <input
          type="url"
          value={host}
          onChange={e => setHost(e.target.value)}
          required
          style={{ padding: '0.4rem 0.6rem', borderRadius: '0.3rem', border: '1px solid #d1d5db', fontSize: '0.9rem' }}
          aria-label="Provider host URL"
        />
      </label>

      <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Model</span>
        <input
          type="text"
          value={model}
          onChange={e => setModel(e.target.value)}
          required
          placeholder={kind === 'ollama' ? 'e.g. llama3.2' : 'gpt-4o-mini'}
          style={{ padding: '0.4rem 0.6rem', borderRadius: '0.3rem', border: '1px solid #d1d5db', fontSize: '0.9rem' }}
          aria-label="Model name"
        />
      </label>

      {kind === 'openai' && (
        <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
          <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>API Key</span>
          <input
            ref={apiKeyRef}
            type="password"
            value={apiKey}
            onChange={e => setApiKey(e.target.value)}
            required
            autoComplete="off"
            placeholder="sk-..."
            style={{ padding: '0.4rem 0.6rem', borderRadius: '0.3rem', border: '1px solid #d1d5db', fontSize: '0.9rem' }}
            aria-label="OpenAI API key (not stored)"
          />
        </label>
      )}

      {error && (
        <div role="alert" style={{ color: '#dc2626', fontSize: '0.85rem' }}>{error}</div>
      )}

      <button
        type="submit"
        disabled={busy}
        style={{
          padding: '0.5rem 1rem',
          background: busy ? '#93c5fd' : '#2563eb',
          color: '#fff',
          border: 'none',
          borderRadius: '0.4rem',
          fontWeight: 600,
          fontSize: '0.9rem',
          cursor: busy ? 'not-allowed' : 'pointer',
          alignSelf: 'flex-start',
        }}
      >
        {busy ? 'Connecting...' : 'Connect provider'}
      </button>
    </form>
  )
}
