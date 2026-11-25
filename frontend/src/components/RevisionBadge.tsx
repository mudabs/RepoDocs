export interface RepositoryRevision {
  repository_id: string
  revision: string
  revision_kind: string
  content_fingerprint: string
  remote: string | null
  note?: string
}

const GITHUB_REMOTE_RE = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\/[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}$/
const SHA_RE = /^[0-9a-f]{40}$/

interface Props {
  sourceIrHash: string
  repositories?: RepositoryRevision[]
}

/**
 * Displays per-repository revision metadata from the evidence map when available:
 * pinned GitHub commit links for clean commits, or working-tree fingerprints for local/dirty trees.
 * Never labels an IR hash as a repository content fingerprint.
 */
export function RevisionBadge({ sourceIrHash, repositories }: Props) {
  if (repositories && repositories.length > 0) {
    return (
      <div
        role="status"
        aria-label="Repository revisions"
        style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}
      >
        {repositories.map(repo => {
          const isPinnedCommit =
            repo.revision_kind === 'commit' &&
            repo.remote !== null &&
            GITHUB_REMOTE_RE.test(repo.remote) &&
            SHA_RE.test(repo.revision)

          return (
            <span
              key={repo.repository_id}
              title={repo.note}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.35rem',
                fontSize: '0.78rem',
                color: '#374151',
                background: '#f3f4f6',
                border: '1px solid #e5e7eb',
                borderRadius: '0.35rem',
                padding: '0.2rem 0.55rem',
              }}
            >
              <strong>{repo.repository_id}:</strong>
              {isPinnedCommit ? (
                <a
                  href={`https://github.com/${repo.remote}/commit/${repo.revision}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: '#2563eb', fontFamily: 'monospace' }}
                >
                  {repo.remote}@{repo.revision.slice(0, 8)}
                </a>
              ) : (
                <span>
                  working-tree (unverified){' '}
                  <code title={`content_fingerprint: ${repo.content_fingerprint}`}>
                    {repo.content_fingerprint.slice(0, 12)}
                  </code>
                </span>
              )}
            </span>
          )
        })}
      </div>
    )
  }

  return (
    <div
      role="status"
      aria-label="Source IR snapshot"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '0.5rem',
        fontSize: '0.8rem',
        color: '#374151',
        background: '#f3f4f6',
        border: '1px solid #e5e7eb',
        borderRadius: '0.35rem',
        padding: '0.25rem 0.6rem',
      }}
    >
      <span>IR snapshot</span>
      <code title={sourceIrHash}>{sourceIrHash.slice(0, 12)}</code>
    </div>
  )
}
