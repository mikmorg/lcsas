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
    _start(tmux_env, "sh", "-c", 'echo "Choose a backup set"; read x')
    assert _rs(tmux_env, "expect", "Password:", "1").returncode == 1
    retry = _rs(tmux_env, "expect", "Choose a backup set", "5")
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


# --- #827: restore-shell swap-loop --------------------------------------------
#
# The prompts' disc-swap loop is one command now, so the agent never types
# it out or saves it to a file (three rc9 runs were failed for saving it).
# Stub disc-loader and sudo on PATH record what the loop does; the
# "restore" is a shell that asks for a disc N times, then says it is
# complete and asks "Restore another backup set?".


def _fake_restore(boxes: int, *, complete: bool = True) -> str:
    end = (
        'echo "[lcsas-restore] restore complete"; '
        "printf 'Restore another backup set from these discs? [y/N] '; "
        'read a; echo "answer=$a"'
        if complete else 'echo "fatal: no more discs"; exit 3'
    )
    return (
        f'i=0; while [ "$i" -lt {boxes} ]; do echo "box $i"; '
        f'echo "{_SWAP}"; printf "> "; read x; i=$((i+1)); done; {end}'
    )


@pytest.fixture
def swap_env(tmux_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "actions.log"
    for name in ("disc-loader", "sudo"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    labels = tmp_path / "disc-labels.txt"
    labels.write_text("LCSAS_META\nLCSAS_D1\nLCSAS_D2\nLCSAS_D3\n")
    return {**tmux_env, "PATH": f"{stubs}:{tmux_env['PATH']}",
            "RESTORE_SHELL_DISC_LABELS": str(labels), "SWAP_LOG": str(log)}


def _actions(env: dict[str, str]) -> list[str]:
    path = Path(env["SWAP_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def test_swap_loop_cycles_the_data_discs_until_complete(
    swap_env: dict[str, str],
) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(4))
    done = _rs(swap_env, "swap-loop")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "RESTORE COMPLETE" in done.stdout
    inserts = [a for a in _actions(swap_env) if a.startswith("disc-loader insert")]
    # Label order, LCSAS_META skipped, starting over after the last.
    assert inserts == [f"disc-loader insert LCSAS_D{n}" for n in (1, 2, 3, 1)]
    actions = _actions(swap_env)
    # eject first: it unmounts /dev/sr0 wherever the agent mounted it.
    assert actions[:4] == ["sudo umount /media", "disc-loader eject",
                           "disc-loader insert LCSAS_D1",
                           "sudo mount /dev/sr0 /media"]
    _wait_on_screen(swap_env, "answer=n")


def test_swap_loop_takes_another_mount_point(swap_env: dict[str, str]) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(1))
    done = _rs(swap_env, "swap-loop", "/mnt")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "sudo mount /dev/sr0 /mnt" in _actions(swap_env)


def test_swap_loop_stops_when_the_session_ends(swap_env: dict[str, str]) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(1, complete=False))
    t0 = time.monotonic()
    gone = _rs(swap_env, "swap-loop")
    assert gone.returncode == 2, f"{gone.stdout}\n{gone.stderr}"
    assert time.monotonic() - t0 < 30
    assert "fatal: no more discs" in gone.stdout
    assert "stopped after 1 disc swaps" in gone.stderr


