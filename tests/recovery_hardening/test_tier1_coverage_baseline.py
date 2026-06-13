"""Opt-in gate: make coverage-c must complete without error.

Run with:  LCSAS_COVERAGE=1 pytest tests/recovery_hardening/test_tier1_coverage_baseline.py -v
"""
import json
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

    # KEY-06: the lcsas-keyshare SLIP-0039 combiner sits on the tier-1
    # critical path and must stay inside the coverage report.  Assert the
    # gcovr filter still includes it, so a future edit that drops the
    # `--filter src/lcsas-keyshare/.*` line fails loudly here instead of
    # silently un-watching the combiner.
    cov_json = REPO_ROOT / "recovery" / "build" / "coverage.json"
    assert cov_json.exists(), f"coverage.json not created at {cov_json}"
    data = json.loads(cov_json.read_text())
    reported = {
        Path(e.get("filename", "")).name
        for e in data.get("files", [])
        if "src/lcsas-keyshare/" in e.get("filename", "")
    }
    for expected in ("slip39.c", "main.c"):
        assert expected in reported, (
            f"src/lcsas-keyshare/{expected} missing from coverage report; "
            f"keyshare files seen: {sorted(reported)}.  Did the gcovr "
            "--filter for src/lcsas-keyshare/ get dropped?"
        )
