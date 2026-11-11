"""Boot-path tripwire (BOOT-01/BOOT-07/BOOT-08, #688).

FAILURE MODE CAUGHT
-------------------
Nothing in this project has ever booted in any test tier, yet for a
long time every heir-facing "no working computer" route promised a
bootable disc that never existed (the BOOT-01 dead end).  The 2026-06
deep audit verdict was DROP, and #688 deleted the leftover scaffolding
(``experimental/boot/``, ``lcsas-init``, the initramfs files and
``create_bootable_iso``).  The last tree that holds them is the tag
``archive/pre-1.0-history``, together with the QEMU boot-smoke gate that
any revival must build first.

This single tripwire fails if a boot instruction or a boot artifact
comes back:

  * an heir-facing document (static manuals or the docs generated onto
    burned discs) tells the reader to boot the disc;
  * the readiness checklist re-grows the manual boot drill;
  * a boot surface reappears in code: a boot-ish ``lcsas`` CLI flag, a
    boot parameter on ``MetaVolumeBuilder``, or a boot method on the
    xorriso runner;
  * a boot artifact reappears in the tree: the deleted directories, or a
    kernel / initramfs / EFI / bootloader file under ``src/`` or
    ``recovery/``.

Static and builder-fixture checks only: no subprocess, no optical
hardware, no network.
"""
from __future__ import annotations

import argparse
import inspect
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "recovery" / "docs"
_CHECKLIST = _DOCS / "READINESS_CHECKLIST.txt"

_REVIVAL = (
    "No LCSAS disc is bootable.  The dropped boot stack and the QEMU "
    "boot-smoke gate a revival must build first are at the tag "
    "archive/pre-1.0-history; booting may not be re-advertised until "
    "that gate exists and is green in CI."
)

# A boot-the-disc promise.  Anchored on disc/medium, deliberately NOT
# on USB: BOOT.txt's live-USB instruction ("boot ... from USB") is the
# supported no-OS route and must stay allowed.
_BOOT_CLAIM = re.compile(
    r"(?i)boot\s+(directly\s+)?(from|the)\s+(the\s+)?(disc|recovery\s+medium)"
)

# Boot-ish identifier (flag, parameter or method).  "bootstrap" is
# restore vocabulary (metadata bootstrap phase), not a boot promise.
_BOOT_IDENT = re.compile(r"(?i)boot(?!strap)")

# File names that are boot artifacts wherever they appear.
_BOOT_FILE = re.compile(
    r"(?i)^vmlinuz|^initramfs|\.squashfs$|\.efi$|^efiboot\.img$"
    r"|^isolinux|^grub\.cfg$|^init\.c$"
)

# Paths #688 deleted.  Any of them coming back is a revival attempt.
_DELETED_PATHS = (
    "experimental/boot",
    "recovery/src/lcsas-init",
    "recovery/scripts/initramfs",
    "docs/workflows/restore-bare-metal.md",
)

# Trees scanned for boot-artifact file names.  Build output is skipped.
_SCANNED_TREES = ("src", "recovery")
_SKIPPED_DIRS = {"build", "__pycache__", ".pytest_cache"}


