"""#968 items 8 to 10: Ctrl-C and a closed stdout end a command quietly.

- Ctrl-C printed a Python traceback after ``stage``'s own message, and
  after "Ctrl-C safely cancels THIS command" for a command waiting on the
  catalog lock.  It now exits 130 with no traceback.
- Any command piped into ``head`` printed "--- Logging error ---
  BrokenPipeError" per log line.  A closed stdout is now silenced once.
- On TEST_TINY an interrupt before the first volume printed no plain
  "nothing was staged" line.

Each command runs as the owner runs it: ``python -m lcsas`` in its own
process, a real SIGINT, a real pipe; real rustic and xorriso behind it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.db.connection import locked_connection
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _env(inst: ScratchInstall) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": _SRC,
            "TMPDIR": str(inst.root / "tmp"), "HOME": str(inst.root / "tmp")}


def _default_sigint() -> None:
    """In the child: Ctrl-C as in a terminal, even when the test run
    itself was started with SIGINT ignored (a background job of a
    non-interactive shell), which Python would otherwise keep."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _spawn(inst: ScratchInstall, *argv: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "lcsas", "--config", str(inst.config), *argv],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=_env(inst), preexec_fn=_default_sigint,
    )


def _interrupt_after(proc: subprocess.Popen[str], marker: str) -> str:
    """SIGINT *proc* as soon as it prints a line holding *marker*; return
    everything it printed."""
    assert proc.stdout is not None
    seen: list[str] = []
    for line in proc.stdout:
        seen.append(line)
        if marker in line:
            proc.send_signal(signal.SIGINT)
            break
    rest, _ = proc.communicate(timeout=120)
    return "".join(seen) + (rest or "")


@pytest.fixture
def inst(tmp_path: Path) -> ScratchInstall:
    inst = ScratchInstall(tmp_path, repos=("alpha",))
    inst.backup("alpha", "f1.bin", size=60000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def test_ctrl_c_while_waiting_on_the_lock_exits_130_quietly(
    inst: ScratchInstall,
) -> None:
    with locked_connection(inst.db):
        proc = _spawn(inst, "scan")
        out = _interrupt_after(proc, "Ctrl-C safely cancels THIS command")
    assert "Traceback" not in out, out
    assert "KeyboardInterrupt" not in out, out
    assert proc.returncode == 130, out


def test_ctrl_c_before_the_first_volume_says_nothing_was_staged(
    inst: ScratchInstall,
) -> None:
    """The interrupt lands in the stage's checks, after the metadata
    reserve is measured and before the burn session exists."""
    proc = _spawn(inst, "stage")
    out = _interrupt_after(proc, "Disc metadata:")
    assert "Traceback" not in out, out
    assert "KeyboardInterrupt" not in out, out
    assert proc.returncode == 130, out
    assert "Interrupted: nothing was staged." in out, out
    assert inst.sessions() == []
    assert inst.labels() == []


def _closed_reader_run(inst: ScratchInstall, *argv: str) -> tuple[int, str]:
    """Run with stdout a pipe whose read end is already closed, as
    ``| head`` leaves it once head has read its lines."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "lcsas", "--config", str(inst.config),
             *argv],
            stdout=write_fd, stderr=subprocess.PIPE, text=True,
            env=_env(inst), timeout=300,
        )
    finally:
        os.close(write_fd)
    return proc.returncode, proc.stderr


@pytest.mark.parametrize("argv", [["repo", "list"], ["status"], ["scan"]])
def test_closed_stdout_is_not_reported_per_line(
    inst: ScratchInstall, argv: list[str],
) -> None:
    rc, err = _closed_reader_run(inst, *argv)
    assert "Logging error" not in err, err
    assert "BrokenPipeError" not in err, err
    assert "Traceback" not in err, err
    assert "Exception ignored" not in err, err
    assert rc == 0, err


def test_closed_stdout_lets_stage_finish(inst: ScratchInstall) -> None:
    """A stage whose reader went away still stages: silenced, not
    stopped half way."""
    rc, err = _closed_reader_run(inst, "stage")
    assert "BrokenPipeError" not in err, err
    assert rc == 0, err
    assert len(inst.sessions()) == 1
    assert len(inst.labels()) == 1


def test_closed_stdout_under_print_is_quiet(inst: ScratchInstall) -> None:
    """``password split`` reports with print() as well as the log: the
    closed pipe is still met once and quietly, after the shares exist."""
    rc, err = _closed_reader_run(
        inst, "password", "split", "--repo", "alpha",
        "--out", str(inst.root / "shares"),
    )
    assert "BrokenPipeError" not in err, err
    assert "Unexpected error" not in err, err
    assert "Exception ignored" not in err, err
    assert rc in (0, 141), err
    assert len(list((inst.root / "shares").glob("alpha-share-*-card.txt"))) == 5
