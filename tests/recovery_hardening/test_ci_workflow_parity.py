"""Hardening: CI workflow ↔ `make gate` suite parity (GATE-02).

`make gate` is "the final gate that says this build is shippable" — its
transitive prerequisites are the canonical shippable-suite list.  CI must
run every one of those tiers, or a behavioral regression (notably to
recovery/scripts/restore.sh, whose only behavioral coverage lives in
tests/recovery_hardening/) can merge with zero machine enforcement.

This test parses the Makefile to derive the transitive leaf prerequisites
of `gate`, then asserts each appears in .github/workflows/test.yml — either
as `make <target>` or via an explicit, commented equivalence (e.g. the raw
`pytest tests/recovery_hardening/` invocation in the dedicated job).  A
declared KNOWN_UNWIRED set lets a deliberate, tracked gap be *visible*
rather than silent.  Fails if a new tier is added to test-all without CI
wiring, or if a wired step is deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

# Tiers intentionally not yet wired into CI, each with the plan that removes
# it from this set.  A gap here is *declared*, never invisible.
#   (empty — GATE-10 wired test-e2e via LCSAS_E2E_BASE; see EQUIVALENCE.)
KNOWN_UNWIRED: set[str] = set()

# Some gate prerequisites are run in CI via an equivalent pinned command
# rather than the bare `make <target>`.  Maintain the equivalence here so
# the parity check understands "this raw invocation == that make target".
# Each value is a regex matched against the workflow text.
EQUIVALENCE: dict[str, str] = {
    # The recovery-hardening job runs the *whole* suite directly with
    # --junitxml so the skip-rot floor can read the result, instead of `make
    # test-recovery-hardening` (which has no junit output).  Require the
    # full-directory form (trailing slash then a flag/whitespace) so a
    # single-file invocation — e.g. the meta-bundling smoke in the `test`
    # job — does NOT satisfy this; only running the entire suite counts.
    "test-recovery-hardening": r"pytest\s+tests/recovery_hardening/\s",
    # GATE-10 runs the e2e pipeline test directly (with LCSAS_E2E_BASE +
    # a grep-for-`1 passed` guard) rather than `make test-e2e`, so the CI
    # step proves it RAN instead of going green-by-skip.  Match that pinned
    # invocation of the canonical pipeline test.
    "test-e2e": r"pytest\s+tests/e2e/test_scripts\.py::test_e2e_pipeline",
}


def _make_target_prereqs(makefile_text: str) -> dict[str, list[str]]:
    """Map each Makefile target to its declared prerequisites (rule lines only)."""
    prereqs: dict[str, list[str]] = {}
    for line in makefile_text.splitlines():
        # A rule line: `target: dep1 dep2`  (not `:=` assignments, not recipes).
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:(?!=)\s*(.*)$", line)
        if not m:
            continue
        name, deps = m.group(1), m.group(2)
        # Strip recipe/inline-comment tails.
        deps = deps.split("#", 1)[0]
        prereqs[name] = [d for d in deps.split() if d]
    return prereqs


def _transitive_leaf_prereqs(target: str, prereqs: dict[str, list[str]]) -> set[str]:
    """Return the leaf (no-further-prereq) prerequisites reachable from target."""
    leaves: set[str] = set()
    seen: set[str] = set()
    stack = [target]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        children = prereqs.get(cur, [])
        if not children and cur != target:
            leaves.add(cur)
            continue
        for child in children:
            if child in prereqs and prereqs[child]:
                stack.append(child)
            else:
                leaves.add(child)
    return leaves


def _gate_suite() -> set[str]:
    prereqs = _make_target_prereqs(MAKEFILE.read_text())
    assert "gate" in prereqs, "Makefile no longer defines a `gate` target"
    return _transitive_leaf_prereqs("gate", prereqs)


def test_gate_suite_contains_expected_tiers() -> None:
    """Guard the derivation itself: the canonical tiers must be present."""
    suite = _gate_suite()
    expected = {
        "lint",
        "typecheck",
        "test-unit",
        "test-integration",
        "test-e2e",
        "test-recovery-hardening",
    }
    missing = expected - suite
    assert not missing, (
        f"`make gate` no longer pulls in {sorted(missing)} — either the "
        f"Makefile changed or this parity test's parser drifted."
    )


def _workflow_noncomment_text() -> str:
    """Workflow text with full-line YAML comments stripped.

    Prevents a wiring claim in a `#` comment from satisfying the parity
    check; only real `run:` invocations should count.
    """
    lines = []
    for line in WORKFLOW.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_every_gate_tier_is_wired_or_declared_unwired() -> None:
    suite = _gate_suite()
    workflow = _workflow_noncomment_text()
    unwired: list[str] = []
    for tier in sorted(suite):
        if tier in KNOWN_UNWIRED:
            continue
        wired = re.search(rf"make\s+{re.escape(tier)}\b", workflow) is not None
        if not wired and tier in EQUIVALENCE:
            wired = re.search(EQUIVALENCE[tier], workflow) is not None
        if not wired:
            unwired.append(tier)
    assert not unwired, (
        f"`make gate` tiers not run in CI: {unwired}. Add a `make <target>` "
        f"step (or an EQUIVALENCE entry for a pinned command) in "
        f".github/workflows/test.yml, or add the tier to KNOWN_UNWIRED with "
        f"the plan that will wire it."
    )


def test_known_unwired_are_actually_gate_tiers() -> None:
    """KNOWN_UNWIRED must not rot: every entry is still a real gate tier."""
    suite = _gate_suite()
    stale = KNOWN_UNWIRED - suite
    assert not stale, (
        f"KNOWN_UNWIRED lists {sorted(stale)} which are no longer `make gate` "
        f"prerequisites — remove them so the declared-gap list stays honest."
    )


def test_recovery_hardening_job_has_skip_rot_floor() -> None:
    """The hardening job must guard against silent skip growth (GATE-02)."""
    workflow = WORKFLOW.read_text()
    assert "ci_min_passed.py" in workflow, (
        "the recovery-hardening CI job no longer runs scripts/ci_min_passed.py "
        "— the skip-rot floor is the piece that makes the hardening job "
        "meaningful (otherwise 50 silent skips still read as green)."
    )
