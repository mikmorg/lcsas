"""Hardening guard: tier-3 standalone restorer is SKIP-AND-CONTINUE.

RST-03 — the pure-Python last-resort restorer (standalone_restorer.py,
shipped on every data disc) must tolerate a single corrupt/missing blob
instead of aborting the whole restore on the first bad byte.  For a
non-technical heir this is the worst possible failure shape: one bad
blob → 0% restored, when 99.9% of the family archive is intact.

These are static guards (no optical hardware, no slow decode):
  * the generated _CLI_BLOCK wraps restore() in try/except and surfaces
    RESTORE_FAILURES.txt + exit code 2 (a raw traceback for the heir is
    the regression we are pinning against);
  * TIERS.txt documents the tolerant behaviour AND the remedy for
    already-burned data discs (re-run tier 3 from a NEWER meta disc).

The functional proof (real subprocess against a corrupt-blob fixture)
lives in tests/unit/test_standalone_subprocess.py; this file pins the
contract so a future _CLI_BLOCK refactor can't silently regress it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_TIERS_TXT = REPO_ROOT / "recovery" / "docs" / "TIERS.txt"


def _build_standalone_text() -> str:
    spec = importlib.util.spec_from_file_location(
        "_sb_rst03",
        REPO_ROOT / "src" / "lcsas" / "restore" / "standalone_builder.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_standalone()  # type: ignore[no-any-return]


def test_cli_block_wraps_restore_in_try_except() -> None:
    """The generated CLI must guard restore() so the heir never sees a
    raw Python traceback when a blob is unreadable."""
    text = _build_standalone_text()
    call_idx = text.find("restorer.restore(")
    assert call_idx >= 0, "generated CLI no longer calls restorer.restore()."
    # A try: must appear before the restore() call within the CLI body.
    head = text[:call_idx]
    assert head.rfind("try:") > head.rfind("def _cli_main"), (
        "restorer.restore() is no longer wrapped in a try/except inside "
        "_cli_main — a corrupt blob would raise a raw traceback to the heir."
    )


def test_cli_block_reports_manifest_and_exit_2() -> None:
    """The generated CLI must check restorer.failures, point the operator
    at RESTORE_FAILURES.txt, and exit 2 (distinct from a hard failure)."""
    text = _build_standalone_text()
    assert "restorer.failures" in text, (
        "generated CLI no longer inspects restorer.failures — skipped "
        "files would not be reported."
    )
    assert "RESTORE_FAILURES.txt" in text, (
        "generated CLI no longer mentions RESTORE_FAILURES.txt — the heir "
        "has no list of what could not be restored."
    )
    assert "sys.exit(2)" in text, (
        "generated CLI no longer exits 2 on partial restore — callers "
        "can't distinguish 'some files skipped' from a hard failure."
    )


def test_tiers_txt_documents_tolerant_tier3() -> None:
    """TIERS.txt must document the skip-and-continue behaviour and the
    remedy for already-burned (abort-on-first-error) data discs."""
    text = _TIERS_TXT.read_text(encoding="utf-8")
    assert "RESTORE_FAILURES.txt" in text, (
        "TIERS.txt does not mention RESTORE_FAILURES.txt — the tier-3 "
        "tolerant-restore behaviour is undocumented."
    )
    # The remedy note: old data discs ship the old script; re-run tier 3
    # from a newer meta disc against them.
    assert "newer meta disc" in text or "newer meta-disc" in text, (
        "TIERS.txt does not document the remedy for already-burned data "
        "discs (re-run tier 3 from a newer meta disc)."
    )
