"""Hardening test (GATE-06): the blind-restore XFAIL ledger must stay
honest and expire.

The ``tier1-missing`` blind variant was a permanent XFAIL with no expiry
and no mechanism that ever forced promotion (issue #227).  This test
turns the ledger into a tripwire:

  * every entry in ``tests/e2e/cdemu_blind_restore/XFAIL.list`` MUST carry
    an ``issue=#N`` reference AND a future ``expires=YYYY-MM-DD`` date;
  * an expired entry FAILS the hardening suite -- forcing the team to
    either promote the variant (delete the line) or consciously re-date
    it in a reviewed diff;
  * ``run_variant.sh`` must read the ledger rather than hard-coding a
    non-empty XFAIL default -- so a reintroduced inline default (which
    would silence a variant invisibly) is caught here.

Pure file parsing: always-on, no external tools, no LLM.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BLIND_DIR = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
XFAIL_LIST = BLIND_DIR / "XFAIL.list"
RUN_VARIANT = BLIND_DIR / "run_variant.sh"

_ISSUE_RE = re.compile(r"\bissue=#(\d+)\b")
_EXPIRES_RE = re.compile(r"\bexpires=(\d{4}-\d{2}-\d{2})\b")


def _ledger_entries() -> list[tuple[int, str]]:
    """Return (line_number, raw_line) for every non-comment, non-blank
    ledger line."""
    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(XFAIL_LIST.read_text().splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((lineno, raw))
    return out


def test_xfail_list_exists() -> None:
    assert XFAIL_LIST.is_file(), (
        f"the blind-restore XFAIL ledger is missing: {XFAIL_LIST}"
    )


def test_every_entry_has_issue_and_future_expiry() -> None:
    """Each ledger entry must name a tracking issue and an unexpired
    expiry date.  Expired entries fail loudly so an XFAIL can never
    outlive its justification silently."""
    entries = _ledger_entries()
    today = dt.date.today()
    problems: list[str] = []
    for lineno, raw in entries:
        variant = raw.split()[0]
        if not _ISSUE_RE.search(raw):
            problems.append(
                f"line {lineno}: variant '{variant}' has no issue=#N reference"
            )
        m = _EXPIRES_RE.search(raw)
        if not m:
            problems.append(
                f"line {lineno}: variant '{variant}' has no "
                f"expires=YYYY-MM-DD date"
            )
            continue
        try:
            expires = dt.date.fromisoformat(m.group(1))
        except ValueError:
            problems.append(
                f"line {lineno}: variant '{variant}' has an unparseable "
                f"expires={m.group(1)!r}"
            )
            continue
        if expires <= today:
            problems.append(
                f"line {lineno}: variant '{variant}' XFAIL expired on "
                f"{expires.isoformat()} -- promote it (remove the line) or "
                f"re-date it in a reviewed diff"
            )
    assert not problems, "XFAIL ledger problems:\n  " + "\n  ".join(problems)


def test_run_variant_reads_ledger_not_inline_default() -> None:
    """run_variant.sh must source the XFAIL set from XFAIL.list, not from
    a hard-coded non-empty inline default.  A reintroduced inline default
    like ``XFAIL="${LCSAS_VARIANT_XFAIL:-tier1-missing}"`` would silence a
    variant invisibly, defeating the ledger's expiry mechanism."""
    text = RUN_VARIANT.read_text()
    assert "XFAIL.list" in text, (
        "run_variant.sh no longer references the XFAIL.list ledger"
    )
    # An inline `:-<non-empty>` default on the XFAIL assignment is the
    # exact anti-pattern GATE-06 removed.  `${LCSAS_VARIANT_XFAIL+x}`
    # (presence test, no default value) and `${XFAIL:+...}` (the
    # accumulator) are fine; `${LCSAS_VARIANT_XFAIL:-something}` is not.
    bad = re.search(r"LCSAS_VARIANT_XFAIL:-\s*\S", text)
    assert bad is None, (
        "run_variant.sh reintroduced a hard-coded inline XFAIL default "
        f"({bad.group(0)!r}); the XFAIL set must come from XFAIL.list"
    )


def test_tier1_missing_is_tracked_against_issue_227() -> None:
    """The plan's named subject -- tier1-missing -- must be in the ledger
    tied to issue #227 until a live 15/15 promotion deletes it."""
    entries = _ledger_entries()
    matches = [raw for _, raw in entries if raw.split()[0] == "tier1-missing"]
    if not matches:
        pytest.skip(
            "tier1-missing no longer in the ledger -- presumably promoted; "
            "this guard is intentionally a skip, not a failure, post-promotion"
        )
    assert len(matches) == 1, f"duplicate tier1-missing entries: {matches}"
    assert _ISSUE_RE.search(matches[0]) and "#227" in matches[0], (
        f"tier1-missing must reference issue #227; got: {matches[0]!r}"
    )
