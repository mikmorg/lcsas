"""
test_keyshare_docs.py -- static guard: heir docs name the C combiner [KEY-05].

FAILURE MODE CAUGHT
-------------------
Phase 5 shipped a tier-1-grade static combiner (lcsas-keyshare) on all six
targets so split-key reconstruction needs no Python, and a blind run proved
that path works.  But every heir-facing document said only ``python3
keyshare_combine.py``; RECOVER_WINDOWS.txt and restore.bat had zero key-share
mentions (bare Windows has no python3 at all); and RECOVER.txt flatly claimed
"There is no password recovery path" — false for split-key archives.  An heir
holding K valid share cards on a python-less host dead-ended with no
documented way forward.

These tests pin the key-share pre-step into the four static surfaces an heir
can reach (RECOVER.txt, RECOVER_WINDOWS.txt, restore.bat, RECOVERY_GUIDE.md).
The rendered START_HERE.txt / KEY_INFO.txt ordering is pinned separately by
tests/unit/test_staging_metadata.py.

Tests are intentionally static (Path.read_text assertions only) — they add
zero runtime cost and survive in environments with no optical hardware.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Read each file once at module level — tests are static string checks only.
_RECOVER = (REPO_ROOT / "recovery" / "docs" / "RECOVER.txt").read_text(
    encoding="utf-8"
)
_RECOVER_WIN = (
    REPO_ROOT / "recovery" / "docs" / "RECOVER_WINDOWS.txt"
).read_text(encoding="utf-8")
_RESTORE_BAT = (
    REPO_ROOT / "recovery" / "scripts" / "restore.bat"
).read_text(encoding="utf-8")
_GUIDE = (REPO_ROOT / "docs" / "RECOVERY_GUIDE.md").read_text(
    encoding="utf-8"
)


def test_recover_txt_has_key_shares_section():
    """RECOVER.txt must carry a key-shares section naming the static
    combiner (primary) and the python fallback."""
    idx = _RECOVER.find("KEY SHARES (SPLIT PASSWORDS)")
    assert idx != -1, (
        "RECOVER.txt is missing the KEY SHARES (SPLIT PASSWORDS) section. "
        "Split-key heirs have no documented reconstruct-first pre-step."
    )
    window = _RECOVER[idx:]
    assert "lcsas-keyshare" in window, (
        "RECOVER.txt's key-shares section does not name the static "
        "lcsas-keyshare combiner (the python-free tier-1 path)."
    )
    assert "keyshare_combine.py" in window, (
        "RECOVER.txt's key-shares section does not name the pure-Python "
        "fallback combiner."
    )


def test_recover_txt_password_recovery_carries_split_exception():
    """The 'no password recovery path' claim must carry the split-key
    exception — it is false for split-key archives."""
    idx = _RECOVER.find("PASSWORD RECOVERY")
    assert idx != -1, "RECOVER.txt is missing the PASSWORD RECOVERY section."
    window = _RECOVER[idx: idx + 400]
    assert "no password recovery path" in window, (
        "PASSWORD RECOVERY section lost its core claim sentence."
    )
    assert "split" in window.lower(), (
        "RECOVER.txt's 'no password recovery path' sentence does not carry "
        "the split-key exception. An heir holding valid share cards would "
        "read it as 'data unrecoverable' and stop."
    )


def test_recover_windows_has_key_shares_section():
    """RECOVER_WINDOWS.txt must document lcsas-keyshare.exe — bare
    Windows has no python3, so the .exe is the only combiner there."""
    assert "KEY SHARES" in _RECOVER_WIN, (
        "RECOVER_WINDOWS.txt is missing a KEY SHARES section."
    )
    assert "lcsas-keyshare.exe" in _RECOVER_WIN, (
        "RECOVER_WINDOWS.txt does not name lcsas-keyshare.exe. A split-key "
        "heir on bare Windows (no python3) has no documented combiner."
    )


def test_restore_bat_points_split_key_users_at_keyshare():
    """restore.bat must mention lcsas-keyshare.exe so a split-key user
    sitting at the Password: prompt learns about the pre-step."""
    assert "lcsas-keyshare.exe" in _RESTORE_BAT, (
        "restore.bat does not mention lcsas-keyshare.exe. Split-key users "
        "reach the Password: prompt with no pointer to the share combiner."
    )
    assert "KEY_INFO.txt" in _RESTORE_BAT, (
        "restore.bat does not point users at KEY_INFO.txt (which says "
        "whether the archive's password is split)."
    )


def test_recovery_guide_has_split_key_prestep():
    """The printable RECOVERY_GUIDE.md must carry the conditional
    'if KEY_INFO.txt says the password is split' pre-step."""
    assert "lcsas-keyshare" in _GUIDE, (
        "docs/RECOVERY_GUIDE.md (the printed-binder guide) does not name "
        "the lcsas-keyshare combiner for split-key archives."
    )
    assert "KEY_INFO.txt" in _GUIDE, (
        "docs/RECOVERY_GUIDE.md does not tell readers KEY_INFO.txt is "
        "where to check whether the password was split."
    )
