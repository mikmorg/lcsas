"""Shared base for subprocess-backed tool wrappers."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple of ints.

    Extracts the first sequence of dot-separated integers, ignoring any
    pre-release suffixes like '-beta', '+build', etc.

    Examples::

        parse_version("1.7.4") == (1, 7, 4)
        parse_version("xorriso 1.5.4-pl02") == (1, 5, 4)
        parse_version("dvdisaster 0.79.6-pl002") == (0, 79, 6)
    """
    m = re.search(r"(\d+(?:\.\d+)+)", version_str)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def check_binary_version(
    binary: str,
    min_version: tuple[int, ...],
    version_args: list[str] | None = None,
) -> str:
    """Verify that *binary* meets the minimum version requirement.

    Runs ``binary --version`` (or *version_args* if provided), parses the
    first dotted version string from the output, and compares it against
    *min_version*.

    Returns the raw version string on success.

    Raises:
        lcsas.exceptions.BinaryError: If the binary is missing or too old.
    """
    from lcsas.exceptions import BinaryError

    if shutil.which(binary) is None:
        raise BinaryError(
            f"Required tool '{binary}' not found on PATH.",
            recovery_hint=f"Install {binary} and ensure it is on PATH before continuing.",
        )

    args = version_args if version_args is not None else ["--version"]
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        raise BinaryError(
            f"'{binary} --version' timed out — is the binary functional?",
            recovery_hint=f"Check that {binary} is not hung or corrupted.",
        ) from None
    except OSError as exc:
        raise BinaryError(
            f"Could not run '{binary} --version': {exc}",
            recovery_hint=f"Verify {binary} is installed and executable.",
        ) from exc

    version = parse_version(output)
    min_ver_str = ".".join(str(x) for x in min_version)
    if version < min_version:
        found_str = ".".join(str(x) for x in version)
        raise BinaryError(
            f"'{binary}' version {found_str} is too old; {min_ver_str}+ is required.",
            recovery_hint=f"Upgrade {binary} to version {min_ver_str} or newer before continuing.",
        )

    _logger.debug("%s version %s OK (>= %s)", binary, version, min_ver_str)
    return output


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

    @staticmethod
    def _handle_timeout(
        tool_name: str,
        operation: str,
        exc: subprocess.TimeoutExpired,
    ) -> None:
        """Log and re-raise a TimeoutExpired as a RuntimeError with a clear message."""
        _logger.error(
            "%s timed out after %s seconds during %s. "
            "If the operation legitimately takes this long, consider a higher timeout. "
            "Otherwise the process may be hung — check the device and try again.",
            tool_name,
            exc.timeout,
            operation,
        )
        raise RuntimeError(
            f"{tool_name} timed out after {exc.timeout}s during {operation}. "
            f"The process was killed."
        ) from exc
