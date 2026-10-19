"""Hardening: tier-1 and tier-3 refuse the same scrypt parameters (#470).

A repository key file's scrypt N, r and p are read before any MAC check,
so both readers refuse values past a work cap and a memory cap instead
of deriving with them.  Tier-1 defines the caps in
``recovery/src/lcsas-restore/scrypt.h``; the tier-3 Python reader
(``src/lcsas/restore/restic_fallback.py``) defines its own copy because
it must run with nothing but the standard library.  If the two drift, a
key file one tier refuses is run by the other, or one tier refuses a
repository the other can open.

Stdlib-only, text-level parsing of the C header.
"""

from __future__ import annotations

import re
from pathlib import Path

from lcsas.restore import restic_fallback

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRYPT_H = REPO_ROOT / "recovery" / "src" / "lcsas-restore" / "scrypt.h"


def _c_define(name: str) -> int:
    """Value of ``#define <name> (1UL << K)`` in scrypt.h."""
    m = re.search(
        rf"^#define\s+{name}\s+\(1UL\s*<<\s*(\d+)\)\s*$",
        SCRYPT_H.read_text(),
        re.MULTILINE,
    )
    assert m, f"{name} not found as `(1UL << K)` in {SCRYPT_H}"
    return 1 << int(m.group(1))


def test_work_cap_matches() -> None:
    assert _c_define("LCSAS_SCRYPT_MAX_WORK") == restic_fallback.SCRYPT_MAX_WORK


def test_memory_cap_matches() -> None:
    assert _c_define("LCSAS_SCRYPT_MAX_MEM") == restic_fallback.SCRYPT_MAX_MEM


def test_caps_admit_restics_maximum() -> None:
    """The most restic/rustic write (N=2^20, r=8, p=1) must stay openable
    by both tiers: the caps are for corrupt files, not real repos."""
    n, r, p = 1 << 20, 8, 1
    assert n * r * p <= _c_define("LCSAS_SCRYPT_MAX_WORK")
    assert 128 * r * (n + p + 3) <= _c_define("LCSAS_SCRYPT_MAX_MEM")
