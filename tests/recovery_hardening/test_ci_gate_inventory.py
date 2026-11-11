"""Hardening: every documented opt-in gate is run by something (#694, #693).

The project review of 2026-11-11 found gates that CLAUDE.md named but no
workflow and no make target ever ran: ``LCSAS_FAULT_INJECT``,
``LCSAS_PETABYTE`` and the full ``LCSAS_DIFF`` profiles self-skip unless
their variable is set, and nothing set it.  A gate that never runs gives
no protection, and the docs claimed more than was true.

1. Every ``LCSAS_*`` variable CLAUDE.md names is SET (``VAR=1`` in a
   command, or ``VAR: "1"`` in a workflow ``env:``) by some workflow, the
   install-rustic action, or a Makefile -- or is declared below as run by
   an equivalent command, or as not a gate at all.
2. The pinned rustic install exists once, in .github/actions/install-rustic
   (#693): it was pasted into five jobs, and a bump had to find them all.

Text-level parsing, stdlib only (pattern of test_ci_workflow_parity.py).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
GITHUB = REPO_ROOT / ".github"
RUSTIC_ACTION = GITHUB / "actions" / "install-rustic" / "action.yml"

# Variables CLAUDE.md names that are not opt-in gates, with the reason.
NOT_A_GATE: dict[str, str] = {
    # A cost acknowledgement the blind-restore make targets CHECK for; a
    # person sets it on purpose, nothing should set it for them.
    "LCSAS_BLIND_ACK_COST": "cost acknowledgement, set by a person",
}

# Gates whose test wrapper is not run under its variable, because CI runs
# the same make target directly.  Value: (file, regex that must match it).
EQUIVALENCE: dict[str, tuple[Path, str]] = {
    # test_tier1_coverage_baseline.py / test_tier1_sanitize.py only wrap
    # `make -C recovery coverage-c` / `sanitize`; audit-gate.yml runs both
    # through `make -C recovery audit-gate` on every gate-relevant PR.
    "LCSAS_COVERAGE": (
        GITHUB / "workflows" / "audit-gate.yml",
        r"make\s+-C\s+recovery\s+audit-gate",
    ),
    "LCSAS_SANITIZE": (
        GITHUB / "workflows" / "audit-gate.yml",
        r"make\s+-C\s+recovery\s+audit-gate",
    ),
}


def _code(path: Path) -> str:
    """File text without full-line comments (`#`), so a mention in a
    comment never counts as running a gate."""
    return "\n".join(
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


def _setters() -> list[Path]:
    files = sorted((GITHUB / "workflows").glob("*.yml"))
    files += sorted((GITHUB / "actions").glob("*/action.yml"))
    files += [REPO_ROOT / "Makefile", REPO_ROOT / "recovery" / "Makefile"]
    assert len(files) > 5, "no workflows found -- glob broken?"
    return files


def _named_in_claude_md() -> list[str]:
    found = sorted(set(re.findall(r"\bLCSAS_[A-Z0-9_]+\b", CLAUDE_MD.read_text())))
    # CLAUDE.md names the ECC, fault-injection and qemu gates at least.
    assert len(found) >= 3, "CLAUDE.md names no LCSAS_* variables -- regex broken?"
    return found


def _is_set(var: str, text: str) -> bool:
    # `VAR=1 cmd` / `export VAR=1` / `VAR: "1"` (YAML env).  Not `$VAR`,
    # not `VAR != 1` (a check is not a setter).
    return re.search(
        rf"(?<![$\w]){re.escape(var)}(=|:[ \t]+)[\"']?[^\s\"'$]", text
    ) is not None


def test_every_opt_in_variable_in_claude_md_is_set_somewhere() -> None:
    texts = {p: _code(p) for p in _setters()}
    unset: list[str] = []
    for var in _named_in_claude_md():
        if var in NOT_A_GATE:
            continue
        if var in EQUIVALENCE:
            path, pattern = EQUIVALENCE[var]
            assert re.search(pattern, _code(path)), (
                f"{var} is declared as run by {path.name} ({pattern!r}), "
                "but that command is gone from it."
            )
            continue
        if not any(_is_set(var, t) for t in texts.values()):
            unset.append(var)
    assert not unset, (
        f"CLAUDE.md names {unset}, but no workflow, action or Makefile sets "
        "them, so the gates behind them never run (#694).  Run the gate from "
        "a workflow (weekly.yml calls opt-in-gates.yml) or a make target, or "
        "take it out of CLAUDE.md."
    )


def test_declared_exceptions_are_still_named_in_claude_md() -> None:
    named = set(_named_in_claude_md())
    stale = sorted((set(NOT_A_GATE) | set(EQUIVALENCE)) - named)
    assert not stale, (
        f"{stale} are declared exceptions here but CLAUDE.md no longer names "
        "them; drop them so the list stays honest."
    )


def test_rustic_install_lives_only_in_the_action() -> None:
    assert RUSTIC_ACTION.is_file(), f"missing {RUSTIC_ACTION}"
    assert "releases/download/v${RUSTIC_VERSION}" in RUSTIC_ACTION.read_text()
    pasted = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in GITHUB.rglob("*.yml")
        if p != RUSTIC_ACTION
        and "rustic-rs/rustic/releases/download" in p.read_text(encoding="utf-8")
    )
    assert not pasted, (
        f"{pasted} download a pinned rustic themselves.  Use "
        "`uses: ./.github/actions/install-rustic` (after actions/checkout) "
        "so the pin lives in one place (#693)."
    )
    users = [
        p.name
        for p in (GITHUB / "workflows").glob("*.yml")
        if "uses: ./.github/actions/install-rustic" in p.read_text(encoding="utf-8")
    ]
    # test.yml, windows-e2e, macos-e2e, live-usb-smoke, opt-in-gates.
    assert len(users) >= 5, f"only {users} use the install-rustic action"
