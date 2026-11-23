"""#968 items 16 to 19: sessions and cleanup.

16. ``session abort`` of a burned session says it cleaned the session,
    not "deleted 0 volume(s)".
17. ``burn --session <cleaned> --dry-run`` says the ISOs are gone.
18. ``stage --clean`` prints the session id it cleaned, not "latest".
19. ``catalog import-receipts`` leaves the session as a local burn does:
    COMPLETE (or PARTIAL after a failed disc), and a durable burn receipt
    beside the catalog.

Real rustic, real xorriso, every command through ``main()``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _run(inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
         *argv: str) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = inst.lcsas(*argv)
    return rc, caplog.text


def _staged(root: Path) -> ScratchInstall:
    inst = ScratchInstall(root, repos=("alpha",))
    inst.lcsas("location", "add", "Home_Shelf")
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    return inst


def _session_status(inst: ScratchInstall, session: str) -> str:
    ((status,),) = inst.query(
        "SELECT status FROM burn_sessions WHERE session_id = ?", session)
    return str(status)


# -- 16 ----------------------------------------------------------------------

def test_abort_of_a_burned_session_says_cleaned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)

    rc, out = _run(inst, caplog, "session", "abort", session)
    assert rc == 0, out
    assert "deleted 0 volume(s)" not in out, out
    assert f"Cleaned session {session}" in out, out
    assert _session_status(inst, session) == "CLEANED"


# -- 17 ----------------------------------------------------------------------

def test_dry_run_of_a_cleaned_session_says_the_isos_are_gone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)
    (label,) = inst.labels()
    assert inst.lcsas("stage", "--clean", "--session", session) == 0

    rc, out = _run(inst, caplog, "burn", "--session", session, "--dry-run",
                   "--location", "Home_Shelf", "--reburn")
    assert rc == 0, out
    line = next(x for x in out.splitlines() if label in x)
    assert "ISO is gone" in line, out


# -- 18 ----------------------------------------------------------------------

def test_stage_clean_prints_the_session_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    rc, out = _run(inst, caplog, "stage", "--clean", "--force")
    assert rc == 0, out
    assert "Cleaned session: latest" not in out, out
    assert session in out.splitlines()[-1], out


# -- 19 ----------------------------------------------------------------------

def _receipt(inst: ScratchInstall, out: Path, *, passed: bool) -> Path:
    """The receipt ``burn-iso --emit-receipt`` writes for the staged ISO."""
    ((label, sha, size),) = inst.query(
        "SELECT v.label, sv.iso_sha256, sv.iso_size_bytes "
        "FROM session_volumes sv JOIN volumes v USING (volume_id)")
    path = out / f"{label}_Home_Shelf.json"
    path.write_text(json.dumps({
        "volume_label": label,
        "session_id": None,
        "location": "Home_Shelf",
        "device": "/dev/sr0",
        "burn_date": "2026-11-23T10:00:00.000000+00:00",
        "iso_sha256": sha,
        "iso_size_bytes": size,
        "verify_passed": passed,
        "device_sha256": sha if passed else None,
        "verify_only": False,
    }, indent=2))
    return path


@pytest.mark.parametrize("passed, status", [(True, "COMPLETE"),
                                            (False, "PARTIAL")])
def test_receipt_import_leaves_the_session_as_a_local_burn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    passed: bool, status: str,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    (label,) = inst.labels()
    elsewhere = tmp_path / "carried"
    elsewhere.mkdir()
    receipt = _receipt(inst, elsewhere, passed=passed)

    rc, out = _run(inst, caplog, "catalog", "import-receipts", str(receipt))
    assert rc == 0, out
    assert _session_status(inst, session) == status

    durable = sorted((inst.db.parent / "receipts").glob("*.json"))
    assert [p.name for p in durable] == [
        f"{session.replace(':', '-')}_{label}_Home_Shelf.json"], durable
    kept = json.loads(durable[0].read_text())
    assert kept["volume_label"] == label
    assert kept["verify_passed"] is passed
    assert kept["session_id"] == session

    # The documented replay of the durable receipts: nothing changes, and
    # no receipt is copied onto itself.
    before = durable[0].read_bytes()
    rc, out = _run(inst, caplog, "catalog", "import-receipts",
                   *(str(p) for p in durable))
    assert rc == 0, out
    assert "already imported" in out, out
    assert durable[0].read_bytes() == before
    assert _session_status(inst, session) == status
