const GITHUB_OWNER_RE = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/
const GITHUB_REPO_RE = /^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}$/
const SHA_RE = /^[0-9a-f]{40}$/

interface Props {
  sourceIrHash: string
  githubSource?: {
    owner: string
    repository: string
    revision: string
    dirty?: boolean
  } | null
}

/**
 * Displays a pinned GitHub commit link only when owner/repo/40-hex SHA are valid and clean.
 * Local or dirty sources show an unverified label with the source_ir_hash fingerprint.
 */
export function RevisionBadge({ sourceIrHash, githubSource }: Props) {
  const isPinnedGitHub =
    githubSource &&
    !githubSource.dirty &&
    GITHUB_OWNER_RE.test(githubSource.owner) &&
    GITHUB_REPO_RE.test(githubSource.repository) &&
    SHA_RE.test(githubSource.revision)

  return (
    <div
      role="status"
      aria-label="Source revision"
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
      {isPinnedGitHub ? (
        <>
          <span>Pinned revision:</span>
          <a
            href={`https://github.com/${githubSource.owner}/${githubSource.repository}/commit/${githubSource.revision}`}
            target="_blank"
            rel="noreferrer"
            style={{ color: '#2563eb', fontFamily: 'monospace' }}
          >
            {githubSource.owner}/{githubSource.repository}@{githubSource.revision.slice(0, 8)}
          </a>
        </>
      ) : (
        <>
          <span>Local source (unverified)</span>
          <code title={sourceIrHash}>IR {sourceIrHash.slice(0, 12)}</code>
        </>
      )}
    </div>
  )
}
