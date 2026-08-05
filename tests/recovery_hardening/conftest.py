"""Hardening-suite fixtures + autouse hooks.

Two cross-cutting concerns are handled here by wrapping
``subprocess.run`` once, rather than by editing hundreds of call sites.

**1. Shell tracing.**  When `LCSAS_TRACE_VIA_BASH=1` is set in the
environment, every `subprocess.run` call inside a hardening test that
invokes `['sh', restore.sh, ...]` is rewritten to use `bash` instead,
AND the test's env dict is augmented with `LCSAS_SHELL_TRACE` so
restore.sh's preamble enables `bash -x` tracing to the named file.
This is what `make shell-coverage` uses to drive coverage measurement
across the entire test_restore_*.py suite without modifying every test
individually.

**2. Hang-guard timeout scaling (#453).**  This tier is the last gate
before a build ships, and it was going spuriously red under its own
load: `test_zero_byte_tier2_skipped` runs in ~1 s alone but tripped its
`timeout=15` during a full-tier run.  A gate that cries wolf trains the
eye to skim it, which is the one thing this tier cannot afford.

The key observation is that these short timeouts are *hang-guards*, not
performance assertions: nothing asserts an elapsed duration anywhere in
this directory.  Their only job is to stop a wedged subprocess from
grinding the suite forever.  A hang-guard costs nothing when the test
passes, so making it generous is strictly better than leaving it tight
enough to misfire.

Timeouts at or above ``_HANG_GUARD_CEILING_S`` are left ALONE, because
at that scale they stop being hang-guards and become deliberate
patience limits.  The load-bearing example is the 90 s ``wineboot``
probe in the restore.bat suites: it exists precisely to fail FAST to a
clean skip when wine is wedged (#390).  Stretching it would defeat its
purpose and make a bad host grind for minutes instead of seconds.

Scale with ``LCSAS_TEST_TIMEOUT_SCALE`` (default 4; 1 disables).
"""
from __future__ import annotations

import os
import subprocess

import pytest

_real_run = subprocess.run

# At or above this many seconds, a timeout is read as a deliberate wait
# (e.g. the 90 s wineboot fail-fast probe, the 120 s per-wine-call cap)
# rather than a guard against a hung child, and is left untouched.
_HANG_GUARD_CEILING_S = 60.0

_DEFAULT_TIMEOUT_SCALE = 4.0


def _timeout_scale() -> float:
    """Multiplier for hang-guard timeouts; ``>= 1.0``, never shrinking."""
    raw = os.environ.get("LCSAS_TEST_TIMEOUT_SCALE")
    if raw is None:
        return _DEFAULT_TIMEOUT_SCALE
    try:
        scale = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SCALE
    # A scale below 1 would tighten the guards — the opposite of the
    # point — so clamp rather than honour it.
    return max(1.0, scale)


def _scaled_timeout(timeout: object) -> object:
    """Stretch a hang-guard timeout; pass anything else through as-is."""
    if timeout is None or isinstance(timeout, bool):
        return timeout
    if not isinstance(timeout, (int, float)):
        return timeout
    if timeout <= 0 or timeout >= _HANG_GUARD_CEILING_S:
        return timeout
    return timeout * _timeout_scale()


def _run_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Scale hang-guard timeouts (#453), then apply shell tracing.

    Both concerns funnel through one wrapper so ``subprocess.run`` is
    patched exactly once; the tracing behaviour below is unchanged from
    when it was the wrapper's only job.
    """
    if "timeout" in kwargs:
        kwargs["timeout"] = _scaled_timeout(kwargs["timeout"])

    trace_file = os.environ.get("LCSAS_SHELL_TRACE")
    if not trace_file or not os.environ.get("LCSAS_TRACE_VIA_BASH"):
        return _real_run(*args, **kwargs)

    # Normalise to positional argv.
    argv = args[0] if args else kwargs.get("args")
    if not (isinstance(argv, (list, tuple)) and len(argv) >= 2):
        return _real_run(*args, **kwargs)

    head = str(argv[0])
    sub = str(argv[1])
    if os.path.basename(head) == "sh" and sub.endswith("restore.sh"):
        # Rewrite argv: sh → bash.  Bash is universally available on
        # systems that have the rest of LCSAS's dev deps; on a real
        # recovery host the script still uses /bin/sh via the shebang.
        new_argv = list(argv)
        new_argv[0] = "bash"
        # Augment env so the LCSAS_SHELL_TRACE hook in restore.sh
        # fires.  Preserve the test's existing env if it passed one.
        env = dict(kwargs.get("env") or os.environ)
        env["LCSAS_SHELL_TRACE"] = trace_file
        kwargs["env"] = env
        if args:
            args = (new_argv,) + args[1:]
        else:
            kwargs["args"] = new_argv
    return _real_run(*args, **kwargs)


@pytest.fixture(autouse=True)
def _wrap_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the subprocess wrapper for every test in this directory.

    Installed unconditionally, unlike the trace-only predecessor: the
    timeout scaling has to apply to a plain ``make test-recovery-hardening``
    run, which is exactly where the spurious failures showed up.  With
    neither LCSAS_TRACE_VIA_BASH nor a raised scale set, the wrapper is
    still a behaviour change (default scale is 4) — that is the fix.
    """
    monkeypatch.setattr(subprocess, "run", _run_wrapper)
