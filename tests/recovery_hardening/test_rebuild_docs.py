"""
test_rebuild_docs.py -- static regression guard for the mixed-age-discs
catalog-rebuild caveat in recovery/docs/RECOVER.txt (FMA-06).

FAILURE MODE CAUGHT
-------------------
Discs burned before a volume was deprecated or destroyed carry holographic
catalogs that still record it as VERIFIED.  The old rank-based merge
silently resurrected destroyed volumes during `lcsas catalog rebuild`; the
fix merges newest-first and prints a resurrection warning.  An operator
rebuilding a master catalog from a mixed-age disc box must find the caveat
(and the meaning of the warning) documented, or the warning output is
uninterpretable and the heir goes hunting for a shredded disc.

Tests are intentionally static (Path.read_text assertions only) — they add
zero runtime cost and survive in environments with no optical hardware.
The behavioural coverage lives in tests/unit/test_db_rebuild.py.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_RECOVER_TXT = REPO_ROOT / "recovery" / "docs" / "RECOVER.txt"

_RECOVER_TEXT = _RECOVER_TXT.read_text(encoding="utf-8")

_SECTION_HEADER = "REBUILDING THE MASTER CATALOG FROM MIXED-AGE DISCS"


def _section_window() -> str:
    """Return RECOVER.txt from the rebuild-caveat header onward."""
    idx = _RECOVER_TEXT.find(_SECTION_HEADER)
    assert idx != -1, (
        f"RECOVER.txt is missing the '{_SECTION_HEADER}' section. "
        "Operators rebuilding a catalog from mixed-age discs have no guide "
        "to the conflicting-status warnings (FMA-06)."
    )
    return _RECOVER_TEXT[idx:]


def test_recover_txt_has_mixed_age_rebuild_section():
    """RECOVER.txt must contain the mixed-age rebuild caveat section."""
    _section_window()


def test_rebuild_section_shows_the_command():
    """The section must show the actual rebuild invocation."""
    assert "lcsas catalog rebuild" in _section_window(), (
        "The mixed-age rebuild section of RECOVER.txt does not show the "
        "'lcsas catalog rebuild' command."
    )


def test_rebuild_section_explains_newest_first_merge():
    """The section must explain that discs are merged newest-first
    automatically, so operators know feed order is irrelevant."""
    window = _section_window()
    assert "NEWEST-FIRST" in window or "newest-first" in window, (
        "The mixed-age rebuild section of RECOVER.txt does not explain the "
        "automatic newest-first merge order."
    )
    assert "order" in window, (
        "The mixed-age rebuild section of RECOVER.txt does not state that "
        "disc feed order does not matter."
    )


def test_rebuild_section_explains_resurrection_warning():
    """The section must explain the resurrection warning and what the
    operator should do with a disc named in it (verify it physically)."""
    window = _section_window()
    assert "DESTROYED" in window and "VERIFIED" in window, (
        "The mixed-age rebuild section of RECOVER.txt does not show the "
        "stale-VERIFIED vs newer-DESTROYED conflict the warning reports."
    )
    assert "lcsas verify" in window and "--disc" in window, (
        "The mixed-age rebuild section of RECOVER.txt does not tell the "
        "operator to check a warned disc with 'lcsas verify <label> --disc'."
    )


def test_rebuild_section_documents_newest_session_staging_gap():
    """FMA-10: a disc cannot record its own burn (its holographic catalog
    is frozen at STAGING time), so the newest session looks unfinished
    after a disc-rebuild.  Operators must find that documented as EXPECTED
    plus the remedy, or they will mistake it for corruption / data loss."""
    window = _section_window()
    assert "STAGING" in window, (
        "RECOVER.txt does not document that the newest session's volumes "
        "appear as STAGING after a disc-rebuild (FMA-10)."
    )
    assert "EXPECTED" in window or "expected" in window, (
        "RECOVER.txt does not state that the STAGING/no-location appearance "
        "of the newest session is EXPECTED, not corruption (FMA-10)."
    )
    assert "import-receipts" in window, (
        "RECOVER.txt does not point operators at 'lcsas catalog "
        "import-receipts' to re-ingest the newest session's burn "
        "provenance (FMA-10)."
    )
