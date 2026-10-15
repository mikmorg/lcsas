"""#453: hang-guard timeouts are stretched; deliberate waits are not.

This tier is the last gate before a build ships, and it was going
spuriously red under its own load — `test_zero_byte_tier2_skipped` runs
in ~1 s alone but tripped its `timeout=15` during a full-tier run. The
conftest wrapper stretches short timeouts so a loaded box stops
producing false alarms.

The scaling must be *selective*, and these tests pin that: stretching
the 90 s `wineboot` probe would defeat the fail-fast skip that stops a
wedged wine host from grinding `make gate` for minutes (#390).

The wrapper only sees `subprocess.run`, so the pty tests that need
`Popen` scale their own hang-guards; a static guard below keeps the
next one from bypassing the fix (#458).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from .conftest import (
    _DEFAULT_TIMEOUT_SCALE,
    _HANG_GUARD_CEILING_S,
    _scaled_timeout,
    _timeout_scale,
)

# Captured at import time — before the autouse fixture swaps it — so the
# "is it actually installed?" check below has an unpatched reference.
_real_run_snapshot = subprocess.run


@pytest.fixture(autouse=True)
def _pin_default_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert against the default, not whatever the shell exports (#458).

    LCSAS_TEST_TIMEOUT_SCALE is a knob someone on a slow box is *meant*
    to set, so this file must not inherit it -- at the documented
    disable value of 1 the end-to-end test below would raise an uncaught
    TimeoutExpired.  Tests that exercise the knob set it on top of this.
    """
    monkeypatch.delenv("LCSAS_TEST_TIMEOUT_SCALE", raising=False)


class TestScaledTimeout:
    def test_short_hang_guards_are_stretched(self):
        """The failing case from #453: restore.sh's timeout=15."""
        assert _scaled_timeout(15) == 15 * _DEFAULT_TIMEOUT_SCALE

    @pytest.mark.parametrize("value", [1, 5, 10, 15, 30, 59, 59.9])
    def test_everything_below_the_ceiling_grows(self, value):
        assert _scaled_timeout(value) > value

    @pytest.mark.parametrize("value", [60, 90, 120, 300, 600, 1800])
    def test_deliberate_waits_are_untouched(self, value):
        """At/above the ceiling a timeout is a patience limit, not a
        hang-guard. The 90 s wineboot probe is the load-bearing case:
        stretching it would turn #390's fast skip into a long grind."""
        assert _scaled_timeout(value) == value

    def test_the_wineboot_probe_specifically_is_untouched(self):
        """Named explicitly so a future ceiling change has to confront
        this case rather than silently swallow it."""
        assert _HANG_GUARD_CEILING_S <= 90
        assert _scaled_timeout(90) == 90

    def test_none_passes_through(self):
        """`timeout=None` means "wait forever" — scaling it is nonsense."""
        assert _scaled_timeout(None) is None

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_is_untouched(self, value):
        assert _scaled_timeout(value) == value


class TestTimeoutScaleEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LCSAS_TEST_TIMEOUT_SCALE", raising=False)
        assert _timeout_scale() == _DEFAULT_TIMEOUT_SCALE

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("LCSAS_TEST_TIMEOUT_SCALE", "10")
        assert _timeout_scale() == 10.0
        assert _scaled_timeout(15) == 150.0

    def test_scale_of_one_disables_stretching(self, monkeypatch):
        monkeypatch.setenv("LCSAS_TEST_TIMEOUT_SCALE", "1")
        assert _scaled_timeout(15) == 15

    def test_a_shrinking_scale_is_clamped_not_honoured(self, monkeypatch):
        """Tightening the guards is the opposite of the point, so a value
        below 1 must clamp rather than make the flakiness worse."""
        monkeypatch.setenv("LCSAS_TEST_TIMEOUT_SCALE", "0.1")
        assert _timeout_scale() == 1.0
        assert _scaled_timeout(15) == 15

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        """A typo'd env var must not silently disable the fix."""
        monkeypatch.setenv("LCSAS_TEST_TIMEOUT_SCALE", "banana")
        assert _timeout_scale() == _DEFAULT_TIMEOUT_SCALE