def _swap_loop_within(
    env: dict[str, str], secs: int, *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run swap-loop, failing the test (not hanging it) past *secs*."""
    try:
        return subprocess.run(
            ["sh", str(RESTORE_SHELL), "swap-loop", *args],
            env=env, capture_output=True, text=True, timeout=secs,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"swap-loop did not swap within {secs} s: it waited for a disc "
            f"box that was already on the screen.\nstdout:\n{exc.stdout!r}"
        )


def test_swap_loop_swaps_for_a_box_already_seen(swap_env: dict[str, str]) -> None:
    """#917: the agent's own ``expect`` returned the box first.

    Every ``swap-loop`` of the 2026-11-18/19 split-key-docs runs stopped
    after 0 disc swaps.  The agent saw the disc box with ``expect``, which
    marks it seen, then ran ``swap-loop``, whose ``expect`` waits for NEW
    output: the restore, waiting at that box, printed none, so the loop
    gave up after 120 s.  The agent then swapped by hand and, in three
    runs, scripted the swaps.  A box waiting at the cursor is the loop's
    first swap.
    """
    _start(swap_env, "sh", "-c", _fake_restore(2))
    seen = _rs(swap_env, "expect", "Insert the right disc", "30")
    assert seen.returncode == 0, f"{seen.stdout}\n{seen.stderr}"
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "RESTORE COMPLETE" in done.stdout
    inserts = [a for a in _actions(swap_env) if a.startswith("disc-loader insert")]
    # One swap per box: the box taken from the screen is not swapped
    # for a second time when the loop's own expect runs.
    assert inserts == ["disc-loader insert LCSAS_D1", "disc-loader insert LCSAS_D2"]
    _wait_on_screen(swap_env, "answer=n")


# Tier 1's disc box as the 2026-11-19 runs' screens show it (#917).
_TIER1_BOX = """\
+----------------------------------------------------------+
| Pack 69b14ed4e3a67f16... is required for the next file.  |
| It is on any disc labelled:                              |
|   LCSAS_TEST_TINY_2026_0003                              |
|                                                          |
| Currently searching:                                     |
|   /mnt                                                   |
|   /media                                                 |
|                                                          |
| Insert the right disc and press ENTER to retry.          |
| Type 'q' then ENTER to stop for now.                     |
| (A disc that mounts by itself is found without ENTER.)   |
+----------------------------------------------------------+"""


def test_swap_loop_swaps_for_a_framed_box_already_seen(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    """The same, with tier 1's framed box: the frame lines below
    "Insert the right disc" do not hide the waiting prompt."""
    box = tmp_path / "box.txt"
    box.write_text(_TIER1_BOX + "\n")
    _start(swap_env, "sh", "-c",
           f'cat "{box}"; printf "> "; read x; '
           'echo "[lcsas-restore] restore complete"; '
           "printf 'Restore another backup set from these discs? [y/N] '; "
           'read a; echo "answer=$a"')
    seen = _rs(swap_env, "expect", "Insert the right disc", "30")
    assert seen.returncode == 0, f"{seen.stdout}\n{seen.stderr}"
    time.sleep(0.3)  # the "> " after the box
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    inserts = [a for a in _actions(swap_env) if a.startswith("disc-loader insert")]
    assert inserts == ["disc-loader insert LCSAS_D1"]


def test_swap_loop_swaps_for_a_box_seen_only_on_screen(
    swap_env: dict[str, str],
) -> None:
    """The agent looked with ``screen``, which marks nothing, so the box
    is still new output to ``expect``.  Taking it from the screen marks
    it seen: the restore, busy for 2 s after ENTER, is not swapped for a
    second time on the same box."""
    _start(swap_env, "sh", "-c",
           f'echo "{_SWAP}"; printf "> "; read x; sleep 2; '
           'echo "[lcsas-restore] restore complete"; '
           "printf 'Restore another backup set from these discs? [y/N] '; "
           'read a; echo "answer=$a"')
    _wait_on_screen(swap_env, _SWAP)
    time.sleep(0.3)  # the "> " after the box
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    inserts = [a for a in _actions(swap_env) if a.startswith("disc-loader insert")]
    assert inserts == ["disc-loader insert LCSAS_D1"]


def test_swap_loop_needs_a_data_disc(swap_env: dict[str, str], tmp_path: Path) -> None:
    only_meta = tmp_path / "meta-only.txt"
    only_meta.write_text("LCSAS_META\n")
    rc = _rs({**swap_env, "RESTORE_SHELL_DISC_LABELS": str(only_meta)}, "swap-loop")
    assert rc.returncode == 2
    assert "no data disc" in rc.stderr
    assert _actions(swap_env) == []


# --- #917: swap-loop inserts the disc the box names ---------------------------
#
# A fake restore that prints tier 1's box (disc_locator.c::print_prompt)
# for each pack of a spec.  It finds the pack only once the disc that holds
# it is the one last inserted (read from the stub disc-loader's log).  A
# wrong disc gets "(still not found ...)" and the same box again, as tier 1
# does.  Spec lines: PACK HOLDER NAMED...  NAMED is the box's "It is on any
# disc labelled:" list, "-" for a box that lists none, or "newer:LABEL" for
# tier 3's "It is on a disc newer than: LABEL".

_FAKE_TIER1 = r'''
log=$1; spec=$2
frame() { printf '|   %-54s |\n' "$1"; }
box() {
    echo
    echo "+----------------------------------------------------------+"
    printf '| Pack %.16s... is required for the next file.  |\n' "$1"
    case "$2" in
        -) echo "| (catalog has the pack, but lists no disc for it)         |" ;;
        newer:*)
            echo "| (catalog has the pack, but lists no disc for it)         |"
            echo "| It is on a disc newer than:                              |"
            frame "${2#newer:}"
            echo "| Insert your newest data disc: its catalog names it.      |" ;;
        *)
            echo "| It is on any disc labelled:                              |"
            for d in $2; do frame "$d"; done ;;
    esac
    echo "|                                                          |"
    echo "| Currently searching:                                     |"
    frame /media
    frame LCSAS_D3
    frame /media/lcsas-blind/LCSAS_D3
    echo "|                                                          |"
    echo "| Insert the right disc and press ENTER to retry.          |"
    echo "| Type 'q' then ENTER to stop for now.                     |"
    echo "| (A disc that mounts by itself is found without ENTER.)   |"
    echo "+----------------------------------------------------------+"
    printf '> '
}
exec 3< "$spec"
while read -r pack holder named <&3; do
    tries=0
    box "$pack" "$named"
    while :; do
        read -r _
        cur=$(grep 'disc-loader insert' "$log" 2>/dev/null | tail -n 1 | cut -d' ' -f3)
        [ "$cur" = "$holder" ] && break
        tries=$((tries + 1))
        [ "$tries" -lt 12 ] || { echo "fatal: never found"; exit 3; }
        echo "(still not found -- check the disc label and try again)"
        box "$pack" "$named"
    done
    echo "[lcsas-restore] found $pack...; continuing."
done
echo "[lcsas-restore] restore complete"
printf 'Restore another backup set from these discs? [y/N] '
read -r a
echo "answer=$a"
'''


def _start_fake_tier1(
    env: dict[str, str], tmp_path: Path, spec: list[str],
) -> None:
    script = tmp_path / "fake_tier1.sh"
    script.write_text(_FAKE_TIER1)
    spec_file = tmp_path / "spec.txt"
    spec_file.write_text("".join(f"{line}\n" for line in spec))
    _start(env, "sh", str(script), env["SWAP_LOG"], str(spec_file))


def _inserts(env: dict[str, str]) -> list[str]:
    return [a.split()[2] for a in _actions(env)
            if a.startswith("disc-loader insert")]


def test_swap_loop_inserts_the_one_disc_the_box_names(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    """The box names one disc: that disc goes in, not the first in label
    order.  LCSAS_D3 in the "Currently searching:" paths is not named."""
    _start_fake_tier1(swap_env, tmp_path, [
        "aaaaaaaaaaaaaaaa11 LCSAS_D2 LCSAS_D2",
    ])
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _inserts(swap_env) == ["LCSAS_D2"]
    assert "LCSAS_D2 at /media (the disc the box names)" in done.stdout
    _wait_on_screen(swap_env, "answer=n")


def test_swap_loop_tries_the_next_named_disc_when_the_box_comes_back(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    """Two discs named, and the first does not hold the pack: the same box
    comes back, and the second named disc goes in, not one in label order."""
    _start_fake_tier1(swap_env, tmp_path, [
        "bbbbbbbbbbbbbbbb22 LCSAS_D3 LCSAS_D2 LCSAS_D3",
    ])
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _inserts(swap_env) == ["LCSAS_D2", "LCSAS_D3"]


@pytest.mark.parametrize("named", ["-", "newer:LCSAS_D3"])
def test_swap_loop_falls_back_to_label_order_when_the_box_names_none(
    swap_env: dict[str, str], tmp_path: Path, named: str,
) -> None:
    """No disc named, or only tier 3's "newer than" disc, which is not the
    disc wanted: label order, skipping the discs already tried."""
    _start_fake_tier1(swap_env, tmp_path, [
        f"cccccccccccccccc33 LCSAS_D2 {named}",
    ])
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _inserts(swap_env) == ["LCSAS_D1", "LCSAS_D2"]
    assert "(label order: the box names no disc left to try)" in done.stdout


def test_swap_loop_reads_a_waiting_box_from_the_screen(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    """The agent's own expect returned the box: swap-loop reads the disc
    it names off the screen."""
    _start_fake_tier1(swap_env, tmp_path, [
        "dddddddddddddddd44 LCSAS_D3 LCSAS_D3",
    ])
    seen = _rs(swap_env, "expect", "Insert the right disc", "30")
    assert seen.returncode == 0, f"{seen.stdout}\n{seen.stderr}"
    time.sleep(0.3)  # the "> " after the box
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "a disc box is waiting on the screen" in done.stdout
    assert _inserts(swap_env) == ["LCSAS_D3"]


def test_swap_loop_inserts_exactly_the_discs_asked_for(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    """The order of the 2026-11-19 red runs: 0001, 0005, 0004, 0003, 0002
    of eight data discs.  Those five go in, in that order, and nothing
    else (the thrash check allows 6)."""
    labels = tmp_path / "eight.txt"
    eight = [f"LCSAS_TEST_TINY_2026_{n:04d}" for n in range(1, 9)]
    labels.write_text("LCSAS_META\n" + "".join(f"{d}\n" for d in eight))
    env = {**swap_env, "RESTORE_SHELL_DISC_LABELS": str(labels)}
    order = [1, 5, 4, 3, 2]
    _start_fake_tier1(env, tmp_path, [
        f"{n:02d}{'e' * 14}{n:02d} {eight[n - 1]} {eight[n - 1]}"
        for n in order
    ])
    done = _swap_loop_within(env, 90)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _inserts(env) == [eight[n - 1] for n in order]
    _wait_on_screen(env, "answer=n")


# --- #942: swap-loop always ends with one fixed line ---------------------------
#
# The 2026-11-21 tier1-tier2-missing run (#942) started swap-loop in the
# background and waited for "RESTORE COMPLETE" in its output.  Tier 3
# says "Restore complete." (capital R), as does restore.sh's own closing
# frame, so swap-loop's case-sensitive "restore complete" never matched:
# it stopped on a 120 s timeout, which printed only a stderr line, and the
# agent's wait ran out its full 600 s.  Every way swap-loop ends now
# prints one of two fixed lines last on stdout, both starting
# "SWAP-LOOP END:", and the agent's instructions name them.


def _end_line(name: str) -> str:
    """The value of the sh constant *name* in restore-shell."""
    for line in RESTORE_SHELL.read_text().splitlines():
        if line.startswith(f"{name}='"):
            return line.split("'")[1]
    pytest.fail(f"restore-shell defines no {name}='...' constant")


def _last_line(out: str) -> str:
    lines = out.rstrip("\n").splitlines()
    return lines[-1] if lines else ""


def test_swap_loop_ends_with_the_done_line(swap_env: dict[str, str]) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(2))
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _last_line(done.stdout) == _end_line("SWAP_LOOP_DONE"), done.stdout
    _wait_on_screen(swap_env, "answer=n")


# How tier 3 and restore.sh end a restore that worked (no lowercase
# "restore complete" anywhere), then the "another set?" question.
_TIER3_END = (
    'echo "[restic-fallback] Restore complete."; '
    "echo '============================================================'; "
    "echo ' Restore complete.  4 files restored to:'; "
    "echo '============================================================'; "
    "printf 'Restore another backup set from these discs? [y/N] '; "
    'read a; echo "answer=$a"'
)


def test_swap_loop_sees_a_tier3_restore_complete(
    swap_env: dict[str, str],
) -> None:
    """#942: the tier-3 end is the end, not 120 s of silence."""
    _start(swap_env, "sh", "-c",
           f'echo "{_SWAP}"; printf "> "; read x; {_TIER3_END}')
    done = _swap_loop_within(swap_env, 60)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _last_line(done.stdout) == _end_line("SWAP_LOOP_DONE"), done.stdout
    assert _inserts(swap_env) == ["LCSAS_D1"]
    _wait_on_screen(swap_env, "answer=n")


def test_swap_loop_ends_with_the_stopped_line_when_the_session_ends(
    swap_env: dict[str, str],
) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(1, complete=False))
    gone = _swap_loop_within(swap_env, 60)
    assert gone.returncode == 2, f"{gone.stdout}\n{gone.stderr}"
    assert _last_line(gone.stdout) == _end_line("SWAP_LOOP_STOPPED"), gone.stdout
    assert "RESTORE COMPLETE" not in gone.stdout


def test_swap_loop_ends_with_the_stopped_line_after_40_swaps(
    swap_env: dict[str, str],
) -> None:
    _start(swap_env, "sh", "-c", _fake_restore(41))
    capped = _swap_loop_within(swap_env, 90)
    assert capped.returncode == 1, f"{capped.stdout}\n{capped.stderr}"
    assert "stopped after 40 disc swaps" in capped.stderr
    assert _last_line(capped.stdout) == _end_line("SWAP_LOOP_STOPPED"), (
        capped.stdout)


def test_swap_loop_ends_with_the_stopped_line_with_no_data_disc(
    swap_env: dict[str, str], tmp_path: Path,
) -> None:
    only_meta = tmp_path / "meta-only.txt"
    only_meta.write_text("LCSAS_META\n")
    rc = _rs({**swap_env, "RESTORE_SHELL_DISC_LABELS": str(only_meta)}, "swap-loop")
    assert rc.returncode == 2
    assert _last_line(rc.stdout) == _end_line("SWAP_LOOP_STOPPED"), rc.stdout
