"""Domain errors and process exit codes."""

from __future__ import annotations


class RepoDocsError(Exception):
    """Expected user-facing RepoDocs failure."""


class ConfigError(RepoDocsError):
    """Invalid or missing project configuration."""


class ConsentError(ConfigError):
    """An external AI provider was requested without recorded consent."""


class ProviderError(RepoDocsError):
    """An AI provider was unavailable or returned unusable output."""


class MalformedProviderOutput(ProviderError):
    """The provider answered, but not with a JSON object. One repair attempt is allowed."""


class PublicationError(RepoDocsError):
    """Documentation could not be published safely."""


class ScanError(RepoDocsError):
    """A repository could not be scanned safely."""


class SnapshotError(RepoDocsError):
    """A repository snapshot does not satisfy the requested snapshot mode."""


class OwnershipError(RepoDocsError):
    """Generated output has an unexpected or manually changed owner."""


class ValidationError(RepoDocsError):
    """Persisted state or generated output failed validation."""


class ConfirmationError(RepoDocsError):
    """Required cross-repository confirmations are unresolved."""


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_OWNERSHIP = 4
EXIT_CONFIRMATION = 5
EXIT_SNAPSHOT = 6
EXIT_AI_PROVIDER = 7
EXIT_PUBLICATION = 8
