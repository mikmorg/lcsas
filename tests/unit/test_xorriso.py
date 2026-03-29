"""Tests for Xorriso wrapper (mocked subprocess)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.iso.xorriso import SubprocessXorrisoRunner


class TestXorrisoMocked:
    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_create_iso_args(self, mock_run, tmp_path):
        """create_iso writes to .iso.tmp then renames to final path."""
        def _create_tmp(cmd, **kwargs):
            """Simulate xorriso creating the output file."""
            # The -o arg is the temp path (.iso.tmp)
            idx = cmd.index("-o")
            Path(cmd[idx + 1]).write_bytes(b"ISO")
            return MagicMock(returncode=0)

        mock_run.side_effect = _create_tmp
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
        # The subprocess receives the .tmp path
        assert str(output.with_suffix(".iso.tmp")) in args
        assert str(source) in args
        # Final file should exist after rename
        assert output.exists()

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

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_create_iso_cleans_tmp_on_failure(self, mock_run, tmp_path):
        """On subprocess failure, the .iso.tmp file is removed."""
        import subprocess as _sp

        def _create_and_fail(cmd, **kwargs):
            idx = cmd.index("-o")
            Path(cmd[idx + 1]).write_bytes(b"PARTIAL")
            raise _sp.CalledProcessError(1, "xorriso")

        mock_run.side_effect = _create_and_fail
        runner = SubprocessXorrisoRunner()

        source = tmp_path / "source"
        source.mkdir()
        output = tmp_path / "output.iso"

        import pytest
        with pytest.raises(_sp.CalledProcessError):
            runner.create_iso(source, output, "TEST_VOL")

        # Neither the temp nor final file should remain
        assert not output.exists()
        assert not output.with_suffix(".iso.tmp").exists()

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_create_iso_no_tmp_file_on_early_failure(self, mock_run, tmp_path):
        """If subprocess fails before writing the file, no cleanup error."""
        import subprocess as _sp

        mock_run.side_effect = _sp.CalledProcessError(1, "xorriso")
        runner = SubprocessXorrisoRunner()

        source = tmp_path / "source"
        source.mkdir()
        output = tmp_path / "output.iso"

        import pytest
        with pytest.raises(_sp.CalledProcessError):
            runner.create_iso(source, output, "TEST_VOL")

        assert not output.exists()
        assert not output.with_suffix(".iso.tmp").exists()

    def test_burn_iso_missing_binary_raises_runtime_error(self, tmp_path):
        """burn_iso raises RuntimeError with helpful message when xorriso not found."""
        import pytest

        runner = SubprocessXorrisoRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"ISO")

        with (
            patch("lcsas.iso.xorriso.subprocess.run", side_effect=FileNotFoundError()),
            pytest.raises(RuntimeError, match="xorriso"),
        ):
            runner.burn_iso(iso, "/dev/sr0")

    def test_check_binary_raises_when_not_on_path(self):
        """check_binary raises RuntimeError when the tool is not on PATH."""
        import pytest

        runner = SubprocessXorrisoRunner()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="xorriso"),
        ):
            runner.check_binary()

    def test_check_binary_passes_when_on_path(self):
        """check_binary succeeds silently when the tool exists."""
        runner = SubprocessXorrisoRunner()
        with patch("shutil.which", return_value="/usr/bin/xorriso"):
            runner.check_binary()  # should not raise
