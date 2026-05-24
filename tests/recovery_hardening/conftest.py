"""Hardening-suite fixtures + autouse hooks.

When `LCSAS_TRACE_VIA_BASH=1` is set in the environment, every
`subprocess.run` call inside a hardening test that invokes
`['sh', restore.sh, ...]` is rewritten to use `bash` instead, AND
the test's env dict is augmented with `LCSAS_SHELL_TRACE` so
restore.sh's preamble enables `bash -x` tracing to the named file.

This is what `make shell-coverage` uses to drive coverage measurement
across the entire test_restore_*.py suite without modifying every
test individually.
"""
from __future__ import annotations

import os
import subprocess

import pytest

_real_run = subprocess.run


def _trace_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Rewrite `sh restore.sh ...` invocations to `bash` + add the
    LCSAS_SHELL_TRACE env-var so restore.sh's preamble enables xtrace.

    Untouched: invocations that aren't `sh restore.sh ...` shape, or
    when LCSAS_TRACE_VIA_BASH isn't set.
    """
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
def _enable_shell_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse fixture that installs the subprocess wrapper for the
    duration of each test in this directory."""
    if os.environ.get("LCSAS_TRACE_VIA_BASH"):
        monkeypatch.setattr(subprocess, "run", _trace_wrapper)
