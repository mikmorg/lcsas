"""BOOT-06 tripwire: the quarantined-boot README must keep recording the
``lcsas-init`` optical-only device scan defect and its sentinel-scan fix
spec, so a future revival does not re-derive either from scratch.

This file is the designated home of BOOT-08's quarantine assertions;
until BOOT-08 lands it carries only the BOOT-06 record-keeping gate.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "experimental" / "boot" / "README.md"


def test_readme_records_optical_only_scan_defect() -> None:
    assert _README.is_file(), f"missing {_README} — delete this test with the tree"
    text = _README.read_text()
    assert "BOOT-06" in text, "README defect index lost its BOOT-06 row"
    # The sentinel-scan fix spec and its two revival-gated tests
    # (C unit test + USB-attach leg of the BOOT-08 QEMU boot smoke).
    for marker in ("sentinel", "test_init_medium_scan.c", "usb-storage"):
        assert marker in text, (
            "experimental/boot/README.md no longer records the BOOT-06 "
            f"sentinel-scan fix spec (missing {marker!r})"
        )
