"""
test_multi_disc_design_header.py -- static regression guard ensuring
MULTI_DISC_DESIGN.txt is clearly marked as a design-intent document and
cross-references the operator guide in RECOVER.txt (closes #110).

FAILURE MODE CAUGHT
-------------------
MULTI_DISC_DESIGN.txt contains detailed UX mockups and C API stubs for
features that have NOT all shipped.  Without a prominent design-intent
header an operator landing on this file could mistake unimplemented UX
sketches for current behavior, follow the wrong procedure, and fail to
recover their archive.

These tests guard against:
  * The design-intent banner ("DESIGN DOCUMENT") being removed.
  * The cross-reference to RECOVER.txt being removed.
  * The "WHAT HAS SHIPPED" section (shipped-feature inventory) being removed.
  * LCSAS_PACK_CACHE_DIR — the most critical shipped env var — being
    absent from the shipped-features section.

Tests are intentionally static (Path.read_text assertions only) — zero
runtime cost and survive in environments with no optical hardware.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_DESIGN_TXT = REPO_ROOT / "recovery" / "docs" / "MULTI_DISC_DESIGN.txt"

_DESIGN_TEXT = _DESIGN_TXT.read_text(encoding="utf-8")


def test_design_doc_has_banner():
    """MULTI_DISC_DESIGN.txt must contain the DESIGN DOCUMENT banner so
    operators know it describes intended future UX, not current behavior."""
    assert "DESIGN DOCUMENT" in _DESIGN_TEXT, (
        "MULTI_DISC_DESIGN.txt is missing the 'DESIGN DOCUMENT' banner. "
        "Without it, operators may mistake unimplemented UX mockups for "
        "current behavior and follow the wrong recovery procedure."
    )


def test_design_doc_cross_references_recover_txt():
    """MULTI_DISC_DESIGN.txt must reference RECOVER.txt so operators are
    directed to the actual operator guide rather than the design doc."""
    assert "RECOVER.txt" in _DESIGN_TEXT, (
        "MULTI_DISC_DESIGN.txt does not reference RECOVER.txt. "
        "Operators need to be directed to the current procedure documented "
        "in RECOVER.txt (MULTI-DISC RESTORE section)."
    )


def test_design_doc_has_what_has_shipped_section():
    """MULTI_DISC_DESIGN.txt must contain a WHAT HAS SHIPPED section so
    readers can quickly distinguish implemented features from design intent."""
    assert "WHAT HAS SHIPPED" in _DESIGN_TEXT, (
        "MULTI_DISC_DESIGN.txt is missing the 'WHAT HAS SHIPPED' section. "
        "Without it the document gives no indication of which parts of the "
        "design are live and which are still unimplemented."
    )


def test_design_doc_mentions_pack_cache_dir():
    """MULTI_DISC_DESIGN.txt must mention LCSAS_PACK_CACHE_DIR to reflect
    that the opportunistic pack cache has shipped (3-16x swap-frequency
    improvement proven in the v3 blind run)."""
    assert "LCSAS_PACK_CACHE_DIR" in _DESIGN_TEXT, (
        "MULTI_DISC_DESIGN.txt does not mention LCSAS_PACK_CACHE_DIR. "
        "This shipped env var is one of the most impactful improvements "
        "since the original design; its absence suggests the 'WHAT HAS "
        "SHIPPED' section is incomplete or has been removed."
    )
