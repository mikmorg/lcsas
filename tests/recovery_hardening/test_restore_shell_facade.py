"""The blind agent's terminal must keep the restore's last lines.

``restore-shell`` (tests/e2e/cdemu_blind_restore/) is the tmux facade
the blind-restore agent drives ``restore.sh`` through.  Its ``start``
set ``remain-on-exit`` with a separate ``tmux set-option`` call made
before ``new-session``.  On the first ``start`` of a run no tmux server
exists yet, so that call failed ("no server running") and was ignored.
When ``restore.sh`` exited, the pane, the session and the server all
vanished, taking ``[lcsas-restore] restore complete`` with them.  The
agent's next ``expect`` printed only "session r ended unexpectedly".

So no blind run ever showed the agent the line its prompt waits for.
Whether it then declared RESTORE COMPLETE depended on its guess: the
2026-07-31 haiku run inferred success from the file count, while the
2026-10-22 run inferred a crash and failed the gate after a
byte-identical restore.

These tests drive the real script against a private tmux server and
check that output written just before the command exits can still be
``expect``-ed afterwards, both when ``start`` creates the server and
when one is already running.

``expect`` also matched the whole pane (#551).  After a disc swap and
``send ""``, the box of the prompt just answered was still on screen,
so ``expect 'Insert the right disc'`` matched it at once, before the
binary had re-scanned.  The agent swapped again for a prompt that never
came: the 2026-10-21 run stopped after 7 of 30 files, and the 10-22 run
cycled all five discs a second time.  ``expect`` now matches only
output written after its last match.  The rest of this file pins that,
including the case that rules out marking at ``send`` instead: the
disc prompt resumes by itself when a disc mounts, so the next prompt
can arrive before the agent's ``send``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from .test_tier1_auto_mount import _DETECTED, _setup
from .test_tier1_rescan import _require_binary, _verify_restore

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SHELL = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "restore-shell"

# The restore's final line followed by a quick exit, as restore.sh does
# after tier 1 succeeds (write_session_log, then exit 0).  Thirty lines
# first, like a real run's screenful, so the line under test sits at the
# bottom of the pane rather than at the top.
_LAST_LINE = "[lcsas-restore] restore complete"
_RESTORE_LIKE = (
    'i=0; while [ "$i" -lt 30 ]; do echo "progress line $i"; '
    f'i=$((i+1)); done; echo "{_LAST_LINE}"'
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytest.fixture
def tmux_env() -> Iterator[dict[str, str]]:
    """Environment pointing tmux at a private, not-yet-started server."""
    # A short directory under /tmp, not tmp_path: tmux's socket path
    # (<dir>/tmux-<uid>/default) must fit in sun_path (108 bytes), and
    # a basetemp under a long $TMPDIR does not.
    sock_dir = tempfile.mkdtemp(prefix="rsh-", dir="/tmp")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TMUX_TMPDIR"] = sock_dir
    try:
        yield env
    finally:
        subprocess.run(
            ["tmux", "kill-server"], env=env, capture_output=True, timeout=10
        )
        shutil.rmtree(sock_dir, ignore_errors=True)


def _rs(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(RESTORE_SHELL), *args],
        env=env, capture_output=True, text=True, timeout=90,
    )


def _start_and_expect_last_line(env: dict[str, str]) -> None:
    started = _rs(env, "start", "sh", "-c", _RESTORE_LIKE)
    assert started.returncode == 0, started.stderr
    seen = _rs(env, "expect", "restore complete", "10")
    assert seen.returncode == 0, (
        f"expect rc={seen.returncode}; the pane did not survive the "
        f"command's exit.\nstdout:\n{seen.stdout}\nstderr:\n{seen.stderr}"
    )
    assert _LAST_LINE in seen.stdout
    screen = _rs(env, "screen")
    assert screen.returncode == 0, screen.stderr
    assert _LAST_LINE in screen.stdout


def test_last_line_survives_exit_when_start_creates_the_server(
    tmux_env: dict[str, str],
) -> None:
    """The blind run's case: the first ``start``, with no server yet."""
    no_server = subprocess.run(
        ["tmux", "list-sessions"], env=tmux_env, capture_output=True, timeout=10
    )
    assert no_server.returncode != 0, "precondition: no tmux server running"
    _start_and_expect_last_line(tmux_env)


