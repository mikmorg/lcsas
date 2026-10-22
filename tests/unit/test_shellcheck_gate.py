"""Unit tests for scripts/shellcheck_gate.py — the restore.sh lint ratchet (#484)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "shellcheck_gate.py"

_spec = importlib.util.spec_from_file_location("shellcheck_gate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules["shellcheck_gate"] = gate
_spec.loader.exec_module(gate)


def test_matching_counts_pass():
    counts = Counter({("a.sh", "SC2086"): 2})
    assert gate.compare(counts, Counter(counts)) == []


def test_new_finding_fails():
    found = Counter({("a.sh", "SC2086"): 3, ("a.sh", "SC2155"): 1})
    baseline = Counter({("a.sh", "SC2086"): 2})
    problems = gate.compare(found, baseline)
    assert any(p.startswith("NEW: a.sh SC2086") for p in problems)
    assert any(p.startswith("NEW: a.sh SC2155") for p in problems)


def test_fixed_finding_demands_a_lower_baseline():
    problems = gate.compare(Counter(), Counter({("a.sh", "SC2086"): 1}))
    assert problems and problems[0].startswith("STALE BASELINE")


def test_baseline_parser_ignores_comments(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("# header\nrecovery/scripts/x.sh SC2317 4  # why\n\n")
    assert gate.load_baseline(f) == Counter({("recovery/scripts/x.sh", "SC2317"): 4})


def test_committed_baseline_matches_the_tree(monkeypatch):
    """The same check CI runs; skips where shellcheck isn't installed."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    files = sorted(str(p.relative_to(REPO_ROOT))
                   for p in (REPO_ROOT / "recovery" / "scripts").glob("*.sh"))
    assert files, "derived file list must not be empty"
    monkeypatch.chdir(REPO_ROOT)
    found = gate.run_shellcheck(files)
    assert gate.compare(found, gate.load_baseline(gate.BASELINE)) == []
