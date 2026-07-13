"""Hardening: audit-gate path-filter has no ghost entries and no holes (GATE-04).

audit-gate is the only CI that compiles or tests any C.  It triggers on a
`paths:` filter; the old hand-picked file list silently excluded vendored C,
the keyshare/iso9660/init/ecc sources, and every recovery/scripts file, and
carried a ghost `sanitize.sh` entry that never existed — so a behavior-breaking
edit to the vendored zstd decoder or a heir-facing combiner merged unbuilt and
untested.

These tests keep the broadened filter honest:

1. Every non-glob entry must name a path that exists — kills future ghosts.
2. Every directory directly under recovery/src/ and recovery/vendored/ must be
   matched by at least one filter glob — so adding recovery/src/lcsas-newtool/
   without gate coverage fails the suite.
3. push.paths and the `changes` job's in-job guard list must be identical —
   they must not drift apart.  (Since the #414 required-check shape the
   pull_request trigger is deliberately UNFILTERED: the relevance decision
   lives in the `changes` job so the always-reporting `audit-gate-required`
   job can be a required status check without blocking unrelated PRs on a
   check that never reports.)

Text-level / YAML-free parsing on purpose: the suite is stdlib-only and runs
with no external tools.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "audit-gate.yml"

# Directories whose every immediate child directory must be covered by the
# filter: these hold C source compiled into heir-facing binaries.
COVERED_PARENTS = (
    REPO_ROOT / "recovery" / "src",
    REPO_ROOT / "recovery" / "vendored",
)


def _path_lists() -> dict[str, list[str]]:
    """Extract the `paths:` list under each top-level `on:` trigger.

    Returns a mapping {trigger_name: [glob, ...]} for `push` and
    `pull_request`.  Parses the indentation-structured block directly so the
    suite needs no YAML dependency.
    """
    text = WORKFLOW.read_text()
    lists: dict[str, list[str]] = {}
    current: str | None = None
    in_paths = False
    paths_indent = -1
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r"^(push|pull_request):\s*$", stripped)
        if m and indent <= 2:
            current = m.group(1)
            in_paths = False
            continue
        if current is not None and stripped == "paths:":
            in_paths = True
            paths_indent = indent
            lists.setdefault(current, [])
            continue
        if in_paths:
            item = re.match(r'^-\s*"?([^"]+?)"?\s*$', stripped)
            if item and indent > paths_indent:
                lists[current].append(item.group(1))  # type: ignore[index]
            else:
                # Dedent or non-list line ends this paths block.
                in_paths = False
    return lists


def _guard_list() -> list[str]:
    """Extract the `changes` job's heredoc guard list (gate-paths.txt).

    The pull_request trigger is unfiltered (#414), so PR-side relevance is
    decided by this in-job list; it is the thing that must stay in lockstep
    with push.paths.
    """
    text = WORKFLOW.read_text()
    m = re.search(
        r"cat > /tmp/gate-paths\.txt <<'EOF'\n(.*?)\n\s*EOF",
        text,
        re.DOTALL,
    )
    assert m, (
        "audit-gate.yml `changes` job lost its gate-paths.txt heredoc — the "
        "PR-side relevance guard is gone, so audit-gate-required would pass "
        "on every PR regardless of what changed."
    )
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]


def test_push_and_changes_guard_paths_identical() -> None:
    lists = _path_lists()
    assert "push" in lists, (
        f"audit-gate.yml must define push.paths; found {sorted(lists)}"
    )
    assert "pull_request" not in lists, (
        "audit-gate.yml pull_request trigger must stay UNFILTERED (#414): a "
        "paths: filter there makes the required `audit-gate-required` check "
        "never report on unrelated PRs, blocking them forever on 'Expected'. "
        "PR relevance belongs in the `changes` job guard list."
    )
    assert lists["push"] == _guard_list(), (
        "audit-gate push.paths and the `changes` job guard list have drifted "
        f"apart — push={lists['push']} guard={_guard_list()}. A change must "
        "trigger the gate identically on push and on PR."
    )


def test_required_wrapper_job_present() -> None:
    """The required status-check context must keep existing: branch
    protection requires `audit-gate-required`, and renaming/deleting the job
    would silently turn the required check into an always-blocking or
    never-reported context."""
    text = WORKFLOW.read_text()
    assert re.search(r"^  audit-gate-required:", text, re.MULTILINE), (
        "audit-gate.yml lost the `audit-gate-required` job — branch "
        "protection requires that exact context."
    )
    assert re.search(r"^  changes:", text, re.MULTILINE), (
        "audit-gate.yml lost the `changes` relevance job."
    )


def test_no_ghost_filter_entries() -> None:
    """Every concrete (non-glob) filter entry must point at a real path.

    A `sanitize.sh`-style ghost means the list has drifted; fail loud.
    """
    lists = _path_lists()
    missing: list[str] = []
    for entry in lists.get("push", []):
        if "*" in entry:
            continue
        if not (REPO_ROOT / entry).exists():
            missing.append(entry)
    assert not missing, (
        f"audit-gate.yml filter names nonexistent path(s): {missing}. "
        "Remove the ghost entry or fix the path — a dead filter entry hides "
        "that the list has drifted."
    )


def _glob_covers_dir(globs: list[str], dir_rel: str) -> bool:
    """True if any filter glob matches paths under dir_rel.

    A `recovery/src/**` glob covers `recovery/src/lcsas-foo` and everything
    beneath it.  Match the directory itself and a representative child path.
    """
    probes = (dir_rel, f"{dir_rel}/x", f"{dir_rel}/x.c")
    for g in globs:
        for probe in probes:
            if fnmatch.fnmatch(probe, g):
                return True
    return False


def test_every_recovery_source_dir_is_filter_covered() -> None:
    """Each subdir of recovery/src and recovery/vendored must be filter-matched.

    Adding recovery/src/lcsas-newtool/ without a covering glob — i.e. shipping
    a new heir-facing binary that audit-gate never builds — must fail here.
    """
    globs = _path_lists().get("push", [])
    assert globs, "audit-gate.yml push.paths is empty"
    uncovered: list[str] = []
    for parent in COVERED_PARENTS:
        if not parent.is_dir():
            continue
        for child in sorted(p for p in parent.iterdir() if p.is_dir()):
            rel = child.relative_to(REPO_ROOT).as_posix()
            if not _glob_covers_dir(globs, rel):
                uncovered.append(rel)
    assert not uncovered, (
        f"audit-gate filter does not cover {uncovered}. Add a glob (e.g. "
        "'recovery/src/**') so a change there triggers the C build + tests."
    )
