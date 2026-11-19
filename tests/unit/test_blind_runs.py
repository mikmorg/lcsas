"""Blind-restore run folders and scoring (#830, #840).

run_variant.sh used to score "the newest /tmp/lcsas-blind-* folder", so a run
could be scored on another run's result, and a 0/0 score (verify.sh refusing
to score) compared equal and passed.  These tests drive the real
run_variant.sh, with stub setup/run/verify scripts beside it, over synthetic
run folders: no sudo, no fixture, no agent, no API spend.  They also pin
blind_runs.py's pruning of kept folders and its base folder.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"

_spec = importlib.util.spec_from_file_location(
    "blind_runs", HARNESS / "blind_runs.py",
)
assert _spec is not None and _spec.loader is not None
blind_runs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(blind_runs)

EXPECTED = blind_runs.EXPECTED_CHECKS

_RESULT_EVENT = {
    "type": "result", "subtype": "success", "num_turns": 31,
    "total_cost_usd": 0.25, "result": "RESTORE COMPLETE",
}


def _transcript_lines() -> list[str]:
    # A tool result that QUOTES the result keys must not be read as the
    # run's own figures.
    quoted = {
        "type": "user", "timestamp": "2026-01-01T00:00:01.000Z",
        "message": {"content": [{
            "type": "tool_result",
            "content": '{"num_turns":999,"total_cost_usd":9.99}',
        }]},
    }
    tool = {
        "type": "assistant", "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]},
    }
    return [json.dumps(tool), json.dumps(quoted), json.dumps(_RESULT_EVENT)]


# --- verify.sh's check count -------------------------------------------------


def test_expected_checks_matches_verify_sh() -> None:
    """EXPECTED_BY_KIND is verify.sh's number of `check` calls of each
    group (#827).  Adding, removing or moving a check in verify.sh must
    move these constants with it."""
    text = (HARNESS / "verify.sh").read_text()
    calls = re.findall(r'^check (\S+) "', text, flags=re.MULTILINE)
    assert len(re.findall(r"^check ", text, flags=re.MULTILINE)) == len(calls)
    by_kind = {kind: calls.count(kind) for kind in set(calls)}
    assert by_kind == blind_runs.EXPECTED_BY_KIND == {"OUTCOME": 9, "CONDUCT": 7}
    assert len(calls) == EXPECTED == 16


# --- run_stats ----------------------------------------------------------------


def test_run_stats_reads_the_result_event_not_quoted_text(tmp_path: Path) -> None:
    t = tmp_path / "transcript.jsonl"
    t.write_text("\n".join(_transcript_lines()) + "\n")
    stats = blind_runs.run_stats(t)
    assert stats["turns"] == 31
    assert stats["cost_usd"] == 0.25
    assert stats["tool_calls"] == 1
    assert stats["agent_result"] == "success"


# --- run_variant.sh end to end, with stubs -------------------------------------

_STUB_RUN = """#!/usr/bin/env bash
# Stub run.sh: writes the run folder the way STUB_RUN says.
set -eu
case "${STUB_RUN:-ok}" in
    ok) cp "$STUB_TRANSCRIPT" "$RUN_DIR/transcript.jsonl" ;;
    nothing) : ;;
    rmdir) rmdir "$RUN_DIR" ;;
esac
"""

_STUB_VERIFY = """#!/usr/bin/env bash
# Stub verify.sh: STUB_PASS PASS lines, STUB_FAIL FAIL lines, exit STUB_RC.
# The first STUB_OUTCOME (default 9) PASS lines are OUTCOME checks, the
# rest CONDUCT; FAIL lines are STUB_FAIL_KIND (default CONDUCT).  STUB_KIND
# set to "none" prints no group, as verify.sh did before #827.
echo "verify called on: $1" >&2
echo "$1" >> "$STUB_LOG"
tag() { [ "${STUB_KIND:-}" = none ] || printf '[%s] ' "$1"; }
i=0; while [ "$i" -lt "${STUB_PASS:-0}" ]; do
    k=OUTCOME; [ "$i" -lt "${STUB_OUTCOME:-9}" ] || k=CONDUCT
    echo "PASS  $(tag $k)check $i"; i=$((i+1)); done
