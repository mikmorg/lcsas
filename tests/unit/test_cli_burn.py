"""Tests for the ``lcsas burn`` and ``lcsas burn-iso`` commands (``lcsas.cli.commands.burn``)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.iso.xorriso import MediaStatus


class TestCmdBurnSession:
    def test_burn_session_requires_config(self, capsys):
        result = main(["burn", "--session", "latest"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_burn_requires_session(self, capsys):
        """`lcsas burn` without `--session` fails fast via argparse.

        Regression test for #60: the legacy stage+burn handler was
        removed, so `--session` is now required and argparse should
        reject the bare `burn` invocation with a non-zero exit code and
        a message referencing `--session`.
        """
        import pytest

        with pytest.raises(SystemExit) as excinfo:
            main(["burn"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "--session" in err


def _fake_iso(path: Path, volid: str) -> Path:
    """A minimal ISO 9660 image: 16 empty sectors, a PVD with `volid`,
    and a set terminator (enough for burn-iso's label inference)."""
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = volid.encode("ascii").ljust(32, b" ")
    term = bytearray(2048)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(term))
    return path


def _drive_holding(iso: Path, tmp_path: Path) -> str:
    """A stand-in optical device: a file holding the burned image, which
    burn-iso's read-back (#664) hashes like a real drive."""
    drive = tmp_path / "drive.img"
    drive.write_bytes(iso.read_bytes())
    return str(drive)


class TestCmdBurnIso:
    def test_burn_iso_missing_file(self, tmp_path, capsys):
        iso = tmp_path / "missing.iso"
        result = main(["burn-iso", str(iso)])
        assert result == 1

    def test_burn_iso_file_exists(self, tmp_path, capsys):
        """Burn ISO calls xorriso runner."""
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\0" * 100)

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso") as mock_burn,
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.media_status",
                  return_value=MediaStatus.BLANK),
            # FUP-01 identity readback: report the inferred label so the
            # wrong-disc guard passes.
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
                  return_value=iso.parent.name),
        ):
            result = main(["burn-iso", str(iso),
                           "--device", _drive_holding(iso, tmp_path)])

        assert result == 0
        mock_burn.assert_called_once()

    def test_burn_iso_emit_receipt_requires_location(self, tmp_path, capsys):
        """--emit-receipt without --location is rejected."""
        iso = tmp_path / "VOL_001" / "test.iso"
        iso.parent.mkdir()
        iso.write_bytes(b"\0" * 100)

        result = main(["burn-iso", str(iso),
                       "--emit-receipt", str(tmp_path / "r")])
        assert result == 1
        out = capsys.readouterr().out
        assert "--location" in out

    def test_burn_iso_label_defaults_to_the_iso_volume_id(self, tmp_path):
        """#514: `lcsas stage` writes <session>/<LABEL>.iso, so the parent
        dir is the session.  The default label is the ISO's PVD volume ID,
        which the identity check also compares against."""
        iso = _fake_iso(
            tmp_path / "2026-10-21T00-00-00.000000+00-00-abcd1234"
            / "LCSAS_TINY_0007.iso",
            "LCSAS_TINY_0007",
        )
        out = tmp_path / "receipts"
        out.mkdir()
        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.media_status",
                  return_value=MediaStatus.BLANK),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
                  return_value="LCSAS_TINY_0007"),
        ):
            rc = main(["burn-iso", str(iso), "--emit-receipt", str(out),
                       "--location", "Home",
                       "--device", _drive_holding(iso, tmp_path)])
        assert rc == 0
        data = json.loads((out / "LCSAS_TINY_0007_Home.json").read_text())
        assert data["volume_label"] == "LCSAS_TINY_0007"
        assert data["verify_passed"] is True

    def test_burn_iso_no_verify_records_null_not_true(self, tmp_path):
        """#514: --no-verify used to write verify_passed: true, which import
        then took as proof and promoted the volume to VERIFIED."""
        iso = _fake_iso(tmp_path / "s" / "VOL_X.iso", "VOL_X")
        out = tmp_path / "r.json"
        with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso"):
            rc = main(["burn-iso", str(iso), "--no-verify",
                       "--emit-receipt", str(out), "--location", "Home"])
        assert rc == 0
        assert json.loads(out.read_text())["verify_passed"] is None

    def test_burn_iso_emits_receipt_with_label_inferred(self, tmp_path, capsys):
        """--emit-receipt produces a JSON file with parent-dir label."""
        iso = tmp_path / "ARCHIVE_TEST_0001" / "vol.iso"
        iso.parent.mkdir()
        iso.write_bytes(b"x" * 200)
        out_dir = tmp_path / "receipts"
        out_dir.mkdir()

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.media_status",
                  return_value=MediaStatus.BLANK),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
                  return_value="ARCHIVE_TEST_0001"),
        ):
            result = main(["burn-iso", str(iso),
                           "--emit-receipt", str(out_dir),
                           "--location", "Offsite_Safe",
                           "--device", _drive_holding(iso, tmp_path)])

        assert result == 0
        receipt_path = out_dir / "ARCHIVE_TEST_0001_Offsite_Safe.json"
        assert receipt_path.exists()
        data = json.loads(receipt_path.read_text())
        assert data["volume_label"] == "ARCHIVE_TEST_0001"
        assert data["location"] == "Offsite_Safe"
        assert data["verify_passed"] is True
        assert len(data["iso_sha256"]) == 64
        # FMA-03: the byte length travels with the hash so an imported
        # receipt can still device-verify the disc.
        assert data["iso_size_bytes"] == 200

    def test_burn_iso_emits_receipt_with_explicit_label(self, tmp_path, capsys):
        """--label overrides parent-dir inference."""
        iso = tmp_path / "renamed.iso"
        iso.write_bytes(b"x" * 200)
        out_file = tmp_path / "r.json"

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.media_status",
                  return_value=MediaStatus.BLANK),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
                  return_value="EXPLICIT_LABEL"),
        ):
            result = main(["burn-iso", str(iso),
                           "--emit-receipt", str(out_file),
                           "--label", "EXPLICIT_LABEL",
                           "--location", "Home",
                           "--device", _drive_holding(iso, tmp_path)])

        assert result == 0
        data = json.loads(out_file.read_text())
        assert data["volume_label"] == "EXPLICIT_LABEL"

    def test_burn_iso_receipt_records_verify_failure(self, tmp_path, capsys):
        """verify_passed=False is recorded when verify_disc returns False."""
        iso = tmp_path / "VOL_002" / "v.iso"
        iso.parent.mkdir()
        iso.write_bytes(b"x" * 50)
        out_dir = tmp_path / "r"
        out_dir.mkdir()

        # media_status is mocked like the sibling tests: unmocked, it reads
        # the host's real /dev/sr0, and a loaded disc there (e.g. CDEmu
        # after a blind-restore run) made this fail with "not blank".
        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=False),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.media_status",
                  return_value=MediaStatus.BLANK),
        ):
            result = main(["burn-iso", str(iso),
                           "--emit-receipt", str(out_dir),
                           "--location", "Test"])

        # Returns 1 because verify failed, but receipt is still written.
        assert result == 1
        receipt_path = out_dir / "VOL_002_Test.json"
        assert receipt_path.exists()
        data = json.loads(receipt_path.read_text())
        assert data["verify_passed"] is False
