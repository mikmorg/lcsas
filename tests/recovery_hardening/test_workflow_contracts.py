"""Hardening: every CI pytest run has a passed-test floor, and triggers stay sane (#831, #837).

1. Skip floors (#831).  Most tests self-skip when a tool is missing, so a
   job whose runner lacks the tool goes green with nothing run.  Four
   weekly canaries (ecc-repair, format-canary, live-usb-smoke,
   macos-tier1) did exactly that: no floor.  Rule: every workflow step
   that runs pytest -- ``pytest``, ``python -m pytest``, or ``make
   <target>`` whose recipe runs pytest -- writes ``--junitxml=<path>``,
   and the same job then runs ``scripts/ci_min_passed.py <path>
   --min-passed N`` with N >= 1.  The few steps that prove "it ran" some
   other way are declared in ALTERNATIVE_FLOORS, with the reason.
2. Skip reasons (#831).  pyproject's addopts carry ``-r`` with ``s``, so
   every log -- CI, ``make gate`` and the release gate, which calls
   pytest directly -- records why each test skipped.
3. Triggers (#837).  A ``push:`` trigger names ``branches: [master]``
   and no tags: a tag push starts no workflow (paths filters are ignored
   for tags), and a PR commit is tested once, by ``pull_request``, not
   again by a branch push.  test.yml keeps both triggers.
4. Tier-3 path filters (#837).  Each PR-filtered workflow that exercises
   the tier-3 reader starts on a change under ``src/lcsas/restore/``.

Text-level, YAML-free parsing on purpose (pattern of
test_ci_gate_inventory.py): the suite is stdlib-only.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# (workflow, job, text in the step) -> (regex the step or Makefile must
# match, why this counts as a floor).  Keep it short: a junit floor is
# the default.
ALTERNATIVE_FLOORS: dict[tuple[str, str, str], tuple[str, str]] = {
    ("test.yml", "test", "make test-e2e"): (
        r"grep -Eq '[^']*test_e2e_pipeline PASSED'",
        "GATE-10: the step greps the log for the pipeline test PASSED; "
        "the other e2e tests need CDEmu/root and skip on every runner.",
    ),
    ("test.yml", "recovery-hardening", "make shell-coverage"): (
        r"cov_shell\.py\s+\\?\s*--threshold\s+\d+",
        "GATE-09: the Makefile recipe fails below a restore.sh "
        "line-coverage threshold, which skipped tests cannot reach.",
    ),
}

# Workflows that exercise the tier-3 reader (src/lcsas/restore/) and are
# path-filtered on pull_request.  test.yml is unfiltered.
TIER3_PR_WORKFLOWS = {
    "macos-e2e.yml",       # the meta disc it restores from carries tier 3
    "windows-e2e.yml",     # ditto
    "opt-in-gates.yml",    # tier3-qemu: the tier-3 zstd decoder on aarch64
    "format-canary.yml",   # latest rustic vs the tier-3 PurePythonRestorer
}
TIER3_GLOB = "src/lcsas/restore/**"


@dataclass
class Step:
    index: int
    text: str  # comment lines removed


@dataclass
class Job:
    workflow: str
    name: str
    steps: list[Step]


def _workflows() -> list[Path]:
    found = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert len(found) > 5, f"no workflows under {WORKFLOWS_DIR} -- glob broken?"
    return found


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _code_lines(text: str) -> list[str]:
    return [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _jobs(name: str, text: str) -> list[Job]:
    """Split a workflow into jobs and steps by indentation."""
    lines = _code_lines(text)
    try:
        start = lines.index("jobs:")
    except ValueError:
        return []
    jobs: list[Job] = []
    current: list[str] = []
    job_name: str | None = None

    def flush() -> None:
        if job_name is not None:
            jobs.append(Job(name, job_name, _steps(current)))

    for line in lines[start + 1:]:
        if _indent(line) == 0:
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            flush()
            job_name, current = m.group(1), []
        else:
            current.append(line)
    flush()
    return jobs


def _steps(job_lines: list[str]) -> list[Step]:
    steps_at = next(
        (i for i, ln in enumerate(job_lines) if ln.strip() == "steps:"), None
    )
    if steps_at is None:
        return []  # a `uses:` job that calls another workflow
    base = _indent(job_lines[steps_at])
    out: list[list[str]] = []
    item_indent: int | None = None
    for line in job_lines[steps_at + 1:]:
        ind = _indent(line)
        if ind <= base:
            break
        if line.lstrip().startswith("- ") and (item_indent is None or ind == item_indent):
            item_indent = ind
            out.append([line])
        elif out:
            out[-1].append(line)
    return [Step(i, "\n".join(s)) for i, s in enumerate(out)]


def _make_pytest_targets() -> set[str]:
    """Root Makefile targets whose own recipe runs pytest."""
    targets: set[str] = set()
    current: str | None = None
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:(?!=)", line)
        if m:
            current = m.group(1)
            continue
        if current and line.startswith("\t") and re.search(r"\bpytest\b", line):
            targets.add(current)
    assert {"test-unit", "test-integration", "test-e2e"} <= targets, (
        f"Makefile pytest targets parsed as {sorted(targets)} -- parser broken?"
    )
    return targets


_PYTEST_CMD = re.compile(r"(?:^|[\s;&|(])(?:python3?\s+-m\s+)?pytest(?=\s|$)")


def _runs_pytest(step: Step, make_targets: set[str]) -> str | None:
    """The pytest command a step runs (for messages), or None."""
    for line in step.text.splitlines():
        if "pip install" in line or "--collect-only" in line:
            continue
        if _PYTEST_CMD.search(line):
            return line.strip()
        # `make <target>`; `make -C <dir>` drives another Makefile, whose
        # pytest runs feed coverage thresholds (audit-gate), not counts.
        for m in re.finditer(r"\bmake\s+([A-Za-z0-9_-]+)", line):
            if m.group(1) in make_targets:
                return line.strip()
    return None


def _env_ints(text: str) -> dict[str, int]:
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^\s+([A-Z_][A-Z0-9_]*):\s*\"?(\d+)\"?\s*$", text, re.M)
    }


def _floor_problems(path: Path, text: str, make_targets: set[str]) -> list[str]:
    problems: list[str] = []
    env = _env_ints(text)
    for job in _jobs(path.name, text):
        for step in job.steps:
            cmd = _runs_pytest(step, make_targets)
            if cmd is None:
                continue
            where = f"{path.name} job {job.name!r} step {step.index} ({cmd!r})"
            alt = next(
                (v for (wf, jb, needle), v in ALTERNATIVE_FLOORS.items()
                 if wf == path.name and jb == job.name and needle in step.text),
                None,
            )
            if alt is not None:
                if not (re.search(alt[0], step.text)
                        or re.search(alt[0], MAKEFILE.read_text(encoding="utf-8"))):
                    problems.append(f"{where}: declared floor {alt[0]!r} is gone")
                continue
            junit = re.search(r"--junitxml[= ]\"?([^\s\"]+)\"?", step.text)
            if not junit:
                problems.append(f"{where}: runs pytest without --junitxml")
                continue
            jpath = re.escape(junit.group(1))
            ok = False
            for later in job.steps[step.index:]:
                m = re.search(
                    rf"ci_min_passed\.py\s+\"?{jpath}\"?\s+--min-passed\s+\"?\$?(\w+)\"?",
                    later.text,
                )
                if not m:
                    continue
                val = m.group(1)
                n = int(val) if val.isdigit() else env.get(val, 0)
                ok = n >= 1
                break
            if not ok:
                problems.append(
                    f"{where}: no `scripts/ci_min_passed.py {junit.group(1)} "
                    "--min-passed N` (N >= 1) later in the job"
                )
    return problems


def _all_jobs() -> list[Job]:
    jobs = [j for p in _workflows() for j in _jobs(p.name, p.read_text(encoding="utf-8"))]
    assert len(jobs) > 10, f"parsed only {len(jobs)} jobs -- parser broken?"
    return jobs


def test_every_pytest_step_has_a_passed_floor() -> None:
    targets = _make_pytest_targets()
    found = [
        (j.workflow, j.name)
        for j in _all_jobs() for s in j.steps if _runs_pytest(s, targets)
    ]
    # test, recovery-hardening, ecc-repair, format-canary, live-usb,
    # macos-tier1, upstream-canary and the three opt-in jobs at least.
    assert len(set(found)) >= 10, f"only found pytest in {sorted(set(found))}"
    problems = [
        msg for p in _workflows()
        for msg in _floor_problems(p, p.read_text(encoding="utf-8"), targets)
    ]
    assert not problems, (
        "These CI steps run pytest with no passed-test floor, so they go "
        "green when every test self-skips (#831).  Add --junitxml=<file> and "
        "a later `python3 scripts/ci_min_passed.py <file> --min-passed N`, "
        "with N the count a good run passes:\n  " + "\n  ".join(problems)
    )


def test_floor_check_catches_a_missing_floor(tmp_path: Path) -> None:
    """The detector itself: a bare pytest step must be reported."""
    targets = _make_pytest_targets()
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "on:\n  workflow_dispatch: {}\n\njobs:\n  canary:\n"
        "    runs-on: ubuntu-24.04\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Run\n        run: python -m pytest tests/x.py -v\n"
        "  other:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - name: Run\n        run: make test-unit\n"
        "      - name: Floor of zero\n"
        "        run: python3 scripts/ci_min_passed.py /tmp/u.xml --min-passed 0\n"
    )
    problems = _floor_problems(bad, bad.read_text(), targets)
    assert len(problems) == 2, problems
    good = tmp_path / "good.yml"
    good.write_text(
        "on:\n  workflow_dispatch: {}\n\njobs:\n  canary:\n"
        "    runs-on: ubuntu-24.04\n    env:\n      FLOOR: \"3\"\n    steps:\n"
        "      - name: Run\n        env:\n"
        "          PYTEST_ADDOPTS: --junitxml=/tmp/c.xml\n"
        "        run: make test-unit\n"
        "      - name: Floor\n"
        "        run: python3 scripts/ci_min_passed.py /tmp/c.xml --min-passed \"$FLOOR\"\n"
    )
    assert _floor_problems(good, good.read_text(), targets) == []


def test_pytest_addopts_record_skip_reasons() -> None:
    """#831: the gate logs (CI, `make gate`, the release gate, which runs
    pytest directly) must say why each test skipped."""
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    addopts = cfg["tool"]["pytest"]["ini_options"]["addopts"]
    r = re.search(r"(?:^|\s)-r(\S+)", addopts)
    assert r and "s" in r.group(1), (
        f"pyproject addopts {addopts!r} must carry -r with `s` (e.g. -rfEs)"
    )
    # A later `-r<chars>` on a command line replaces the addopts value, so
    # a workflow that passes its own -r must keep `s` in it.
    for p in _workflows():
        for m in re.finditer(r"pytest\b[^\n]*?\s-r([A-Za-z]+)", p.read_text(encoding="utf-8")):
            assert "s" in m.group(1), (
                f"{p.name}: `-r{m.group(1)}` replaces addopts' -r and drops "
                "skip reasons; add `s`."
            )


# --------------------------------------------------------------------- #
# Triggers (#837)
# --------------------------------------------------------------------- #


def _on_block(text: str) -> dict[str, list[str]]:
    """{trigger: its indented lines} for the block-form top-level `on:`."""
    lines = _code_lines(text)
    start = lines.index("on:")
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1:]:
        if _indent(line) == 0:
            break
        m = re.match(r"^  ([a-z_]+):(.*)$", line)
        if m:
            current = m.group(1)
            out[current] = []
            continue
        if current:
            out[current].append(line)
    return out


def _list_under(block: list[str], key: str) -> list[str] | None:
    """Values of `key:` inside a trigger block (inline or dash list)."""
    for i, line in enumerate(block):
        m = re.match(rf"^\s+{key}:\s*(.*)$", line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline.startswith("["):
            return [v.strip().strip("'\"") for v in inline.strip("[]").split(",") if v.strip()]
        vals = []
        for nxt in block[i + 1:]:
            if _indent(nxt) <= _indent(line):
                break
            item = re.match(r"^\s+-\s*['\"]?([^'\"]+?)['\"]?\s*$", nxt)
            if item:
                vals.append(item.group(1))
        return vals
    return None


def test_push_triggers_are_master_only() -> None:
    pushers: list[str] = []
    for p in _workflows():
        on = _on_block(p.read_text(encoding="utf-8"))
        if "push" not in on:
            continue
        pushers.append(p.name)
        block = on["push"]
        assert _list_under(block, "tags") is None, f"{p.name}: push has tags:"
        assert _list_under(block, "branches-ignore") is None, (
            f"{p.name}: push uses branches-ignore; name branches: [master]"
        )
        assert _list_under(block, "branches") == ["master"], (
            f"{p.name}: push must name `branches: [master]` (#837).  Without "
            "it a tag push starts the workflow (paths filters do not apply "
            "to tags), and a PR branch push runs it a second time beside "
            "pull_request."
        )
    assert "test.yml" in pushers, "test.yml must keep its push trigger (master)"


def test_test_workflow_runs_once_per_pr_commit_and_on_master() -> None:
    on = _on_block((WORKFLOWS_DIR / "test.yml").read_text(encoding="utf-8"))
    assert "pull_request" in on, "test.yml lost pull_request: no required `test` on PRs"
    assert _list_under(on["pull_request"], "paths") is None, (
        "test.yml's pull_request must stay unfiltered: `test` is a required check"
    )
    assert _list_under(on["push"], "branches") == ["master"]


def test_tier3_workflows_start_on_restore_changes() -> None:
    missing: list[str] = []
    for p in _workflows():
        on = _on_block(p.read_text(encoding="utf-8"))
        paths = _list_under(on.get("pull_request", []), "paths")
        if p.name in TIER3_PR_WORKFLOWS:
            assert paths is not None, f"{p.name}: no pull_request paths filter"
        if paths is None:
            continue
        # A workflow that builds a meta disc bundles the tier-3 reader too.
        if (p.name in TIER3_PR_WORKFLOWS or "src/lcsas/meta/**" in paths) \
                and TIER3_GLOB not in paths:
            missing.append(p.name)
    assert not missing, (
        f"{missing} exercise the tier-3 reader but a change under "
        f"src/lcsas/restore/ does not start them (#837).  Add {TIER3_GLOB!r} "
        "to their pull_request paths."
    )
