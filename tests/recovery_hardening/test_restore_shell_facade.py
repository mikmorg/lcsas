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
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

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
        env=env, capture_output=True, text=True, timeout=30,
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