def _generated_heir_docs(tmp_path: Path) -> dict[str, str]:
    """Render every generated doc an heir reads off a burned disc.

    The artifact is the contract (UX-02): scan the rendered text, not
    the template source.  Uses a split-key config so the conditional
    share-card blocks render too.
    """
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.meta.builder import MetaVolumeBuilder
    from lcsas.staging.metadata import HolographicInjector

    config = LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        repositories={
            "family": RepositoryConfig(
                name="family",
                mirror_path=tmp_path / "mirror" / "family",
                password_file=Path("/keys/family.key"),
            ),
        },
    )
    meta = tmp_path / "meta"
    meta.mkdir()
    builder = MetaVolumeBuilder(meta, config=config)
    builder._write_readme()
    builder._write_readme_txt()
    builder._write_start_here()
    docs = {
        f"<meta>/{name}": (meta / name).read_text(encoding="utf-8")
        for name in (
            "README_RESTORE.md",
            "README_RESTORE.txt",
            "START_HERE.txt",
        )
    }
    docs["<meta-noconfig>/START_HERE.txt"] = builder._render_meta_start_here(
        None
    )
    data = tmp_path / "data-disc"
    data.mkdir()
    injector = HolographicInjector(data)
    injector.write_start_here(config)
    injector.write_restore_instructions()
    docs.update(
        {
            f"<data-disc>/{name}": (data / name).read_text(encoding="utf-8")
            for name in ("START_HERE.txt", "RESTORE_INSTRUCTIONS.txt")
        }
    )
    return docs


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string in the parser tree (recursing into
    subparsers)."""
    seen: set[str] = set()
    stack = [parser]
    while stack:
        current = stack.pop()
        for action in current._actions:
            seen.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return seen


def _boot_artifact_files() -> list[str]:
    """Boot-artifact file names under the scanned trees."""
    found: list[str] = []
    for tree in _SCANNED_TREES:
        for path in sorted((_REPO_ROOT / tree).rglob("*")):
            rel = path.relative_to(_REPO_ROOT)
            if _SKIPPED_DIRS.intersection(rel.parts):
                continue
            if path.is_file() and _BOOT_FILE.search(path.name):
                found.append(rel.as_posix())
    return found


def test_no_boot_instruction_or_artifact_reappears(tmp_path: Path) -> None:
    from lcsas.cli.main import build_parser
    from lcsas.iso.xorriso import SubprocessXorrisoRunner, XorrisoRunner
    from lcsas.meta.builder import MetaVolumeBuilder

    offenders: list[str] = []

    # 1. Heir-facing docs carry no boot-the-disc instruction.
    docs = {
        f"recovery/docs/{name}": (_DOCS / name).read_text(encoding="utf-8")
        for name in ("RECOVER.txt", "RECOVER_WINDOWS.txt", "BOOT.txt")
    }
    docs.update(_generated_heir_docs(tmp_path))
    for name, text in docs.items():
        match = _BOOT_CLAIM.search(text)
        if match is not None:
            offenders.append(
                f"{name}: boot-the-disc instruction {match.group(0)!r}"
            )

    # 2. The readiness checklist keeps pointing here, not at a manual
    #    boot drill against non-bootable discs.
    checklist = _CHECKLIST.read_text(encoding="utf-8")
    if "META DISC BOOT TEST" in checklist:
        offenders.append(
            "READINESS_CHECKLIST.txt re-grew the manual 'META DISC BOOT "
            "TEST' item"
        )
    if "NOT bootable" not in checklist:
        offenders.append(
            "READINESS_CHECKLIST.txt no longer says the discs are NOT "
            "bootable"
        )

    # 3. No boot surface in code.
    offenders.extend(
        f"lcsas CLI flag {opt}"
        for opt in sorted(_all_option_strings(build_parser()))
        if _BOOT_IDENT.search(opt)
    )
    offenders.extend(
        f"MetaVolumeBuilder.__init__ parameter {name!r}"
        for name in inspect.signature(MetaVolumeBuilder.__init__).parameters
        if _BOOT_IDENT.search(name)
    )
    for cls in (XorrisoRunner, SubprocessXorrisoRunner):
        offenders.extend(
            f"{cls.__name__}.{name}"
            for name in vars(cls)
            if _BOOT_IDENT.search(name)
        )

    # 4. No boot artifact in the tree.
    offenders.extend(
        f"{rel} exists again"
        for rel in _DELETED_PATHS
        if (_REPO_ROOT / rel).exists()
    )
    offenders.extend(
        f"{rel}: boot-artifact file name" for rel in _boot_artifact_files()
    )

    assert not offenders, (
        "A boot instruction or boot artifact has reappeared:\n  "
        + "\n  ".join(offenders)
        + "\n"
        + _REVIVAL
    )
