"""Hardening: a scheduled workflow file holds a schedule and nothing else (#462).

GitHub disables a workflow file that has a ``schedule:`` trigger after 60
days without repository activity (public repos), and a disabled file loses
EVERY trigger, not just the schedule.  Master was quiet 2026-08-05 ->
10-15, and seven files went ``disabled_inactivity``.  bin-parity.yml took
the REQUIRED ``bin-parity-required`` check down with it, so every PR sat
BLOCKED behind green checks; ecc-weekly's PR-time repair proof was off too.
Nothing reported any of it: a disabled file produces no run, so the #420
watchdogs had nothing to see.

The structural fix is a split.  The gate lives in a file with no schedule
(``pull_request`` / ``push`` / ``workflow_call``), and a separate wrapper
holds the schedule and calls it, so a quiet spell can only switch off a
schedule.  These tests keep it that way:

1. A file with ``schedule:`` has no trigger but ``workflow_dispatch``
   beside it.
2. Every local ``uses: ./.github/workflows/<file>`` names a file that
   exists and declares ``workflow_call:``.  A typo there errors only at
   run time, i.e. next Monday, and the PR that made it runs nothing that
   would notice.
3. test.yml keeps the workflow-state check that turns a switched-off
   wrapper into a red ``test`` on the next push or PR.

Text-level, YAML-free parsing on purpose (pattern of
test_scheduled_canary_watchdogs.py): the suite is stdlib-only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Triggers that may sit beside `schedule:`.  workflow_dispatch is a manual
# run, which a quiet spell has no PR check to take away from.
_ALLOWED_BESIDE_SCHEDULE = {"schedule", "workflow_dispatch"}

_LOCAL_USES_RE = re.compile(
    r"^\s+uses:\s*\./\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)\s*$", re.M
)


def _triggers(path: Path) -> set[str]:
    """The trigger names directly under a workflow's top-level ``on:``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("on:")
    except ValueError:
        pytest.fail(
            f"{path.name}: no block-form top-level `on:` line -- this "
            "parser only reads the block form every workflow here uses."
        )
    found: set[str] = set()
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            break  # back at column 0: the `on:` block is over
        m = re.match(r"^  ([a-z_]+):", line)
        if m:
            found.add(m.group(1))
    assert found, f"{path.name}: `on:` block has no triggers -- parser broken?"
    return found


def _workflows() -> list[Path]:
    found = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert found, f"no workflows under {WORKFLOWS_DIR} -- glob broken?"
    return found


def _scheduled_workflows() -> list[Path]:
    found = [p for p in _workflows() if "schedule" in _triggers(p)]
    # Everything below iterates this list; empty would pass vacuously.
    # The repo has a whole canary fleet.
    assert found, "no schedule:-triggered workflows found -- parser broken?"
    return found


@pytest.mark.parametrize(
    "workflow", _scheduled_workflows(), ids=lambda p: p.name
)
def test_scheduled_workflow_has_only_a_schedule(workflow: Path) -> None:
    extra = sorted(_triggers(workflow) - _ALLOWED_BESIDE_SCHEDULE)
    assert not extra, (
        f"{workflow.name} has a schedule: AND {extra}.  After 60 quiet "
        "days GitHub disables the whole file, and those triggers go "
        "with it -- in 2026-10 that silently took the required "
        "bin-parity-required check offline and BLOCKED every PR (#462).  "
        "Keep the gate in a file with no schedule (add `workflow_call:` "
        "to it) and move the schedule into a wrapper whose only job is "
        "`uses: ./.github/workflows/<gate>.yml` (see bin-parity-weekly.yml)."
    )


def _local_calls() -> list[tuple[Path, str]]:
    calls = [
        (p, m.group(1))
        for p in _workflows()
        for m in _LOCAL_USES_RE.finditer(p.read_text(encoding="utf-8"))
    ]
    # The split makes every PR-triggered canary a called workflow; none
    # found means the regex no longer matches how the wrappers call.
    assert calls, "no `uses: ./.github/workflows/...` calls found -- regex broken?"
    return calls


@pytest.mark.parametrize(
    "caller,target", _local_calls(), ids=lambda v: getattr(v, "name", v)
)
def test_local_workflow_call_resolves(caller: Path, target: str) -> None:
    called = WORKFLOWS_DIR / target
    assert called.is_file(), (
        f"{caller.name} calls ./.github/workflows/{target}, which does not "
        "exist.  The call fails only when the schedule fires."
    )
    assert "workflow_call" in _triggers(called), (
        f"{caller.name} calls {target}, but {target} has no `workflow_call:` "
        "trigger, so the call fails at run time -- next Monday, unseen by "
        "the PR that broke it."
    )


def test_test_workflow_checks_for_switched_off_workflows() -> None:
    text = (WORKFLOWS_DIR / "test.yml").read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "/actions/workflows" in code and "disabled_inactivity" in code, (
        "test.yml lost its workflow-state check.  Without it a wrapper "
        "switched off by a quiet spell stays dark with nothing red anywhere "
        "(#462): no run, so the #420 watchdogs never fire."
    )
    assert re.search(r"^\s+actions:\s*read\s*$", code, re.M), (
        "test.yml's workflow-state check needs `actions: read`; with a "
        "restricted default token the API call 403s."
    )
