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


def test_no_unconditional_pwfile_delete_before_tier2_fallthrough() -> None:
    """Between tier-1's exit-code capture and its first terminal branch
    there must be NO `del "%PWFILE%"` — deleting there starves the
    tier-2 fallthrough of the password."""
    block = _tier1_block()
    rc_capture = block.index('set "RC=!ERRORLEVEL!"')
    first_branch = block.index("if !RC! equ", rc_capture)
    between = block[rc_capture:first_branch]
    assert 'del "%PWFILE%"' not in between, (
        "restore.bat deletes %PWFILE% unconditionally after tier 1 — "
        "the tier-2 fallthrough then always gets a dead --password-file "
        "(issue #384 review finding)."
    )


def test_terminal_branches_delete_pwfile() -> None:
    """Both terminal tier-1 branches (success and wrong-password 77)
    must still shred the temp password file before exiting."""
    block = _tier1_block()
    for marker in ("if !RC! equ 0 (", "if !RC! equ 77 ("):
        branch_start = block.index(marker)
        branch_exit = block.index("exit /b", branch_start)
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
