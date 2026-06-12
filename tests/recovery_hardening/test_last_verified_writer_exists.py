"""Hardening test: volume_copies.last_verified_at must have a writer.

FAILURE MODE CAUGHT
-------------------
The disc-rot re-verification cadence (FMA-05) depends on
``volume_copies.last_verified_at`` being a LIVE column.  Before
BURN-04/FMA-05 the schema carried this per-copy freshness field but no
production code ever wrote it — so "when was each physical copy last
confirmed good, and which copies are overdue?" was unanswerable, and
the holographic catalogs burned onto later discs carried no
verification history.

These static assertions (same pattern as test_env_var_docs.py) pin:
  * an UPDATE writer for last_verified_at in src/lcsas/db/volume_copies.py,
  * the CLI verify path actually calling that writer,
  * the staleness report that makes the stamp visible to operators.

If any of them trips, the column has regressed to dead and shelf-decay
detection is silently gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
VOLUME_COPIES_PY = REPO_ROOT / "src" / "lcsas" / "db" / "volume_copies.py"
CLI_MAIN_PY = REPO_ROOT / "src" / "lcsas" / "cli" / "main.py"


def test_last_verified_at_has_production_writer() -> None:
    """volume_copies.py must contain an UPDATE that sets last_verified_at."""
    source = VOLUME_COPIES_PY.read_text(encoding="utf-8")
    assert re.search(
        r"UPDATE\s+volume_copies\s+SET\s+last_verified_at", source
    ), (
        "src/lcsas/db/volume_copies.py no longer contains an "
        "'UPDATE volume_copies SET last_verified_at' writer.  The "
        "last_verified_at column has regressed to dead: nothing records "
        "disc re-verification, so 'which copies are overdue?' "
        "(lcsas status --stale-copies) silently reports everything as "
        "never verified.  Restore touch_last_verified() (FMA-05)."
    )


def test_verify_cli_calls_the_writer() -> None:
    """The CLI verify path must call the last_verified_at writer."""
    source = CLI_MAIN_PY.read_text(encoding="utf-8")
    assert "touch_last_verified" in source, (
        "src/lcsas/cli/main.py no longer calls touch_last_verified().  "
        "'lcsas verify --disc' / 'verify --all --disc' must stamp "
        "last_verified_at on a passing copy, or the disc-rot "
        "re-verification loop records nothing (FMA-05)."
    )


def test_staleness_report_exists() -> None:
    """The staleness report that surfaces the stamp must exist."""
    source = CLI_MAIN_PY.read_text(encoding="utf-8")
    assert "--stale-copies" in source and "find_stale_copies" in source, (
        "The 'lcsas status --stale-copies' report is gone.  Without it "
        "the last_verified_at stamps are write-only: neither the owner "
        "nor an heir can ask which physical copies are overdue for "
        "re-verification (FMA-05)."
    )
