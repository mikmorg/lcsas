"""Issue #245 — tier-1 must NOT ftruncate to expected_size on failure.

Surfaced during PR #239 blind-restore (14/15 twice running): one random
file came back as 130 KiB of zero bytes whose SHA matched
sha256(b'\\x00' * 133120) exactly.  The 130 KiB was the file's full
declared size — meaning the binary opened the file, failed to write
its content (transient pack-read fail on a multi-disc fixture), but
then unconditionally ftruncated the empty fd to the snapshot's
declared size and exited "successfully".  The #92 idempotent-resume
check then sees a file at the right size on the retry pass and
skips it — leaving the zero artifact in place permanently.

This test pins: when a blob can't be read (because its pack file is
missing from the repo), the partially-restored file MUST NOT have
its on-disk size extended to expected_size.  The restore can fail —
that's fine and expected — but the on-disk artifact must reflect the
actual written bytes so a resume pass re-attempts it.
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


def _find_bin() -> Path:
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    pytest.skip("no lcsas-restore binary; run `make -C recovery`")


@pytest.mark.skipif(
    shutil.which("rustic") is None,
    reason="rustic CLI required to build a real fixture",
)
def test_failed_restore_does_not_leave_zero_padded_file(tmp_path: Path) -> None:
    """A pack-not-found mid-restore must NOT pad the partial file with
    zeros to its declared size.  Pre-fix (issue #245), the unguarded
    ftruncate-on-failure left every failed-blob file as a sparse hole
    of expected_size — indistinguishable from a successful restore by
    the #92 idempotent-resume check, so retries silently skipped it
    and the operator got 130 KiB of zeros for one random file.
    """
    bin_path = _find_bin()

    # Build a small real-rustic repo with one file large enough to
    # span at least one blob.  130 KiB matches the blind-fixture file
    # size that surfaced the bug.
    src = tmp_path / "src"
    src.mkdir()
    payload = os.urandom(130 * 1024)
    (src / "file_000.bin").write_bytes(payload)

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test")

    env = {**os.environ, "RUSTIC_PASSWORD_FILE": str(pwfile)}
    subprocess.run(
        ["rustic", "-r", str(repo), "init"],
        check=True, env=env, capture_output=True, timeout=30,
    )
    subprocess.run(
        ["rustic", "-r", str(repo), "backup", str(src)],
        check=True, env=env, capture_output=True, timeout=30,
    )

    # Sabotage the repo SURGICALLY: rustic typically writes the file's
    # content blobs and the tree-walk blobs into DIFFERENT pack files.
    # We need the tree-pack to remain readable (so the binary reaches
    # the file-create code path) but the data-pack(s) for file_000.bin
    # to be missing (so the per-blob read fails inside the file's
    # content loop).  The reliable signal: the LARGEST pack is the
    # data pack (rustic puts ~256 KiB of compressed content there);
    # the small one is the tree pack.
    data_dir = repo / "data"
    packs = sorted(
        (p for p in data_dir.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    assert packs, "fixture had no packs to remove"
    # Remove the largest pack(s) — those carry the file's content
    # blobs.  Keep the smallest (tree pack) intact.
    if len(packs) > 1:
        for victim in packs[:-1]:
            victim.unlink()
    else:
        # Single-pack fixture (rare with rustic's defaults) — nuke
        # it but the tree won't be readable either; test will skip
        # below if no file gets created.
        packs[0].unlink()

    # Run lcsas-restore.  We expect it to FAIL (non-zero) — the test
    # is about the on-disk artifact, not the exit code.
    target = tmp_path / "out"
    res = subprocess.run(
        [str(bin_path),
         "--repo", str(repo),
         "--password-file", str(pwfile),
         "--target", str(target),
         "--interactive", "off"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode != 0, (
        f"restore should have failed with all packs missing; "
        f"rc={res.returncode}\nstderr:\n{res.stderr}"
    )

    # Find the partially-restored file.  Path inside `target` may
    # mirror src's absolute path or be flattened — search.
    restored = list(target.rglob("file_000.bin")) if target.exists() else []

    # If the file wasn't created at all, that's also acceptable — the
    # bug is specifically about creating a zero-padded file at the
    # expected size.  Skip the assertion in that case.
    if not restored:
        return

    assert len(restored) == 1, f"unexpected restored copies: {restored}"
    restored_file = restored[0]
    actual_size = restored_file.stat().st_size

    # The CORE assertion: a failed restore must NOT leave the file
    # padded to the full declared size, because the idempotent-resume
    # logic uses size as a "this file is done" signal and would skip
    # it on the retry pass.
    assert actual_size != 130 * 1024, (
        f"failed restore left a zero-padded {actual_size}-byte file — "
        f"this is the issue #245 bug (sparse hole pretending to be a "
        f"complete restore).  The #92 idempotent-resume check would "
        f"see this size match the snapshot's declared size and skip "
        f"it on the retry pass, locking in the zero content."
    )

    # And belt-and-braces: if anything WAS written, it had better not
    # be all zeros at full size (the specific bug signature).
    if actual_size > 0:
        content = restored_file.read_bytes()
        all_zero = all(b == 0 for b in content)
        assert not (all_zero and actual_size == 130 * 1024), (
            "partial file is exactly the bug signature: 130 KiB of zeros"
        )
