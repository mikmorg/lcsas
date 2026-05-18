"""Hardening test: tier-3 pure-Python restorer emits periodic progress.

Tier-3 restore (pure-Python `restic_fallback.PurePythonRestorer`) is
slow (~1 MB/s).  Without periodic stderr output an operator watching a
quiet terminal cannot tell "still working" from "frozen" and may abort
a valid restore.  Recommendation #9 from the blind-restore audit
required that the restorer emit a periodic ``[restic-fallback]`` line
showing N/M files + MB transferred while it works.

What this test catches:
  - Removal of the periodic progress emission entirely.
  - The standalone bundle (concatenated by `standalone_builder`)
    drifting out of sync — e.g. someone edits the source but forgets
    to verify the bundle still picks up the new code, or someone
    accidentally renames a helper such that it can't be referenced
    from the concatenated form.
  - A regression that breaks the ``LCSAS_PROGRESS=0`` escape hatch
    (operators must be able to silence the chatter on quiet hosts).

The test deliberately monkeypatches the file/byte thresholds down so
the tiny synthetic repo (2 files, ~30 bytes) trips the threshold.  In
production the defaults are intentionally chunkier so progress isn't
spammy on a normal restore.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Re-use the existing synthetic-repo builders from the unit-test suite
# rather than re-deriving them — keeps the two suites in lockstep so a
# format change can't drift between them.
_UNIT_DIR = Path(__file__).resolve().parents[1] / "unit"
sys.path.insert(0, str(_UNIT_DIR))
from test_restic_fallback import (  # noqa: E402
    PASSWORD,
    _build_test_repo,
)

from lcsas.restore import restic_fallback  # noqa: E402
from lcsas.restore.restic_fallback import PurePythonRestorer  # noqa: E402
from lcsas.restore.standalone_builder import build_standalone  # noqa: E402

# Pattern the operator sees in the wild — banner prefix, "N/M", and
# a megabyte counter.  Pinned permissively (\.\d) on MB so a future
# formatter change doesn't break the test unnecessarily, but the
# core "files restored, X MB" structure is locked in.
_PROGRESS_RE = re.compile(
    r"\[restic-fallback\].*\d+/\d+ files? restored,\s*[\d.]+\s*MB",
)


def test_progress_line_emitted_during_restore(tmp_path, capsys, monkeypatch):
    """A progress line lands on stderr between the start and end banners."""
    # Force the thresholds down so the 2-file synthetic repo trips them.
    monkeypatch.setattr(restic_fallback, "_PROGRESS_FILES_INTERVAL", 1)
    monkeypatch.setattr(restic_fallback, "_PROGRESS_BYTES_INTERVAL", 1)
    # Keep progress enabled regardless of host env.
    monkeypatch.setenv("LCSAS_PROGRESS", "1")

    repo = _build_test_repo(tmp_path)
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=tmp_path / "restored")

    err = capsys.readouterr().err
    progress_lines = [ln for ln in err.splitlines() if _PROGRESS_RE.search(ln)]
    assert progress_lines, (
        f"No progress line matched {_PROGRESS_RE.pattern!r} in stderr:\n{err}"
    )

    # Sanity: at least one progress line appears *before* the final
    # "Restore complete." banner — otherwise the operator only sees
    # output after the work is already done, defeating the purpose.
    complete_idx = err.find("Restore complete.")
    assert complete_idx >= 0, "Restore-complete banner missing"
    first_progress_idx = err.find(progress_lines[0])
    assert first_progress_idx < complete_idx, (
        "Progress line emitted only after 'Restore complete.' — should "
        "land while work is in progress, not after."
    )


def test_progress_silenced_by_env_var(tmp_path, capsys, monkeypatch):
    """LCSAS_PROGRESS=0 fully silences the progress output."""
    # Even with thresholds set to fire on every file, the env-var gate
    # should suppress the output.
    monkeypatch.setattr(restic_fallback, "_PROGRESS_FILES_INTERVAL", 1)
    monkeypatch.setattr(restic_fallback, "_PROGRESS_BYTES_INTERVAL", 1)
    monkeypatch.setenv("LCSAS_PROGRESS", "0")

    repo = _build_test_repo(tmp_path)
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=tmp_path / "restored")

    err = capsys.readouterr().err
    assert not _PROGRESS_RE.search(err), (
        f"Progress line emitted despite LCSAS_PROGRESS=0:\n{err}"
    )


def test_progress_final_line_at_completion(tmp_path, capsys, monkeypatch):
    """The last progress line reports the full N/N total, not a partial."""
    # Use default thresholds (which won't fire on a tiny repo) so the
    # ONLY progress line comes from the force-emit at the end.  That
    # final line must show all files done, not e.g. 1/2.
    monkeypatch.setenv("LCSAS_PROGRESS", "1")

    repo = _build_test_repo(tmp_path)
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=tmp_path / "restored")

    err = capsys.readouterr().err
    matches = _PROGRESS_RE.findall(err)
    assert matches, f"No progress line found in:\n{err}"

    # Pull the N/M counters off the last progress line and verify
    # N == M (i.e. the restore reported full completion).
    nm_re = re.compile(r"(\d+)/(\d+) files? restored")
    last_match = None
    for ln in err.splitlines():
        m = nm_re.search(ln)
        if m:
            last_match = m
    assert last_match is not None
    done, total = int(last_match.group(1)), int(last_match.group(2))
    assert done == total == 2, (
        f"Final progress line showed {done}/{total}, expected 2/2"
    )


def test_standalone_bundle_includes_progress_code():
    """The generated standalone_restorer.py inherits the progress logic.

    `standalone_builder.build_standalone()` concatenates `_aes_pure.py`
    and `restic_fallback.py` into a single self-contained script for
    burning onto every disc.  If the concatenation logic ever drops a
    function (e.g. someone renames the source file or changes the
    import-stripping regex), the bundle silently loses code — which
    only surfaces during an actual blind restore.

    This test asserts that the new progress helpers make it into the
    bundle AND that the bundle compiles cleanly.
    """
    bundle = build_standalone()

    # Compile-check first — if this fails, all the substring checks
    # below would be misleading.
    compile(bundle, "<standalone_restorer.py>", "exec")

    for sentinel in (
        "_PROGRESS_FILES_INTERVAL",
        "_PROGRESS_BYTES_INTERVAL",
        "_emit_progress",
        "_count_files",
        "LCSAS_PROGRESS",
    ):
        assert sentinel in bundle, (
            f"Standalone bundle is missing '{sentinel}' — the "
            f"concatenation step may have dropped the progress code."
        )


def test_progress_line_format_human_readable():
    """The progress line format documented in the audit is preserved.

    The blind-restore audit specifically requested an `N/M files
    restored, X MB` style line.  Lock that exact shape so a future
    refactor doesn't silently drop the byte count or the slash form.
    """
    sample = "[restic-fallback] Progress: 7/100 files restored, 12.3 MB."
    assert _PROGRESS_RE.search(sample), (
        "Reference progress line no longer matches the documented "
        "format — update either the format or the regex deliberately."
    )


@pytest.mark.parametrize("env_value", ["1", "yes", "on", ""])
def test_progress_default_on_for_truthy_envs(env_value, tmp_path, capsys, monkeypatch):
    """Only the literal LCSAS_PROGRESS=0 silences progress.

    Defensive: an operator setting LCSAS_PROGRESS to anything other
    than "0" (including unset) must continue to see progress output.
    """
    monkeypatch.setattr(restic_fallback, "_PROGRESS_FILES_INTERVAL", 1)
    monkeypatch.setattr(restic_fallback, "_PROGRESS_BYTES_INTERVAL", 1)
    if env_value == "":
        monkeypatch.delenv("LCSAS_PROGRESS", raising=False)
    else:
        monkeypatch.setenv("LCSAS_PROGRESS", env_value)

    repo = _build_test_repo(tmp_path)
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=tmp_path / "restored")

    err = capsys.readouterr().err
    assert _PROGRESS_RE.search(err), (
        f"Progress unexpectedly silenced with LCSAS_PROGRESS={env_value!r}:\n{err}"
    )
