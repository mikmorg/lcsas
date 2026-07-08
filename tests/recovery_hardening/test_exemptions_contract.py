"""Hardening: the tier-1 coverage-exemptions contract cannot silently rot or
decouple (GATE-11, issue #383).

`recovery/docs/EXEMPTIONS.md` is the live, enforced list of every uncovered
line in `recovery/src/lcsas-restore/*.c`.  It went RED-but-unnoticed on the
audit-gate CI for ~3 weeks (#383) because the enforcement runs ONLY in the
~40-minute `audit-gate` job, not in `make gate`.

The gate's *coverage* meaning can only be recomputed by actually building +
instrumenting the C (the audit-gate job's job — see the CI-required-check
recommendation in docs/adr/0001).  What THIS test pins, cheaply and offline,
is the wiring and shape that let the gate mean anything at all, so the
enforcement can't be quietly deleted, renamed, or decoupled between audit-gate
runs, and so gross fence rot (a pinned line past EOF, a bad category, a
dropped FENCE marker) fails in the *watched* suite (`make gate` / test.yml)
rather than three weeks later on a gate nobody was looking at.

Stdlib-only, text-level parsing — same pattern as the sibling GATE tests
(test_ci_workflow_parity / test_audit_gate_threshold_parity /
test_workflow_path_filter).  It deliberately does NOT run coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
EXEMPTIONS_MD = RECOVERY / "docs" / "EXEMPTIONS.md"
CHECK_PY = RECOVERY / "scripts" / "exemptions_check.py"
MAKEFILE = RECOVERY / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "audit-gate.yml"
SRC_DIR = RECOVERY / "src" / "lcsas-restore"

FENCE_BEGIN = "<!-- EXEMPTIONS-FENCE-BEGIN -->"
FENCE_END = "<!-- EXEMPTIONS-FENCE-END -->"
CATEGORIES = {"INTRACTABLE", "DEFENSIVE", "DEFERRED", "VOLATILE"}
# Mirrors exemptions_check.ENTRY_RE: `file.c:NNN  CATEGORY  rationale`.
ENTRY_RE = re.compile(
    r"^([A-Za-z0-9_]+\.c):(\d+)\s+(INTRACTABLE|DEFENSIVE|DEFERRED|VOLATILE)\b"
)


def _fence_block() -> str:
    text = EXEMPTIONS_MD.read_text(encoding="utf-8")
    assert FENCE_BEGIN in text and FENCE_END in text, (
        "EXEMPTIONS.md lost its FENCE markers — exemptions_check.py parses the "
        f"block between {FENCE_BEGIN} and {FENCE_END}; without them the gate "
        "cannot enforce anything."
    )
    return text.split(FENCE_BEGIN, 1)[1].split(FENCE_END, 1)[0]


def _entries() -> list[tuple[str, int, str]]:
    out = []
    for raw in _fence_block().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        m = ENTRY_RE.match(line)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3)))
    return out


# ── enforcement wiring: the check runs, in coverage-c, in audit-gate, in CI ──


def test_exemptions_check_is_invoked_by_coverage_c() -> None:
    """coverage-c must actually run exemptions_check.py, or the contract is
    decorative."""
    mk = MAKEFILE.read_text(encoding="utf-8")
    assert "scripts/exemptions_check.py" in mk, (
        "recovery/Makefile no longer invokes scripts/exemptions_check.py — the "
        "EXEMPTIONS contract is no longer enforced. Re-wire it into coverage-c."
    )
    assert CHECK_PY.is_file(), "recovery/scripts/exemptions_check.py is missing"


def test_audit_gate_runs_coverage_c() -> None:
    """The audit-gate target must depend on coverage-c so the exemptions check
    is part of the gate (not a standalone opt-in)."""
    mk = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(r"^audit-gate:.*?(?=^\S)", mk, re.DOTALL | re.MULTILINE)
    assert m, "no audit-gate target found in recovery/Makefile"
    assert re.search(r"\bcoverage-c\b", m.group(0)), (
        "audit-gate no longer runs coverage-c; the exemptions gate is decoupled "
        "from audit-gate."
    )


def test_audit_gate_wired_into_ci() -> None:
    """audit-gate must run in CI (the only place the C is built/tested), or a
    fresh drift goes unnoticed until someone runs the ~40-min gate by hand.
    (Path filters are pinned by GATE-04 test_workflow_path_filter.)"""
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"make\s+-C\s+recovery\s+audit-gate", wf), (
        ".github/workflows/audit-gate.yml no longer runs `make -C recovery "
        "audit-gate`; the gate is no longer enforced in CI."
    )


# ── fence shape / anti-rot: cheap, offline drift canary ──


def test_fence_is_nonempty_and_well_formed() -> None:
    entries = _entries()
    assert entries, (
        "EXEMPTIONS.md FENCE block parses to zero entries — either it was "
        "emptied or the row format drifted from exemptions_check.ENTRY_RE."
    )
    for f, ln, cat in entries:
        assert cat in CATEGORIES, f"{f}:{ln} has unknown category {cat!r}"


def test_every_exempt_line_exists_in_source() -> None:
    """A pinned uncovered line must still exist in its file. Catches gross rot
    (a source file deleted or shrunk below a pinned line) in the watched suite,
    instead of as a mysterious audit-gate failure weeks later. It does NOT
    catch a line that merely SHIFTED — only the coverage run can — see the ADR
    on making audit-gate a required CI check."""
    linecounts: dict[str, int] = {}
    problems = []
    for f, ln, _cat in _entries():
        if f not in linecounts:
            path = SRC_DIR / f
            if not path.is_file():
                problems.append(f"{f}:{ln} — source file {f} does not exist")
                linecounts[f] = -1
                continue
            linecounts[f] = len(path.read_text(encoding="utf-8").splitlines())
        n = linecounts[f]
        if n >= 0 and ln > n:
            problems.append(f"{f}:{ln} — past EOF (file has {n} lines)")
    assert not problems, (
        "EXEMPTIONS.md references lines that no longer exist:\n  "
        + "\n  ".join(problems)
    )


def test_categories_match_enforcement_script() -> None:
    """The doc's advertised category set must match what exemptions_check.py
    parses, so a category the script silently ignores can't creep into the
    doc (an ignored VOLATILE row would drop its drift exemption)."""
    src = CHECK_PY.read_text(encoding="utf-8")
    m = re.search(r"\((INTRACTABLE\|DEFENSIVE\|DEFERRED\|VOLATILE)\)", src)
    assert m, (
        "could not find the category alternation in exemptions_check.py; if the "
        "categories changed, update this test and EXEMPTIONS.md together."
    )
    script_cats = set(m.group(1).split("|"))
    assert script_cats == CATEGORIES, (
        f"category drift: exemptions_check.py parses {script_cats}, this test "
        f"expects {CATEGORIES}. Keep the doc, the script, and this guard in sync."
    )
