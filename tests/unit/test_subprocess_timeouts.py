"""Tests for subprocess timeout handling across xorriso, dvdisaster, and rustic."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
from lcsas.iso.xorriso import SubprocessXorrisoRunner
from lcsas.rustic.wrapper import SubprocessRusticRunner


REPO = Path("/tmp/test_repo")
PW = Path("/tmp/password.txt")


class TestXorrisoTimeouts:
    @patch("lcsas.iso.xorriso.subprocess.Popen")
    def test_create_iso_timeout_raises_runtime_error(self, mock_popen, tmp_path):
        """create_iso raises RuntimeError and removes temp file when process times out."""

        def _popen_factory(cmd, **kwargs):
            mock_proc = MagicMock()
            mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd, 7200)
            mock_proc.kill.return_value = None
            return mock_proc

        mock_popen.side_effect = _popen_factory
        runner = SubprocessXorrisoRunner()
        source = tmp_path / "source"
        source.mkdir()
        output = tmp_path / "output.iso"

        with pytest.raises(RuntimeError, match="timed out"):
            runner.create_iso(source, output, "TEST_VOL", timeout=7200)

        # Temp file must be cleaned up
        assert not output.with_suffix(".iso.tmp").exists()
        assert not output.exists()

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_burn_iso_timeout_raises_runtime_error(self, mock_run, tmp_path):
        """burn_iso raises RuntimeError when the process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["xorriso"], 14400)
        runner = SubprocessXorrisoRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"ISO")

        with pytest.raises(RuntimeError, match="timed out"):
            runner.burn_iso(iso, "/dev/sr0", timeout=14400)

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_verify_disc_timeout_raises_runtime_error(self, mock_run):
        """verify_disc raises RuntimeError when the process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["xorriso"], 3600)
        runner = SubprocessXorrisoRunner()

        with pytest.raises(RuntimeError, match="timed out"):
            runner.verify_disc("/dev/sr0", timeout=3600)


class TestDVDisasterTimeouts:
    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_iso_timeout_raises_runtime_error(self, mock_run, tmp_path):
        """augment_iso raises RuntimeError and removes temp file when process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["dvdisaster"], 7200)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            runner.augment_iso(iso, timeout=7200)

        # Temp file must be cleaned up
        assert not iso.with_suffix(".iso.ecc.tmp").exists()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_iso_timeout_raises_runtime_error(self, mock_run, tmp_path):
        """verify_iso raises RuntimeError when the process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["dvdisaster"], 3600)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with pytest.raises(RuntimeError, match="timed out"):
            runner.verify_iso(iso, timeout=3600)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_iso_timeout_raises_runtime_error(self, mock_run, tmp_path):
        """repair_iso raises RuntimeError when the process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["dvdisaster"], 3600)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with pytest.raises(RuntimeError, match="timed out"):
            runner.repair_iso(iso, timeout=3600)


class TestRusticTimeouts:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_run_timeout_raises_runtime_error(self, mock_run):
        """_run raises RuntimeError when rustic process times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["rustic"], 3600)
        runner = SubprocessRusticRunner()

        with pytest.raises(RuntimeError, match="timed out"):
            runner._run(["snapshots"], REPO, PW)

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_restore_uses_long_timeout(self, mock_run):
        """restore() passes a longer timeout (21600s) for large restores."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        runner = SubprocessRusticRunner()
        runner.restore("snap1", REPO, PW, Path("/restore/target"))

        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 21600

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_snapshots_uses_default_timeout(self, mock_run):
        """Non-restore operations use the default 3600s timeout."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        runner = SubprocessRusticRunner()
        runner.snapshots(REPO, PW)

        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 3600
