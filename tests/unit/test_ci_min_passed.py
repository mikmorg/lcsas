"""Unit tests for scripts/ci_min_passed.py — the CI skip-rot floor.

The floor script parses a pytest junit XML and exits non-zero when the
passed-test count drops below a declared minimum, so a regression that
silently turns assertions into skips can't keep CI green (GATE-02).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "ci_min_passed.py"

_spec = importlib.util.spec_from_file_location("ci_min_passed", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_cmp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cmp)


def _write_junit(
    path: Path, *, tests: int, failures: int = 0, errors: int = 0, skipped: int = 0
) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{tests}" time="1.0">'
        "</testsuite></testsuites>"
    )
    return path


def test_count_passed_subtracts_skips_and_failures(tmp_path: Path) -> None:
    f = _write_junit(tmp_path / "j.xml", tests=20, failures=1, errors=1, skipped=3)
    assert _cmp.count_passed(f) == 15


def test_count_passed_bare_testsuite_root(tmp_path: Path) -> None:
    f = tmp_path / "bare.xml"
    f.write_text(
        '<testsuite name="pytest" errors="0" failures="0" '
        'skipped="2" tests="10" time="1.0"></testsuite>'
    )
    assert _cmp.count_passed(f) == 8


def test_count_passed_multiple_suites_summed(tmp_path: Path) -> None:
    f = tmp_path / "multi.xml"
    f.write_text(
        '<testsuites name="pytest tests">'
        '<testsuite name="a" errors="0" failures="0" skipped="1" tests="5"/>'
        '<testsuite name="b" errors="0" failures="0" skipped="0" tests="4"/>'
        "</testsuites>"
    )
    assert _cmp.count_passed(f) == 8


def test_at_floor_passes(tmp_path: Path) -> None:
    f = _write_junit(tmp_path / "j.xml", tests=15)
    rc = _cmp.main([str(f), "--min-passed", "15"])
    assert rc == 0


def test_below_floor_fails(tmp_path: Path) -> None:
    f = _write_junit(tmp_path / "j.xml", tests=20, skipped=10)
    rc = _cmp.main([str(f), "--min-passed", "15"])
    assert rc == 1


def test_above_floor_passes(tmp_path: Path) -> None:
    f = _write_junit(tmp_path / "j.xml", tests=50)
    rc = _cmp.main([str(f), "--min-passed", "30"])
    assert rc == 0


def test_missing_file_errors(tmp_path: Path) -> None:
    rc = _cmp.main([str(tmp_path / "nope.xml"), "--min-passed", "1"])
    assert rc == 2


def test_min_passed_required(tmp_path: Path) -> None:
    f = _write_junit(tmp_path / "j.xml", tests=5)
    with pytest.raises(SystemExit):
        _cmp.main([str(f)])
