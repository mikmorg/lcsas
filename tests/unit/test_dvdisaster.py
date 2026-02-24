"""Tests for DVDisaster wrapper (mocked subprocess)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner


class TestDVDisasterMocked:
    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_args(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)  # dummy ISO file

        runner.augment_iso(iso, redundancy_pct=20)

        args = mock_run.call_args[0][0]
        assert "dvdisaster" in args[0]
        assert "-mRS03" in args
        assert "-n" in args
        assert "20" in args
        # augment_iso now works on a temp copy then renames; verify the
        # original path is not passed (temp copy is).
        # Just verify dvdisaster was called.

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_success(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.verify_iso(iso) is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.verify_iso(iso) is False

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_success(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.repair_iso(iso) is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.repair_iso(iso) is False