def test_last_line_survives_exit_on_a_running_server(
    tmux_env: dict[str, str],
) -> None:
    """``start`` on a server another session keeps alive works too.

    The keep-alive session needs its own name: ``start`` kills any
    prior "r" session, and killing the only session ends the server.
    """
    keep = _rs({**tmux_env, "RESTORE_SHELL_SESSION": "keep"},
               "start", "sh", "-c", "sleep 60")
    assert keep.returncode == 0, keep.stderr
    _start_and_expect_last_line(tmux_env)


# --- #551: expect matches only output newer than its last match -------

# The disc-swap prompt cut down to the line the agent's loop waits on,
# with the cursor left after "> ", as disc_locator.c prints it.
_SWAP = "Insert the right disc and press ENTER to retry."


def _box(n: int) -> str:
    return f'echo "box {n}"; echo "{_SWAP}"; printf "> "'


def _start(env: dict[str, str], *cmd: str) -> None:
    started = _rs(env, "start", *cmd)
    assert started.returncode == 0, started.stderr


def _wait_on_screen(env: dict[str, str], text: str) -> None:
    deadline = time.monotonic() + 20
    while text not in _rs(env, "screen").stdout:
        assert time.monotonic() < deadline, f"{text!r} never reached the pane"
        time.sleep(0.2)


def test_answered_prompt_does_not_match_again(tmux_env: dict[str, str]) -> None:
    """The agent's loop: expect, swap discs, ``send ""``, expect.

    The second expect must wait for the box the re-scan prints, not
    return the first one, which is still on screen.
    """
    _start(tmux_env, "sh", "-c", f"{_box(1)}; read x; sleep 3; {_box(2)}; read y")
    first = _rs(tmux_env, "expect", "Insert the right disc", "10")
    assert first.returncode == 0, first.stderr
    assert "box 1" in first.stdout
    _rs(tmux_env, "send", "")

    early = _rs(tmux_env, "expect", "Insert the right disc", "1")
    assert early.returncode == 1, (
        "expect matched before the next box was printed: the answered "
        f"prompt still counted.\nstdout:\n{early.stdout}"
    )
    second = _rs(tmux_env, "expect", "Insert the right disc", "15")
    assert second.returncode == 0, second.stderr
    assert "box 2" in second.stdout
    assert "box 1" not in second.stdout, "expect returned output it had returned before"


@pytest.mark.parametrize(
    ("after", "awaited"),
    [(f"{_box(2)}; read y", "box 2"), (f'echo "{_LAST_LINE}"', _LAST_LINE)],
    ids=["next-prompt", "restore-complete"],
)
def test_output_printed_before_send_still_matches(
    tmux_env: dict[str, str], after: str, awaited: str
) -> None:
    """A disc that mounts by itself resumes the restore without ENTER.

    So the next box, or the final line, can be printed after the agent's
    expect returned and before its ``send ""``.  The next expect must
    still see it, which is why a match moves the mark and a send does
    not.
    """
    _start(tmux_env, "sh", "-c", f"{_box(1)}; sleep 3; {after}")
    first = _rs(tmux_env, "expect", "Insert the right disc", "10")
    assert first.returncode == 0, first.stderr
    assert awaited not in first.stdout, "precondition: expect ran before the resume"
    _wait_on_screen(tmux_env, awaited)
    _rs(tmux_env, "send", "")  # may land on a dead pane: not checked

    nxt = _rs(tmux_env, "expect", "Insert the right disc|restore complete", "10")
    assert nxt.returncode == 0, (
        "output printed before the send was hidden from expect.\n"
        f"stdout:\n{nxt.stdout}\nstderr:\n{nxt.stderr}"
    )
    assert awaited in nxt.stdout
    assert "box 1" not in nxt.stdout


