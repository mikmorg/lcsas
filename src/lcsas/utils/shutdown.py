"""Graceful shutdown manager for long-running CLI operations."""

from __future__ import annotations

import signal
from collections.abc import Callable


class ShutdownManager:
    """Registers cleanup callbacks and installs signal handlers.

    Usage::

        mgr = ShutdownManager()
        mgr.register(lambda: cleanup_staging(path))
        mgr.install()
        # … long-running work …
        mgr.uninstall()
    """

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []
        self._shutting_down = False
        self._prev_sigterm = signal.SIG_DFL
        self._prev_sigint = signal.SIG_DFL

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def register(self, callback: Callable[[], None]) -> None:
        """Register a cleanup callback (LIFO order on shutdown)."""
        self._callbacks.append(callback)

    def install(self) -> None:
        """Install SIGTERM and SIGINT handlers."""
        self._prev_sigterm = signal.getsignal(signal.SIGTERM)
        self._prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def uninstall(self) -> None:
        """Restore previous signal handlers."""
        signal.signal(signal.SIGTERM, self._prev_sigterm)
        signal.signal(signal.SIGINT, self._prev_sigint)

    def run_cleanups(self) -> None:
        """Execute registered callbacks in reverse order (LIFO)."""
        for cb in reversed(self._callbacks):
            try:
                cb()
            except Exception:
                pass  # Best-effort cleanup

    def _handler(self, signum: int, frame: object) -> None:
        """Signal handler: run cleanups then re-raise."""
        if self._shutting_down:
            return  # Avoid re-entrancy
        self._shutting_down = True
        self.run_cleanups()
        self.uninstall()
        # Re-raise the signal with default handler
        signal.raise_signal(signum)
