"""Hardening: CI coverage threshold tracks the local measured floor (GATE-07).

The tier-1 C binary's coverage contract is the local floor in
`recovery/Makefile` (`THRESHOLD ?= 88` — the measured floor that prevents
regressions). CI runs the same gate, but in an environment that lands a
few points below local (the conftest/lcsas-install effect documented in
the workflow's Install step), so CI uses a *derived* number: the local
floor minus a small tolerance — not an unrelated magic constant.

These tests pin that relationship so the CI threshold can't silently
drift back down to a no-op (the original 60 vs 88 gap), and so AUDIT.md
quotes the same CI number the workflow actually runs.

Stdlib-only, text-level parsing — same pattern as the other doc/workflow
parity hardening tests in this directory.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "audit-gate.yml"
RECOVERY_MAKEFILE = REPO_ROOT / "recovery" / "Makefile"
AUDIT_DOC = REPO_ROOT / "recovery" / "docs" / "AUDIT.md"

# How far below the local floor the CI environment is allowed to sit. The
# measured delta is ~5 pts (conftest/lcsas-install effect; see the workflow
# Install-step comment). Tightening this past the real measurement would
# make CI flaky; widening it weakens the gate.
CI_TOLERANCE = 5


def _ci_threshold() -> int:
    """THRESHOLD=N from the audit-gate `run:` line (ignores comments)."""
    for raw in WORKFLOW.read_text().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = re.search(r"audit-gate\s+THRESHOLD=(\d+)", raw)
        if m:
            return int(m.group(1))
    raise AssertionError("no `make ... audit-gate THRESHOLD=N` run line in audit-gate.yml")


def _local_threshold() -> int:
    """THRESHOLD ?= N default from recovery/Makefile."""
    m = re.search(r"^THRESHOLD\s*\?=\s*(\d+)", RECOVERY_MAKEFILE.read_text(), re.MULTILINE)
    assert m, "recovery/Makefile no longer defines `THRESHOLD ?= N`"
    return int(m.group(1))


def test_ci_threshold_within_tolerance_of_local_floor() -> None:
    ci = _ci_threshold()
    local = _local_threshold()
    assert ci >= local - CI_TOLERANCE, (
        f"CI audit-gate threshold ({ci}) is more than {CI_TOLERANCE} pts below "
        f"the local measured floor ({local} in recovery/Makefile). The CI "
        f"number must be a derived value (local − measured CI delta), not a "
        f"weaker magic constant — raise it or document a larger measured delta."
    )
    assert ci <= local, (
        f"CI threshold ({ci}) exceeds the local floor ({local}); CI runs in a "
        f"lower-coverage environment, so it cannot be stricter than local."
    )


def test_audit_doc_quotes_the_ci_threshold() -> None:
    ci = _ci_threshold()
    text = AUDIT_DOC.read_text()
    assert f"THRESHOLD={ci}" in text, (
        f"recovery/docs/AUDIT.md must quote the CI threshold `THRESHOLD={ci}` "
        f"that the workflow actually runs (CI integration section). The doc "
        f"and the workflow have drifted apart."
    )
