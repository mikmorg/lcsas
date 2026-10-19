"""Hardening: tier-1 picks up a newly mounted disc without ENTER (#455).

Before #455 the disc-swap prompt blocked on stdin.  A disc the desktop
had already auto-mounted sat unread until the operator pressed ENTER,
once per swap, across what can be a 50-disc archive.  The locator now
re-scans the search paths and mount parents every 2 s while the prompt
waits, and ENTER and 'q' keep working throughout.

These run the production binary with ``--interactive on`` and stdin a
pipe that stays open with nothing written: an operator who never
touches the keyboard.  A disc is "inserted" by renaming a fully built
directory into the mount parent.  rename(2) is atomic, as a mount is, so
the binary can never see a half-copied pack.

  1. A disc mounted while the prompt waits resumes the restore with no
     input at all, and the prompt says that it will.
  2. ``--auto-mount off`` keeps the old contract: the disc is not picked
     up until ENTER, and the prompt makes no such promise.
  3. Typing still works while polling: ENTER rescans (and re-prompts
     when the disc is still missing), then 'q' aborts.
  4. EOF on stdin while polling aborts, as it always has.

The C side (recovery/tests/test_disc_locator.c) pins the stdio-buffer
trap: typed-ahead answers must not be stranded in a FILE buffer that
select() can't see.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .test_tier1_rescan import (
    BINARY,
    _build_split_fixture,
    _require_binary,
    _verify_restore,
)

_PROMPT = "is required for the next file"
_HINT = "found without ENTER"
_DETECTED = "new disc detected -- no ENTER needed"

# The binary polls every 2 s.  Waiting this long with the disc in place
# is more than two full periods: an auto-detecting binary would have
# reported the disc by now.
_TWO_POLLS_S = 5.0


class _Run:
    """The binary under a pipe stdin, with stderr collected by a thread."""

    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: list[str] = []
        self.prompts = threading.Semaphore(0)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in iter(self.proc.stderr.readline, ""):
            sys.stderr.write(line)
            self.lines.append(line)
            if _PROMPT in line:
                self.prompts.release()

    def wait_for_prompt(self) -> None:
        deadline = time.monotonic() + 60
        while not self.prompts.acquire(timeout=0.5):
            if self.proc.poll() is not None:
                # Let the reader drain what the binary printed on its
                # way out before deciding it never prompted.
                self._reader.join(timeout=5)
                if self.prompts.acquire(blocking=False):
                    return
                raise AssertionError(
                    f"binary exited {self.proc.returncode} without the "
                    f"disc-swap prompt:\n{self.output()}"
                )
            assert time.monotonic() < deadline, (
                "binary never printed the disc-swap prompt"
            )

    def type(self, text: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def finish(self) -> int:
        rc = self.proc.wait(timeout=120)
        self._reader.join(timeout=5)
        return rc

    def output(self) -> str:
        return "".join(self.lines)

    def reap(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=120)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _setup(tmp: Path) -> tuple[list[str], Path, Path, Path, dict[str, bytes]]:
    """Return (cmd, staged_disc_b, mount_parent, target, files).

    disc_a is on --pack-search from the start; disc_b is staged outside
    the (empty) mount parent, ready to be renamed in.
    """
    repo, pwfile, disc_a, disc_b, files = _build_split_fixture(tmp)
    target = tmp / "out"
    target.mkdir()
    mount_parent = tmp / "media"
    mount_parent.mkdir()
    staged = tmp / "staged" / "LCSAS_DISC_B"
    staged.parent.mkdir()
    shutil.move(str(disc_b), str(staged))
    cmd = [
        str(BINARY),
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--snapshot", "latest",
        "--pack-search", str(disc_a),
        "--mount-parent", str(mount_parent),
        "--interactive", "on",
    ]
    return cmd, staged, mount_parent, target, files


def test_disc_mounted_while_waiting_resumes_without_enter(tmp_path: Path) -> None:
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    run = _Run(cmd)
    try:
        run.wait_for_prompt()
        os.rename(staged, mount_parent / staged.name)
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert rc == 0, f"binary exited {rc}; expected 0 once the disc mounted"
    assert _DETECTED in out, (
        "restore completed but not through auto-detect -- nothing was "
        "typed, so this should be impossible"
    )
    assert _HINT in out, "the prompt no longer tells the operator ENTER is optional"
    _verify_restore(target, files)


def test_auto_mount_off_waits_for_enter(tmp_path: Path) -> None:
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    run = _Run(cmd + ["--auto-mount", "off"])
    try:
        run.wait_for_prompt()
        os.rename(staged, mount_parent / staged.name)
        time.sleep(_TWO_POLLS_S)
        assert run.proc.poll() is None, (
            "--auto-mount off resumed without ENTER"
        )
        run.type("\n")
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert rc == 0, f"binary exited {rc}; expected 0 after ENTER"
    assert _DETECTED not in out, "--auto-mount off still auto-detected the disc"
    assert _HINT not in out, (
        "--auto-mount off prompt still promises discs are found without ENTER"
    )
    _verify_restore(target, files)


def test_enter_and_q_still_work_while_polling(tmp_path: Path) -> None:
    _require_binary()
    cmd, _staged, _mount_parent, _target, _files = _setup(tmp_path)
    run = _Run(cmd)
    try:
        run.wait_for_prompt()
        time.sleep(_TWO_POLLS_S)  # polls run, find nothing
        run.type("\n")
        run.wait_for_prompt()     # ENTER rescanned, still missing: re-prompt
        run.type("q\n")
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert "still not found" in out, "ENTER while polling did not rescan"
    assert rc != 0, "'q' while polling did not abort"
    assert "aborted by user" in out


def test_eof_while_polling_aborts(tmp_path: Path) -> None:
    _require_binary()
    cmd, _staged, _mount_parent, _target, _files = _setup(tmp_path)
    run = _Run(cmd)
    try:
        run.wait_for_prompt()
        time.sleep(_TWO_POLLS_S)
        assert run.proc.stdin is not None
        run.proc.stdin.close()
        rc = run.finish()
    finally:
        run.reap()
    assert rc != 0, "EOF on stdin while polling did not abort"
    assert "aborted by user" in run.output()


def test_auto_mount_rejects_anything_but_on_or_off(tmp_path: Path) -> None:
    _require_binary()
    pwfile = tmp_path / "pw"
    pwfile.write_text("x\n")
    res = subprocess.run(
        [str(BINARY), "--repo", str(tmp_path), "--password-file", str(pwfile),
         "--target", str(tmp_path / "out"), "--auto-mount", "yes"],
        capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 2, (
        f"--auto-mount yes exited {res.returncode}; a typo must not "
        "silently pick a mode"
    )
    assert "--auto-mount takes on or off" in res.stderr
