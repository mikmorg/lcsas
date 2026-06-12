"""Hardening tests: restore-side free-space preflight [FMA-09].

The burn side checks staging free space before writing a byte;
until FMA-09 the restore side — the path the non-technical heir
actually walks — had no space check at all, so a too-small target
failed with ENOSPC *after* a long disc-swapping session.

These tests pin:

* ``restore.sh`` with a 1 MiB tmpfs ``--target`` and a catalog
  describing a much larger archive exits with the
  required-vs-available sizing message BEFORE any password or disc
  prompt;
* ``LCSAS_SKIP_SPACE_CHECK=1`` bypasses the check and proceeds to
  the next prompt (the password prompt);
* the ``disc_locator.c`` drain guard refuses to drain into a cache
  on a critically-full (<10 % free) filesystem
  (``LCSAS_TEST_FULL_FS_DIR`` seam in the C unit test);
* the drain copy-failure branch (fs fills up MID-drain) unlinks the
  truncated destination — exercised by the C unit's
  ``RLIMIT_FSIZE`` case, which needs a roomy TMPDIR to get past the
  <10 %-free guard.

Skipped when the harness can't ``sudo mount``/``umount`` a tmpfs
(same gating as test_tier1_target_full.py); the C-unit tests also
skip when ``recovery/build/test_disc_locator`` has not been built.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
DISC_LOCATOR_TEST_BIN = REPO_ROOT / "recovery" / "build" / "test_disc_locator"

# Default rust-triple the script picks on Linux x86_64 hosts.
HOST_TARGET = "x86_64-unknown-linux-musl"

SIZING_SNIPPET = "Need about"


def _can_sudo_mount() -> bool:
    """True when we can mount/umount a tmpfs without prompting."""
    if shutil.which("mount") is None or shutil.which("umount") is None:
        return False
    res = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, timeout=5,
    )
    return res.returncode == 0


@contextlib.contextmanager
def _tmpfs(mnt: Path, size: str) -> Iterator[Path]:
    """Mount a user-writable tmpfs of ``size`` at ``mnt``; always unmount."""
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["sudo", "mount", "-t", "tmpfs",
         "-o", f"size={size},uid={os.getuid()},gid={os.getgid()},mode=0700",
         "tmpfs", str(mnt)],
        check=True, capture_output=True, timeout=10,
    )
    try:
        yield mnt
    finally:
        subprocess.run(
            ["sudo", "umount", "-l", str(mnt)],
            capture_output=True, timeout=10,
        )
        with contextlib.suppress(OSError):
            mnt.rmdir()


def _make_repo_skeleton(root: Path, name: str) -> Path:
    """Minimal restic-format-shaped repo dir at root/<name>."""
    repo = root / name
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    (repo / "snapshots").mkdir()
    (repo / "keys" / "stub_key").write_text("stub")
    return repo


def _install_stub_binary(recovery: Path, target: str, name: str) -> Path:
    """Stub tier binary.

    Mimics the two lcsas-restore behaviours the preflight relies on:
    ``--list-pending-packs`` prints the catalog "Total:" summary line
    (50 MB — far larger than the 1 MiB tmpfs targets used here); any
    other invocation echoes its argv (``ARG: ...``) and exits 0 so
    the tests can tell whether a tier actually ran.
    """
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        for a in "$@"; do
            if [ "$a" = "--list-pending-packs" ]; then
                printf 'Pending packs by disc:\\n'
                printf 'Total: 3 packs, 50.0 MB across 1 disc.\\n'
                exit 0
            fi
        done
        for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _write_catalog(path: Path, total_bytes: int) -> None:
    """Real SQLite catalog with one repo ('alpha') and packs summing
    to ``total_bytes`` — read by the sqlite3-CLI derivation path when
    that tool is on PATH (the stub binary covers hosts without it)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE repositories (
                repo_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                mirror_path TEXT NOT NULL DEFAULT '',
                encryption_key_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE packs (
                pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT UNIQUE NOT NULL,
                size_bytes INTEGER NOT NULL,
                repo_id TEXT NOT NULL,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO repositories (repo_id, name) VALUES ('alpha', 'alpha')"
        )
        conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, 'alpha')",
            ("aa" * 32, total_bytes),
        )
        conn.commit()
    finally:
        conn.close()


def _recovery_fixture(tmp_path: Path) -> Path:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    _write_catalog(recovery / "catalog.db", 50 * 1024 * 1024)
    return recovery


def _env(**extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
    }
    for var in (
        "LCSAS_PASSWORD",
        "LCSAS_PWFILE",
        "LCSAS_SKIP_SPACE_CHECK",
        "LCSAS_PACK_CACHE_DIR",
    ):
        env.pop(var, None)
    env.update(extra)
    return env


# ── restore.sh: tiny tmpfs target refused before any prompt ──────────


