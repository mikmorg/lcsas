"""Opt-in gate: make sanitize must complete without any ASan/UBSan/LSan findings.

Run with:  LCSAS_SANITIZE=1 pytest tests/recovery_hardening/test_tier1_sanitize.py -v
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LCSAS_SANITIZE"),
    reason="set LCSAS_SANITIZE=1 to run the sanitizer gate (~3 min, needs clang)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sanitize_completes_with_zero_findings() -> None:
    """make sanitize must exit 0 with no ASan/UBSan/LSan findings."""
    if shutil.which("clang") is None:
        pytest.skip("clang not installed")
    res = subprocess.run(
        ["make", "-C", str(REPO_ROOT / "recovery"), "sanitize"],
        capture_output=True, text=True, timeout=600,
    )
    assert res.returncode == 0, (
        f"make sanitize failed — likely ASan/UBSan/LSan finding.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "PASS: 0 ASan/UBSan/LSan findings" in res.stdout, (
        f"sanitize target did not print the expected PASS line.\n"
        f"stdout:\n{res.stdout}"
    )
