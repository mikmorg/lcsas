"""Opt-in gate: make coverage-c must complete without error.

Run with:  LCSAS_COVERAGE=1 pytest tests/recovery_hardening/test_tier1_coverage_baseline.py -v
"""
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LCSAS_COVERAGE"),
    reason="set LCSAS_COVERAGE=1 to run the coverage baseline test (~5 min)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_coverage_c_completes() -> None:
    """make coverage-c must exit 0 and produce coverage.txt."""
    res = subprocess.run(
        ["make", "-C", str(REPO_ROOT / "recovery"), "coverage-c"],
        capture_output=True, text=True, timeout=600,
    )
    assert res.returncode == 0, (
        f"make coverage-c failed.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    cov_txt = REPO_ROOT / "recovery" / "build" / "coverage.txt"
    assert cov_txt.exists(), (
        f"coverage.txt not created at {cov_txt}.\nmake output:\n{res.stdout}"
    )
    content = cov_txt.read_text()
    assert "LINE_COVERAGE=" in content, (
        f"coverage.txt has unexpected format:\n{content}"
    )
