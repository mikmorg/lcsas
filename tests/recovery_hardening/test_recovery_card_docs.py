"""Hardening test: the single-key Recovery Card artifact + its references.

KEY-09 — for the DEFAULT (single-key) archive, the whole key-availability
story used to be the owner manually transcribing the password with no
transcription check of any kind.  A typo is discovered DECADES later, at
restore time, as an unrecoverable archive.  The fix ships a static
fill-in template (docs/RECOVERY_CARD.txt) plus a generator
(`lcsas key card`) carrying a SHA-256 check code.

These static assertions catch:
  * docs/RECOVERY_CARD.txt being deleted or moved.
  * The check-code computation one-liner being stripped from it
    (heirs lose the offline transcription check).
  * docs/ESTATE_PLANNING.md no longer pointing owners at the card.
  * The UX_CONCERNS ID 006 mitigation bullet going dangling again
    (the promise and reality drifting apart).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_CARD = REPO_ROOT / "docs" / "RECOVERY_CARD.txt"
ESTATE_PLANNING = REPO_ROOT / "docs" / "ESTATE_PLANNING.md"
UX_CONCERNS = REPO_ROOT / "recovery" / "docs" / "UX_CONCERNS.txt"


def test_recovery_card_exists() -> None:
    """docs/RECOVERY_CARD.txt must exist (UX_CONCERNS ID 006 mitigation)."""
    assert RECOVERY_CARD.is_file(), (
        f"docs/RECOVERY_CARD.txt is missing. UX_CONCERNS ID 006 promises a "
        f"paper-printable Recovery Card for the single-key archive; without "
        f"it the owner's only key-availability tool is hand transcription "
        f"with no check. Expected at: {RECOVERY_CARD}"
    )


def test_recovery_card_has_check_code_oneliner() -> None:
    """The card must carry the SHA-256 check-code one-liner for heirs."""
    content = RECOVERY_CARD.read_text()
    assert "hashlib.sha256" in content and "hexdigest()[:4]" in content, (
        "docs/RECOVERY_CARD.txt no longer contains the check-code one-liner "
        "(hashlib.sha256(...).hexdigest()[:4]). This is the offline "
        "transcription check that lets an owner/heir confirm a hand-copied "
        "password without any LCSAS binary installed."
    )


def test_recovery_card_documents_check_code_disclosure() -> None:
    """The card must state the check code's ~16-bit oracle disclosure."""
    content = RECOVERY_CARD.read_text()
    assert "16 bits" in content, (
        "docs/RECOVERY_CARD.txt must honestly state that the check code "
        "leaks ~16 bits of an oracle on the password (FUP-03 disclosure)."
    )


def test_estate_planning_references_recovery_card() -> None:
    """ESTATE_PLANNING.md must point owners at the Recovery Card."""
    content = ESTATE_PLANNING.read_text()
    assert "RECOVERY_CARD.txt" in content or "lcsas key card" in content, (
        "docs/ESTATE_PLANNING.md no longer references the Recovery Card "
        "(docs/RECOVERY_CARD.txt or `lcsas key card`). The card must be in "
        "the key-management checklist or owners will never know it exists."
    )


def test_ux_concerns_id006_mitigation_implemented() -> None:
    """UX_CONCERNS ID 006's Recovery Card bullet must be marked implemented."""
    content = UX_CONCERNS.read_text()
    assert "IMPLEMENTED (KEY-09)" in content, (
        "recovery/docs/UX_CONCERNS.txt ID 006 still describes the Recovery "
        "Card as unimplemented homework. The mitigation now exists "
        "(docs/RECOVERY_CARD.txt + `lcsas key card`) and the ledger must "
        "say so, or the promise and reality drift apart."
    )
    assert "lcsas key card" in content, (
        "UX_CONCERNS ID 006 should name the `lcsas key card` generator."
    )
