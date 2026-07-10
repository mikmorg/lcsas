"""Hardening test (GATE-08): the bin-parity exemption ledger must stay honest.

``recovery/BIN_PARITY_EXEMPT`` lists committed binaries that bin-parity
clean-rebuilds but does NOT byte-compare, because the current zig toolchain
produces non-reproducible Mach-O/PE output.  An exemption is a hole in the
gate, so this test (following the GATE-06 XFAIL-ledger pattern) keeps it
disciplined:

  * every exempt entry MUST carry an ``issue=#N`` reference;
  * every exempt entry MUST name a target bin-parity actually knows about
    (a dead exemption silently protects nothing);
  * the macOS + Windows targets that are non-reproducible under the pinned
    toolchain MUST be present (so dropping a line is a conscious, reviewed
    promotion, not an accident);
  * ``recovery/TOOLCHAIN`` must exist and pin a concrete ziglang version;
  * the bin-parity Makefile target + workflow must reference the ledger and
    the toolchain pin, so the gate can't be quietly decoupled from them.

Pure file parsing: always-on, no external tools, no LLM.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
EXEMPT = RECOVERY / "BIN_PARITY_EXEMPT"
TOOLCHAIN = RECOVERY / "TOOLCHAIN"
PARITY_PY = RECOVERY / "scripts" / "bin_parity.py"
MAKEFILE = RECOVERY / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bin-parity.yml"

_ISSUE_RE = re.compile(r"\bissue=#(\d+)\b")
_LINE_RE = re.compile(r"^(?P<path>\S+)\s+issue=#\d+\b")

# The targets that are non-reproducible under the pinned toolchain and so must
# remain exempt until the toolchain produces deterministic Mach-O/PE output.
# EMPTY since the #320 promotion: link-time stripping (-Wl,-S Mach-O,
# -Wl,--strip-debug PE) made both former exemption classes byte-reproducible,
# so every committed target is byte-gated. If a toolchain bump regresses a
# flavor, re-add its targets here AND to BIN_PARITY_EXEMPT with a fresh issue.
EXPECTED_EXEMPT: set[str] = set()


def _entries() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(EXEMPT.read_text().splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append((lineno, raw.strip()))
    return out


def test_exempt_file_exists() -> None:
    assert EXEMPT.is_file(), f"bin-parity exemption ledger missing: {EXEMPT}"


def test_every_entry_has_issue_and_known_target() -> None:
    """Each exempt entry must name a tracking issue and a real committed
    target (the same target set bin_parity.py rebuilds)."""
    # Import the target matrix from the gate itself so the two never drift.
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("bin_parity", PARITY_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module via sys.modules.
    sys.modules["bin_parity"] = mod
    spec.loader.exec_module(mod)
    known = {f"{t.arch}/{t.exe}" for t in mod.TARGETS}

    problems: list[str] = []
    for lineno, raw in _entries():
        m = _LINE_RE.match(raw)
        if not m:
            problems.append(
                f"line {lineno}: unparseable (need '<path> issue=#N ...'): "
                f"{raw!r}"
            )
            continue
        if not _ISSUE_RE.search(raw):
            problems.append(f"line {lineno}: no issue=#N reference")
        path = m.group("path")
        if path not in known:
            problems.append(
                f"line {lineno}: '{path}' is not a committed bin-parity "
                f"target (dead exemption); known: {sorted(known)}"
            )
    assert not problems, "BIN_PARITY_EXEMPT problems:\n  " + "\n  ".join(problems)


def test_nondeterministic_targets_are_tracked() -> None:
    """The macOS/Windows targets that are non-reproducible under the pinned
    toolchain must stay in the ledger (each against an issue) until a
    deterministic toolchain lets them be promoted in a reviewed diff."""
    paths = {_LINE_RE.match(raw).group("path") for _, raw in _entries()
             if _LINE_RE.match(raw)}
    missing = EXPECTED_EXEMPT - paths
    assert not missing, (
        "these non-reproducible targets dropped out of BIN_PARITY_EXEMPT "
        f"without a promotion: {sorted(missing)} -- if their toolchain is now "
        "byte-reproducible, also remove them from EXPECTED_EXEMPT here"
    )


def test_toolchain_pin_present() -> None:
    """recovery/TOOLCHAIN must pin a concrete ziglang version."""
    assert TOOLCHAIN.is_file(), f"toolchain pin missing: {TOOLCHAIN}"
    text = TOOLCHAIN.read_text().strip()
    assert re.fullmatch(r"ziglang==\d+\.\d+\.\d+", text), (
        f"recovery/TOOLCHAIN must pin an exact ziglang version "
        f"('ziglang==X.Y.Z'); got: {text!r}"
    )


def test_makefile_wires_the_gate() -> None:
    """The bin-parity target must invoke the script and depend on the
    toolchain check, so the gate stays bound to the pin."""
    mk = MAKEFILE.read_text()
    assert "bin-parity:" in mk, "no bin-parity target in recovery/Makefile"
    assert "scripts/bin_parity.py" in mk, (
        "bin-parity target no longer invokes scripts/bin_parity.py"
    )
    assert "check-toolchain" in mk, (
        "bin-parity no longer depends on the check-toolchain warning"
    )


def test_workflow_installs_pinned_toolchain_and_runs_gate() -> None:
    """The CI workflow must install the pinned toolchain and run the gate."""
    wf = WORKFLOW.read_text()
    assert "recovery/TOOLCHAIN" in wf, (
        "bin-parity.yml does not install from recovery/TOOLCHAIN"
    )
    assert "make -C recovery bin-parity" in wf, (
        "bin-parity.yml does not run the bin-parity gate"
    )
