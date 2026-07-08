"""restore.bat temp-password-file lifetime (issue #384 review finding).

restore.bat writes the typed password to a transient %PWFILE% and hands
it to each tier via --password-file.  A prior revision deleted %PWFILE%
unconditionally right after tier-1's exit-code capture, which made the
tier-2 fallthrough structurally dead on Windows: rustic-static.exe was
always invoked with a --password-file pointing at an already-deleted
file, so the documented tier-2 hedge could never succeed.

These are structural (text-shape) checks on the committed script — the
behavioural proof needs a real Windows/wine host, but the shape that
caused the bug is cheap to pin: the tier-1 block may only delete
%PWFILE% inside its TERMINAL branches (success / wrong-password), never
on the fall-through path.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_BAT = REPO_ROOT / "recovery" / "scripts" / "restore.bat"


def _tier1_block() -> str:
    text = RESTORE_BAT.read_text()
    start = text.index("----- Tier 1:")
    end = text.index("----- Tier 2:")
    return text[start:end]


def test_no_pwfile_delete_on_tier1_fallthrough_path() -> None:
    """No `del "%PWFILE%"` may sit on the tier-1 FALL-THROUGH path — i.e.
    anywhere in the block outside the two terminal branch bodies
    (success rc==0, wrong-password rc==77).  A delete there — before OR
    after the 77 branch — starves the tier-2 fallthrough of its
    password (issue #384 review finding).  Checking only up to the first
    branch would miss a delete reintroduced between the 77 branch and
    the tier-2 heading."""
    block = _tier1_block()

    # Carve out the two terminal branch bodies; whatever `del` remains is
    # on the fall-through path.  A terminal branch ends at whichever
    # comes first: an `exit /b` or a `goto :` (the 77 branch leaves the
    # block via `goto :wrong_password` so its exit propagates from top
    # level — see restore.bat).
    def _branch_body(marker: str) -> str:
        start = block.index(marker)
        exit_at = min(
            (i for i in (block.find("exit /b", start),
                         block.find("goto :", start)) if i != -1),
            default=len(block),
        )
        return block[start:exit_at]

    fallthrough = block
    for marker in ("if !RC! equ 0 (", "if !RC! equ 77 ("):
        fallthrough = fallthrough.replace(_branch_body(marker), "")

    # Only look after the rc capture (the invocation line above it has no
    # del; the point is the post-run dispatch).
    fallthrough = fallthrough[fallthrough.index('set "RC=!ERRORLEVEL!"'):]
    assert 'del "%PWFILE%"' not in fallthrough, (
        "restore.bat deletes %PWFILE% on the tier-1 fall-through path — "
        "the tier-2 fallthrough then gets a dead --password-file "
        "(issue #384 review finding)."
    )


def test_terminal_branches_delete_pwfile() -> None:
    """Both terminal tier-1 branches (success and wrong-password 77)
    must still shred the temp password file before exiting."""
    block = _tier1_block()
    for marker in ("if !RC! equ 0 (", "if !RC! equ 77 ("):
        branch_start = block.index(marker)
        # 77 branch leaves via `goto :wrong_password`; success via `exit /b`.
        branch_exit = min(
            (i for i in (block.find("exit /b", branch_start),
                         block.find("goto :", branch_start)) if i != -1),
            default=len(block),
        )
        branch = block[branch_start:branch_exit]
        assert 'del "%PWFILE%"' in branch, (
            f"tier-1 branch {marker!r} exits without deleting %PWFILE% — "
            "the plaintext password would outlive the run."
        )


def test_every_terminal_path_deletes_pwfile() -> None:
    """Belt-and-braces: the script must contain deletes for tier-2's
    paths and the no-tier epilogue too (they pre-date #384; pin them so
    a refactor cannot drop one silently)."""
    text = RESTORE_BAT.read_text()
    dels = len(re.findall(r'del "%PWFILE%"', text))
    # tier-1 success + tier-1 77 + tier-2 + epilogue
    assert dels >= 4, (
        f"expected >=4 `del \"%PWFILE%\"` sites (tier-1 success/77, "
        f"tier-2, epilogue); found {dels}"
    )
