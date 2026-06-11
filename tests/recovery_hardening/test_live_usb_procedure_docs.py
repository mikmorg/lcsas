"""test_live_usb_procedure_docs.py -- static doc-pinning gate for the
live-USB no-OS recovery procedure [BOOT-04].

FAILURE MODE CAUGHT
-------------------
BOOT-01 dropped the never-built bootable-meta-disc stack and replaced the
no-OS story with "boot any CURRENT live-Linux image, then run restore.sh
from the META disc".  That replacement is pure documentation -- if its
load-bearing content silently regresses out of recovery/docs/BOOT.txt or
docs/workflows/restore-live-usb.md, an heir on a dead machine is back to
the pre-BOOT-01 dead end, and no code path notices.

These tests pin the five pieces of content the procedure cannot work
without (audit plan BOOT-04 §Fix design A):

  1. a concrete live-image source by name (Ubuntu),
  2. the firmware boot-menu key hint (F12 / F2),
  3. the exact read-only mount command for the META disc,
  4. the exact restore.sh invocation off the mounted disc,
  5. the one-sentence rationale for using a CURRENT image
     (Secure-Boot signed + current hardware drivers).

Pins are intentionally loose (case-insensitive substrings) so wording can
evolve without gaming the gate.  Also asserts the UX_CONCERNS.txt ID 007
ledger entry reflects the mitigation (status MITIGATED, Secure-Boot
rationale tracked) so the 2035-hardware argument stays recorded.

Tests are static (Path.read_text only) -- zero runtime cost, no optical
hardware, same style as test_disc_swap_docs.py.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOT_TXT = REPO_ROOT / "recovery" / "docs" / "BOOT.txt"
_WORKFLOW_MD = REPO_ROOT / "docs" / "workflows" / "restore-live-usb.md"
_UX_CONCERNS_TXT = REPO_ROOT / "recovery" / "docs" / "UX_CONCERNS.txt"

# Read once at module level; compare case-insensitively throughout.
_BOOT_TEXT = _BOOT_TXT.read_text(encoding="utf-8").lower()
_WORKFLOW_TEXT = _WORKFLOW_MD.read_text(encoding="utf-8").lower()
_UX_CONCERNS_TEXT = _UX_CONCERNS_TXT.read_text(encoding="utf-8")

# (doc id, lowered text) pairs -- both documents must carry the full
# procedure independently: BOOT.txt is what the heir reads off the disc,
# the workflow page is what the repo-side operator reads.
_DOCS = [
    ("recovery/docs/BOOT.txt", _BOOT_TEXT),
    ("docs/workflows/restore-live-usb.md", _WORKFLOW_TEXT),
]

_DOC_IDS = [doc_id for doc_id, _ in _DOCS]


@pytest.mark.parametrize(("doc_id", "text"), _DOCS, ids=_DOC_IDS)
def test_names_concrete_live_image_source(doc_id, text):
    """Pin 1: a concrete live-image source by name (Ubuntu)."""
    assert "ubuntu" in text, (
        f"{doc_id} no longer names a concrete live-image source. "
        "The procedure must give one worked example by name (Ubuntu) -- "
        "'any Linux' alone is not actionable for a non-technical heir."
    )


@pytest.mark.parametrize(("doc_id", "text"), _DOCS, ids=_DOC_IDS)
def test_documents_boot_menu_key(doc_id, text):
    """Pin 2: the firmware boot-menu key hint (F12 / F2)."""
    assert "f12" in text and "f2" in text, (
        f"{doc_id} lost the boot-menu key hint (F12/F2). Without it the "
        "heir cannot select the USB stick at power-on."
    )


@pytest.mark.parametrize(("doc_id", "text"), _DOCS, ids=_DOC_IDS)
def test_documents_exact_mount_command(doc_id, text):
    """Pin 3: the exact read-only mount command for the META disc."""
    assert "mount -o ro /dev/sr0 /mnt" in text, (
        f"{doc_id} lost the exact mount command "
        "('mount -o ro /dev/sr0 /mnt'). The fallback for live systems "
        "that do not auto-mount the disc must be copy-pasteable."
    )


@pytest.mark.parametrize(("doc_id", "text"), _DOCS, ids=_DOC_IDS)
def test_documents_exact_restore_invocation(doc_id, text):
    """Pin 4: the exact restore.sh invocation off the mounted disc."""
    assert "sh /mnt/recovery/scripts/restore.sh" in text, (
        f"{doc_id} lost the exact recovery invocation "
        "('sh /mnt/recovery/scripts/restore.sh ...'). This is the single "
        "command the whole procedure leads up to."
    )


@pytest.mark.parametrize(("doc_id", "text"), _DOCS, ids=_DOC_IDS)
def test_explains_why_current_image(doc_id, text):
    """Pin 5: rationale for a CURRENT image -- Secure-Boot signed +
    current drivers.  This is the argument that replaced the dropped
    self-built boot stack (BOOT-01); losing it invites someone to
    'helpfully' archive a frozen ISO next to the discs, which decays
    against future hardware."""
    assert "secure-boot signed" in text, (
        f"{doc_id} lost the Secure-Boot rationale. It must state that a "
        "current live image is Secure-Boot signed, so modern firmware "
        "accepts it without configuration changes."
    )
    assert "drivers" in text, (
        f"{doc_id} lost the drivers rationale. It must state that a "
        "current live image carries current hardware drivers, so it "
        "boots on machines newer than the discs."
    )


def test_ux_concerns_id007_is_mitigated():
    """UX_CONCERNS.txt ID 007 must be MITIGATED (not DEFERRED) now that
    the live-USB route covers drive-less / no-OS machines."""
    idx = _UX_CONCERNS_TEXT.find("ID 007")
    assert idx != -1, "UX_CONCERNS.txt is missing the ID 007 entry entirely."
    window = _UX_CONCERNS_TEXT[idx: idx + 300]
    assert "MITIGATED" in window, (
        "UX_CONCERNS.txt ID 007 does not show STATUS: MITIGATED within its "
        "block. The live-USB route (BOOT.txt OPTION 2) mitigates the "
        "optical-drive-rarity concern; the ledger must say so."
    )


def test_ux_concerns_id007_keeps_bd_reader_requirement():
    """The residual hardware dependency (a USB BD reader is still needed
    to READ the discs) must not be edited away while marking ID 007
    mitigated -- the concern is mitigated, not closed."""
    idx = _UX_CONCERNS_TEXT.find("ID 007")
    assert idx != -1, "UX_CONCERNS.txt is missing the ID 007 entry entirely."
    window = _UX_CONCERNS_TEXT[idx: idx + 2200]
    assert "USB BD reader" in window, (
        "UX_CONCERNS.txt ID 007 no longer mentions the USB BD reader. "
        "The live-USB route gets an heir a working OS, but reading the "
        "discs still requires optical hardware -- keep that residual "
        "requirement documented."
    )


def test_ux_concerns_tracks_secure_boot_rationale():
    """The Secure-Boot / hardware-evolution rationale must be tracked in
    the permanent on-disc ledger (acceptance: `grep -rni secureboot
    recovery/docs/` hits), not only in repo-side plan files."""
    assert "secureboot" in _UX_CONCERNS_TEXT.lower(), (
        "recovery/docs/UX_CONCERNS.txt lost the SecureBoot rationale "
        "(ID 007). The reason the no-OS path rides current signed live "
        "images -- UEFI Secure Boot default-on + driver churn -- must "
        "stay recorded on-disc."
    )
