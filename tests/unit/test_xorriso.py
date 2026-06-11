"""Tests for Xorriso wrapper (mocked subprocess)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.iso.xorriso import SubprocessXorrisoRunner


class TestXorrisoMocked:
    @patch("lcsas.iso.xorriso.subprocess.Popen")
    def test_create_iso_args(self, mock_popen, tmp_path):
        """create_iso writes to .iso.tmp then renames to final path."""
        captured_cmd = []

        def _popen_factory(cmd, **kwargs):
            captured_cmd.extend(cmd)
            # Simulate xorriso writing the temp output file
            idx = cmd.index("-o")
            Path(cmd[idx + 1]).write_bytes(b"ISO")
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 0
            return mock_proc

        mock_popen.side_effect = _popen_factory
        runner = SubprocessXorrisoRunner()

        source = tmp_path / "source"
        source.mkdir()
        output = tmp_path / "output.iso"

        runner.create_iso(source, output, "TEST_VOL")

        mock_popen.assert_called_once()
        args = captured_cmd
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
        iso.write_bytes(b"fake ISO")
        runner.burn_iso(iso, "/dev/sr0")

        args = mock_run.call_args[0][0]
        assert "cdrecord" in args
        assert "dev=/dev/sr0" in args
        assert str(iso) in args

    def test_burn_iso_missing_iso_raises(self, tmp_path):
        """burn_iso raises FileNotFoundError when ISO does not exist."""
        import pytest
        runner = SubprocessXorrisoRunner()
        iso = tmp_path / "nonexistent.iso"
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.burn_iso(iso, "/dev/sr0")

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

    @patch("lcsas.iso.xorriso.subprocess.Popen")
    def test_create_iso_cleans_tmp_on_failure(self, mock_popen, tmp_path):
        """On subprocess failure, the .iso.tmp file is removed."""
        import subprocess as _sp

        def _popen_factory(cmd, **kwargs):
            idx = cmd.index("-o")
            Path(cmd[idx + 1]).write_bytes(b"PARTIAL")
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "xorriso: some error")
            mock_proc.returncode = 1
            return mock_proc

        mock_popen.side_effect = _popen_factory
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

    @patch("lcsas.iso.xorriso.subprocess.Popen")
    def test_create_iso_no_tmp_file_on_early_failure(self, mock_popen, tmp_path):
        """If subprocess fails before writing the file, no cleanup error."""
        import subprocess as _sp

        def _popen_factory(cmd, **kwargs):
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 1
            return mock_proc

        mock_popen.side_effect = _popen_factory
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


class TestReadDiscVolumeId:
    """FMA-03: PVD Volume ID read for the wrong-disc identity check."""

    _PVD_OUTPUT = (
        "xorriso 1.5.4 : RockRidge filesystem manipulator\n"
        "Drive current: -indev '/dev/sr0'\n"
        "Media current: BD-R\n"
        "Volume id    : 'LCSAS_BD_2026_0001'\n"
        "Volume timestamp : c : 2026061100000000\n"
    )

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_parses_volume_id(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=self._PVD_OUTPUT,
        )
        runner = SubprocessXorrisoRunner()
        assert runner.read_disc_volume_id("/dev/sr0") == "LCSAS_BD_2026_0001"
        args = mock_run.call_args[0][0]
        assert "-pvd_info" in args
        assert "/dev/sr0" in args

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_nonzero_returncode_yields_empty(self, mock_run):
        """No readable disc → '' (an unknown identity must never match)."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        runner = SubprocessXorrisoRunner()
        assert runner.read_disc_volume_id("/dev/sr0") == ""

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_missing_volume_id_line_yields_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Drive current: -indev '/dev/sr0'\n",
        )
        runner = SubprocessXorrisoRunner()
        assert runner.read_disc_volume_id("/dev/sr0") == ""

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_timeout_yields_empty(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(["xorriso"], 300)
        runner = SubprocessXorrisoRunner()
        assert runner.read_disc_volume_id("/dev/sr0") == ""

    @patch("lcsas.iso.xorriso.subprocess.run")
    def test_missing_binary_yields_empty(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        runner = SubprocessXorrisoRunner()
        assert runner.read_disc_volume_id("/dev/sr0") == ""


# ── create_bootable_iso (El Torito records) ──────────────────────
# Moved from tests/unit/test_bootable.py when the Alpine live stack
# was deleted (BOOT-07); this API is generic ISO mastering, not part
# of the dropped boot path.


@pytest.fixture()
def staging_dir(tmp_path: Path) -> Path:
    """Create a minimal meta-volume staging directory."""
    d = tmp_path / "staging"
    d.mkdir()
    (d / "restore.sh").write_text("#!/bin/bash\necho restore\n")
    (d / "volume_info.json").write_text('{"type": "meta"}')
    return d


class TestXorrisoBootableISO:
    """Test create_bootable_iso on SubprocessXorrisoRunner."""

    @patch("subprocess.run")
    def test_create_bootable_iso_bios_only(
        self, mock_run: MagicMock, staging_dir: Path, tmp_path: Path
    ):
        # Set up BIOS boot files
        isolinux = staging_dir / "isolinux"
        isolinux.mkdir()
        (isolinux / "isolinux.bin").write_bytes(b"\x00" * 64)

        tmp_iso = tmp_path / "out.iso.tmp"

        def fake_run(cmd, **kwargs):
            tmp_iso.write_bytes(b"ISO")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run

        runner = SubprocessXorrisoRunner()
        runner.create_bootable_iso(
            staging_dir,
            tmp_path / "out.iso",
            "TEST_LABEL",
            bios_boot=True,
            uefi_boot=False,
        )

        call_args = mock_run.call_args[0][0]
        assert "isolinux/isolinux.bin" in call_args
        assert "-no-emul-boot" in call_args
        # UEFI should not be present
        assert "-eltorito-alt-boot" not in call_args

    @patch("subprocess.run")
    def test_create_bootable_iso_uefi_only(
        self, mock_run: MagicMock, staging_dir: Path, tmp_path: Path
    ):
        # Set up UEFI boot files
        boot = staging_dir / "boot"
        boot.mkdir()
        (boot / "efiboot.img").write_bytes(b"\x00" * 4096)

        tmp_iso = tmp_path / "out.iso.tmp"

        def fake_run(cmd, **kwargs):
            tmp_iso.write_bytes(b"ISO")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run

        runner = SubprocessXorrisoRunner()
        runner.create_bootable_iso(
            staging_dir,
            tmp_path / "out.iso",
            "TEST_LABEL",
            bios_boot=False,
            uefi_boot=True,
        )

        call_args = mock_run.call_args[0][0]
        assert "-eltorito-alt-boot" in call_args
        assert "boot/efiboot.img" in call_args
        # BIOS should not be present
        assert "-b" not in call_args

    @patch("subprocess.run")
    def test_create_bootable_iso_missing_source(
        self, mock_run: MagicMock, tmp_path: Path
    ):
        runner = SubprocessXorrisoRunner()
        with pytest.raises(FileNotFoundError, match="Source directory"):
            runner.create_bootable_iso(
                tmp_path / "nonexistent",
                tmp_path / "out.iso",
                "TEST",
            )

    @patch("subprocess.run")
    def test_create_bootable_iso_cleanup_on_failure(
        self, mock_run: MagicMock, staging_dir: Path, tmp_path: Path
    ):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "xorriso", stderr="error"
        )
        runner = SubprocessXorrisoRunner()
        with pytest.raises(subprocess.CalledProcessError):
            runner.create_bootable_iso(
                staging_dir,
                tmp_path / "out.iso",
                "TEST",
            )
        assert not (tmp_path / "out.iso.tmp").exists()
