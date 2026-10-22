"""Run recovery/tests/test_multidisc.py's cases inside the hardening tier (#465).

That script drives the real tier-1 binary through the multi-disc flows
(pack search across two discs, fail-fast, the interactive swap prompt,
meta-disc exclusion, catalog freshest-pick and labels, restore.sh
relocation).  It was a standalone script that no Makefile target or CI
job ran, so one case rotted for months after #143 changed the meta-disc
liveness rule.  Each case is now its own test here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "recovery" / "tests" / "test_multidisc.py"

_spec = importlib.util.spec_from_file_location("lcsas_test_multidisc", SCRIPT)
assert _spec is not None and _spec.loader is not None
multidisc = importlib.util.module_from_spec(_spec)
sys.modules["lcsas_test_multidisc"] = multidisc
_spec.loader.exec_module(multidisc)

CASES = multidisc.CASES


def test_every_case_is_listed():
    """A case_* function missing from CASES would silently never run."""
    defined = {n for n in vars(multidisc) if n.startswith("case_")}
    assert defined, "expected case_* functions in test_multidisc.py"
    assert defined == {c.__name__ for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_multidisc_case(case):
    if not multidisc.BINARY.is_file() or not os.access(multidisc.BINARY, os.X_OK):
        pytest.skip(f"{multidisc.BINARY} not built (make -C recovery)")
    assert case() == 0, f"{case.__name__} failed; see its stderr above"
