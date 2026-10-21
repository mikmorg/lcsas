"""Integration: the documented portable burn loop, end to end (issue #517).

Pins the "no test chains the two halves" gap in
``docs/workflows/burn-iso-portable.md`` ("Remote burner -- assemble on A, burn
on B, reconcile on A").  Until now ``burn-iso`` and ``catalog import-receipts``
were only tested against each other's *hand-written* JSON, so nothing proved
that the receipt ``burn-iso`` really emits for a really staged ISO is what the
importer needs -- and the staged-ISO label mismatch (#514: the default label was
the session directory, not the volume label) went unnoticed for exactly that
reason.

The loop, with the same real tools the production path uses:

1. **A: stage** -- real rustic repo, ``lcsas init / repo add / scan / stage``
   through ``main()``, real xorriso mastering a TEST_TINY ISO.  The volume is
   ``STAGING`` and ``session_volumes`` records the ISO hash/size.
2. **Transport** the ISO to **B** (a directory with no catalog and a neutral
   name, so the parent-directory fallback could not produce the label).
3. **B: ``burn-iso <iso> --emit-receipt <dir> --location ...``** with no
   ``--config``, no ``--label``.  Only the drive is mocked: ``burn_iso``,
   ``verify_disc``, ``media_status`` and ``read_disc_volume_id`` (the latter
   answers what a burned disc really reports -- the ISO's own PVD volume ID,
   read here by an independent parser) with an unplugged device name, as
   ``tests/unit/test_burn_iso_receipt_io.py`` does on the batch-1 branch.
4. **Transport** the receipt back to **A** and ``catalog import-receipts`` it.

Requires: ``rustic`` and ``xorriso`` on PATH (self-skip otherwise).
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.iso.xorriso import MediaStatus
from lcsas.utils.hashing import sha256_file

# `make test-integration` selects with `-m integration`; without this the
# module is deselected and runs in no gate target at all (#426).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_RUNNER = "lcsas.iso.xorriso.SubprocessXorrisoRunner"
_FAKE_DEVICE = "/dev/lcsas-test-no-such-drive"
_SITE = "Offsite_Safe"
_SECOND_SITE = "Cold_Vault"


def _pvd_volume_id(iso: Path) -> str:
    """The ISO 9660 primary volume descriptor's volume ID (independent parser)."""
    with open(iso, "rb") as f:
        f.seek(16 * 2048)
        pvd = f.read(2048)
    assert pvd[0] == 1 and pvd[1:6] == b"CD001", "not an ISO 9660 image"
    return pvd[40:72].decode("ascii").strip()


@contextlib.contextmanager
def _drive(*, disc_id: str, verify: bool = True) -> Iterator:
    """Stand in for the optical drive on host B; yields the ``burn_iso`` mock."""
    with contextlib.ExitStack() as stack:
        burn = stack.enter_context(patch(f"{_RUNNER}.burn_iso"))
        stack.enter_context(patch(f"{_RUNNER}.verify_disc", return_value=verify))
        stack.enter_context(
            patch(f"{_RUNNER}.media_status", return_value=MediaStatus.BLANK)
        )
        stack.enter_context(
            patch(f"{_RUNNER}.read_disc_volume_id", return_value=disc_id)
        )
        yield burn


