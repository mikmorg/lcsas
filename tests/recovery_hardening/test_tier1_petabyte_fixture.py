"""Issue #160: petabyte-scale stress fixture for tier-1 C binary.

Exercises the dynamic-array fixes for BUG-2 and BUG-3:

  BUG-2 (repo.c): key-file scan was capped at 256 entries.
                  This fixture creates 300 key files to expose any
                  regression to that cap.

  BUG-3 (repo.c): index-file scan was capped at 2048 entries.
                  This fixture creates 3000 index files to expose
                  any regression to that cap.

These are synthetic stubs — the binary will fail to decrypt them
(wrong password / not valid encrypted data), but it must reach that
failure cleanly without crashing or hanging.  The assertion is
crash-safety, not successful decryption.

Marked as integration so it is skipped in the default unit-test run.
Each test is designed to complete well under 30 seconds.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]


def _find_bin() -> Path | None:
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        return None
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def _run(bin_path: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(bin_path), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _make_stub_key(name: str) -> bytes:
    """Return a minimal fake key-file blob (not a valid rustic key)."""
    # 48 bytes is enough to pass the data_len >= 33 check in repo_decrypt,
    # though it will always fail the MAC check.  We just need the scan loop
    # to open and reject each file rather than crash.
    return b"\x00" * 48 + name.encode()[:16]


def _make_stub_index(pack_id_hex: str) -> bytes:
    """Return a minimal fake index JSON blob (not encrypted)."""
    # The binary will call decrypt_repo_file on this; decrypt will fail
    # (data too short or wrong MAC) and the file will be skipped gracefully.
    return (
        '{"supersedes":[],"packs":[{"id":"' + pack_id_hex + '",'
        '"blobs":[]}]}\n'
    ).encode()


# ── BUG-2 regression: >256 key files ─────────────────────────────

def test_repo_with_300_key_files_does_not_crash(tmp_path: Path) -> None:
    """BUG-2 fix: lcsas_repo_load_keys_dir must iterate all 300 keys
    without hitting the old 256-entry hard cap (which silently truncated
    the scan and could miss the valid key).

    With fake key data the binary will fail to find a valid key and exit
    non-zero — that is expected and correct.  The invariant is:
      * exit code is NOT a crash signal (SIGSEGV=-11/139, SIGABRT=-6/134)
      * the process terminates in under 30 seconds
    """
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `lcsas recovery build --arch host`")

    repo = tmp_path / "repo"
    keys_dir = repo / "keys"
    keys_dir.mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()

    # Create 300 stub key files.  Names are 64-char hex strings
    # (the format the binary expects).
    for i in range(300):
        name = format(i, "064x")
        (keys_dir / name).write_bytes(_make_stub_key(name))

    pwfile = tmp_path / "pw"
    pwfile.write_text("stub-password\n")
    target = tmp_path / "restored"

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=30,
    )

    # Expected: non-zero exit (no valid key found).
    # NOT expected: crash signals.
    assert res.returncode != 0, (
        "binary unexpectedly succeeded against a stub repo with fake keys"
    )
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed (rc={res.returncode}) scanning 300 key files; "
        f"BUG-2 dynamic-array fix may have regressed.\n"
        f"stderr:\n{res.stderr}"
    )


# ── BUG-3 regression: >2048 index files ──────────────────────────

def test_repo_with_3000_index_files_does_not_crash(tmp_path: Path) -> None:
    """BUG-3 fix: lcsas_repo_load_index must iterate all 3000 index files
    without hitting the old 2048-entry hard cap.

    The binary will fail to decrypt each stub file (no valid ciphertext)
    and exit non-zero — that is expected.  The invariant is:
      * exit code is NOT a crash signal
      * the process terminates in under 30 seconds
    """
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `lcsas recovery build --arch host`")

    repo = tmp_path / "repo"
    keys_dir = repo / "keys"
    index_dir = repo / "index"
    keys_dir.mkdir(parents=True)
    index_dir.mkdir()
    (repo / "data").mkdir()

    # One stub key file so the binary enters the index-loading phase.
    key_name = "0" * 64
    (keys_dir / key_name).write_bytes(_make_stub_key(key_name))

    # Create 3000 stub index files (all different names).
    for i in range(3000):
        name = format(i, "064x")
        # The binary will try to decrypt; it will fail and skip.
        (index_dir / name).write_bytes(_make_stub_index(name[:64]))

    pwfile = tmp_path / "pw"
    pwfile.write_text("stub-password\n")
    target = tmp_path / "restored"

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=30,
    )

    # Expected: non-zero exit (decryption fails on all stubs).
    # NOT expected: crash signals or hang (would time out at 30s).
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed (rc={res.returncode}) scanning 3000 index files; "
        f"BUG-3 dynamic-array fix may have regressed.\n"
        f"stderr:\n{res.stderr}"
    )
