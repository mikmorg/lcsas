"""Hardening (#496): tier binaries that are present but can't be run.

A copy of the meta disc on a FAT/exFAT USB stick has no executable bits
(udisks mounts vfat ``showexec``), and a ``noexec`` mount refuses to run
anything; either way ``[ -x ]`` is false for intact binaries.  restore.sh
used to skip tiers 1-2 without a word and, with no python3, tell the heir
the disc "lacks all recovery binaries" and to find another copy.

Now: such binaries trigger relocation (the copy gets its bits back in a
directory that may run programs), tier 1 says why it is skipped when it
must be, and the last-resort message says the disc is fine.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"
EXIT_NO_RECOVERY_BIN = 64

STUB = textwrap.dedent("""\
    #!/bin/sh
    for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
    exit 0
""")


def _stub(path: Path, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB)
    path.chmod(mode)
    return path


def _python_free_path(tmp_path: Path) -> str:
    shim = tmp_path / "python_shim"
    shim.mkdir()
    for name in ("python", "python3"):
        p = shim / name
        p.write_text("#!/bin/sh\nexit 127\n")
        p.chmod(0o755)
    # /usr/local/bin (where a host restic/rustic usually lives) is left
    # out on purpose: a stock tool on PATH would satisfy the guard.
    return f"{shim}:/usr/bin:/bin"


def _repo(root: Path) -> Path:
    repo = root / "metadata" / "alpha"
    for sub in ("keys", "index", "data"):
        (repo / sub).mkdir(parents=True)
    return repo


def test_no_runnable_copy_says_the_disc_is_fine(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    _stub(recovery / "bin" / HOST_TARGET / "lcsas-restore", 0o644)
    _repo(recovery)
    env = {
        "HOME": str(tmp_path),
        "PATH": _python_free_path(tmp_path),
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "out"), "latest"],
        input="", capture_output=True, text=True, env=env, timeout=30,
    )
    assert res.returncode == EXIT_NO_RECOVERY_BIN, res.stderr
    assert "will not run them" in res.stderr, res.stderr
    assert "The disc itself is fine" in res.stderr, res.stderr
    assert "chmod -R u+x ~/lcsas-meta/recovery/bin" in res.stderr, res.stderr
    assert "lacks all recovery binaries" not in res.stderr, (
        "a present-but-unexecutable binary was blamed on the disc"
    )


def test_tier1_present_but_not_runnable_is_named(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    _stub(recovery / "bin" / HOST_TARGET / "lcsas-restore", 0o644)
    _stub(recovery / "bin" / HOST_TARGET / "rustic-static", 0o755)
    _repo(recovery)
    env = {
        "HOME": str(tmp_path),
        "PATH": _python_free_path(tmp_path),
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "out"), "latest"],
        input="pw\n", capture_output=True, text=True, env=env, timeout=30,
    )
    assert "present but this computer will not run it" in res.stderr, res.stderr
    assert "[tier 2]" in res.stderr, res.stderr


def test_unexecutable_bins_trigger_relocation_and_run(tmp_path: Path) -> None:
    """The writable-folder case (a USB-stick copy): nothing forced
    relocation before, so tier 1 never ran.  Now the relocated copy gets
    its executable bit back and tier 1 runs from it."""
    meta = tmp_path / "stick"
    meta.mkdir()
    (meta / "restore.sh").write_text(RESTORE_SH.read_text())
    (meta / "recovery" / "scripts").mkdir(parents=True)
    _stub(meta / "recovery" / "bin" / HOST_TARGET / "lcsas-restore", 0o644)
    _repo(meta)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TMPDIR": str(scratch),
        "LCSAS_MOUNT_DIRS": str(meta),
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
    }
    env.pop("LCSAS_RELOCATED", None)
    env.pop("LCSAS_NO_RELOCATE", None)
    env.pop("LCSAS_META_DISC", None)
    res = subprocess.run(
        ["sh", str(meta / "restore.sh"), str(tmp_path / "out")],
        input="pw\n", capture_output=True, text=True, env=env, timeout=60,
    )
    # Where it lands depends on the host: a TMPDIR on the same filesystem
    # as the "stick" is skipped (it counts as inside the meta mount).
    assert "copied recovery binaries to" in res.stderr, res.stderr
    assert "[tier 1] using prebuilt lcsas-restore" in res.stderr, res.stderr
    assert "ARG: --repo" in res.stdout, res.stdout


def test_too_small_candidate_is_skipped(tmp_path: Path) -> None:
    """A relocation dir without room for the bin/ tree (a small tmpfs
    such as /run/user/<uid>) used to get a silently partial copy.  Now
    it is skipped and the next candidate is used.  A `df` shim reports
    the first candidate as nearly full."""
    import shutil
    import uuid
    shm = Path("/dev/shm")
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        import pytest
        pytest.skip("/dev/shm not available")
    base = shm / f"lcsas-496-{uuid.uuid4().hex[:8]}"
    small, roomy = base / "small", base / "roomy"
    small.mkdir(parents=True)
    roomy.mkdir()
    try:
        meta = tmp_path / "stick"
        meta.mkdir()
        (meta / "restore.sh").write_text(RESTORE_SH.read_text())
        (meta / "recovery" / "scripts").mkdir(parents=True)
        _stub(meta / "recovery" / "bin" / HOST_TARGET / "lcsas-restore", 0o644)
        _repo(meta)
        shim = tmp_path / "df_shim"
        shim.mkdir()
        real_df = shutil.which("df") or "/usr/bin/df"
        (shim / "df").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            for a in "$@"; do
                case "$a" in {small}*)
                    echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
                    echo "fake 100 99 1 99% {small}"
                    exit 0 ;;
                esac
            done
            exec {real_df} "$@"
        """))
        (shim / "df").chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "TMPDIR": str(small),
            "XDG_RUNTIME_DIR": str(roomy),
            "LCSAS_MOUNT_DIRS": str(meta),
            "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
            "LCSAS_SKIP_SPACE_CHECK": "1",
        }
        for k in ("LCSAS_RELOCATED", "LCSAS_NO_RELOCATE", "LCSAS_META_DISC"):
            env.pop(k, None)
        res = subprocess.run(
            ["sh", str(meta / "restore.sh"), str(tmp_path / "out")],
            input="pw\n", capture_output=True, text=True, env=env, timeout=60,
        )
        assert f"{small} has 1 KB free" in res.stderr, res.stderr
        assert f"copied recovery binaries to {roomy}/lcsas-restore." in res.stderr, (
            res.stderr
        )
        assert "[tier 1] using prebuilt lcsas-restore" in res.stderr, res.stderr
    finally:
        shutil.rmtree(base, ignore_errors=True)
