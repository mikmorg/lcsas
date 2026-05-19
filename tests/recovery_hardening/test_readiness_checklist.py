"""
test_readiness_checklist.py -- static regression guard for the operator
production-readiness checklist in recovery/docs/READINESS_CHECKLIST.txt.

FAILURE MODE CAUGHT
-------------------
The checklist exists to give operators a documented pre-flight gate before
relying on an LCSAS archive in a real disaster.  Without a guard test, the
file can be silently deleted (e.g. by a bad merge, an accidental `git rm`,
or a directory restructure) and nobody notices until an operator shows up
during an emergency and finds no checklist.

These tests assert that the checklist file exists, is non-empty, and retains
the key operator-facing content that motivated its creation (issue #112):
  * the sha256sum audit step
  * the offsite key storage requirement
  * the monthly and annual cadence indicators
  * the test-restore verification step
  * the MANIFEST.sha256 reference

Tests are intentionally static (Path.read_text assertions only) — they add
zero runtime cost and survive in environments with no optical hardware.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKLIST = REPO_ROOT / "recovery" / "docs" / "READINESS_CHECKLIST.txt"

# Read the file once at module level — tests are static string checks only.
_TEXT = _CHECKLIST.read_text(encoding="utf-8")


def test_readiness_checklist_exists_and_nonempty():
    """READINESS_CHECKLIST.txt must exist and contain operator-readable text."""
    assert _CHECKLIST.exists(), (
        "recovery/docs/READINESS_CHECKLIST.txt is missing. "
        "Operators have no documented pre-flight gate before relying on the "
        "archive. Re-create the file (see issue #112)."
    )
    assert len(_TEXT.strip()) > 0, (
        "recovery/docs/READINESS_CHECKLIST.txt exists but is empty. "
        "The file was likely corrupted or truncated."
    )


def test_readiness_checklist_has_sha256sum():
    """Checklist must document the sha256sum binary audit step."""
    assert "sha256sum" in _TEXT, (
        "READINESS_CHECKLIST.txt does not mention 'sha256sum'. "
        "The binary audit step (sha256sum -c MANIFEST.sha256) must be "
        "documented so operators can verify recovery binaries before relying "
        "on them."
    )


def test_readiness_checklist_has_offsite():
    """Checklist must document the offsite key/copy requirement."""
    assert "offsite" in _TEXT, (
        "READINESS_CHECKLIST.txt does not mention 'offsite'. "
        "The requirement to store the encryption key and a disc copy at an "
        "offsite location must be documented — losing the only copy of the key "
        "or the only set of discs to a single incident is unrecoverable."
    )


def test_readiness_checklist_has_test_restore():
    """Checklist must document the test-restore verification step."""
    assert "test-restore" in _TEXT, (
        "READINESS_CHECKLIST.txt does not mention 'test-restore'. "
        "The step to run a test restore and verify file count must be "
        "documented so operators confirm the archive is actually readable "
        "before a real disaster."
    )


def test_readiness_checklist_has_cadence_indicators():
    """Checklist must document both monthly and annual maintenance cadences."""
    assert "monthly" in _TEXT.lower(), (
        "READINESS_CHECKLIST.txt does not mention 'monthly'. "
        "The monthly maintenance cadence (test-restore, disc scan, volume "
        "count check) must be documented."
    )
    assert "annually" in _TEXT.lower(), (
        "READINESS_CHECKLIST.txt does not mention 'annually'. "
        "The annual drill and key-escrow review cadence must be documented."
    )


def test_readiness_checklist_references_manifest():
    """Checklist must reference MANIFEST.sha256 for the binary audit."""
    assert "MANIFEST.sha256" in _TEXT, (
        "READINESS_CHECKLIST.txt does not reference 'MANIFEST.sha256'. "
        "The binary audit section must name the manifest file so operators "
        "know which file to run sha256sum against."
    )