class _SiteA:
    """Host A: the master catalog, the rustic mirror and the staging area."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.db = root / "archive.db"
        self.config = root / "config.toml"
        mirror = root / "mirror" / "family"
        password_file = root / "family.key"
        password_file.write_text("portable-loop-test-password\n")
        (root / "staging").mkdir()
        mirror.parent.mkdir()
        source = root / "source"
        source.mkdir()
        (source / "photo.bin").write_bytes(random.Random(514).randbytes(4096))

        env = {**os.environ, "TMPDIR": str(root)}
        for args in (["init"], ["backup", str(source)]):
            subprocess.run(
                ["rustic", "-r", str(mirror), "--password-file", str(password_file),
                 *args],
                capture_output=True, text=True, check=True, env=env,
            )
        self.config.write_text(
            f'[paths]\nmirror_base = "{root / "mirror"}"\n'
            f'staging = "{root / "staging"}"\n'
            f'database = "{self.db}"\n'
            "\n[defaults]\n"
            'media_type = "TEST_TINY"\n'
            "ecc_redundancy_pct = 0\n"
            'location = "Home_Shelf"\n'
            'optical_device = "/dev/null"\n'
            'label_prefix = "PORT"\n'
            "metadata_reserve_mb = 0\n"
            '\n[repos.family]\n'
            f'mirror_path = "{mirror}"\n'
            f'password_file = "{password_file}"\n'
        )
        for argv in (
            ["init"],
            ["repo", "add", "family", str(mirror)],
            ["scan", "--no-snapshots"],
            ["stage"],
        ):
            assert self.lcsas(*argv) == 0, argv

        (self.session_dir,) = (root / "staging").iterdir()
        self.manifest = json.loads((self.session_dir / "session.json").read_text())
        (volume,) = self.manifest["volumes"]
        self.label: str = volume["label"]
        self.iso = Path(volume["iso_path"])
        self.session_id: str = self.manifest["session_id"]

    def lcsas(self, *argv: str) -> int:
        return main(["--config", str(self.config), *argv])

    def query(self, sql: str, *params: object) -> list[sqlite3.Row]:
        conn = get_connection(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def volume(self) -> sqlite3.Row:
        (row,) = self.query("SELECT * FROM volumes WHERE label = ?", self.label)
        return row

    def copies(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT c.* FROM volume_copies c JOIN volumes v USING (volume_id) "
            "WHERE v.label = ? ORDER BY c.location", self.label,
        )

    def events(self, event_type: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT e.* FROM volume_events e JOIN volumes v USING (volume_id) "
            "WHERE v.label = ? AND e.event_type = ? ORDER BY e.event_id",
            self.label, event_type,
        )

    def staged_iso_record(self) -> sqlite3.Row:
        (row,) = self.query(
            "SELECT sv.iso_sha256, sv.iso_size_bytes FROM session_volumes sv "
            "JOIN volumes v USING (volume_id) WHERE v.label = ?", self.label,
        )
        return row


class _SiteB:
    """Host B: a burner with the shipped ISO and nothing else (no catalog)."""

    def __init__(self, root: Path, iso: Path) -> None:
        self.root = root
        root.mkdir()
        self.receipts = root / "receipts"
        self.receipts.mkdir()
        inbox = root / "inbox"
        inbox.mkdir()
        self.iso = inbox / iso.name
        shutil.copy2(iso, self.iso)

    def burn(self, *, session: str, location: str, disc_id: str,
             verify: bool = True) -> tuple[int, Path | None, object]:
        """``lcsas burn-iso`` exactly as the doc's step 4 spells it."""
        with _drive(disc_id=disc_id, verify=verify) as burn:
            rc = main([
                "burn-iso", str(self.iso),
                "--device", _FAKE_DEVICE,
                "--emit-receipt", str(self.receipts),
                "--location", location,
                "--session", session,
            ])
        written = sorted(self.receipts.glob(f"*_{location}.json"))
        return rc, (written[0] if written else None), burn


@pytest.fixture
def site_a(tmp_path, monkeypatch) -> _SiteA:
    monkeypatch.chdir(tmp_path)
    return _SiteA(tmp_path / "a")


@pytest.fixture
def site_b(site_a, tmp_path, monkeypatch) -> _SiteB:
    b = _SiteB(tmp_path / "b", site_a.iso)
    monkeypatch.chdir(b.root)  # B has no catalog: nothing may resolve one
    return b


# ---------------------------------------------------------------------------
# Step 1: what staging leaves behind for the loop to rely on
# ---------------------------------------------------------------------------


def test_staging_leaves_a_staging_volume_whose_iso_carries_the_label(site_a):
    vol = site_a.volume()
    assert vol["status"] == "STAGING"
    assert site_a.copies() == []
    assert site_a.iso.is_file() and site_a.iso.name == f"{site_a.label}.iso"
    # The contract burn-iso's identity check leans on: the label the volume
    # was staged under is the ISO's PVD volume ID.
    assert _pvd_volume_id(site_a.iso) == site_a.label
    record = site_a.staged_iso_record()
    assert record["iso_sha256"] == sha256_file(site_a.iso)
    assert record["iso_size_bytes"] == site_a.iso.stat().st_size


# ---------------------------------------------------------------------------
# Steps 2-4: the loop
# ---------------------------------------------------------------------------


