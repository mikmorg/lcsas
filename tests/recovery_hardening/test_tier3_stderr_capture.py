"""Hardening test: restore.sh tier-3 stderr capture-and-replay on failure.

Issue #240 -- when tier-3 (`standalone_restorer.py`) exits non-zero,
`restore.sh` used to `exec` it directly and silently inherit the exit
code with NO captured stderr.  Operators saw only a numeric exit code
(`Exit code 2`, `session r ended unexpectedly`); tracebacks vanished
into the shell-process replacement and the failure mode was opaque.

The fix switches tier-3 dispatch from `exec` to a subprocess +
stderr-tee.  On non-zero exit, the captured stderr is replayed to the
real stderr with a labelled separator so the failure mode is visible
in agent transcripts.

What this test catches:
  - Reverting back to `exec "$PYBIN" "$PYREST" ...` (which would not
    capture stderr or echo it on failure).
  - Breaking the rc propagation (e.g. a future refactor that reads
    the pipeline's `$?` directly under POSIX sh, picking up `tee`'s
    rc instead of Python's).
  - Dropping the separator markers (which gate transcript-grep
    based agent-side diagnostics).
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

# The fake Python interpreter exits with this rc so the test can
# distinguish "restore.sh exited because tier 3 told it to" from
# "restore.sh exited because of an internal bug".  Any non-zero value
# distinct from common shell error codes (1, 126, 127) works.
TIER3_STUB_RC = 42

# Distinctive error text the stub emits to stderr -- the test asserts
# this string survives the capture-and-replay path.
TIER3_STUB_ERROR = "ModuleNotFoundError: No module named foo"


def _make_minimal_repo(root: Path) -> Path:
    repo = root / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def _install_failing_python_stub(bin_dir: Path) -> None:
    """A `python3` shim that writes a fake traceback to stderr and
    exits non-zero.  Mimics what a real broken standalone_restorer.py
    would emit at module-import time (e.g. ImportError pre-argparse).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        # Emit a multi-line "traceback" so we can verify the separator
        # markers wrap the captured stderr correctly.
        printf 'Traceback (most recent call last):\\n' >&2
        printf '  File "standalone_restorer.py", line 1, in <module>\\n' >&2
        printf '    import foo\\n' >&2
        printf '{TIER3_STUB_ERROR}\\n' >&2
        exit {TIER3_STUB_RC}
    """))
    stub.chmod(0o755)


def test_tier3_failure_captures_and_echoes_stderr(tmp_path: Path) -> None:
    """When tier-3 exits non-zero, restore.sh must:
      1. propagate the Python process's exit code (not tee's rc=0),
      2. label the failure with `[tier 3] FAILED (rc=N)`,
      3. replay the captured stderr verbatim,
      4. wrap the replay in separator markers so it is grep-able.
    """
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    # Empty bin/<target> forces tier 1 and tier 2 to be absent, so the
    # cascade falls through to tier 3.
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_minimal_repo(recovery)
    # tier-3 script search probes ../standalone_restorer.py first.
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")

    stub_dir = tmp_path / "stubbin"
    _install_failing_python_stub(stub_dir)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        # No real data discs; bypass the discovery gate so we reach
        # the tier-3 dispatch under test.
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )

    # 1. Exit code must match the stub's, not tee's (rc=0).
    assert res.returncode == TIER3_STUB_RC, (
        f"restore.sh exited {res.returncode}, expected {TIER3_STUB_RC} "
        f"(tier-3 stub's rc).  rc propagation through the pipeline is "
        f"broken -- the pipeline probably reads `tee`'s exit code "
        f"instead of the Python process's.\nstderr:\n{res.stderr}"
    )

    # 2. Failure label must surface with the correct rc.
    assert f"[tier 3] FAILED (rc={TIER3_STUB_RC})" in res.stderr, (
        f"missing tier-3 failure label on stderr.  stderr was:\n{res.stderr}"
    )

    # 3. Stub's stderr content must be replayed.  The `[tier 3] ` prefix
    # is the sed-rewritten form; the original (un-prefixed) form also
    # appears because tee streams it through unmodified.  We assert the
    # original text appears somewhere in the captured stream.
    assert TIER3_STUB_ERROR in res.stderr, (
        f"stub's stderr content ({TIER3_STUB_ERROR!r}) was not echoed "
        f"to restore.sh's stderr.  Capture path is broken.\n"
        f"stderr:\n{res.stderr}"
    )

    # And specifically check the sed-prefixed form (proves the labelled
    # replay block fired, not just the tee passthrough).
    assert f"[tier 3] {TIER3_STUB_ERROR}" in res.stderr, (
        f"sed-prefixed replay line missing -- the failure-replay block "
        f"did not fire.\nstderr:\n{res.stderr}"
    )

    # 4. Both separator markers must appear, wrapping the replay.
    open_marker = "[tier 3] -------------- captured stderr --------------"
    close_marker = "[tier 3] --------------------------------------------"
    assert open_marker in res.stderr, (
        f"open separator missing.\nstderr:\n{res.stderr}"
    )
    assert close_marker in res.stderr, (
        f"close separator missing.\nstderr:\n{res.stderr}"
    )

    # Open marker must precede close marker, and the error text must
    # fall between them (so the block is well-formed).
    open_at = res.stderr.index(open_marker)
    close_at = res.stderr.index(close_marker)
    err_at = res.stderr.index(f"[tier 3] {TIER3_STUB_ERROR}")
    assert open_at < err_at < close_at, (
        f"separator markers do not wrap the replayed stderr.  "
        f"open@{open_at} err@{err_at} close@{close_at}\n"
        f"stderr:\n{res.stderr}"
    )


def test_tier3_success_does_not_emit_failure_label(tmp_path: Path) -> None:
    """The capture/replay block must only fire on non-zero exit.

    When tier-3 succeeds, restore.sh must propagate rc=0 cleanly and
    must NOT spam the `[tier 3] FAILED` label or separator markers --
    those are reserved for the failure path and would be noise on a
    successful restore.
    """
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_minimal_repo(recovery)
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")

    # A python stub that prints a benign progress line to stderr and
    # exits 0 -- mirrors a successful real restore which DOES emit
    # `[restic-fallback]` messages on stderr along the way.
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        printf '[restic-fallback] restored successfully\\n' >&2
        exit 0
    """))
    stub.chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )

    assert res.returncode == 0, (
        f"restore.sh exited {res.returncode}; expected 0 on tier-3 "
        f"success.\nstderr:\n{res.stderr}"
    )

    # The success-path stderr must still flow through (tee passes it).
    assert "[restic-fallback] restored successfully" in res.stderr, (
        f"tee did not pass tier-3's stderr through on success.\n"
        f"stderr:\n{res.stderr}"
    )

    # But the failure-only markers MUST NOT appear.
    assert "[tier 3] FAILED" not in res.stderr, (
        f"failure label spuriously appeared on success path.\n"
        f"stderr:\n{res.stderr}"
    )
    assert "captured stderr" not in res.stderr, (
        f"capture-block separator spuriously appeared on success path.\n"
        f"stderr:\n{res.stderr}"
    )
