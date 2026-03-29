"""Unit tests for ShutdownManager (lcsas.utils.shutdown)."""

from __future__ import annotations

import signal
from unittest.mock import patch

from lcsas.utils.shutdown import ShutdownManager


class TestRegisterAndRunCleanups:
    def test_register_runs_on_run_cleanups(self):
        mgr = ShutdownManager()
        calls: list[int] = []
        mgr.register(lambda: calls.append(1))
        mgr.run_cleanups()
        assert calls == [1]

    def test_lifo_order(self):
        mgr = ShutdownManager()
        order: list[int] = []
        mgr.register(lambda: order.append(1))
        mgr.register(lambda: order.append(2))
        mgr.register(lambda: order.append(3))
        mgr.run_cleanups()
        assert order == [3, 2, 1]

    def test_run_cleanups_suppresses_exceptions(self):
        mgr = ShutdownManager()
        mgr.register(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # Should not raise
        mgr.run_cleanups()

    def test_run_cleanups_continues_after_exception(self):
        mgr = ShutdownManager()
        calls: list[int] = []
        mgr.register(lambda: calls.append(1))
        mgr.register(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        mgr.register(lambda: calls.append(3))
        mgr.run_cleanups()
        # Both non-failing callbacks ran (LIFO, so 3 then 1)
        assert calls == [3, 1]

    def test_no_callbacks_run_cleanups_is_noop(self):
        mgr = ShutdownManager()
        mgr.run_cleanups()  # Should not raise


class TestShuttingDownFlag:
    def test_initially_false(self):
        mgr = ShutdownManager()
        assert mgr.shutting_down is False

    def test_set_by_handler(self):
        mgr = ShutdownManager()
        with patch.object(mgr, "run_cleanups"), patch.object(mgr, "uninstall"), patch(
            "signal.raise_signal"
        ):
            mgr._handler(signal.SIGTERM, None)
        assert mgr.shutting_down is True


class TestInstallUninstall:
    def test_install_replaces_sigterm_and_sigint(self):
        mgr = ShutdownManager()
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)
        try:
            mgr.install()
            assert signal.getsignal(signal.SIGTERM) == mgr._handler
            assert signal.getsignal(signal.SIGINT) == mgr._handler
        finally:
            signal.signal(signal.SIGTERM, original_term)
            signal.signal(signal.SIGINT, original_int)

    def test_uninstall_restores_handlers(self):
        mgr = ShutdownManager()
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)
        try:
            mgr.install()
            mgr.uninstall()
            assert signal.getsignal(signal.SIGTERM) == original_term
            assert signal.getsignal(signal.SIGINT) == original_int
        finally:
            # Ensure cleanup even if assertions fail
            signal.signal(signal.SIGTERM, original_term)
            signal.signal(signal.SIGINT, original_int)

    def test_install_saves_prev_handlers(self):
        mgr = ShutdownManager()
        prev_term = signal.getsignal(signal.SIGTERM)
        prev_int = signal.getsignal(signal.SIGINT)
        try:
            mgr.install()
            assert mgr._prev_sigterm == prev_term
            assert mgr._prev_sigint == prev_int
        finally:
            mgr.uninstall()


class TestReentrancyGuard:
    def test_handler_does_not_reenter(self):
        mgr = ShutdownManager()
        cleanup_count = [0]

        def counting_cleanup():
            cleanup_count[0] += 1

        mgr.register(counting_cleanup)
        mgr._shutting_down = True  # Simulate already shutting down
        with patch("signal.raise_signal"):
            mgr._handler(signal.SIGTERM, None)
        # Cleanup should NOT have run again
        assert cleanup_count[0] == 0

    def test_handler_full_flow(self):
        mgr = ShutdownManager()
        cleanup_ran = [False]

        def cleanup():
            cleanup_ran[0] = True

        mgr.register(cleanup)
        with patch.object(mgr, "uninstall") as mock_uninstall, patch(
            "signal.raise_signal"
        ) as mock_raise:
            mgr._handler(signal.SIGTERM, None)

        assert cleanup_ran[0] is True
        mock_uninstall.assert_called_once()
        mock_raise.assert_called_once_with(signal.SIGTERM)