i=0; while [ "$i" -lt "${STUB_FAIL:-0}" ]; do
    echo "FAIL  $(tag "${STUB_FAIL_KIND:-CONDUCT}")check f$i"
    echo "        why check f$i failed"; i=$((i+1)); done
exit "${STUB_RC:-0}"
"""


@pytest.fixture
def harness(tmp_path: Path) -> dict[str, Path]:
    here = tmp_path / "harness"
    here.mkdir()
    shutil.copy2(HARNESS / "run_variant.sh", here / "run_variant.sh")
    shutil.copy2(HARNESS / "blind_runs.py", here / "blind_runs.py")
    shutil.copy2(HARNESS / "transcript.py", here / "transcript.py")  # blind_runs reads with it
    (here / "XFAIL.list").write_text("# ledger\nno-catalog issue=#1 expires=2099-01-01\n")
    for name, body in (
        ("setup.py", "#!/bin/sh\nexit 0\n"),
        ("run.sh", _STUB_RUN),
        ("verify.sh", _STUB_VERIFY),
    ):
        p = here / name
        p.write_text(body)
        p.chmod(0o755)
    base = tmp_path / "runs"
    base.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(_transcript_lines()) + "\n")
    return {"here": here, "base": base, "transcript": transcript,
            "log": tmp_path / "verify-calls.log"}


def _run_variant(h: dict[str, Path], variant: str = "default", **env: str):
    full = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "LCSAS_BLIND_TMPDIR": str(h["base"]),
        "STUB_TRANSCRIPT": str(h["transcript"]),
        "STUB_LOG": str(h["log"]),
        **env,
    }
    proc = subprocess.run(
        ["bash", str(h["here"] / "run_variant.sh"), variant],
        capture_output=True, text=True, env=full, timeout=60,
    )
    m = re.search(r"^RUN_DIR: (.+)$", proc.stdout, flags=re.MULTILINE)
    run_dir = Path(m.group(1)) if m else None
    return proc, run_dir


def _result(stdout: str) -> dict:
    m = re.search(r"^RESULT: (\{.*\})$", stdout, flags=re.MULTILINE)
    assert m, stdout
    return json.loads(m.group(1))


def test_full_pass_scores_own_folder_and_removes_it(harness) -> None:
    proc, run_dir = _run_variant(harness, STUB_PASS=str(EXPECTED))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert run_dir is not None and run_dir.parent == harness["base"]
    assert (f"SCORE: {EXPECTED}/{EXPECTED} (variant=default)  "
            "OUTCOME PASS  CONDUCT PASS") in proc.stdout
    result = _result(proc.stdout)
    assert result["outcome"] == "pass" and result["conduct"] == "pass"
    assert result["run_dir"] == str(run_dir)
    assert result["turns"] == 31 and result["cost_usd"] == 0.25
    assert harness["log"].read_text().split() == [str(run_dir)]
    assert not run_dir.exists(), "a passing run must remove its folder (#840)"


def test_keep_run_keeps_a_passing_folder(harness) -> None:
    proc, run_dir = _run_variant(
        harness, STUB_PASS=str(EXPECTED), LCSAS_BLIND_KEEP_RUN="1",
    )
    assert proc.returncode == 0
    assert run_dir is not None and run_dir.is_dir()
    assert json.loads((run_dir / "score.json").read_text())["passed"] == EXPECTED


def test_a_stale_newer_folder_is_never_scored(harness) -> None:
    """#830: a newer folder from another run sits beside ours; ours holds
    no transcript (the agent never ran).  The run must fail unscored, not
    borrow the other folder's 16/16."""
    stale = harness["base"] / "lcsas-blind-variant-default-1"
    stale.mkdir()
    shutil.copy2(harness["transcript"], stale / "transcript.jsonl")
    future = time.time() + 3600
    os.utime(stale, (future, future))
    proc, run_dir = _run_variant(
        harness, STUB_RUN="nothing", STUB_PASS=str(EXPECTED),
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert run_dir is not None and run_dir != stale
    assert "could not be scored" in proc.stderr
    assert "no transcript" in proc.stderr
    assert not harness["log"].exists(), "verify.sh must not run on any folder"
    assert run_dir.is_dir(), "an unscored run keeps its folder"
    assert stale.is_dir()


def test_missing_run_folder_fails(harness) -> None:
    proc, run_dir = _run_variant(
        harness, STUB_RUN="rmdir", STUB_PASS=str(EXPECTED),
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "does not exist" in proc.stderr
    assert not harness["log"].exists()


def test_zero_of_zero_is_not_a_pass(harness) -> None:
    """verify.sh's fixture guard prints FAIL to stderr only and exits 2:
    0 PASS + 0 FAIL lines.  Not a pass, and not an XFAIL either."""
    proc, _ = _run_variant(harness, STUB_PASS="0", STUB_FAIL="0", STUB_RC="2")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"SCORE: 0/{EXPECTED}" in proc.stdout
    xf, _ = _run_variant(harness, "no-catalog", STUB_RC="2")
    assert xf.returncode == 2, "an unscored run is never an XFAIL"
    assert "XFAIL:" not in xf.stdout


def test_all_passed_but_too_few_checks_is_not_a_pass(harness) -> None:
    proc, _ = _run_variant(harness, STUB_PASS=str(EXPECTED - 1))
    assert proc.returncode == 2
    assert f"reported {EXPECTED - 1} checks" in proc.stderr


def test_a_failed_check_fails_and_keeps_the_folder(harness) -> None:
    proc, run_dir = _run_variant(
        harness, STUB_PASS=str(EXPECTED - 1), STUB_FAIL="1", STUB_RC="1",
    )
    assert proc.returncode == 1
    assert f"SCORE: {EXPECTED - 1}/{EXPECTED}" in proc.stdout
    assert run_dir is not None and run_dir.is_dir()


def test_outcome_pass_conduct_fail_is_a_failed_run(harness) -> None:
    """#827: a run passes only when OUTCOME and CONDUCT both pass."""
    proc, run_dir = _run_variant(
        harness, STUB_PASS=str(EXPECTED - 1), STUB_FAIL="1", STUB_RC="1",
    )
    assert proc.returncode == 1
    assert "OUTCOME PASS  CONDUCT FAIL" in proc.stdout
    result = _result(proc.stdout)
    assert (result["outcome"], result["conduct"]) == ("pass", "fail")
    assert result["conduct_failed"] == 1 and result["outcome_passed"] == 9
    assert "        why check f0 failed" in proc.stdout
    proc, _ = _run_variant(
        harness, STUB_PASS=str(EXPECTED - 1), STUB_FAIL="1", STUB_RC="1",
        STUB_OUTCOME="8", STUB_FAIL_KIND="OUTCOME",
    )
    assert proc.returncode == 1
    assert "OUTCOME FAIL  CONDUCT PASS" in proc.stdout


def test_checks_without_a_group_are_not_scored(harness) -> None:
    proc, _ = _run_variant(harness, STUB_PASS=str(EXPECTED), STUB_KIND="none")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "OUTCOME checks, expected 9" in proc.stderr


def test_wrong_split_between_groups_is_not_scored(harness) -> None:
    proc, _ = _run_variant(harness, STUB_PASS=str(EXPECTED), STUB_OUTCOME="10")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "not scored" in proc.stderr


def test_xfail_listed_failure_exits_0_and_keeps_folder(harness) -> None:
    proc, run_dir = _run_variant(
        harness, "no-catalog",
        STUB_PASS=str(EXPECTED - 1), STUB_FAIL="1", STUB_RC="1",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "XFAIL: variant=no-catalog" in proc.stdout
    assert run_dir is not None and run_dir.is_dir()


def test_xfail_listed_full_pass_is_an_xpass_failure(harness) -> None:
    proc, run_dir = _run_variant(harness, "no-catalog", STUB_PASS=str(EXPECTED))
    assert proc.returncode == 1
    assert "XPASS" in proc.stdout
    assert run_dir is not None and run_dir.is_dir()


# --- base folder and pruning (#840) -----------------------------------------


def test_base_dir_prefers_harness_variable_then_tmpdir() -> None:
    assert blind_runs.base_dir({}) == Path("/tmp")
    assert blind_runs.base_dir({"TMPDIR": "/t"}) == Path("/t")
    assert blind_runs.base_dir(
        {"TMPDIR": "/t", "LCSAS_BLIND_TMPDIR": "/h"}
    ) == Path("/h")


def _aged(path: Path, days: float) -> Path:
    path.mkdir()
    (path / "transcript.jsonl").write_text("{}\n")
    then = time.time() - days * 86400
    os.utime(path, (then, then))
    return path


def test_prune_removes_only_old_harness_folders(tmp_path: Path) -> None:
    old_variant = _aged(tmp_path / "lcsas-blind-variant-default-11", 30)
    old_run = _aged(tmp_path / "lcsas-blind-run-12", 30)
    young = _aged(tmp_path / "lcsas-blind-variant-default-13", 1)
    current = _aged(tmp_path / "lcsas-blind-run-14", 30)
    other = _aged(tmp_path / "lcsas-blind-store", 30)  # not a run folder
    unrelated = _aged(tmp_path / "something-else", 30)
    target = _aged(tmp_path / "target", 30)
    link = tmp_path / "lcsas-blind-run-link"
    link.symlink_to(target)
    os.utime(link, follow_symlinks=False)

    removed, failed = blind_runs.prune(tmp_path, 14, keep=current)
    assert sorted(removed) == sorted([old_variant, old_run])
    assert failed == []
    for kept in (young, current, other, unrelated, target, link):
        assert kept.exists() or kept.is_symlink(), kept
    assert not old_variant.exists() and not old_run.exists()


def test_prune_of_a_missing_base_is_a_no_op(tmp_path: Path) -> None:
    assert blind_runs.prune(tmp_path / "nope", 1) == ([], [])


def test_prune_cli_honours_keep_days(tmp_path: Path) -> None:
    old = _aged(tmp_path / "lcsas-blind-run-1", 3)
    env = {**os.environ, "LCSAS_BLIND_TMPDIR": str(tmp_path),
           "LCSAS_BLIND_KEEP_DAYS": "5"}
    script = str(HARNESS / "blind_runs.py")
    subprocess.run(["python3", script, "prune"], env=env, check=True)
    assert old.exists()
    env["LCSAS_BLIND_KEEP_DAYS"] = "2"
    out = subprocess.run(["python3", script, "prune"], env=env, check=True,
                         capture_output=True, text=True)
    assert not old.exists()
    assert "pruned old blind run folder" in out.stdout


def test_run_sh_prunes_and_uses_the_base_folder() -> None:
    text = (HARNESS / "run.sh").read_text()
    assert 'blind_runs.py" prune --keep "$RUN_DIR"' in text
    assert 'blind_runs.py" base)/lcsas-blind-run-' in text
    assert "/tmp/lcsas-blind-run-" not in text


def test_no_harness_entry_point_scores_the_newest_folder() -> None:
    for path in (HARNESS / "run_variant.sh", REPO_ROOT / "Makefile"):
        assert "ls -1dt" not in path.read_text(), path
