"""Hardening test: operational-friendliness features for repeat operators.

Two recommendations from the v4 blind-restore agent transcript:

  #7  `disc-loader status` only printed the label; an operator with
      a stack of discs in flight had no way to tell at a glance which
      kind of disc was loaded ("is this the meta-disc or a data disc?").
      The fix decorates `LOADED <label>` lines with a role tag —
      `[meta]` / `[data]` / `[unknown]` — inferred from the label prefix.

  #10 `restore.sh` left no trace of a completed restore.  A second-time
      operator could not tell what they restored last time, when, or
      which tier handled it.  The fix appends one ISO-8601 UTC line to
      `$HOME/.lcsas-restore-log` per successful dispatch, with tenant /
      target / snapshot / tier / disc-count.

This file pins both behaviors with stub-driven end-to-end tests so a
future refactor cannot silently drop either feature.

What it catches:
  - disc-loader.c reverting to a plain exec of the backend (no role tag).
  - The session-log writer being moved out of the tier-1 default-exec
    path (the path real operators actually take), leaving the log empty
    unless they happen to use LCSAS_TIER_FALLBACK=1.
  - The session log losing its ISO-8601 UTC timestamp shape.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
DISC_LOADER_C = (
    REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "disc-loader.c"
)
HOST_TARGET = "x86_64-unknown-linux-musl"

# ISO-8601 UTC: 2026-05-18T16:42:11Z
ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


# ---------------------------------------------------------------------------
# Recommendation #7: disc-loader status role decoration
# ---------------------------------------------------------------------------

def _compile_disc_loader(tmp_path: Path, backend_stub: Path) -> Path:
    """Compile disc-loader.c with ROBOT_BACKEND pointed at our stub.

    Production builds use `cc -O2 -Wall` (see
    tests/e2e/cdemu_blind_restore/setup.py); we match that and
    additionally override the ROBOT_BACKEND macro so the test
    binary execs *our* stub instead of /opt/disc-robot/libexec.

    The wrapper's `setuid(0)` call is gated on `geteuid() != getuid()`,
    so a non-setuid test binary (the pytest case) skips the elevation
    branch entirely.
    """
    out = tmp_path / "disc-loader"
    rc = subprocess.run(
        [
            "cc", "-O2", "-Wall",
            f'-DROBOT_BACKEND="{backend_stub}"',
            str(DISC_LOADER_C),
            "-o", str(out),
        ],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, (
        f"disc-loader.c compile failed:\n{rc.stderr}"
    )
    return out


def _write_backend_stub(path: Path, status_line: str) -> None:
    """A fake cdr-robotctl that emits `status_line` for `status`."""
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "${{1:-}}" in
            status) printf '%s\\n' '{status_line}' ;;
            *)      printf 'stub backend: %s\\n' "$*" ;;
        esac
    """))
    path.chmod(0o755)


