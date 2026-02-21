"""LCSAS logging configuration."""

from __future__ import annotations

import logging
import os
import sys


class _StdoutHandler(logging.StreamHandler):
    """Handler that always writes to the *current* ``sys.stdout``.

    Unlike ``StreamHandler(sys.stdout)`` which captures the stream
    reference at construction time, this handler resolves ``sys.stdout``
    on every emit — allowing pytest's ``capsys`` fixture (and any other
    monkey-patching) to capture log output transparently.
    """

    def __init__(self) -> None:  # noqa: D107
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value):  # type: ignore[override]
        pass  # ignore attempts to set the stream


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the root ``lcsas`` logger.

    Parameters
    ----------
    verbose:
        If *True* the log level is set to ``DEBUG``; otherwise ``INFO``.

    The format intentionally omits timestamps — LCSAS is a CLI tool, not
    a daemon.  Users who need timestamps can set the ``LCSAS_LOG_FORMAT``
    environment variable.

    The handler writes to **stdout** so that ``capsys`` in tests captures
    log output the same way it captured the old ``print()`` calls.
    """
    logger = logging.getLogger("lcsas")

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        # Still update the level in case verbose changed between calls
        level = logging.DEBUG if verbose else logging.INFO
        logger.setLevel(level)
        for h in logger.handlers:
            h.setLevel(level)
        return logger

    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    fmt = os.environ.get("LCSAS_LOG_FORMAT", "%(message)s")
    handler = _StdoutHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)

    return logger


def get_logger() -> logging.Logger:
    """Return the ``lcsas`` logger (creates it unconfigured if needed)."""
    return logging.getLogger("lcsas")


_PASSWORD_SUFFIXES = (".key", ".pem", ".pass", ".password", ".secret")


def mask_password_path(path: str) -> str:
    """Mask a path if it looks like a password/key file.

    Returns ``***`` if the path ends with a known password-file suffix,
    otherwise returns the path unchanged.
    """
    lower = path.lower()
    for suffix in _PASSWORD_SUFFIXES:
        if lower.endswith(suffix):
            return "***"
    return path
