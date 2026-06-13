"""Hardening test: ECC repair-capacity claims must match configured redundancy.

Two heir/operator-facing recovery docs previously claimed dvdisaster
RS03 repair "restores up to ~30% of unreadable sectors".  The configured
default is 15% redundancy, and Reed-Solomon erasure capacity is roughly
the redundancy fraction -- so the honest margin is ~13-15%, not 30%.  The
inflated figure shapes decisions made under stress (an heir delays a
re-burn believing they have double the real margin), so it is a
durability hazard, not a cosmetic typo (FMT-06).

These static assertions pin the docs to the configured math:
  * The literal "30%" must not appear in either doc.
  * Each doc must state a numeric range whose endpoints bracket
    LCSASConfig.default_ecc_redundancy_pct, imported here so the docs
    can never silently drift from the configured redundancy again.
    Changing default_ecc_redundancy_pct without updating the docs makes
    this test fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lcsas.config.settings import LCSASConfig

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVER_TXT = REPO_ROOT / "recovery" / "docs" / "RECOVER.txt"
READINESS_TXT = REPO_ROOT / "recovery" / "docs" / "READINESS_CHECKLIST.txt"

# The configured redundancy is the source of truth the docs must track.
# LCSASConfig is a frozen dataclass; read the field default directly.
CONFIGURED_PCT = LCSASConfig.__dataclass_fields__["default_ecc_redundancy_pct"].default
assert isinstance(CONFIGURED_PCT, int)

_DOCS = pytest.mark.parametrize(
    "doc_path",
    [
        pytest.param(RECOVER_TXT, id="RECOVER.txt"),
        pytest.param(READINESS_TXT, id="READINESS_CHECKLIST.txt"),
    ],
)

# Matches "13-15%" / "13 - 15 %" style ranges.
_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*%")


@_DOCS
def test_doc_exists(doc_path: Path) -> None:
    assert doc_path.is_file(), f"recovery doc missing: {doc_path}"


@_DOCS
def test_no_inflated_30pct_claim(doc_path: Path) -> None:
    """The discredited ~30% repair-capacity figure must not reappear."""
    content = doc_path.read_text()
    assert "30%" not in content, (
        f"{doc_path.name} contains '30%'.  ECC repair capacity is bounded by "
        f"the configured redundancy ({CONFIGURED_PCT}%), giving a conservative "
        f"~13-15% repairable margin -- NOT 30% (that was dvdisaster's 'high' "
        f"preset, not our config).  Overstating the margin makes heirs delay "
        f"re-burn past the repairable window (FMT-06)."
    )


@_DOCS
def test_stated_range_brackets_configured_redundancy(doc_path: Path) -> None:
    """The doc's repair-capacity range must bracket the configured pct."""
    content = doc_path.read_text()
    ranges = _RANGE_RE.findall(content)
    bracketing = [
        (lo, hi)
        for lo_s, hi_s in ranges
        for lo, hi in [(int(lo_s), int(hi_s))]
        if lo <= CONFIGURED_PCT <= hi
    ]
    assert bracketing, (
        f"{doc_path.name} does not state an ECC repair-capacity range that "
        f"brackets the configured default_ecc_redundancy_pct={CONFIGURED_PCT}%.  "
        f"Found ranges: {ranges}.  The docs must derive the repairable margin "
        f"from the configured redundancy so they cannot drift from the math "
        f"(FMT-06).  If you changed default_ecc_redundancy_pct, update both "
        f"recovery docs to match."
    )


@_DOCS
def test_doc_mentions_configured_pct(doc_path: Path) -> None:
    """Each doc must name the configured redundancy value explicitly."""
    content = doc_path.read_text()
    assert f"{CONFIGURED_PCT}%" in content, (
        f"{doc_path.name} does not mention the configured "
        f"{CONFIGURED_PCT}% redundancy.  Tie the repair-capacity claim to the "
        f"configured value so an heir can see where the margin comes from "
        f"(FMT-06)."
    )