def test_prompt_on_the_cursor_row_does_not_match_again(
    tmux_env: dict[str, str],
) -> None:
    """The cursor sits on the prompt's own row, after "Password: ".

    After a send that row reads "Password: <typed>".  The part of it an
    expect already returned must not match the next one.
    """
    _start(tmux_env, "sh", "-c",
           'printf "Password: "; read a; sleep 3; echo "wrong password"; '
           'printf "Password: "; read b')
    assert _rs(tmux_env, "expect", "Password:", "10").returncode == 0
    _rs(tmux_env, "send", "hunter2")

    early = _rs(tmux_env, "expect", "Password:", "1")
    assert early.returncode == 1, f"the answered prompt matched again:\n{early.stdout}"
    again = _rs(tmux_env, "expect", "Password:", "15")
    assert again.returncode == 0, again.stderr
    assert "wrong password" in again.stdout


def test_timeout_leaves_the_output_unseen(tmux_env: dict[str, str]) -> None:
    """Only a match marks output as seen, so a retry with a better REGEX
    still finds a prompt that is already on screen."""
    _start(tmux_env, "sh", "-c", 'echo "Choose a repository"; read x')
    assert _rs(tmux_env, "expect", "Password:", "1").returncode == 1
    retry = _rs(tmux_env, "expect", "Choose a repository", "5")
    assert retry.returncode == 0, retry.stderr


def test_match_scrolled_off_the_pane_is_found(tmux_env: dict[str, str]) -> None:
    """New output longer than the pane still matches, and is all printed.

    The old capture read only the visible rows.
    """
    _start(tmux_env, "sh", "-c",
           'echo "repository list follows"; i=0; '
           'while [ "$i" -lt 60 ]; do echo "row $i"; i=$((i+1)); done; read x')
    seen = _rs(tmux_env, "expect", "repository list follows", "10")
    assert seen.returncode == 0, seen.stderr
    assert "repository list follows" in seen.stdout
    assert "row 59" in seen.stdout


def test_exit_without_a_match_returns_at_once(tmux_env: dict[str, str]) -> None:
    """remain-on-exit keeps the dead pane, which never prints again.

    expect waited out its whole timeout on one and called it a timeout.
    It now exits 2 at once and says the command exited.
    """
    _start(tmux_env, "sh", "-c", 'echo "lcsas-restore: fatal: bad disc"; exit 3')
    t0 = time.monotonic()
    gone = _rs(tmux_env, "expect", "restore complete", "30")
    assert gone.returncode == 2, gone.stderr
    assert time.monotonic() - t0 < 15
    assert "status 3" in gone.stderr
    assert "fatal: bad disc" in gone.stdout


def test_real_disc_prompt_round_trip(
    tmux_env: dict[str, str], tmp_path: Path
) -> None:
    """The agent's loop against tier 1's own disc-swap prompt.

    ENTER with the disc still missing re-prints the box, and expect
    returns that box alone.  With nothing new after it, expect times
    out rather than match it again.  Then the disc "mounts" (renamed
    into the mount parent, as in test_tier1_auto_mount), the restore
    finishes with no ENTER, and expect sees the final line.
    """
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    _start(tmux_env, *cmd)
    first = _rs(tmux_env, "expect", "Insert the right disc", "60")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    _rs(tmux_env, "send", "")

    second = _rs(tmux_env, "expect", "Insert the right disc", "30")
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert second.stdout.count("Insert the right disc") == 1, second.stdout
    idle = _rs(tmux_env, "expect", "Insert the right disc", "5")
    assert idle.returncode == 1, f"an answered box matched again:\n{idle.stdout}"

    os.rename(staged, mount_parent / staged.name)
    done = _rs(tmux_env, "expect", "restore complete", "60")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _DETECTED in done.stdout
    _verify_restore(target, files)
