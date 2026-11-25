/**
 * Typed API client for the RepoDocs local HTTP adapter.
 *
 * Session cookie is sent via credentials:'include'. The CSRF token is taken from session state
 * and sent as the x-csrf-token header on every mutating request. Provider keys are never stored
 * in localStorage, sessionStorage, URLs, or logs.
 */

import type {
  AnySource,
  ConsentResponse,
  Job,
  JobRequest,
  PreviewResponse,
  ProviderResponse,
  SessionResponse,
  WorkspaceResponse,
} from './types'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly retryable: boolean,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let code = 'operation-failure'
  let message = `HTTP ${response.status}`
  let retryable = response.status === 429 || response.status === 503
  try {
    const body = await response.json()
    if (body?.error?.code) {
      code = body.error.code
      message = body.error.message ?? message
      retryable = body.error.retryable ?? retryable
    }
  } catch {
    // ignore JSON parse errors; use defaults above
  }
  return new ApiError(response.status, code, message, retryable)
}

async function request<T>(
  method: string,
  path: string,
  csrfToken: string | null,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  if (csrfToken) {
    headers['x-csrf-token'] = csrfToken
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'include',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    throw await parseError(response)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return response.json() as Promise<T>
}

export class RepoDocsClient {
  private csrfToken: string | null = null

  async openSession(authToken?: string): Promise<void> {
    const body = authToken ? { auth_token: authToken } : {}
    const data = await request<SessionResponse>('POST', '/api/v1/session', null, body)
    this.csrfToken = data.csrf_token
  }

  async endSession(): Promise<void> {
    await request<void>('POST', '/api/v1/session/end', this.csrfToken)
    this.csrfToken = null
  }

  async createWorkspace(name: string, sources: AnySource[]): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>('POST', '/api/v1/workspaces', this.csrfToken, { name, sources })
  }

  async preview(
    workspaceId: string,
    providerSessionId?: string,
    includeDocuments = true,
  ): Promise<PreviewResponse> {
    const body: Record<string, unknown> = { include_documents: includeDocuments }
    if (providerSessionId) {
      body['provider_session_id'] = providerSessionId
    }
    return request<PreviewResponse>('POST', `/api/v1/workspaces/${workspaceId}/preview`, this.csrfToken, body)
  }

  /**
   * Connect a provider. The api_key is passed directly from the caller and never stored here.
   * Pass api_key only for the duration of this call; discard it in the caller immediately after.
   */
  async connectProvider(opts: {
    provider: string
    host: string
    model: string
    api_key?: string
  }): Promise<ProviderResponse> {
    return request<ProviderResponse>('POST', '/api/v1/providers', this.csrfToken, opts)
  }

  async getProvider(handle: string): Promise<ProviderResponse> {
    return request<ProviderResponse>('GET', `/api/v1/providers/${handle}`, this.csrfToken)
  }

  async disconnectProvider(handle: string): Promise<void> {
    return request<void>('POST', `/api/v1/providers/${handle}/disconnect`, this.csrfToken)
  }

  async grantConsent(opts: {
    workspace_id: string
    provider_session_id: string
    context_hash: string
    kinds: string[]
  }): Promise<ConsentResponse> {
    return request<ConsentResponse>('POST', '/api/v1/consents', this.csrfToken, opts)
  }

  async revokeConsent(receiptId: string): Promise<void> {
    return request<void>('POST', `/api/v1/consents/${receiptId}/revoke`, this.csrfToken)
  }

  async submitJob(jobRequest: JobRequest): Promise<{ job: Job; created: boolean }> {
    const response = await fetch('/api/v1/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(this.csrfToken ? { 'x-csrf-token': this.csrfToken } : {}),
      },
      credentials: 'include',
      body: JSON.stringify(jobRequest),
    })
    if (!response.ok) {
      throw await parseError(response)
    }
    const job = (await response.json()) as Job
    return { job, created: response.status === 202 }
  }

  async getJob(jobId: string): Promise<Job> {
    return request<Job>('GET', `/api/v1/jobs/${jobId}`, this.csrfToken)
  }

  async cancelJob(jobId: string): Promise<Job> {
    return request<Job>('POST', `/api/v1/jobs/${jobId}/cancel`, this.csrfToken)
  }

  /** Returns the artifact URL for direct download via an anchor tag. */
  artifactUrl(jobId: string, artifactId: string): string {
    return `/api/v1/jobs/${jobId}/artifacts/${artifactId}`
  }

  /** Fetches artifact content as text (for diagram rendering). */
  async fetchArtifactText(jobId: string, artifactId: string): Promise<string> {
    const response = await fetch(this.artifactUrl(jobId, artifactId), {
      credentials: 'include',
      headers: this.csrfToken ? { 'x-csrf-token': this.csrfToken } : {},
    })
    if (!response.ok) {
      throw await parseError(response)
    }
    return response.text()
  }
}

/** Singleton client for the application lifetime of one browser session. */
export const client = new RepoDocsClient()
