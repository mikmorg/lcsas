"""Hardening test #10: restore.sh tier fallback under
LCSAS_TIER_FALLBACK=1.

The v3 blind run revealed that `restore.sh` `exec`s tier 1 and has
no fall-through if tier 1 crashes mid-restore.  The agent worked
around it by `mv lcsas-restore lcsas-restore.broken` — a real
human-in-chair wouldn't.  The user picked an opt-in fix:
`LCSAS_TIER_FALLBACK=1` runs each non-last tier as a subprocess
and falls through to the next tier on non-zero exit.

This test pins three properties of the opt-in fallback:

  • Without LCSAS_TIER_FALLBACK, a crashing tier 1 kills the run
    (default `exec` behavior — preserves the bare-minimum recovery
    story where tier 1 IS the recovery).
  • With LCSAS_TIER_FALLBACK=1 and a crashing tier 1, the script
    falls through to tier 2.
  • With LCSAS_TIER_FALLBACK=1 and tier 1 + tier 2 both crashing,
    the script reaches tier 3.

The companion verify.sh check #15 ("agent did not rename recovery
binaries") closes the loophole on the agent side; this test closes
it on the production-script side.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _install_failing_binary(recovery: Path, name: str,
                            exit_code: int = 17) -> Path:
    """A stub that prints a tier-failure banner and exits non-zero."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "FAKE {name}: simulated crash" >&2
        exit {exit_code}
    """))
    stub.chmod(0o755)
    return stub


def _install_succeeding_binary(recovery: Path, name: str) -> Path:
    """A stub that prints `SUCCESS_<NAME>` and exits 0."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "SUCCESS_{name}: stub ran"
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _make_repo(recovery: Path) -> None:
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    # data/ marks this as a single-disc fixture — tier 2 (rustic-static)
    # only runs when packs are local; multi-disc archives without a
    # local data/ now skip tier 2 entirely per issue #227.
    (repo / "data").mkdir()


def _run(recovery: Path, target_dir: Path, env_extra: dict[str, str],
         ) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        # These tests exercise tier dispatch with stub binaries; no
        # real data discs are mounted, so bypass the discovery gate.
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        **env_extra,
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_default_no_fallback_on_tier1_crash(tmp_path: Path) -> None:
    """Default behavior: tier 1 crash kills the run.  This preserves
    the bare-minimum recovery story — tier 1 IS the recovery, and
    failures must surface to the operator immediately."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={})
    # tier 1 was exec'd → its exit code propagates.
    assert res.returncode == 17, (
        f"default behavior should propagate tier-1 exit; got "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" not in res.stdout, (
        "without LCSAS_TIER_FALLBACK, tier 2 must NOT run after "
        "tier 1 — that would change default semantics."
    )


def test_fallback_to_tier2_on_tier1_crash(tmp_path: Path) -> None:
    """LCSAS_TIER_FALLBACK=1: tier 1 crashes → tier 2 runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={"LCSAS_TIER_FALLBACK": "1"})
    assert res.returncode == 0, (
        f"fallback should reach tier 2 and succeed; got "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" in res.stdout, (
        f"tier 2 stub did not run; stdout:\n{res.stdout}"
    )
    assert "falling through to tier 2" in res.stderr, (
        f"diagnostic banner missing; stderr:\n{res.stderr}"
    )


def test_fallback_to_tier3_when_tier1_and_tier2_crash(tmp_path: Path) -> None:
    """Both tier 1 and tier 2 crash → tier 3 runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_failing_binary(recovery, "rustic-static", exit_code=19)
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")

    # Stub python3 that just prints a marker and exits 0.
    pybin_dir = tmp_path / "stubbin"
    pybin_dir.mkdir()
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        # Stub binaries / no real data discs in the fixture — bypass the
        # discovery gate so this test exercises tier dispatch.
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"tier 3 should run and succeed; rc={res.returncode}\n"
        f"stdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "TIER3_PYTHON_RAN" in res.stdout, res.stdout
    assert "falling through to tier 3" in res.stderr, (
        f"tier-2-to-3 diagnostic missing:\n{res.stderr}"
    )


def _make_tier3_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Return (recovery, pybin_dir) for a tier-3-only fixture.

    Both tier-1 and tier-2 stubs crash.  standalone_restorer.py is
    placed at recovery.parent.  Caller is responsible for installing
    the desired stub python3 into the returned pybin_dir.
    """
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_failing_binary(recovery, "rustic-static", exit_code=19)
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")
    pybin_dir = tmp_path / "stubbin"
    pybin_dir.mkdir()
    return recovery, pybin_dir


def test_tier3_snap_args_non_latest(tmp_path: Path) -> None:
    """Passing a non-'latest' snapshot ID → TIER3_SNAP_ARGS is set."""
    recovery, pybin_dir = _make_tier3_fixture(tmp_path)
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{pybin_dir}:{os.environ.get('PATH', '')}",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "abc123snap"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, f"rc={res.returncode}\nstderr:{res.stderr}"
    assert "--snapshot abc123snap" in res.stdout, (
        "TIER3_SNAP_ARGS not forwarded to standalone restorer; "
        f"stdout:\n{res.stdout}"
    )


