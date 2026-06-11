"""
test_no_boot_deadend_routing.py -- static regression guard against the
boot-the-disc dead end in the heir-facing recovery routing (BOOT-01).

FAILURE MODE CAUGHT
-------------------
No disc any build has ever produced is bootable: `lcsas meta build` never
passes `bootable=True`, no kernel/initramfs artifact exists in the repo, and
the documented `--recovery-boot` flag never existed in the argparse tree.
Yet for a long time every "no working computer" route — RECOVER.txt's
decision flow, BOOT.txt, and the START_HERE heredoc burned onto every meta
disc — told the heir to boot the disc directly.  An heir with a dead
computer would follow the instruction, get "no bootable device", and have
no hint the path was fictional.

The 2026-06 deep audit verdict was DROP: the boot scaffolding was
quarantined to experimental/boot/ and the no-OS journey rerouted to a
current live-Linux USB stick.  These tests pin that routing so it cannot
silently regress back to the dead end.

Tests are intentionally static (Path.read_text assertions only) — they add
zero runtime cost and survive in environments with no optical hardware.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_RECOVER_TXT = REPO_ROOT / "recovery" / "docs" / "RECOVER.txt"
_BOOT_TXT = REPO_ROOT / "recovery" / "docs" / "BOOT.txt"
_BUILDER_PY = REPO_ROOT / "src" / "lcsas" / "meta" / "builder.py"

# Read each file once at module level — tests are static string checks only.
_RECOVER_TEXT = _RECOVER_TXT.read_text(encoding="utf-8")
_BOOT_TEXT = _BOOT_TXT.read_text(encoding="utf-8")
_BUILDER_SRC = _BUILDER_PY.read_text(encoding="utf-8")


def test_recover_txt_no_os_branch_does_not_route_to_booting_the_disc():
    """The no-OS branch of the decision flow must not tell the heir to boot
    the (non-bootable) recovery medium."""
    assert "Boot the recovery medium" not in _RECOVER_TEXT, (
        "RECOVER.txt routes the 'no working OS' case to booting the recovery "
        "medium, but no LCSAS disc is bootable (BOOT-01). The branch must "
        "route to another computer or a live-Linux USB stick instead."
    )


def test_recover_txt_no_os_branch_routes_to_live_usb():
    """The no-OS branch must route to the live-USB procedure."""
    idx = _RECOVER_TEXT.find("Do you have a working OS?")
    assert idx != -1, (
        "RECOVER.txt is missing the 'Do you have a working OS?' decision "
        "question entirely."
    )
    # Bounded window: only the no-OS branch itself, not unrelated mentions.
    window = _RECOVER_TEXT[idx: idx + 400]
    assert "live" in window and "USB" in window, (
        "The no-OS branch of RECOVER.txt's decision flow does not mention a "
        "live-Linux USB stick. The replacement route for 'no working "
        "computer' is a current live-Linux USB (see BOOT.txt / BOOT-01)."
    )


def test_boot_txt_has_not_bootable_banner():
    """BOOT.txt must open by stating that the discs are NOT bootable."""
    assert "NOT bootable" in _BOOT_TEXT, (
        "BOOT.txt does not carry the 'NOT bootable' banner. Without it, an "
        "heir with a dead computer has no hint that booting the disc is a "
        "dead end (the historical failure this guard exists for)."
    )


def test_boot_txt_carries_the_live_usb_restore_command():
    """BOOT.txt must give the exact restore command to run from the live
    environment, matching the on-disc layout."""
    assert "sh /mnt/recovery/scripts/restore.sh" in _BOOT_TEXT, (
        "BOOT.txt is missing the exact restore command "
        "'sh /mnt/recovery/scripts/restore.sh ...' for the live-USB "
        "environment. The heir needs a copy-pasteable command."
    )


def test_boot_txt_does_not_document_the_phantom_recovery_boot_flag():
    """The `--recovery-boot` CLI flag never existed; BOOT.txt must not
    document it."""
    assert "--recovery-boot" not in _BOOT_TEXT, (
        "BOOT.txt documents the '--recovery-boot' flag, which exists nowhere "
        "in the lcsas argparse tree. Phantom commands must not ship on-disc."
    )


def test_builder_start_here_does_not_promise_a_bootable_disc():
    """Belt-and-braces for UX-03: the minimal START_HERE heredoc in
    builder.py must not tell the heir to boot the disc."""
    assert "Boot directly from the disc" not in _BUILDER_SRC, (
        "src/lcsas/meta/builder.py still contains the 'Boot directly from "
        "the disc' START_HERE text. That instruction is a dead end (no disc "
        "is bootable) and must route to the live-USB procedure instead."
    )
