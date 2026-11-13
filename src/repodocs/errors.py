"""Domain errors and process exit codes."""

from __future__ import annotations


class RepoDocsError(Exception):
    """Expected user-facing RepoDocs failure."""


class ConfigError(RepoDocsError):
    """Invalid or missing project configuration."""


class ScanError(RepoDocsError):
    """A repository could not be scanned safely."""


class OwnershipError(RepoDocsError):
    """Generated output has an unexpected or manually changed owner."""


class ValidationError(RepoDocsError):
    """Persisted state or generated output failed validation."""


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_OWNERSHIP = 4
EXIT_CONFIRMATION = 5
