"""merge_timeline.py builds each blind run's timeline.txt (#835).

It raised TypeError on every run since July (an aware disc-loader stamp
compared with a naive ``datetime.max``), and run.sh threw the error away, so
every timeline.txt was empty.  These tests feed it a small hand-written run
(the shapes of a real stream-json transcript and disc-loader log, no real
session data) and run run.sh's timeline step with a stub sudo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
SCRIPT = HARNESS / "merge_timeline.py"

_spec = importlib.util.spec_from_file_location("merge_timeline", SCRIPT)
assert _spec is not None and _spec.loader is not None
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)


def _tool_use(ts: str, command: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}},
    ]}}


def _tool_result(ts: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": text}]},
    ]}}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    events = [
        {"type": "system", "subtype": "init", "cwd": "/home/agent"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
        _tool_use("2026-01-02T10:00:00.100Z", "disc-loader insert LCSAS_META"),
        _tool_result("2026-01-02T10:00:01.000Z", "LOADED LCSAS_META"),
        {"type": "assistant", "timestamp": "2026-01-02T10:00:02.000Z",
         "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        _tool_use("2026-01-02T10:00:05.500Z",
                  "disc-loader insert LCSAS_TEST_TINY_2026_0001"),
        {"type": "assistant", "timestamp": "2026-01-02T10:00:09.000Z",
         "message": {"content": [{"type": "text", "text": "RESTORE COMPLETE"}]}},
        {"type": "result", "subtype": "success", "num_turns": 3,
         "total_cost_usd": 0.01, "result": "RESTORE COMPLETE"},
    ]
    (tmp_path / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\nnot json\n"
    )
    (tmp_path / "disc-loader.log").write_text(
        "2026-01-02T10:00:00+00:00 list\n"
        "2026-01-02T10:00:01+00:00 insert LCSAS_META\n"
        "2026-01-02T10:00:06+00:00 insert LCSAS_TEST_TINY_2026_0001\n"
    )
    return tmp_path


def _run(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, timeout=30,
    )


def test_merges_a_recorded_run_in_time_order(run_dir: Path) -> None:
    proc = _run(run_dir / "transcript.jsonl", run_dir / "disc-loader.log")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    bodies = [ln.split("  ", 1)[1] for ln in lines]
    order = [
        next(i for i, b in enumerate(bodies) if needle in b)
        for needle in (
            "[disc-loader] list",
            "Bash", "LOADED LCSAS_META",
            "[disc-loader] insert LCSAS_META",
            "insert LCSAS_TEST_TINY_2026_0001\"",
            "[disc-loader] insert LCSAS_TEST_TINY_2026_0001",
            "[assistant] RESTORE COMPLETE",
            "[result] turns=3",
        )
    ]
    assert order == sorted(order), "\n".join(lines)
    assert not any("thinking_tokens" in b for b in bodies)
    assert any(b == "[assistant] (thinking)" for b in bodies)
    # Unstamped events keep their place: init first, result last.
    assert bodies[0].startswith("[system]")
    assert bodies[-1].startswith("[result]")


def test_naive_and_z_stamps_sort_together() -> None:
    aware = mt._parse_ts("2026-01-02T10:00:00+00:00")
    zulu = mt._parse_ts("2026-01-02T10:00:00.5Z")
    naive = mt._parse_ts("2026-01-02T10:00:01")
    assert aware is not None and zulu is not None and naive is not None
    assert sorted([naive, zulu, aware]) == [aware, zulu, naive]
    assert mt._parse_ts("garbage") is None
    assert mt._parse_ts(None) is None


def test_one_missing_input_still_gives_a_timeline(run_dir: Path) -> None:
    proc = _run(run_dir / "transcript.jsonl", run_dir / "no-such.log")
    assert proc.returncode == 0
    assert "cannot read" in proc.stderr
    assert "[result]" in proc.stdout


def test_nothing_to_merge_is_an_error(tmp_path: Path) -> None:
    proc = _run(tmp_path / "a", tmp_path / "b")
    assert proc.returncode == 1
    assert "nothing to merge" in proc.stderr


# --- run.sh's timeline step ----------------------------------------------------


def _timeline_step() -> str:
    text = (HARNESS / "run.sh").read_text()
    m = re.search(
        r"^# The merged timeline is a diagnostic.*?^fi$", text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, "run.sh lost its timeline step"
    return m.group(0)


def _run_step(here: Path, run_dir: Path, tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    sudo = bindir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    script = "set -euo pipefail\n" + _timeline_step() + "\necho STEP-DONE\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "HERE": str(here), "RUN_DIR": str(run_dir)},
    )


def test_run_sh_writes_the_timeline(run_dir: Path, tmp_path: Path) -> None:
    proc = _run_step(HARNESS, run_dir, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "STEP-DONE" in proc.stdout
    timeline = (run_dir / "timeline.txt").read_text()
    assert "[disc-loader] insert LCSAS_META" in timeline
    assert "WARNING" not in proc.stderr


def test_run_sh_reports_a_failed_timeline_without_failing(
    run_dir: Path, tmp_path: Path,
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    (here / "merge_timeline.py").write_text(
        "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"
    )
    proc = _run_step(here, run_dir, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "STEP-DONE" in proc.stdout, "the run must go on"
    assert "WARNING: the merged timeline could not be built" in proc.stderr
    assert "boom" in (run_dir / "timeline-errors.log").read_text()


def test_real_run_shape_does_not_raise(run_dir: Path, tmp_path: Path) -> None:
    """The exact failure of #835: stamped transcript events, stamped
    disc-loader lines AND unstamped events together."""
    dst = tmp_path / "copy"
    shutil.copytree(run_dir, dst)
    proc = _run(dst / "transcript.jsonl", dst / "disc-loader.log")
    assert proc.returncode == 0
    assert "TypeError" not in proc.stderr