class TestWrapperIsActuallyInstalled:
    """The helpers above are pure functions; these prove the autouse
    fixture actually routes real calls through them.  Without this, the
    scaling could be correct and entirely unwired — the vacuity failure
    mode that #430 and #428 both had to guard against."""

    def test_subprocess_run_is_patched_during_a_test(self):
        assert subprocess.run is not _real_run_snapshot

    def test_a_real_call_receives_the_scaled_timeout(self, monkeypatch):
        """Capture what the underlying runner is handed."""
        seen: dict[str, object] = {}

        def _fake(*args, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0)

        from . import conftest as _cf

        monkeypatch.setattr(_cf, "_real_run", _fake)
        subprocess.run(["true"], timeout=15)
        assert seen["timeout"] == 15 * _DEFAULT_TIMEOUT_SCALE

    def test_a_real_call_keeps_a_deliberate_wait_intact(self, monkeypatch):
        seen: dict[str, object] = {}

        def _fake(*args, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0)

        from . import conftest as _cf

        monkeypatch.setattr(_cf, "_real_run", _fake)
        subprocess.run(["true"], timeout=90)
        assert seen["timeout"] == 90

    def test_calls_without_a_timeout_are_left_alone(self, monkeypatch):
        seen: dict[str, object] = {}

        def _fake(*args, **kwargs):
            seen.update(kwargs)
            seen["_called"] = True
            return subprocess.CompletedProcess(args=args, returncode=0)

        from . import conftest as _cf

        monkeypatch.setattr(_cf, "_real_run", _fake)
        subprocess.run(["true"])
        assert seen.get("_called") is True
        assert "timeout" not in seen

    def test_end_to_end_a_slow_child_survives_a_tight_guard(self):
        """The whole point, demonstrated against a real subprocess.

        A 3-second child under a `timeout=2` hang-guard: unscaled that
        raises TimeoutExpired, which is exactly the #453 false alarm
        (the child was fine, the guard was too tight for the load).
        Scaled 4x the guard becomes 8s and the child completes.
        """
        with pytest.raises(subprocess.TimeoutExpired):
            _real_run_snapshot(["sleep", "3"], timeout=2)

        # Same call through the patched run(): the guard is stretched.
        done = subprocess.run(["sleep", "3"], timeout=2)
        assert done.returncode == 0

    def test_end_to_end_a_deliberate_wait_is_not_stretched(self, monkeypatch):
        """And the ceiling really holds against a real child: at/above it
        the guard fires on time rather than waiting 4x longer.

        The ceiling is lowered for the test so this costs ~2s instead of
        the ~60s a real 60-second guard would burn on every tier run —
        the flakiness fix must not itself make the gate slower.
        """
        from . import conftest as _cf

        monkeypatch.setattr(_cf, "_HANG_GUARD_CEILING_S", 2.0)
        monkeypatch.setenv("LCSAS_TEST_TIMEOUT_SCALE", "4")
        # 2 is now AT the ceiling, so it must NOT be stretched to 8 —
        # the 5-second child has to trip it.
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(["sleep", "5"], timeout=2)


# Calls whose ``timeout`` never reaches the run() wrapper: Popen's own
# waits, plus the two convenience helpers built on them rather than on
# run().  Positional index is where ``timeout`` lands if not passed by
# keyword.
_BYPASSING_CALLS = {"wait": 0, "communicate": 1, "call": None, "check_call": None}


def _scan_bypassing_timeouts() -> tuple[list[str], list[str]]:
    """Return (unscaled, scaled) ``file:line`` sites across the tier."""
    unscaled: list[str] = []
    scaled: list[str] = []
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _BYPASSING_CALLS):
                continue
            value = next((kw.value for kw in node.keywords
                          if kw.arg == "timeout"), None)
            pos = _BYPASSING_CALLS[node.func.attr]
            if value is None and pos is not None and len(node.args) > pos:
                value = node.args[pos]
            if value is None:
                continue
            where = f"{path.name}:{node.lineno}"
            if (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "_scaled_timeout"):
                scaled.append(where)
            elif isinstance(value, ast.Constant) and (
                value.value is None
                or (isinstance(value.value, (int, float))
                    and not isinstance(value.value, bool)
                    and value.value >= _HANG_GUARD_CEILING_S)):
                # A deliberate wait (or none) -- the wrapper would leave
                # it alone too, so there is nothing to bypass.
                pass
            else:
                unscaled.append(where)
    return unscaled, scaled


class TestPopenHangGuardsAreScaled:
    """``Popen.wait()``/``communicate()`` never pass through the run()
    wrapper, so a pty test that needs ``Popen`` must scale its own
    hang-guard -- otherwise it misfires under full-tier load exactly as
    #453's did.  Wrapping is always safe: ``_scaled_timeout`` already
    passes deliberate waits (>= the ceiling) through untouched."""

    def test_no_unscaled_hang_guard_bypasses_the_wrapper(self):
        unscaled, _ = _scan_bypassing_timeouts()
        assert not unscaled, (
            "These Popen-style waits bypass the subprocess.run timeout "
            "scaling (#453/#458). Wrap the value in _scaled_timeout() "
            "(from .conftest), or use a literal >= "
            f"{_HANG_GUARD_CEILING_S:g}s if it is a deliberate wait:\n  "
            + "\n  ".join(unscaled)
        )

    def test_the_scan_actually_sees_the_scaled_sites(self):
        """Non-vacuity: a walker that matched nothing would pass the
        guard above trivially.  The three #458 pty sites must show up."""
        _, scaled = _scan_bypassing_timeouts()
        files = {where.split(":")[0] for where in scaled}
        assert {
            "test_nonempty_target_guard.py",
            "test_restore_sh_target_guards.py",
            "test_restore_sh_tmpfs_target_warns.py",
        } <= files, scaled
