"""LCSAS exception hierarchy.

All user-facing errors inherit from ``LcsasError`` so the CLI can catch them
cleanly and print a friendly message without a traceback, reserving full
tracebacks for unexpected internal errors only.
"""

from __future__ import annotations


class LcsasError(Exception):
    """Base class for all LCSAS operational errors.

    Carries an optional *recovery_hint* that the CLI prints after the
    error message to guide the operator towards resolution.
    """

    def __init__(self, message: str, recovery_hint: str = "") -> None:
        super().__init__(message)
        self.recovery_hint = recovery_hint


class ConfigError(LcsasError):
    """Configuration is invalid or incomplete."""


class BinaryError(LcsasError):
    """A required external binary is missing or too old."""


class BurnError(LcsasError):
    """An error occurred during the burn pipeline."""


class RestoreError(LcsasError):
    """An error occurred during restore planning or execution."""


class CatalogError(LcsasError):
    """Catalog validation or rebuild failed."""


class StagingError(LcsasError):
    """Error assembling the staging directory."""
