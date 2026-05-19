"""
test_disc_swap_docs.py -- static regression guard for the MULTI-DISC RESTORE
operator guide in recovery/docs/RECOVER.txt.

FAILURE MODE CAUGHT
-------------------
The automated test rig uses a setuid C wrapper (disc-loader) to simulate disc
swaps without human interaction.  Real operators must physically eject and
insert discs.  For a period no operator-facing documentation of that process
existed; a passing blind-restore test gave false confidence that the UX was
smooth (UX_CONCERNS.txt ID 011, closes GitHub #106).

These tests guard against the documentation regressing out of RECOVER.txt by
asserting the presence of the key strings that make the section useful:
  * the section header itself
  * the physical eject command that operators need
  * the LCSAS_PACK_CACHE_DIR env-var knob that reduces swap frequency
  * closure of UX concern ID 005 (the swap-prompt clarity concern)

Tests are intentionally static (Path.read_text assertions only) — they add
zero runtime cost and survive in environments with no optical hardware.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_RECOVER_TXT = REPO_ROOT / "recovery" / "docs" / "RECOVER.txt"
_UX_CONCERNS_TXT = REPO_ROOT / "recovery" / "docs" / "UX_CONCERNS.txt"

# Read each file once at module level — tests are static string checks only.
_RECOVER_TEXT = _RECOVER_TXT.read_text(encoding="utf-8")
_UX_CONCERNS_TEXT = _UX_CONCERNS_TXT.read_text(encoding="utf-8")


def test_recover_txt_has_multi_disc_section():
    """RECOVER.txt must contain the MULTI-DISC RESTORE section header."""
    assert "MULTI-DISC RESTORE" in _RECOVER_TEXT, (
        "RECOVER.txt is missing the MULTI-DISC RESTORE section. "
        "Operators swapping discs during recovery have no guide."
    )


def test_recover_txt_mentions_eject():
    """RECOVER.txt must document the physical eject command."""
    assert "eject" in _RECOVER_TEXT, (
        "RECOVER.txt does not mention 'eject'. "
        "The physical disc-swap step (eject /dev/sr0) must be documented."
    )


def test_recover_txt_mentions_pack_cache():
    """RECOVER.txt must document LCSAS_PACK_CACHE_DIR to inform operators
    that the cache reduces swap frequency."""
    assert "LCSAS_PACK_CACHE_DIR" in _RECOVER_TEXT, (
        "RECOVER.txt does not mention LCSAS_PACK_CACHE_DIR. "
        "Operators need to know about the pack cache that reduces disc swaps."
    )


def test_ux_concerns_id005_is_closed():
    """UX_CONCERNS.txt ID 005 must be marked CLOSED following the doc update."""
    idx = _UX_CONCERNS_TEXT.find("ID 005")
    assert idx != -1, "UX_CONCERNS.txt is missing the ID 005 entry entirely."
    # Check within a bounded window so the test is robust to unrelated edits
    # elsewhere in the file while still catching a STATUS revert to OPEN.
    window = _UX_CONCERNS_TEXT[idx: idx + 300]
    assert "CLOSED" in window, (
        "UX_CONCERNS.txt ID 005 does not show STATUS: CLOSED within the first "
        "300 characters of its block. This concern was resolved by the "
        "MULTI-DISC RESTORE section in RECOVER.txt."
    )
