"""LCSAS exception hierarchy.

All errors meant for the owner inherit from ``LcsasError`` so the CLI can catch them
cleanly and print a friendly message without a traceback, reserving full
tracebacks for unexpected internal errors only.
"""

from __future__ import annotations


class LcsasError(Exception):
    """Base class for all LCSAS operational errors.

    Carries an optional *recovery_hint* that the CLI prints after the
    error message to guide the owner towards resolution.
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


class CatalogUnreadableError(CatalogError, RuntimeError):
    """The catalog file is there but is not a readable SQLite catalog
    (#968): damaged, or some other file.  A RuntimeError too, as the
    integrity check raised before."""


class InputError(LcsasError, ValueError):
    """A value the owner typed is not usable, or names nothing (#968).

    A ValueError too, so the callers that already catch ValueError keep
    doing so; ``main()`` words the rest instead of "Unexpected error".
    """


class StagingError(LcsasError):
    """Error assembling a volume's tree in the staging area."""


class CatalogLockTimeout(LcsasError):  # noqa: N818 - "Timeout" reads as the error
    """Timed out waiting to acquire the catalog file lock.

    The CLI maps this to exit code 75 (``EX_TEMPFAIL``) so scripts can
    retry; the message names the holder so the owner knows what to wait
    on (and not to kill it — it may be mid-burn).
    """
