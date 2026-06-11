"""BOOT-03: BootableISOBuilder menu-entry / staged-tree consistency.

Recovery mode used to install boot configs whose kernel/initrd paths
did not exist on the staged disc (grub loaded /boot/linux/vmlinuz, the
reused Alpine isolinux.cfg loaded /boot/initramfs without the
.cpio.gz suffix).  These tests pin the fixed contract: every path a
menu entry loads exists in the staging tree, and build() refuses to
master an ISO when one does not.

The builder under test is the QUARANTINED ``experimental/boot/
bootable.py`` (moved out of the installed package by BOOT-07), so it
is loaded from its file path and the whole module skips if the
experimental tree has been deleted.
"""

from __future__ import annotations

import gzip
import importlib.util
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_GRUB_CFG = REPO_ROOT / "experimental" / "boot" / "efi" / "grub.cfg"
_BOOTABLE_PY = REPO_ROOT / "experimental" / "boot" / "bootable.py"

if not _BOOTABLE_PY.is_file():
    pytest.skip(
        "experimental/boot/bootable.py absent — quarantined builder "
        "deleted; remove this test file with it",
        allow_module_level=True,
    )

_spec = importlib.util.spec_from_file_location(
    "experimental_bootable", _BOOTABLE_PY
)
assert _spec is not None and _spec.loader is not None
_bootable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootable)

BootableISOBuilder = _bootable.BootableISOBuilder
_MENU_PATH_RE = _bootable._MENU_PATH_RE


def _trailer_only_cpio_gz(path: Path) -> None:
    """Write a minimal valid gzipped newc cpio (trailer record only)."""
    name = b"TRAILER!!!\x00"
    fields = [0] * 13
    fields[11] = len(name)  # namesize (includes NUL)
    header = b"070701" + b"".join(f"{v:08X}".encode() for v in fields)
    data = header + name
    data += b"\x00" * (-len(data) % 4)
    path.write_bytes(gzip.compress(data))


@pytest.fixture()
def recovery_boot(tmp_path: Path) -> Path:
    """Fake experimental/boot tree with stubbed artifacts at the
    documented names and the *real* repo grub.cfg."""
    boot = tmp_path / "experimental" / "boot"
    (boot / "linux").mkdir(parents=True)
    (boot / "linux" / "vmlinuz-x86_64").write_bytes(b"\x00" * 64)
    _trailer_only_cpio_gz(boot / "initramfs-x86_64.cpio.gz")
    (boot / "efi").mkdir()
    shutil.copy2(REPO_GRUB_CFG, boot / "efi" / "grub.cfg")
    return boot


@pytest.fixture()
def staging_dir(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    (d / "restore.sh").write_text("#!/bin/sh\necho restore\n")
    return d


def _builder(staging: Path, boot: Path, tmp_path: Path) -> BootableISOBuilder:
    return BootableISOBuilder(
        staging_dir=staging,
        recovery_boot_dir=boot,
        recovery_arch="x86_64",
        output_iso=tmp_path / "out.iso",
    )


def test_builder_staged_tree_consistency(
    staging_dir: Path, recovery_boot: Path, tmp_path: Path
) -> None:
    """Every path referenced by the installed configs exists in staging."""
    bib = _builder(staging_dir, recovery_boot, tmp_path)
    bib._install_boot_files()
    with patch.object(BootableISOBuilder, "_find_file", return_value=None):
        bib._install_isolinux()

    refs: list[str] = []
    for cfg in (
        staging_dir / "boot" / "grub" / "grub.cfg",
        staging_dir / "isolinux" / "isolinux.cfg",
    ):
        assert cfg.is_file()
        for m in _MENU_PATH_RE.finditer(cfg.read_text()):
            refs.append(m.group(1) or m.group(2))

    assert refs, "no menu-entry paths parsed — vacuous test"
    missing = [r for r in refs if not (staging_dir / r.lstrip("/")).is_file()]
    assert not missing, f"menu entries reference unstaged paths: {missing}"

    # And the builder's own guard agrees.
    bib._validate_boot_config_paths()


def test_recovery_isolinux_not_reused_from_alpine(
    staging_dir: Path, recovery_boot: Path, tmp_path: Path
) -> None:
    """Recovery mode generates isolinux.cfg from the staged names
    instead of reusing the Alpine live/isolinux.cfg (whose initrd=
    lacks the .cpio.gz suffix)."""
    bib = _builder(staging_dir, recovery_boot, tmp_path)
    bib._install_boot_files()
    with patch.object(BootableISOBuilder, "_find_file", return_value=None):
        bib._install_isolinux()

    text = (staging_dir / "isolinux" / "isolinux.cfg").read_text()
    assert "KERNEL /boot/vmlinuz" in text
    assert "initrd=/boot/initramfs.cpio.gz" in text


def test_build_raises_on_missing_menu_path(
    staging_dir: Path, recovery_boot: Path, tmp_path: Path
) -> None:
    """build() must refuse to master an ISO whose menu entries 404."""
    # Reintroduce the pre-fix defect in the source grub.cfg.
    cfg = recovery_boot / "efi" / "grub.cfg"
    cfg.write_text(
        'menuentry "Linux" {\n'
        "    linux  /boot/linux/vmlinuz quiet\n"
        "    initrd /boot/initramfs.cpio.gz\n"
        "}\n"
    )
    bib = _builder(staging_dir, recovery_boot, tmp_path)
    with (
        patch.object(BootableISOBuilder, "_find_file", return_value=None),
        patch.object(BootableISOBuilder, "_install_efi"),
        patch.object(BootableISOBuilder, "_create_iso"),
        pytest.raises(ValueError, match="boot menu references missing path"),
    ):
        bib.build()


def test_build_passes_with_consistent_configs(
    staging_dir: Path, recovery_boot: Path, tmp_path: Path
) -> None:
    """With the real repo grub.cfg, build() clears the path guard."""
    bib = _builder(staging_dir, recovery_boot, tmp_path)
    with (
        patch.object(BootableISOBuilder, "_find_file", return_value=None),
        patch.object(BootableISOBuilder, "_install_efi"),
        patch.object(BootableISOBuilder, "_create_iso"),
    ):
        assert bib.build() == tmp_path / "out.iso"


def test_builder_validates_missing_boot_artifacts(tmp_path: Path) -> None:
    """_validate_inputs raises if kernel/initramfs are missing (moved
    from tests/integration/test_recovery_orchestration.py by BOOT-07)."""
    staging = tmp_path / "staging"
    staging.mkdir()
    rb = tmp_path / "recovery_boot"
    rb.mkdir()
    (rb / "linux").mkdir()

    bib = _builder(staging, rb, tmp_path)
    with pytest.raises(FileNotFoundError, match="vmlinuz"):
        bib._validate_inputs()
