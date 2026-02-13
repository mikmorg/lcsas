"""Tests for Xorriso wrapper (mocked subprocess)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from lcsas.iso.xorriso import SubprocessXorrisoRunner


class TestXorrisoMocked:
    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_create_iso_args(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessXorrisoRunner()

        source = tmp_path / "source"
        source.mkdir()
        output = tmp_path / "output.iso"

        runner.create_iso(source, output, "TEST_VOL")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "xorriso" in args[0]
        assert "-as" in args
        assert "mkisofs" in args
        assert "-V" in args
        assert "TEST_VOL" in args
        assert str(output) in args
        assert str(source) in args

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_burn_iso_args(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessXorrisoRunner()
        iso = tmp_path / "test.iso"
        runner.burn_iso(iso, "/dev/sr0")

        args = mock_run.call_args[0][0]
        assert "cdrecord" in args
        assert "dev=/dev/sr0" in args
        assert str(iso) in args

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_verify_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessXorrisoRunner()
        assert runner.verify_disc("/dev/sr0") is True

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_verify_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessXorrisoRunner()
        assert runner.verify_disc("/dev/sr0") is False
