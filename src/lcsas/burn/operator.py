"""Operator interaction seam for multi-disc burns (FUP-01).

A real multi-disc session cannot run unattended: between volumes the
operator must swap the just-burned disc for a fresh blank.  The
orchestrator drives that protocol through the :class:`OperatorPrompt`
Protocol so unit tests inject a recording fake and the cdemu/CI flows
run non-interactively, while the production path blocks on a TTY.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol


class OperatorPrompt(Protocol):
    """Blocks until the operator confirms a physical disc action."""

    def checkpoint(self, message: str) -> None:
        """Show *message* and block until the operator is ready."""
        ...


class ConsoleOperatorPrompt:
    """TTY-backed prompt: print to stderr, wait for ENTER."""

    def checkpoint(self, message: str) -> None:
        print(message, file=sys.stderr)
        input("Press ENTER when ready... ")


class NullOperatorPrompt:
    """No-op prompt for scripted / non-interactive burns (``--no-prompt``)."""

    def checkpoint(self, message: str) -> None:  # noqa: D401 - trivial
        return None


def eject_tray(device: str) -> bool:
    """Best-effort tray eject after a verified burn (FUP-01).

    Runs ``eject <device>``; a failure is logged by the caller and never
    aborts the session (some drives/loaders have no software eject).
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["eject", device],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
