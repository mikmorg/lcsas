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


def test_readiness_checklist_names_key_verify_drill():
    """Key-redundancy item must name the `lcsas key verify` drill [KEY-03].

    The annual escrow re-confirmation was prose-only and named no command;
    an operator had no documented way to prove the escrowed shares still
    unlock the repo (a silent re-key voids them undetectably).
    """
    # The root --config flag precedes the subcommand (argparse position
    # rules, enforced by tests/unit/test_doc_command_contract.py), so pin
    # the subcommand token rather than a fixed full command line.
    assert "key verify" in _TEXT, (
        "READINESS_CHECKLIST.txt does not name the 'key verify' drill. "
        "The annual key-redundancy drill must point operators at the "
        "command that proves the escrowed shares/password still unlock the "
        "repo (KEY-03)."
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


def test_readiness_checklist_has_blast_radius_review():
    """Checklist must document the monthly blast-radius review (FMA-08).

    The pre-mortem question — "all copies of ONE disc fail: which data
    is lost?" — is answered by `lcsas status --redundancy` (which discs
    are single points of failure) and `lcsas volume impact <LABEL>`
    (what exactly one disc's loss costs).  Without a checklist item the
    commands exist but nobody runs them until after a disc has failed.
    """
    assert "BLAST-RADIUS REVIEW" in _TEXT, (
        "READINESS_CHECKLIST.txt has no 'BLAST-RADIUS REVIEW' item. "
        "Operators need a monthly prompt to ask which discs are single "
        "points of failure (FMA-08)."
    )
    assert "lcsas status --redundancy" in _TEXT, (
        "READINESS_CHECKLIST.txt does not quote 'lcsas status "
        "--redundancy' — the command that lists under-replicated packs "
        "grouped by holding disc."
    )
    assert "lcsas volume impact" in _TEXT, (
        "READINESS_CHECKLIST.txt does not quote 'lcsas volume impact' — "
        "the per-disc blast-radius command."
    )


def test_readiness_checklist_has_journey_drill_log():
    """Checklist must carry the JOURNEY DRILL LOG section (UX-08).

    The blind-restore variants are the only end-to-end proof an heir can
    recover, but they are opt-in, cost-gated, and never run in CI — so the
    only durable record that they ran is this log.  If the section is
    deleted the audit trail vanishes silently.  Pin the section header and
    every variant row so a drop is caught at commit time.
    """
    assert "JOURNEY DRILL LOG" in _TEXT, (
        "READINESS_CHECKLIST.txt has no 'JOURNEY DRILL LOG' section. "
        "The opt-in blind-restore drills are uncovered by CI; this log is "
        "their only audit trail (UX-08)."
    )
    for variant in ("single-key", "split-2of5", "tier1-missing", "windows"):
        assert variant in _TEXT, (
            f"JOURNEY DRILL LOG is missing the '{variant}' variant row. "
            "Every blind-restore variant must have a dated drill-log line "
            "(UX-08)."
        )
    # The model column must pin haiku (project policy: blind drills run on
    # haiku only, never sonnet/opus).
    assert "haiku" in _TEXT.lower(), (
        "JOURNEY DRILL LOG does not record the model. Per project policy "
        "blind-restore drills run on haiku only; the log must say so so a "
        "future reader can trust the recorded score (UX-08)."
    )
