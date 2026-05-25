"""Tier-1 operator-error fault handling tests.

These pin the C binary's behaviour when an operator hands it bad
inputs that aren't a crash but ARE a recovery failure: the wrong
password, a truncated pack file, or an unknown snapshot ID.  In
every case the binary must exit non-zero AND print a useful error
message so the operator knows what went wrong, NOT silently
succeed with garbage output or crash with a stacktrace.

Issue traceability:
  - test_wrong_password_fails_with_clear_error   -> #219
  - test_truncated_pack_fails_with_clear_error   -> #220
  - test_wrong_snapshot_id_fails_with_clear_error -> #226
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]
FIXTURE_REPO = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo"


def _find_bin() -> Path:
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        pytest.skip(f"LCSAS_RESTORE_BIN={path} not executable")
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    pytest.skip(
        "no lcsas-restore binary; run `lcsas recovery build --arch host`"
    )


def _require_fixture() -> Path:
    if FIXTURE_REPO.is_dir() and (FIXTURE_REPO / "keys").is_dir():
        return FIXTURE_REPO
    pytest.skip("fixture repo not generated; run gen_fixture.py")


def _run(
    bin_path: Path, *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(bin_path), *args],
        capture_output=True, text=True,
        env=full_env, timeout=timeout,
    )


# ── Issue #219: wrong-password handling ─────────────────────────────


def test_wrong_password_fails_with_clear_error(tmp_path: Path) -> None:
    """Closes #219.  Operator typo: the wrong password is supplied to a
    valid fixture repo.  The binary must exit non-zero with a message
    that points the operator at the password — not crash, and not
    silently appear to succeed."""
    bin_path = _find_bin()
    repo = _require_fixture()
    target = tmp_path / "restored"
    pwfile = tmp_path / "wrong-pw"
    pwfile.write_text("not-the-real-password")

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=30,
    )

    # Must NOT crash (SIGSEGV=-11/139, SIGABRT=-6/134).
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed on wrong password (rc={res.returncode}); "
        f"stderr:\n{res.stderr}"
    )
    # Must exit non-zero.
    assert res.returncode != 0, (
        f"binary appeared to succeed with the wrong password "
        f"(rc=0); stderr:\n{res.stderr}"
    )
    # Must name the cause.  The message in main.c is:
    #   "ERROR: could not decrypt any key file (wrong password?)"
    err = (res.stdout + res.stderr).lower()
    assert "wrong password" in err or "could not decrypt" in err, (
        f"wrong-password error message missing or unclear; stderr:\n"
        f"{res.stderr}"
    )
    # Restore target must NOT contain any restored files — wrong
    # password means we should have bailed before writing anything.
    if target.exists():
        leftovers = [p for p in target.rglob("*") if p.is_file()]
        assert leftovers == [], (
            f"wrong-password run produced output files: {leftovers}"
        )


# ── Issue #220: truncated pack handling ─────────────────────────────


def test_truncated_pack_fails_with_clear_error(tmp_path: Path) -> None:
    """Closes #220.  A pack file present on disc but shorter than the
    index says it should be (truncated during burn, partial copy, ECC
    failure, etc).  The binary must detect the short read and exit
    non-zero with a message that names the pack — not silently produce
    a partial restore."""
    bin_path = _find_bin()
    src = _require_fixture()

    # Copy the fixture so we don't mutate the shared one.
    repo = tmp_path / "repo_truncated"
    shutil.copytree(src, repo)

    # Locate the single pack file under data/ and truncate it.
    pack_files = list((repo / "data").rglob("*"))
    pack_files = [p for p in pack_files if p.is_file()]
    assert pack_files, "fixture has no data/ pack files — regenerate"
    victim = pack_files[0]
    original_size = victim.stat().st_size
    assert original_size > 32, (
        f"pack {victim} is suspiciously small ({original_size} bytes); "
        "fixture may be malformed"
    )
    # Truncate to ~half so even the index-declared offset of the first
    # blob almost certainly falls past EOF.
    with victim.open("r+b") as fh:
        fh.truncate(original_size // 2)
    assert victim.stat().st_size == original_size // 2

    pwfile = tmp_path / "pw"
    pwfile.write_text("test")
    target = tmp_path / "restored"

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=30,
    )

    # Must NOT crash.
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed on truncated pack (rc={res.returncode}); "
        f"stderr:\n{res.stderr}"
    )
    # Must exit non-zero.
    assert res.returncode != 0, (
        f"binary appeared to succeed with truncated pack (rc=0); "
        f"stderr:\n{res.stderr}"
    )
    # Must surface a recognisable corruption diagnostic.  Depending on
    # how far the truncation cut, the failure can surface as the explicit
    # pack-truncated check we added in lcsas_repo_read_blob (issue #220),
    # as a zstd-decompression error on a referenced blob/index, or as a
    # generic "tree restore failed" once the read cascade gives up.  All
    # three are legitimate clear failures from the operator's POV — the
    # binary refused to silently produce a bad restore.
    err = (res.stdout + res.stderr).lower()
    assert ("pack truncated" in err
            or "pack read failed" in err
            or "short read" in err
            or "zstd" in err
            or "tree restore failed" in err
            or "decompression failed" in err), (
        f"truncated-pack error message missing or unclear; stderr:\n"
        f"{res.stderr}"
    )


# ── Issue #226: wrong-snapshot-id handling ──────────────────────────


def test_wrong_snapshot_id_fails_with_clear_error(tmp_path: Path) -> None:
    """Closes #226.  Operator typo: a snapshot ID that doesn't exist
    in the repository is supplied.  The binary must say "snapshot not
    found" (or equivalent) and exit non-zero — not silently fall back
    to "latest" or crash."""
    bin_path = _find_bin()
    repo = _require_fixture()
    pwfile = tmp_path / "pw"
    pwfile.write_text("test")
    target = tmp_path / "restored"

    # A syntactically-valid hex ID that won't match anything.
    bogus_id = "f" * 64

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--snapshot", bogus_id,
        timeout=30,
    )

    # Must NOT crash.
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed on unknown snapshot id (rc={res.returncode}); "
        f"stderr:\n{res.stderr}"
    )
    # Must exit non-zero.
    assert res.returncode != 0, (
        f"binary appeared to succeed with unknown snapshot id (rc=0); "
        f"stderr:\n{res.stderr}"
    )
    # Must name the cause.
    err = (res.stdout + res.stderr).lower()
    assert "snapshot not found" in err, (
        f"unknown-snapshot error message missing or unclear; stderr:\n"
        f"{res.stderr}"
    )
    # The bogus ID should appear in the error so the operator knows
    # which ID was rejected.
    assert bogus_id in (res.stdout + res.stderr), (
        f"error did not include the rejected snapshot id; stderr:\n"
        f"{res.stderr}"
    )
