"""BOOT-03: boot menu entry paths must equal the staged-tree paths.

The pre-fix ``efi/grub.cfg`` loaded ``/boot/linux/vmlinuz`` while the
builder staged the kernel at ``/boot/vmlinuz`` — every UEFI menu entry
404'd on a disc that otherwise looked complete.  Sibling defects: a
FreeBSD menuentry chainloading ``/boot/freebsd/loader.efi`` (an artifact
that never existed anywhere in the repo) and a dead
``isolinux/isolinux.cfg`` requiring an uncopied ``menu.c32``.

These gates are pure static text checks so they survive any future
deletion of ``BootableISOBuilder`` (BOOT-07) and keep the quarantined
``experimental/boot/`` material honest for whoever revives it.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOT_DIR = _REPO_ROOT / "experimental" / "boot"

# The staged-on-disc names produced by
# ``BootableISOBuilder._install_boot_files`` in recovery mode
# (src/lcsas/meta/bootable.py).  This set IS the contract: every
# kernel/initrd path in the quarantined boot configs must be in it.
_STAGED_BOOT_PATHS = {"/boot/vmlinuz", "/boot/initramfs.cpio.gz"}

# GRUB menu lines that load files: `linux <path> ...` / `initrd <path>`.
_GRUB_PATH_RE = re.compile(r"^\s*(?:linux|initrd)\s+(\S+)", re.MULTILINE)

# Artifacts that exist nowhere in the repo and are never staged by the
# builder; referencing one is a guaranteed dead menu entry.
_PHANTOM_TOKENS = ("loader.bin", "loader.efi", "menu.c32", "/boot/freebsd/")


def test_grub_entries_reference_staged_names() -> None:
    cfg = _BOOT_DIR / "efi" / "grub.cfg"
    assert cfg.is_file(), f"missing {cfg} — delete this test with the tree"
    refs = _GRUB_PATH_RE.findall(cfg.read_text())
    assert refs, f"no linux/initrd menu lines parsed from {cfg}"
    offenders = sorted(set(refs) - _STAGED_BOOT_PATHS)
    assert not offenders, (
        f"{cfg} menu entries load paths the builder never stages: "
        f"{offenders} (staged contract: {sorted(_STAGED_BOOT_PATHS)})"
    )


def test_no_phantom_artifact_references() -> None:
    # README.md is excluded: its defect index legitimately names the
    # deleted phantom artifacts when recording why they were removed.
    files = [
        p
        for p in sorted(_BOOT_DIR.rglob("*"))
        if p.is_file() and p.name != "README.md"
    ]
    assert files, f"nothing under {_BOOT_DIR} — delete this test with the tree"
    offenders = [
        f"{p.relative_to(_REPO_ROOT)}: {token}"
        for p in files
        for token in _PHANTOM_TOKENS
        if token in p.read_text(errors="replace")
    ]
    assert not offenders, (
        "experimental/boot/ references boot artifacts that exist nowhere "
        f"in the repo: {offenders}"
    )
