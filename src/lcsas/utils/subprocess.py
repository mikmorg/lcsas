"""Shared base for subprocess-backed tool wrappers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)


class SubprocessRunnerBase:
    """Common init and environment handling for subprocess runners.

    Subclasses get ``self._binary`` (tool path) and ``self._tmpdir``
    (optional temp directory override), plus ``_env()`` which returns
    an env dict with ``TMPDIR`` set when configured.
    """

    def __init__(self, binary: str, tmpdir: Path | None = None) -> None:
        self._binary = binary
        self._tmpdir = tmpdir

    def check_binary(self) -> None:
        """Raise RuntimeError if the binary is not found on PATH.

        Call this as a preflight check before any heavy operation so the
        user gets a clear message immediately rather than deep inside a
        pipeline.
        """
        if shutil.which(self._binary) is None:
            raise RuntimeError(
                f"Required tool '{self._binary}' not found on PATH. "
                f"Install it before continuing."
            )

    def _env(self) -> dict[str, str] | None:
        if self._tmpdir is None:
            return None
        return {**os.environ, "TMPDIR": str(self._tmpdir)}

    @staticmethod
    def _log_stderr(tool_name: str, exc: subprocess.CalledProcessError) -> None:
        """Log stderr lines from a failed subprocess call."""
        if exc.stderr:
            for line in exc.stderr.strip().splitlines():
                _logger.error("  %s: %s", tool_name, line)
