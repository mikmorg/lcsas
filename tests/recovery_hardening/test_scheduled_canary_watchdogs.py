"""Hardening: every scheduled canary self-tickets when it goes red (#420).

Two canaries rotted in parallel for ~5 weeks against a zero-issue backlog:
live-usb-smoke failed every scheduled run in its history (#418) and
ecc-weekly was timeout-cancelled every Monday (#419) -- and bin-parity's
four red Mondays in June (the #408 breakage) were likewise never ticketed.
Scheduled-run failures only email the workflow author; the repo's operating
model ("every open issue is real work") assumes red CI *becomes* an issue,
and for the canary fleet it never did.

The fix is a uniform ``canary-watchdog`` job in every ``schedule:``-triggered
workflow.  These tests pin the load-bearing properties of that pattern so the
NEXT scheduled workflow cannot ship unwatched, and nobody quietly reintroduces
one of the failure modes we just paid for:

1. Every workflow with a ``schedule:`` trigger has a ``canary-watchdog`` job.
2. Its condition covers ``cancelled`` -- a ``timeout-minutes`` kill yields
   conclusion ``cancelled``, for which ``failure()`` is false; ecc-weekly's
   old step-level watchdog showed ``skipped`` on every dead Monday (#419).
3. Its condition contains ``always()`` -- without it, a failed/cancelled
   ``needs`` dependency skips the watchdog, which is the whole event it
   exists to report.
4. It declares ``needs:`` -- with no ``needs``, ``needs.*.result`` is empty
   and the fire condition is vacuously false.
5. It grants itself ``issues: write`` -- the default token may be read-only,
   and a 403 on ticket-filing is this bug all over again one level up.
6. No ``|| true`` inside the job -- ecc-weekly's old step swallowed its own
   errors, so it had never been proven to work.
7. Since #693 one watchdog serves every canary in weekly.yml, so its
   ``needs:`` must list every other job in the file, and its ticket table
   must have a title for each.  A canary left out of ``needs:`` is a
   canary whose red run files nothing.

Text-level / YAML-free parsing on purpose (pattern of
test_workflow_path_filter.py): the suite is stdlib-only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_SCHEDULE_RE = re.compile(r"^\s{2,4}schedule:\s*$", re.M)
_WATCHDOG_JOB_RE = re.compile(r"^  canary-watchdog:\s*$", re.M)


def _scheduled_workflows() -> list[Path]:
    found = [
        p
        for p in sorted(WORKFLOWS_DIR.glob("*.yml"))
        if _SCHEDULE_RE.search(p.read_text(encoding="utf-8"))
    ]
    # The invariant below iterates this list; an empty list would pass
    # every test vacuously.  The repo currently has a whole canary fleet.
    assert found, "no schedule:-triggered workflows found -- glob broken?"
    return found


def _watchdog_block(text: str, name: str) -> str:
    """Return the canary-watchdog job block (to the next 2-space-indented
    sibling job or EOF), with comment lines stripped.

    Stripping matters in both directions: the watchdogs' own comments
    *mention* the ``|| true`` anti-pattern they forbid (a literal-substring
    check would flag its own documentation), and conversely a comment
    saying 'cancelled' must not satisfy the check while the actual ``if:``
    stays blind to it.  Assertions below therefore run against code lines
    only.
    """
    m = _WATCHDOG_JOB_RE.search(text)
    assert m, (
        f"{name} has a schedule: trigger but no canary-watchdog job -- a "
        "scheduled canary that cannot self-ticket rots silently (#418/#419 "
        "sat red for ~5 weeks against a zero-issue backlog).  Add the "
        "uniform watchdog job (see #420)."
    )
    rest = text[m.end():]
    nxt = re.search(r"^  \S", rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    return "\n".join(
        ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
    )


@pytest.mark.parametrize(
    "workflow", _scheduled_workflows(), ids=lambda p: p.name
)
def test_scheduled_workflow_has_sound_watchdog(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    block = _watchdog_block(text, workflow.name)

    assert "cancelled" in block, (
        f"{workflow.name} canary-watchdog does not cover 'cancelled': a "
        "timeout-minutes kill yields conclusion `cancelled`, for which "
        "failure() is false -- exactly how ecc-weekly died unreported for "
        "four straight Mondays (#419)."
    )
    assert "always()" in block, (
        f"{workflow.name} canary-watchdog lacks always(): without it, the "
        "failed/cancelled dependency it exists to report would SKIP it."
    )
    assert re.search(r"^\s+needs:", block, re.M), (
        f"{workflow.name} canary-watchdog has no needs: -- needs.*.result "
        "is then empty and the fire condition is vacuously false, so the "
        "watchdog never fires."
    )
    assert re.search(r"issues:\s*write", block), (
        f"{workflow.name} canary-watchdog does not grant issues: write -- "
        "with a read-only default token, ticket-filing 403s and the "
        "watchdog is silently dead."
    )
    assert "|| true" not in block, (
        f"{workflow.name} canary-watchdog swallows errors with '|| true' -- "
        "if ticket-filing fails the job must fail loud (ecc-weekly's old "
        "step had never been proven to fire)."
    )
    assert "github.event_name == 'schedule'" in block, (
        f"{workflow.name} canary-watchdog is not scoped to schedule events "
        "-- PR/dispatch failures are already visible to whoever triggered "
        "them; unscoped watchdogs file duplicate noise."
    )


_JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)


def _job_ids(text: str) -> list[str]:
    """Top-level job ids: 2-space-indented keys after the `jobs:` line."""
    m = re.search(r"^jobs:\s*$", text, re.M)
    assert m, "no top-level `jobs:` line -- parser broken?"
    return _JOB_RE.findall(text[m.end():])


def _watchdog_needs(block: str) -> set[str]:
    """The watchdog's `needs:`, block form (`- job` lines) or inline list."""
    m = re.search(r"^    needs:[ \t]*(.*)$", block, re.M)
    assert m, "canary-watchdog has no needs: line"
    inline = m.group(1).strip()
    if inline:
        return {n.strip() for n in inline.strip("[]").split(",") if n.strip()}
    found: set[str] = set()
    for line in block[m.end():].splitlines()[1:]:
        item = re.match(r"^      - ([A-Za-z0-9_-]+)\s*$", line)
        if not item:
            break
        found.add(item.group(1))
    return found


@pytest.mark.parametrize(
    "workflow", _scheduled_workflows(), ids=lambda p: p.name
)
def test_watchdog_covers_every_job(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    block = _watchdog_block(text, workflow.name)
    jobs = set(_job_ids(text)) - {"canary-watchdog"}
    assert jobs, f"{workflow.name}: no jobs besides the watchdog -- parser broken?"
    needs = _watchdog_needs(block)
    missing = sorted(jobs - needs)
    assert not missing, (
        f"{workflow.name}: canary-watchdog does not need {missing}, so a "
        "red run of those files no ticket.  Add them to its needs: list."
    )
    unknown = sorted(needs - jobs)
    assert not unknown, (
        f"{workflow.name}: canary-watchdog needs {unknown}, which are not "
        "jobs in this file (GitHub rejects the workflow)."
    )
    untitled = sorted(
        j for j in jobs
        if not re.search(rf"^\s+{re.escape(j)}\)\s*$", block, re.M)
    )
    assert not untitled, (
        f"{workflow.name}: canary-watchdog has no `case` entry (title and "
        f"ticket text) for {untitled}; they would be ticketed under a "
        "generic title."
    )
