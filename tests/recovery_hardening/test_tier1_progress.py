"""Hardening test for tier-1 restore progress output (recommendation #9).

The v3 blind-restore transcript flagged a "did it freeze?" concern:
tier 3 (`restic_fallback.py`) prints one banner ("Loaded index: N blobs
across M index files.") at startup and then silently restores; tier 1
(`lcsas-restore`) was even quieter.  An operator watching a multi-disc
restore had no way to tell whether the binary was making progress or
had wedged on a stalled read.

The fix: ``lcsas-restore`` now emits ``[lcsas-restore] progress:
N/M blobs, X MB`` to stderr at restore start, every ~16 blobs OR
~1 MB of decoded data (whichever fires first), and at restore end.

This test pins three properties of that contract:

  * ``progress:`` lines appear in stderr for a restore that processes
    more than the per-tick threshold (>= 32 blobs).
  * At least one progress line matches the canonical pattern
    ``\\[lcsas-restore\\] progress: \\d+/\\d+ blobs, \\d+ MB``.
  * Progress is interleaved with the restore (a progress line shows
    up *before* "restore complete"), not just dumped at the end —
    otherwise the anti-freeze UX value is zero.

What this catches:
  - Regression that removes ``lcsas_progress_tick`` from the inner
    blob loop in ``tree.c`` (the operator sees only the start banner
    and the final summary, with nothing in between for a long restore).
  - A refactor that swaps ``stderr`` for ``stdout`` and breaks the
    contract that progress is on stderr (so it composes cleanly with
    pipelines that capture restore output).
  - Changing the format string in a way that breaks the canonical
    regex used by downstream log scrapers / dashboards.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
# ``LCSAS_RESTORE_BIN`` overrides the default build location so the
# audit's parallel-instrumented builds (coverage-c #150, sanitiser
# #152) can point this test at an alternate binary without forking.
BINARY = Path(os.environ["LCSAS_RESTORE_BIN"]) if os.environ.get(
    "LCSAS_RESTORE_BIN"
) else RECOVERY / "build" / "lcsas-restore"

# Reuse the e2e fixture builder so we get a real restic-format repo
# the C binary can parse.  This is the cheapest way to exercise the
# whole tree -> blob -> tick code path; a unit test that stubbed
# read_blob would not catch the "tick called from the wrong place"
# regressions this hardening test exists for.
sys.path.insert(0, str(RECOVERY / "tests"))
sys.path.insert(0, str(REPO_ROOT / "src"))

PROGRESS_RE = re.compile(
    r"\[lcsas-restore\] progress: (\d+)/(\d+) blobs, (\d+) MB"
)


def _build_repo_or_skip(tmp: Path) -> tuple[Path, Path, Path]:
    """Build a synthetic repo with >= 32 blobs.  Returns (repo, target, pw).

    Skips the test (rather than failing) if the tier-1 binary isn't
    built or the e2e fixture helper can't be imported -- this matches
    the rest of recovery_hardening's "skip-when-toolchain-absent" rule.
    """
    _override = os.environ.get("LCSAS_RESTORE_BIN")
    if _override and not (BINARY.is_file() and os.access(BINARY, os.X_OK)):
        pytest.skip(f"LCSAS_RESTORE_BIN={_override!r} not executable")
    if not BINARY.exists():
        pytest.skip(f"tier-1 binary not built at {BINARY} "
                    f"(run `make -C recovery build/lcsas-restore`)")
    try:
        import test_e2e  # type: ignore  # noqa: F401
    except ImportError as e:
        pytest.skip(f"recovery/tests/test_e2e helper not importable: {e}")
    import test_e2e  # type: ignore

    repo = tmp / "repo"
    target = tmp / "out"
    pwfile = tmp / "pw"
    pwfile.write_text("p\n")
    # 40 files -> 40 data blobs + 1 tree blob = 41 blobs, well above
    # the 16-blob per-tick threshold so we are guaranteed >= 2 ticks
    # (one mid-restore, one final summary).
    files = {f"file_{i:03d}.bin": os.urandom(64) for i in range(40)}
    test_e2e.build_repo(repo, "p", files, v2=False)
    return repo, target, pwfile


def _run_restore(repo: Path, target: Path, pwfile: Path
                 ) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BINARY),
         "--repo", str(repo),
         "--password-file", str(pwfile),
         "--target", str(target),
         "--snapshot", "latest"],
        capture_output=True, text=True, timeout=30,
    )


def test_progress_lines_emitted() -> None:
    """Restore over >= 32 blobs must emit at least one canonical
    progress line on stderr."""
    tmp = Path(tempfile.mkdtemp(prefix="lcsas_progress_"))
    try:
        repo, target, pw = _build_repo_or_skip(tmp)
        res = _run_restore(repo, target, pw)
        assert res.returncode == 0, (
            f"restore failed: rc={res.returncode}\n"
            f"stdout:{res.stdout}\nstderr:{res.stderr}"
        )
        matches = PROGRESS_RE.findall(res.stderr)
        assert matches, (
            "no canonical progress line in stderr; the operator gets "
            f"no anti-freeze signal during long restores.\nstderr:\n"
            f"{res.stderr}"
        )
        # Sanity: progress denominator is non-zero (so N/M is informative).
        for done, total, _mb in matches:
            assert int(total) > 0, (
                f"progress denominator is 0 -- the format string lost the "
                f"index size: {done}/{total}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_is_interleaved_not_only_final() -> None:
    """A progress line must appear *before* the 'restore complete'
    banner -- otherwise the anti-freeze UX value is zero (the operator
    only sees motion after the run is already done)."""
    tmp = Path(tempfile.mkdtemp(prefix="lcsas_progress_inter_"))
    try:
        repo, target, pw = _build_repo_or_skip(tmp)
        res = _run_restore(repo, target, pw)
        assert res.returncode == 0, res.stderr

        # Find positions of the first progress line and the 'restore
        # complete' line.  Both should be present; progress should be
        # strictly first.
        complete_match = re.search(r"restore complete", res.stderr)
        progress_match = PROGRESS_RE.search(res.stderr)
        assert complete_match, (
            f"missing 'restore complete' banner; stderr:\n{res.stderr}"
        )
        assert progress_match, (
            f"missing progress line; stderr:\n{res.stderr}"
        )
        assert progress_match.start() < complete_match.start(), (
            "progress line appears AFTER 'restore complete' -- it's not "
            "actually surfacing during-restore progress, just a final "
            "dump.  This defeats the anti-freeze UX.\n"
            f"stderr:\n{res.stderr}"
        )

        # And require at least one mid-restore progress line (not just
        # the start banner with 0/N).  With 40 data blobs the inner
        # loop should tick at blob 16, 32, and 40 (final summary).
        non_zero = [m for m in PROGRESS_RE.findall(res.stderr)
                    if int(m[0]) > 0]
        assert non_zero, (
            "only the 0/N start banner appeared; the inner-loop tick "
            "is not running.\nstderr:\n" + res.stderr
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
