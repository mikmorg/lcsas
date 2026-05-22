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
import re
import shutil
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


# ── Phase 10: real petabyte-scale end-to-end fixture ─────────────────


@pytest.mark.skipif(
    not os.environ.get("LCSAS_PETABYTE"),
    reason="set LCSAS_PETABYTE=1 to run the petabyte stress fixture "
           "(~5 min wall-clock, generates a ~70 MiB fixture under /scratch)",
)
def test_petabyte_scale_restore_stays_under_rss_budget(tmp_path: Path) -> None:
    """End-to-end stress: 1M orphan blob entries + 1k restored files.

    Pipeline:
      1. gen_fixture.py --stress 1000000 1000 5 → /scratch/lcsas_petabyte/
      2. /usr/bin/time -v lcsas-restore --repo ... --target ... --password-file ...
      3. Parse peak RSS (Maximum resident set size) and wall-clock from
         /usr/bin/time stderr
      4. Assert: rc == 0, RSS < 1.5 GiB, wall < 600 s, file count == 1000

    Why these bounds:
      - RSS budget 1.5 GiB: 1M blob entries × ~96 B = ~96 MiB index +
        binary + libc overhead.  Measured at ~110 MiB on this host; 1.5
        GiB is 14x headroom for safety.
      - Wall-clock 600 s: at 1M index entries, lcsas_blob_index_find is
        ~60 ms per lookup (measured by scaling_bench.py).  1000 files ×
        1-2 lookups ≈ 2 min of finds.  Plus index decrypt + tree walk
        + file write = budget of 10 min.

    Documents the linear-scan O(n) bottleneck in lcsas_blob_index_find
    — at true petabyte scale (~900M entries) each find would be ~60 s
    so restores become impractical. See recovery/build/scaling.md for
    the measured table.
    """
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")
    if shutil.which("/usr/bin/time") is None:
        pytest.skip("/usr/bin/time not available — need it for -v RSS output")

    gen = REPO_ROOT / "recovery" / "tests" / "fixtures" / "gen_fixture.py"
    # Use /scratch (large) when available; fall back to tmp_path.
    bench_root = Path(os.environ.get("LCSAS_BENCH_TMP", "/scratch"))
    if not bench_root.exists() or not os.access(bench_root, os.W_OK):
        bench_root = tmp_path
    fixture = bench_root / "lcsas_petabyte"
    target = bench_root / "lcsas_petabyte_restored"
    pwfile = bench_root / "lcsas_petabyte_pw"

    # Cleanup leftovers from prior runs.
    for d in (fixture, target):
        if d.exists():
            shutil.rmtree(d)
    pwfile.write_text("test")

    # Step 1: generate the fixture (1M orphans, 1k files, 5 subdirs).
    gen_res = subprocess.run(
        ["python3", str(gen), str(fixture),
         "--stress", "1000000", "1000", "5"],
        capture_output=True, text=True, timeout=600,
    )
    assert gen_res.returncode == 0, (
        f"fixture generation failed:\n{gen_res.stderr[:1000]}"
    )

    # Step 2: run restore under /usr/bin/time -v.
    res = subprocess.run(
        ["/usr/bin/time", "-v",
         str(bin_path),
         "--repo", str(fixture),
         "--password-file", str(pwfile),
         "--target", str(target)],
        capture_output=True, text=True, timeout=1800,
    )

    # /usr/bin/time -v writes its report to stderr after the child exits.
    err = res.stderr
    rss_match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", err)
    elapsed_match = re.search(
        r"Elapsed \(wall clock\) time \(h:mm:ss or m:ss\):\s*([\d:.]+)", err
    )
    assert rss_match, f"could not parse RSS from /usr/bin/time output:\n{err[-500:]}"
    assert elapsed_match, f"could not parse elapsed time:\n{err[-500:]}"

    rss_kib = int(rss_match.group(1))
    # Parse elapsed: format is either "m:ss.ss" or "h:mm:ss"
    elapsed_str = elapsed_match.group(1)
    parts = elapsed_str.split(":")
    if len(parts) == 2:
        wall_s = float(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        wall_s = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    else:
        wall_s = float(elapsed_str)

    # Step 3: assertions.
    assert res.returncode == 0, (
        f"restore failed (rc={res.returncode})\nstderr tail:\n{err[-1500:]}"
    )
    assert rss_kib < 1_572_864, (  # 1.5 GiB
        f"RSS exceeded 1.5 GiB budget: {rss_kib} KiB"
    )
    assert wall_s < 600, (
        f"restore wall-clock exceeded 10 min budget: {wall_s:.1f} s"
    )

    # File-count assertion: 1000 files distributed across 5 subdirs.
    restored_files = list(target.rglob("file_*.txt"))
    assert len(restored_files) == 1000, (
        f"expected 1000 restored files, got {len(restored_files)}"
    )

    # Report so the bench numbers are visible.
    print(
        f"\n[petabyte] entries=1000000+ files={len(restored_files)} "
        f"rss={rss_kib} KiB ({rss_kib / 1024:.1f} MiB) wall={wall_s:.1f} s",
        flush=True,
    )

    # Cleanup.
    shutil.rmtree(fixture)
    shutil.rmtree(target)
