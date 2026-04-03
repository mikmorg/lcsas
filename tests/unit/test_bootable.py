"""Unit tests for the bootable ISO builder and live boot infrastructure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.meta.bootable import BootableISOBuilder


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture()
def alpine_dir(tmp_path: Path) -> Path:
    """Create a fake Alpine artifacts directory."""
    d = tmp_path / "alpine"
    d.mkdir()
    (d / "vmlinuz").write_bytes(b"\x00" * 1024)
    (d / "initramfs").write_bytes(b"\x00" * 1024)
    (d / "rootfs.squashfs").write_bytes(b"\x00" * 2048)
    return d


@pytest.fixture()
def staging_dir(tmp_path: Path) -> Path:
    """Create a minimal meta-volume staging directory."""
    d = tmp_path / "staging"
    d.mkdir()
    (d / "restore.sh").write_text("#!/bin/bash\necho restore\n")
    (d / "volume_info.json").write_text('{"type": "meta"}')
    return d


# ── BootableISOBuilder ──────────────────────────────────────────


class TestBootableISOBuilder:
    """Tests for BootableISOBuilder."""

    def test_validate_missing_staging(self, tmp_path: Path, alpine_dir: Path):
        bib = BootableISOBuilder(
            staging_dir=tmp_path / "nonexistent",
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        with pytest.raises(FileNotFoundError, match="Staging directory"):
            bib._validate_inputs()

    def test_validate_missing_alpine_artifacts(
        self, staging_dir: Path, tmp_path: Path
    ):
        bad_alpine = tmp_path / "empty_alpine"
        bad_alpine.mkdir()
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=bad_alpine,
            output_iso=tmp_path / "out.iso",
        )
        with pytest.raises(FileNotFoundError, match="Alpine artifact"):
            bib._validate_inputs()

    def test_validate_success(
        self, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        # Should not raise
        bib._validate_inputs()

    def test_install_boot_files(
        self, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        bib._install_boot_files()

        boot = staging_dir / "boot"
        assert (boot / "vmlinuz").is_file()
        assert (boot / "initramfs").is_file()
        assert (boot / "rootfs.squashfs").is_file()
        assert (boot / "grub" / "grub.cfg").is_file()

    def test_install_boot_files_idempotent(
        self, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """Calling _install_boot_files twice should not error."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        bib._install_boot_files()
        bib._install_boot_files()  # second call — should be fine
        assert (staging_dir / "boot" / "vmlinuz").is_file()

    def test_install_isolinux_without_system_deps(
        self, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """If isolinux.bin is not on the system, the directory is
        still created with the config but no bootloader binary."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        with patch.object(
            BootableISOBuilder, "_find_file", return_value=None
        ):
            bib._install_isolinux()

        isolinux = staging_dir / "isolinux"
        assert isolinux.is_dir()
        assert (isolinux / "isolinux.cfg").is_file()
        # isolinux.bin should NOT exist (not found on system)
        assert not (isolinux / "isolinux.bin").exists()

    def test_install_isolinux_with_system_deps(
        self, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """If isolinux.bin is found, it gets copied."""
        # Create a fake isolinux.bin
        fake_isolinux = tmp_path / "isolinux.bin"
        fake_isolinux.write_bytes(b"\xeb\x3c" + b"\x00" * 62)

        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )

        def mock_find(name, dirs):
            if name == "isolinux.bin":
                return fake_isolinux
            return None

        with patch.object(BootableISOBuilder, "_find_file", side_effect=mock_find):
            bib._install_isolinux()

        assert (staging_dir / "isolinux" / "isolinux.bin").is_file()

    @patch("lcsas.meta.bootable.shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_create_iso_calls_xorriso(
        self, mock_run: MagicMock, mock_which: MagicMock,
        staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """Verify xorriso is called with correct El Torito arguments."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        # Set up boot files so that the El Torito flags get added
        boot = staging_dir / "boot"
        boot.mkdir(parents=True, exist_ok=True)
        (staging_dir / "isolinux").mkdir(exist_ok=True)
        (staging_dir / "isolinux" / "isolinux.bin").write_bytes(b"\x00" * 64)
        (boot / "efiboot.img").write_bytes(b"\x00" * 4096)

        tmp_iso = tmp_path / "out.iso.tmp"

        def fake_run(cmd, **kwargs):
            # xorriso writes to the tmp file — simulate that
            tmp_iso.write_bytes(b"ISO")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run

        bib._create_iso()

        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert "-b" in call_args or "-eltorito-alt-boot" in call_args

        # Check BIOS boot args
        assert "isolinux/isolinux.bin" in call_args
        assert "-no-emul-boot" in call_args
        assert "-boot-load-size" in call_args
        assert "-boot-info-table" in call_args

        # Check UEFI boot args
        assert "-eltorito-alt-boot" in call_args
        assert "boot/efiboot.img" in call_args

    @patch("lcsas.meta.bootable.shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_create_iso_no_boot_files(
        self, mock_run: MagicMock, mock_which: MagicMock,
        staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """Without boot files, ISO should still be created (non-bootable)."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        tmp_iso = tmp_path / "out.iso.tmp"

        def fake_run(cmd, **kwargs):
            tmp_iso.write_bytes(b"ISO")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run

        bib._create_iso()

        call_args = mock_run.call_args[0][0]
        # Standard ISO args should still be present
        assert "-r" in call_args
        assert "-J" in call_args
        # But boot-specific args should NOT be present
        assert "-b" not in call_args
        assert "-eltorito-alt-boot" not in call_args

    @patch("subprocess.run")
    def test_create_iso_cleanup_on_failure(
        self, mock_run: MagicMock, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """If xorriso fails, tmp file should be cleaned up."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        mock_run.side_effect = subprocess.CalledProcessError(1, "xorriso", stderr="fail")

        with pytest.raises(subprocess.CalledProcessError):
            bib._create_iso()

        # The temp file should be cleaned up
        assert not (tmp_path / "out.iso.tmp").exists()
        assert not (tmp_path / "out.iso").exists()

    def test_find_file_finds_existing(self, tmp_path: Path):
        d = tmp_path / "search"
        d.mkdir()
        (d / "target.bin").write_bytes(b"hello")
        result = BootableISOBuilder._find_file("target.bin", [d])
        assert result is not None
        assert result.name == "target.bin"

    def test_find_file_returns_none(self, tmp_path: Path):
        result = BootableISOBuilder._find_file("nope.bin", [tmp_path])
        assert result is None

    def test_write_default_grub_cfg(self, tmp_path: Path):
        cfg = tmp_path / "grub.cfg"
        BootableISOBuilder._write_default_grub_cfg(cfg)
        content = cfg.read_text()
        assert "timeout" in content
        assert "vmlinuz" in content
        assert "initramfs" in content
        assert "menuentry" in content

    def test_write_default_isolinux_cfg(self, tmp_path: Path):
        cfg = tmp_path / "isolinux.cfg"
        BootableISOBuilder._write_default_isolinux_cfg(cfg)
        content = cfg.read_text()
        assert "DEFAULT" in content
        assert "KERNEL" in content
        assert "vmlinuz" in content

    @patch("shutil.which", return_value=None)
    def test_make_hybrid_skips_when_no_isohybrid(
        self, mock_which: MagicMock, staging_dir: Path, alpine_dir: Path, tmp_path: Path
    ):
        """If isohybrid is not installed, _make_hybrid is a no-op."""
        bib = BootableISOBuilder(
            staging_dir=staging_dir,
            alpine_dir=alpine_dir,
            output_iso=tmp_path / "out.iso",
        )
        # Should not raise
        bib._make_hybrid()


# ── Boot config files ────────────────────────────────────────────


class TestBootConfigs:
    """Verify boot config files exist and have valid contents."""

    @pytest.fixture()
    def live_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "src" / "lcsas" / "meta" / "live"

    def test_grub_cfg_exists(self, live_dir: Path):
        cfg = live_dir / "grub.cfg"
        assert cfg.is_file(), "grub.cfg not found in live/"

    def test_grub_cfg_has_recovery_entry(self, live_dir: Path):
        content = (live_dir / "grub.cfg").read_text()
        assert "menuentry" in content
        assert "vmlinuz" in content
        assert "initramfs" in content
        assert "timeout" in content

    def test_grub_cfg_has_hard_drive_fallback(self, live_dir: Path):
        content = (live_dir / "grub.cfg").read_text()
        assert "Hard Drive" in content or "hard drive" in content.lower()
        assert "chainloader" in content

    def test_isolinux_cfg_exists(self, live_dir: Path):
        cfg = live_dir / "isolinux.cfg"
        assert cfg.is_file(), "isolinux.cfg not found in live/"

    def test_isolinux_cfg_has_default(self, live_dir: Path):
        content = (live_dir / "isolinux.cfg").read_text()
        assert "DEFAULT" in content
        assert "KERNEL" in content
        assert "vmlinuz" in content

    def test_init_script_exists(self, live_dir: Path):
        init = live_dir / "init"
        assert init.is_file(), "init script not found in live/"

    def test_init_script_mounts_squashfs(self, live_dir: Path):
        content = (live_dir / "init").read_text()
        assert "squashfs" in content
        assert "mount" in content
        assert "switch_root" in content

    def test_init_script_has_debug_shell(self, live_dir: Path):
        content = (live_dir / "init").read_text()
        assert "lcsas.shell" in content

    def test_build_rootfs_script_exists(self, live_dir: Path):
        script = live_dir / "build_rootfs.sh"
        assert script.is_file(), "build_rootfs.sh not found in live/"

    def test_build_rootfs_installs_required_packages(self, live_dir: Path):
        content = (live_dir / "build_rootfs.sh").read_text()
        for pkg in ("linux-lts", "dialog", "e2fsprogs", "bash"):
            assert pkg in content, f"Required package '{pkg}' missing from build_rootfs.sh"

    def test_build_rootfs_produces_outputs(self, live_dir: Path):
        content = (live_dir / "build_rootfs.sh").read_text()
        assert "vmlinuz" in content
        assert "initramfs" in content
        assert "rootfs.squashfs" in content
        assert "mksquashfs" in content


# ── Restore wizard module ────────────────────────────────────────


class TestRestoreWizard:
    """Tests for the restore wizard module (import + basic logic)."""

    def test_module_imports(self):
        """The wizard module should import without errors."""
        from lcsas.meta.live import restore_wizard
        assert hasattr(restore_wizard, "RestoreWizard")
        assert hasattr(restore_wizard, "main")

    def test_wizard_init(self):
        from lcsas.meta.live.restore_wizard import RestoreWizard
        wiz = RestoreWizard()
        assert wiz.key_file == ""
        assert wiz.target_dir == ""
        assert wiz.repo == ""
        assert wiz.ripped_isos == []

    @patch("lcsas.meta.live.restore_wizard.shutil.which", return_value=None)
    def test_main_fails_without_dialog(self, mock_which: MagicMock):
        from lcsas.meta.live.restore_wizard import main
        result = main()
        assert result == 1

    @patch("lcsas.meta.live.restore_wizard.shutil.which", return_value="/usr/bin/dialog")
    @patch("lcsas.meta.live.restore_wizard.RestoreWizard.run")
    def test_main_launches_wizard(
        self, mock_run: MagicMock, mock_which: MagicMock
    ):
        from lcsas.meta.live.restore_wizard import main
        result = main()
        assert result == 0
        mock_run.assert_called_once()

    def test_find_key_files_returns_empty_default(self, tmp_path: Path):
        from lcsas.meta.live.restore_wizard import find_key_files
        # Search a directory with no key files
        result = find_key_files([str(tmp_path)])
        assert result == []

    def test_find_key_files_finds_keys(self, tmp_path: Path):
        from lcsas.meta.live.restore_wizard import find_key_files
        key = tmp_path / "my_secret.key"
        key.write_text("secret")
        result = find_key_files([str(tmp_path)])
        assert len(result) == 1
        assert result[0].name == "my_secret.key"

    @patch("subprocess.run")
    def test_rip_disc_to_iso_failure(self, mock_run: MagicMock):
        from lcsas.meta.live.restore_wizard import rip_disc_to_iso
        mock_run.side_effect = subprocess.CalledProcessError(1, "blockdev")
        result = rip_disc_to_iso("/dev/sr0", "/tmp/out.iso")
        assert result is False

    def test_read_volume_info_empty_dir(self, tmp_path: Path):
        from lcsas.meta.live.restore_wizard import read_volume_info
        info = read_volume_info(str(tmp_path))
        assert info["volumes"] == []

    def test_read_volume_info_with_json(self, tmp_path: Path):
        from lcsas.meta.live.restore_wizard import read_volume_info
        vol_info = tmp_path / "volume_info.json"
        vol_info.write_text('{"type": "data", "label": "VOL_001"}')
        info = read_volume_info(str(tmp_path))
        assert len(info["volumes"]) == 1
        assert info["volumes"][0]["type"] == "data"

    @patch("subprocess.run")
    def test_dialog_helpers(self, mock_run: MagicMock):
        """Verify dialog helpers call subprocess correctly."""
        from lcsas.meta.live.restore_wizard import _run_dialog
        mock_run.return_value = MagicMock(
            returncode=0, stderr="user_input"
        )
        rc, output = _run_dialog(["--msgbox", "hello", "10", "50"])
        assert rc == 0
        assert output == "user_input"


# ── MetaVolumeBuilder bootable flag ─────────────────────────────


class TestMetaVolumeBuilderBootable:
    """Test the bootable parameter on MetaVolumeBuilder."""

    def test_bootable_false_by_default(self):
        from lcsas.meta.builder import MetaVolumeBuilder
        builder = MetaVolumeBuilder(Path("/tmp/test"))
        assert builder._bootable is False

    def test_bootable_requires_alpine_dir(self, tmp_path: Path):
        from lcsas.meta.builder import MetaVolumeBuilder
        builder = MetaVolumeBuilder(
            tmp_path / "meta", bootable=True, alpine_dir=None
        )
        with pytest.raises(ValueError, match="alpine_dir"):
            builder._install_live_boot()

    def test_bootable_validates_artifacts(self, tmp_path: Path):
        from lcsas.meta.builder import MetaVolumeBuilder
        empty_alpine = tmp_path / "alpine"
        empty_alpine.mkdir()
        builder = MetaVolumeBuilder(
            tmp_path / "meta", bootable=True, alpine_dir=empty_alpine
        )
        (tmp_path / "meta").mkdir()
        with pytest.raises(FileNotFoundError, match="Alpine artifact"):
            builder._install_live_boot()

    def test_bootable_installs_boot_files(
        self, tmp_path: Path, alpine_dir: Path
    ):
        """When alpine_dir has valid artifacts, boot files get installed."""
        from lcsas.meta.builder import MetaVolumeBuilder
        meta = tmp_path / "meta"
        meta.mkdir()

        builder = MetaVolumeBuilder(
            meta, bootable=True, alpine_dir=alpine_dir
        )

        # Patch out isolinux/EFI lookups since system deps may not exist
        with patch(
            "lcsas.meta.bootable.BootableISOBuilder._install_isolinux"
        ), patch(
            "lcsas.meta.bootable.BootableISOBuilder._install_efi"
        ):
            builder._install_live_boot()

        assert (meta / "boot" / "vmlinuz").is_file()
        assert (meta / "boot" / "initramfs").is_file()
        assert (meta / "boot" / "rootfs.squashfs").is_file()
        assert (meta / "boot" / "grub" / "grub.cfg").is_file()


# ── XorrisoRunner protocol ───────────────────────────────────────


class TestXorrisoBootableISO:
    """Test create_bootable_iso on SubprocessXorrisoRunner."""

    @patch("subprocess.run")
    def test_create_bootable_iso_bios_only(
        self, mock_run: MagicMock, staging_dir: Path, tmp_path: Path
    ):
        from lcsas.iso.xorriso import SubprocessXorrisoRunner

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
        from lcsas.iso.xorriso import SubprocessXorrisoRunner

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
        from lcsas.iso.xorriso import SubprocessXorrisoRunner
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
        from lcsas.iso.xorriso import SubprocessXorrisoRunner
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
