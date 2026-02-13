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

        runner.augment_iso(iso, redundancy_pct=20)

        args = mock_run.call_args[0][0]
        assert "dvdisaster" in args[0]
        assert "-mRS03" in args
        assert "-n" in args
        assert "20" in args
        assert str(iso) in args

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_success(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        assert runner.verify_iso(tmp_path / "test.iso") is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessDVDisasterRunner()
        assert runner.verify_iso(tmp_path / "test.iso") is False

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_success(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        assert runner.repair_iso(tmp_path / "test.iso") is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessDVDisasterRunner()
        assert runner.repair_iso(tmp_path / "test.iso") is False