def test_tier3_pack_search_converted_to_mount_args(tmp_path: Path) -> None:
    """When a data disc with data/ is found, PACK_SEARCH_ARGS is rewritten
    to --mount-point args for tier-3 dispatch (line ~1097 of restore.sh)."""
    recovery, pybin_dir = _make_tier3_fixture(tmp_path)
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    # Create a fake mount parent with a disc subdir that has data/.
    # The pack-search walker adds it to PACK_SEARCH_ARGS only when
    # data/ exists under the disc subdir.
    mount_parent = tmp_path / "fake_mount"
    mount_parent.mkdir()
    disc_dir = mount_parent / "disc01"
    (disc_dir / "data").mkdir(parents=True)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": str(mount_parent),
        "LCSAS_TIER_FALLBACK": "1",
        "PATH": f"{pybin_dir}:{os.environ.get('PATH', '')}",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, f"rc={res.returncode}\nstderr:{res.stderr}"
    assert "--mount-point" in res.stdout, (
        "PACK_SEARCH_ARGS not converted to --mount-point for tier 3; "
        f"stdout:\n{res.stdout}"
    )


def test_tier3_mount_dirs_appended_when_not_in_pack_search(tmp_path: Path) -> None:
    """LCSAS_MOUNT_DIRS entries not already in PACK_SEARCH_ARGS are added
    as additional --mount-point args for tier-3 (lines 1101-1109)."""
    recovery, pybin_dir = _make_tier3_fixture(tmp_path)
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    # A mount parent without a data/ inside → PACK_SEARCH_ARGS stays empty.
    # The parent itself is still added as --mount-point by the MOUNT_DIRS
    # dedup loop (lines 1101-1109) because it isn't already covered.
    mount_parent = tmp_path / "fake_mount"
    mount_parent.mkdir()

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": str(mount_parent),
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{pybin_dir}:{os.environ.get('PATH', '')}",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, f"rc={res.returncode}\nstderr:{res.stderr}"
    assert str(mount_parent) in res.stdout, (
        "LCSAS_MOUNT_DIRS entry not forwarded as --mount-point to tier 3; "
        f"stdout:\n{res.stdout}"
    )


def test_tier3_pythonpath_set_from_bundled_zstandard(tmp_path: Path) -> None:
    """When tools/lib/python3.X/zstandard/ exists under META_ROOT,
    PYTHONPATH is exported so tier-3 can import zstandard (lines 1138-1146)."""
    recovery, pybin_dir = _make_tier3_fixture(tmp_path)
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        # Print PYTHONPATH so the test can assert it's set.
        echo "PYTHONPATH=${PYTHONPATH:-unset}"
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    # Create the bundled zstandard layout: recovery/../tools/lib/python3.12/
    meta_root = tmp_path  # META_ROOT = dirname(RECOVERY) = tmp_path
    zstd_dir = meta_root / "tools" / "lib" / "python3.12" / "zstandard"
    zstd_dir.mkdir(parents=True)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{pybin_dir}:{os.environ.get('PATH', '')}",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, f"rc={res.returncode}\nstderr:{res.stderr}"
    zstd_libdir = str(meta_root / "tools" / "lib" / "python3.12")
    assert "bundled zstandard" in res.stderr, (
        "PYTHONPATH for zstandard not logged to stderr; "
        f"stderr:\n{res.stderr}"
    )
    assert zstd_libdir in res.stdout, (
        f"zstandard lib dir not in PYTHONPATH; stdout:\n{res.stdout}"
    )


def test_tier3_failure_stderr_replayed(tmp_path: Path) -> None:
    """When tier-3 exits non-zero and wrote to stderr, restore.sh
    replays the captured stderr with a labelled separator (lines 1220-1223)."""
    recovery, pybin_dir = _make_tier3_fixture(tmp_path)
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "ImportError: No module named 'zstandard'" >&2
        exit 1
    """))
    (pybin_dir / "python3").chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{pybin_dir}:{os.environ.get('PATH', '')}",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 1, (
        f"tier-3 failure should propagate; got rc={res.returncode}"
    )
    assert "[tier 3] FAILED" in res.stderr, (
        f"tier-3 failure banner missing from stderr:\n{res.stderr}"
    )
    assert "zstandard" in res.stderr, (
        f"captured tier-3 stderr not replayed:\n{res.stderr}"
    )


def test_fallback_preserves_success_when_tier1_works(tmp_path: Path) -> None:
    """LCSAS_TIER_FALLBACK=1 + tier 1 succeeds → exit 0, no tier 2."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_succeeding_binary(recovery, "lcsas-restore")
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={"LCSAS_TIER_FALLBACK": "1"})
    assert res.returncode == 0, res.stderr
    assert "SUCCESS_lcsas-restore" in res.stdout
    assert "SUCCESS_rustic-static" not in res.stdout, (
        "tier 2 ran after a successful tier 1 — the fallback path "
        "is short-circuiting incorrectly."
    )
