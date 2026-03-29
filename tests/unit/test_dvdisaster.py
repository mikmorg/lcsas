"""Tests for DVDisaster wrapper (mocked subprocess)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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

    def test_check_binary_raises_when_not_on_path(self):
        """check_binary raises RuntimeError when dvdisaster is not on PATH."""
        runner = SubprocessDVDisasterRunner()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="dvdisaster"),
        ):
            runner.check_binary()

    def test_check_binary_passes_when_on_path(self):
        """check_binary succeeds silently when dvdisaster exists on PATH."""
        runner = SubprocessDVDisasterRunner()
        with patch("shutil.which", return_value="/usr/bin/dvdisaster"):
            runner.check_binary()  # should not raise

    def test_augment_raises_when_insufficient_disk_space(self, tmp_path):
        """augment_iso raises OSError when there is not enough free disk space."""
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "big.iso"
        iso.write_bytes(b"\x00" * 1024)  # 1 KiB ISO

        # Simulate a disk with only 512 bytes free (less than ISO + 1 MiB margin)
        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=512),
            ),
            pytest.raises(OSError, match="Insufficient disk space"),
        ):
            runner.augment_iso(iso)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_succeeds_when_sufficient_disk_space(self, mock_run, tmp_path):
        """augment_iso proceeds normally when disk space is adequate."""
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        # Simulate 1 GiB free — more than enough
        with patch(
            "lcsas.ecc.dvdisaster.shutil.disk_usage",
            return_value=MagicMock(free=1_073_741_824),
        ):
            runner.augment_iso(iso)  # should not raise

        mock_run.assert_called_once()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_called_process_error_propagates(self, mock_run, tmp_path):
        """augment_iso propagates CalledProcessError from dvdisaster."""
        import subprocess as sp
        mock_run.side_effect = sp.CalledProcessError(1, "dvdisaster")
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(sp.CalledProcessError),
        ):
            runner.augment_iso(iso)

        # Temp file must be cleaned up on failure
        assert not iso.with_suffix(".iso.ecc.tmp").exists()