def test_disc_loader_status_includes_role_for_meta(tmp_path: Path) -> None:
    """A `LOADED LCSAS_META` line from the backend gets `[meta]`."""
    stub = tmp_path / "cdr-stub"
    _write_backend_stub(stub, "LOADED LCSAS_META")
    bin_ = _compile_disc_loader(tmp_path, stub)
    res = subprocess.run(
        [str(bin_), "status"], capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "[meta]" in res.stdout, (
        f"expected [meta] tag for LCSAS_META; got:\n{res.stdout!r}"
    )
    assert "LOADED LCSAS_META" in res.stdout


def test_disc_loader_status_includes_role_for_data(tmp_path: Path) -> None:
    """A `LOADED LCSAS_TEST_TINY_2026_0003` line gets `[data]`."""
    stub = tmp_path / "cdr-stub"
    _write_backend_stub(stub, "LOADED LCSAS_TEST_TINY_2026_0003")
    bin_ = _compile_disc_loader(tmp_path, stub)
    res = subprocess.run(
        [str(bin_), "status"], capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "[data]" in res.stdout, (
        f"expected [data] tag for non-meta LCSAS_*; got:\n{res.stdout!r}"
    )
    assert "LOADED LCSAS_TEST_TINY_2026_0003" in res.stdout


def test_disc_loader_status_passes_empty_through(tmp_path: Path) -> None:
    """An `EMPTY` backend response is not decorated."""
    stub = tmp_path / "cdr-stub"
    _write_backend_stub(stub, "EMPTY")
    bin_ = _compile_disc_loader(tmp_path, stub)
    res = subprocess.run(
        [str(bin_), "status"], capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "EMPTY" in res.stdout
    # No spurious tag on EMPTY — the role only makes sense when loaded.
    assert "[meta]" not in res.stdout
    assert "[data]" not in res.stdout
    assert "[unknown]" not in res.stdout


# ---------------------------------------------------------------------------
# Recommendation #10: session log on successful restore
# ---------------------------------------------------------------------------

def _make_minimal_repo(recovery: Path, tenant: str = "alpha") -> Path:
    repo = recovery / "metadata" / tenant
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def _install_succeeding_tier1(recovery: Path) -> Path:
    """A stub lcsas-restore that exits 0 so the default exec path runs."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "lcsas-restore"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "SUCCESS_lcsas-restore: stub ran"
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _run_restore(recovery: Path, target_dir: Path, home: Path,
                 snap: str = "latest",
                 ) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        # No mounted discs in the test sandbox.
        "LCSAS_MOUNT_DIRS": "",
        # The PR #83 discovery-gate would refuse to dispatch when no
        # pack-search dirs are present; these tests are exercising
        # tier dispatch + session-log, not discovery, so bypass it.
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        # Force the host triple so detect_arch never bites us.
        "LCSAS_TARGET": HOST_TARGET,
        # Don't try to relocate to RAM under pytest.
        "LCSAS_NO_RELOCATE": "1",
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), snap],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )


def test_session_log_written_on_success(tmp_path: Path) -> None:
    """A successful tier-1 dispatch appends one line to ~/.lcsas-restore-log."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_succeeding_tier1(recovery)
    _make_minimal_repo(recovery, tenant="alpha")
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, home, snap="latest")
    assert res.returncode == 0, (
        f"restore.sh exited {res.returncode}; stderr:\n{res.stderr}"
    )

    log = home / ".lcsas-restore-log"
    assert log.is_file(), (
        f"~/.lcsas-restore-log was not created; HOME contents: "
        f"{[p.name for p in home.iterdir()]}"
    )
    lines = log.read_text().splitlines()
    assert len(lines) == 1, f"expected 1 log line, got {len(lines)}: {lines}"
    line = lines[0]
    for token in ("tenant=alpha", f"target={target}",
                  "snapshot=latest", "tier=1"):
        assert token in line, f"token {token!r} missing from log line:\n{line}"


def test_session_log_iso8601_timestamp(tmp_path: Path) -> None:
    """The log line begins with an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_succeeding_tier1(recovery)
    _make_minimal_repo(recovery, tenant="alpha")
    home = tmp_path / "home"
    home.mkdir()

    res = _run_restore(recovery, tmp_path / "restored", home)
    assert res.returncode == 0, res.stderr

    line = (home / ".lcsas-restore-log").read_text().splitlines()[0]
    # The timestamp is the first whitespace-delimited token.
    ts = line.split()[0]
    assert ISO8601_UTC_RE.match(ts), (
        f"first token is not ISO-8601 UTC: {ts!r}; full line:\n{line}"
    )


def test_session_log_skipped_when_home_unwritable(tmp_path: Path) -> None:
    """If $HOME is unset/empty the log is skipped silently (no error)."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_succeeding_tier1(recovery)
    _make_minimal_repo(recovery)
    target = tmp_path / "restored"

    env = {
        **os.environ,
        "HOME": "",  # explicitly empty
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",  # bypass PR #83 discovery gate
        "LCSAS_TARGET": HOST_TARGET,
        "LCSAS_NO_RELOCATE": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    # Restore must still succeed — log is best-effort, not load-bearing.
    assert res.returncode == 0, (
        f"restore.sh failed when HOME was empty; stderr:\n{res.stderr}"
    )


def test_session_log_includes_disc_count(tmp_path: Path) -> None:
    """The disc-count field is present and parses as a non-negative integer.

    The exact value depends on whether any /Volumes, /media, or /mnt
    paths happened to be populated when the test ran; the field's
    *shape* is what we pin here.  A semantic counter test would need
    a containerized mount setup, which is overkill for a hardening
    test and brittle across CI hosts.
    """
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_succeeding_tier1(recovery)
    _make_minimal_repo(recovery)
    home = tmp_path / "home"
    home.mkdir()

    res = _run_restore(recovery, tmp_path / "restored", home)
    assert res.returncode == 0, res.stderr
    line = (home / ".lcsas-restore-log").read_text().splitlines()[0]
    m = re.search(r"\bdiscs=(\d+)\b", line)
    assert m, f"discs= field missing from log line:\n{line}"
    assert int(m.group(1)) >= 0  # always a non-negative int