def test_stage_burn_iso_import_marks_the_volume_verified_with_one_copy(
    site_a, site_b, capsys,
):
    disc_id = _pvd_volume_id(site_b.iso)  # what the burned disc reports
    assert disc_id == site_a.label

    rc, receipt_path, burn = site_b.burn(
        session=site_a.session_id, location=_SITE, disc_id=disc_id,
    )

    # --- host B: burn-iso with no --config and no --label ---------------
    assert rc == 0
    burn.assert_called_once()
    assert burn.call_args.args[0] == site_b.iso
    assert burn.call_args.kwargs == {"device": _FAKE_DEVICE}
    # Auto-named <label>_<location>.json, from the label read off the ISO.
    assert receipt_path == site_b.receipts / f"{site_a.label}_{_SITE}.json"
    receipt = json.loads(receipt_path.read_text())
    recorded = site_a.staged_iso_record()
    assert receipt["volume_label"] == site_a.label
    assert receipt["location"] == _SITE
    assert receipt["session_id"] == site_a.session_id
    assert receipt["device"] == _FAKE_DEVICE
    assert receipt["verify_passed"] is True
    assert receipt["iso_sha256"] == recorded["iso_sha256"]  # B hashed what A staged
    assert receipt["iso_size_bytes"] == recorded["iso_size_bytes"]
    assert datetime.fromisoformat(receipt["burn_date"]).tzinfo is not None
    # B needed, and made, no catalog; A's catalog was not touched by B's burn.
    assert list(site_b.root.rglob("*.db")) == []
    assert site_a.volume()["status"] == "STAGING"
    assert site_a.copies() == []
    capsys.readouterr()

    # --- host A: reconcile ----------------------------------------------
    assert site_a.lcsas("catalog", "import-receipts", str(receipt_path)) == 0

    assert "Imported 1 receipt(s)" in capsys.readouterr().out
    vol = site_a.volume()
    assert vol["status"] == "VERIFIED"
    assert vol["closed_at"] is not None
    (copy,) = site_a.copies()
    assert copy["location"] == _SITE
    assert copy["status"] == "ACTIVE"
    assert copy["iso_sha256"] == receipt["iso_sha256"]
    assert copy["iso_size_bytes"] == receipt["iso_size_bytes"]
    assert copy["burn_date"] == receipt["burn_date"]
    assert [r["name"] for r in site_a.query(
        "SELECT name FROM locations WHERE name = ?", _SITE)] == [_SITE]
    (passed,) = site_a.events("VERIFY_PASS")
    assert passed["location"] == _SITE
    assert receipt_path.name in passed["detail"]
    (imported,) = site_a.events("BURN_RECEIPT_IMPORTED")
    provenance = json.loads(imported["detail"])
    assert provenance["iso_sha256"] == receipt["iso_sha256"]
    assert provenance["session_id"] == site_a.session_id
    assert provenance["device"] == _FAKE_DEVICE
    assert provenance["receipt_file"] == receipt_path.name
    assert provenance["verify_passed"] is True
    assert site_a.events("VERIFY_FAIL") == []


def test_second_burn_at_another_site_only_adds_a_copy(site_a, site_b):
    """Doc "Multi-copy": the volume is already VERIFIED, so the second
    receipt adds a ``volume_copies`` row and leaves the status alone."""
    disc_id = _pvd_volume_id(site_b.iso)
    for location in (_SITE, _SECOND_SITE):
        rc, receipt_path, _burn = site_b.burn(
            session=site_a.session_id, location=location, disc_id=disc_id,
        )
        assert rc == 0 and receipt_path is not None
        assert site_a.lcsas("catalog", "import-receipts", str(receipt_path)) == 0

    assert site_a.volume()["status"] == "VERIFIED"
    copies = site_a.copies()
    assert [c["location"] for c in copies] == sorted([_SITE, _SECOND_SITE])
    assert {c["status"] for c in copies} == {"ACTIVE"}
    # Both discs came from one ISO: same hash on both rows, one VERIFY_PASS.
    assert {c["iso_sha256"] for c in copies} == {site_a.staged_iso_record()["iso_sha256"]}
    assert len(site_a.events("VERIFY_PASS")) == 1
    assert len(site_a.events("BURN_RECEIPT_IMPORTED")) == 2


def test_failed_readback_verify_imports_as_burned_with_no_copy(site_a, site_b):
    """Doc "Partial burn that passes write but fails verify" (as of #514):
    burn-iso exits 1 but still writes the receipt; on A the volume becomes
    BURNED (never VERIFIED), VERIFY_FAIL is logged, and no copy is recorded
    because a failed disc is not a copy."""
    rc, receipt_path, _burn = site_b.burn(
        session=site_a.session_id, location=_SITE,
        disc_id=_pvd_volume_id(site_b.iso), verify=False,
    )

    assert rc == 1
    assert receipt_path is not None
    assert json.loads(receipt_path.read_text())["verify_passed"] is False

    assert site_a.lcsas("catalog", "import-receipts", str(receipt_path)) == 0

    assert site_a.volume()["status"] == "BURNED"
    assert site_a.volume()["closed_at"] is None
    assert site_a.copies() == []
    (failed,) = site_a.events("VERIFY_FAIL")
    assert failed["location"] == _SITE
    assert site_a.events("VERIFY_PASS") == []
    provenance = json.loads(site_a.events("BURN_RECEIPT_IMPORTED")[0]["detail"])
    assert provenance["verify_passed"] is False


def test_wrong_disc_in_the_drive_is_caught_by_the_staged_label(site_a, site_b):
    """The identity check compares the disc against the label burn-iso read off
    the staged ISO.  With the pre-#514 default (the parent directory, here
    ``inbox``) a correct disc would fail and a wrong one could not be told apart;
    now a disc that identifies as some other volume fails verification."""
    rc, receipt_path, _burn = site_b.burn(
        session=site_a.session_id, location=_SITE, disc_id="SOME_OTHER_DISC",
    )

    assert rc == 1
    assert receipt_path is not None
    receipt = json.loads(receipt_path.read_text())
    assert receipt["volume_label"] == site_a.label
    assert receipt["verify_passed"] is False

    assert site_a.lcsas("catalog", "import-receipts", str(receipt_path)) == 0
    assert site_a.volume()["status"] == "BURNED"
    assert site_a.copies() == []
