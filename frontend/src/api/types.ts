/** TypeScript types matching the web-workflow-1.0.json contract and API responses. */

export type JobMode = 'deterministic' | 'architecture'
export type JobView = 'overview' | 'components' | 'erd'
export type JobState =
  | 'queued'
  | 'preflight'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'budget-exhausted'
  | 'cancelled'
  | 'failed'
  | 'interrupted'
  | 'stale'
export type JobStage = 'queued' | 'scan' | 'context' | 'discover' | 'generate' | 'validate' | 'finished'

export type ProviderKind = 'ollama' | 'openai'

export interface JobLimits {
  max_requests: number
  max_total_tokens: number | null
  max_cost_microunits: number | null
  currency: 'USD' | null
}

export interface JobBudget {
  requests: number
  count_requests: number
  remaining_requests: number
  remaining_total_tokens: number | null
  remaining_cost_microunits: number | null
  max_estimated_cost_microunits: number | null
  usage_complete: boolean
  stop_reason: string | null
  stop_message: string | null
}

export interface JobError {
  code: string
  message: string
  retryable: boolean
}

export interface JobResult {
  completeness: 'complete' | 'partial'
  view_hash: string | null
  source_ir_hash: string
  artifact_ids: string[]
  warnings: string[]
}

export interface JobUsage {
  requests: number
  prompt_tokens: number | null
  output_tokens: number | null
  cost_microunits: number | null
  currency: string | null
}

export interface Job {
  contract_version: string
  kind: 'job'
  job_id: string
  workspace_id: string
  state: JobState
  stage: JobStage
  sequence: number
  created_at: string
  updated_at: string
  error: JobError | null
  result: JobResult | null
  budget: JobBudget | null
  usage: JobUsage | null
}

export interface JobRequest {
  contract_version: '1.0'
  kind: 'request'
  workspace_id: string
  idempotency_key: string
  mode: JobMode
  provider_session_id: string | null
  context_hash: string | null
  consent_receipt_id: string | null
  views: JobView[]
  limits: JobLimits
  refresh?: boolean
}

export interface ApiError {
  contract_version: string
  kind: 'error'
  error: {
    code: string
    message: string
    retryable: boolean
  }
}

export interface WorkspaceSource {
  kind: 'local'
  name: string
}

export interface GitHubSource {
  kind: 'github'
  owner: string
  repository: string
  revision: string
}

export interface ResolvedGitHubSource extends GitHubSource {
  default_branch: string
  canonical_url: string
}

export type AnySource = WorkspaceSource | GitHubSource

export interface WorkspaceResponse {
  workspace_id: string
  sources: Array<Record<string, unknown>>
}

export interface PreviewExcerpt {
  repository_id: string
  path: string
  line_start: number
  line_end: number
  redactions: number
  text: string
}

export interface PreviewBudget {
  limits: {
    max_requests?: number
    max_total_tokens?: number | null
    max_cost_usd?: string | number | null
    [key: string]: unknown
  }
  price_profile: Record<string, unknown> | null
  max_estimated_cost_usd: string | null
  notes: string[]
}

export interface PreviewSnapshot {
  repository_id: string
  revision: string
  revision_kind: string
  working_tree_state: string
}

export interface PreviewResponse {
  workspace_id: string
  context_hash: string
  source_ir_hash: string
  policy: string
  documents: number
  context_bytes: number
  redactions: number
  omitted: Record<string, number>
  view_status: string
  snapshots: PreviewSnapshot[]
  limitations: number
  candidate_states: Record<string, number>
  warnings: string[]
  provider: ProviderResponse | null
  provider_is_external: boolean | null
  budget: PreviewBudget | null
  excerpts?: PreviewExcerpt[]
}

export interface ProviderResponse {
  provider_session_id: string
  provider: ProviderKind
  host: string
  model: string
  credential: 'stored' | 'none'
}

export interface ConsentResponse {
  consent_receipt_id: string
  kinds: string[]
  policy: string
  context_hash: string
  provider: ProviderResponse
}

export interface SessionResponse {
  csrf_token: string
}