def test_restore_sh_refuses_tiny_target_before_password(tmp_path: Path) -> None:
    """A 1 MiB tmpfs target vs a 50 MB archive must exit with the
    required-vs-available message BEFORE the password prompt fires."""
    if not _can_sudo_mount():
        pytest.skip("passwordless sudo + mount/umount required")
    recovery = _recovery_fixture(tmp_path)

    with _tmpfs(Path(f"/tmp/lcsas-fma09-target-{os.getpid()}"), "1m") as mnt:
        res = subprocess.run(
            ["sh", str(RESTORE_SH), str(recovery), str(mnt / "restored")],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            env=_env(), timeout=30,
            start_new_session=True,
        )

    assert res.returncode != 0, (
        f"a too-small target must abort the restore; got rc=0.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert SIZING_SNIPPET in res.stderr, (
        f"the refusal must show the required-vs-available sizing line.\n"
        f"stderr:\n{res.stderr}"
    )
    assert "MB" in res.stderr and "available" in res.stderr, (
        f"the sizing line must name both sizes (50 MB archive vs 1 MiB "
        f"target).\nstderr:\n{res.stderr}"
    )
    assert "Password:" not in res.stderr, (
        f"the space check must fire BEFORE the password prompt — an heir "
        f"must not type a secret into a doomed run.\nstderr:\n{res.stderr}"
    )
    assert "ARG: " not in res.stdout, (
        f"no tier may run when the target is too small.\nstdout:\n{res.stdout}"
    )
    assert "LCSAS_SKIP_SPACE_CHECK" in res.stderr, (
        f"the refusal must name the bypass knob.\nstderr:\n{res.stderr}"
    )


def test_skip_space_check_proceeds_to_password_prompt(tmp_path: Path) -> None:
    """LCSAS_SKIP_SPACE_CHECK=1 bypasses the check: the run reaches the
    next prompt (Password:) despite the too-small target."""
    if not _can_sudo_mount():
        pytest.skip("passwordless sudo + mount/umount required")
    recovery = _recovery_fixture(tmp_path)

    with _tmpfs(Path(f"/tmp/lcsas-fma09-skip-{os.getpid()}"), "1m") as mnt:
        res = subprocess.run(
            ["sh", str(RESTORE_SH), str(recovery), str(mnt / "restored")],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            env=_env(LCSAS_SKIP_SPACE_CHECK="1"), timeout=30,
            start_new_session=True,
        )

    assert SIZING_SNIPPET not in res.stderr, (
        f"LCSAS_SKIP_SPACE_CHECK=1 must silence the sizing refusal.\n"
        f"stderr:\n{res.stderr}"
    )
    assert "Password:" in res.stderr, (
        f"with the check skipped the run must reach the password prompt.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


# ── disc_locator.c drain guard + mid-drain fs-full (C unit seams) ─────


def test_disc_locator_drain_guard_on_critically_full_fs(tmp_path: Path) -> None:
    """The drain guard must refuse a cache on a <10 %-free filesystem:
    warning on stderr, no pack copied, locate still succeeds (asserted
    inside the C unit via the LCSAS_TEST_FULL_FS_DIR seam)."""
    if not _can_sudo_mount():
        pytest.skip("passwordless sudo + mount/umount required")
    if not DISC_LOCATOR_TEST_BIN.is_file():
        pytest.skip("recovery/build/test_disc_locator not built "
                    "(run `make -C recovery`)")

    with _tmpfs(Path(f"/tmp/lcsas-fma09-full-{os.getpid()}"), "1m") as mnt:
        # Fill the 1 MiB tmpfs to ~94 % so statvfs reports <10 % free.
        (mnt / "filler").write_bytes(b"\0" * (960 * 1024))
        res = subprocess.run(
            [str(DISC_LOCATOR_TEST_BIN)],
            env={**os.environ, "LCSAS_TEST_FULL_FS_DIR": str(mnt)},
            capture_output=True, text=True, timeout=60,
        )

    assert res.returncode == 0, (
        f"C unit assertions failed (truncated/dirty cache or broken "
        f"locate).\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "disabling further drains" in res.stderr, (
        f"the critically-full guard must warn the operator once.\n"
        f"stderr:\n{res.stderr}"
    )
    assert "skipping critically-full drain-guard test" not in res.stderr, (
        f"the gated case must actually run (not self-skip).\n"
        f"stderr:\n{res.stderr}"
    )


def test_disc_locator_mid_drain_fs_full_unlinks_truncated(tmp_path: Path) -> None:
    """The RLIMIT_FSIZE drain case needs a roomy TMPDIR (>10 % free) to
    get past the guard; a 16 MiB tmpfs guarantees that even on hosts
    whose /tmp is nearly full.  The C unit then asserts the truncated
    cache copy is unlinked and the locate still succeeds."""
    if not _can_sudo_mount():
        pytest.skip("passwordless sudo + mount/umount required")
    if not DISC_LOCATOR_TEST_BIN.is_file():
        pytest.skip("recovery/build/test_disc_locator not built "
                    "(run `make -C recovery`)")

    with _tmpfs(Path(f"/tmp/lcsas-fma09-roomy-{os.getpid()}"), "16m") as mnt:
        res = subprocess.run(
            [str(DISC_LOCATOR_TEST_BIN)],
            env={**os.environ, "TMPDIR": str(mnt)},
            capture_output=True, text=True, timeout=60,
        )

    assert res.returncode == 0, (
        f"C unit assertions failed.\nstdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )
    assert "copy-failure branch is not exercised" not in res.stderr, (
        f"the roomy TMPDIR must let the RLIMIT case run for real.\n"
        f"stderr:\n{res.stderr}"
    )
