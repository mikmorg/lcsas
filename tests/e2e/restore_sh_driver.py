"""Drive the meta disc's ``restore.sh`` through a pseudoterminal (#756).

Shared by ``tests/integration/test_interactive_restore.py`` (loop
devices) and ``scripts/smoke_single_drive.py`` (a cdemu drive).  Both
need root to put a disc "in the drive"; everything here does not, so
``tests/unit/test_restore_sh_driver.py`` checks it on every run: the
command line passes only flags ``restore.sh`` accepts, every prompt
below is text ``restore.sh`` or tier 1 really prints, and
:func:`drive_restore` answers a fake ``restore.sh`` that prints them.

The interface (``recovery/scripts/restore.sh``, since #689):

- Flags: ``--repo NAME``, ``--target DIR``, ``--key FILE`` (and
  ``--version``, ``--check-disc IMAGE``, ``--help``).  Any other flag
  exits 2.  There is no drive flag and no work folder flag.
- The work folder is the tools folder, ``LCSAS_TOOLS_DIR``.  Run from a
  META disc named by ``LCSAS_META_DISC``, the script copies itself
  there first.
- Discs are found where they are mounted: each folder in
  ``LCSAS_MOUNT_DIRS`` (colon-separated) and each folder in it.  A
  "swap" is: unmount the disc, mount the next one at the same place.
- Step 0, on a terminal with only the META disc in: "put in your
  NEWEST data disc", answered with ENTER once it is in.
- Tier 1 asks for each disc in a framed box naming the labels the pack
  is on, then "Insert the right disc and press ENTER to retry." and
  "Type 'q' then ENTER to abort."  ``q`` stops the restore with exit 75
  (the files restored so far are kept).
- After a restore that worked, with more than one set: "Restore another
  backup set from these discs? [y/N]", answered N.
"""

from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH_SOURCE = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
DISC_LOCATOR_SOURCE = (
    REPO_ROOT / "recovery" / "src" / "lcsas-restore" / "disc_locator.c"
)

# What restore.sh prints (recovery/scripts/restore.sh).
NEWEST_DISC_PROMPT = b"ENTER, S or Q: "
ANOTHER_SET_PROMPT = b"Restore another backup set from these discs? [y/N] "
RAM_TARGET_PROMPT = b"Continue restoring into RAM anyway? [y/N] "
# What tier 1 prints (recovery/src/lcsas-restore/disc_locator.c, main.c).
TIER1_LABELS_LINE = b"It is on any disc labelled:"
TIER1_NO_RECORD_LINE = b"(catalog has no record of this pack ID)"
TIER1_RETRY_LINE = b"Insert the right disc and press ENTER to retry."
TIER1_ABORT_LINE = b"Type 'q' then ENTER to abort."
TIER1_DONE = b"[lcsas-restore] restore complete"

# restore.sh's exit code for q at tier 1's disc prompt (#615).
EXIT_DISC_PROMPT_QUIT = 75

ENTER = b"\n"
QUIT = b"q\n"
NO = b"n\n"

# One label line of tier 1's box: "|   LABEL      |".  Labels are
# letters, digits and underscores; the box's "|" ends the match.
_LABEL_LINE = re.compile(rb"\|\s+([A-Za-z0-9_]+)\s+\|")
# The end of a tier 1 box: the abort line, its frame, then "> ".
_TIER1_PROMPT_END = re.compile(
    re.escape(TIER1_ABORT_LINE) + rb".*?\n\+-+\+\r?\n> ", re.S,
)


def restore_sh_valid_flags(text: str | None = None) -> set[str]:
    """The flags ``restore.sh`` accepts, read from its "valid flags:" line."""
    if text is None:
        text = RESTORE_SH_SOURCE.read_text(encoding="utf-8")
    m = re.search(r"valid flags: ([^\n\\']+)", text)
    if m is None:
        raise ValueError("restore.sh has no 'valid flags:' line")
    return set(re.findall(r"--[a-z][a-z-]*", m.group(1)))


def build_restore_cmd(
    meta_root: Path, *, key: Path, target: Path, repo: str,
) -> list[str]:
    """``sh <meta>/restore.sh`` with the flags a scripted restore uses."""
    return [
        "sh", str(meta_root / "restore.sh"),
        "--key", str(key),
        "--target", str(target),
        "--repo", repo,
    ]


def build_restore_env(
    *,
    home: Path,
    tools_dir: Path,
    mount_dir: Path,
    meta_disc: Path,
    tmpdir: Path,
    path: str = "/usr/sbin:/usr/bin:/sbin:/bin",
) -> dict[str, str]:
    """The environment for a scripted restore.

    *mount_dir* is the one place discs are looked for; mount each data
    disc at a folder in it.  *meta_disc* is where the META disc is (the
    folder ``restore.sh`` is run from): ``restore.sh`` copies itself
    into *tools_dir* before it runs.
    """
    return {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "TERM": "dumb",
        "LCSAS_TOOLS_DIR": str(tools_dir),
        "LCSAS_MOUNT_DIRS": str(mount_dir),
        "LCSAS_META_DISC": str(meta_disc),
    }


@dataclass
class Prompt:
    """One question the restore asked.

    ``kind`` is ``"newest"`` (step 0), ``"disc"`` (tier 1), ``"another"``
    or ``"ram"``.  For ``"disc"``, ``labels`` are the discs tier 1 named
    (empty when its catalog has no record of the pack).
    """

    kind: str
    labels: list[str] = field(default_factory=list)


class PromptScanner:
    """Finds the restore's questions in its output, as it arrives."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[Prompt]:
        self._buf += chunk
        found: list[Prompt] = []
        while True:
            hits: list[tuple[int, int, Prompt]] = []
            for needle, kind in ((NEWEST_DISC_PROMPT, "newest"),
                                 (ANOTHER_SET_PROMPT, "another"),
                                 (RAM_TARGET_PROMPT, "ram")):
                i = self._buf.find(needle)
                if i >= 0:
                    hits.append((i, i + len(needle), Prompt(kind)))
            m = _TIER1_PROMPT_END.search(self._buf)
            if m is not None:
                box_start = self._buf.rfind(b"\n+--", 0, m.start())
                box = self._buf[max(box_start, 0):m.start()]
                hits.append((max(box_start, 0), m.end(),
                             Prompt("disc", _box_labels(box))))
            if not hits:
                return found
            hits.sort(key=lambda h: h[0])
            _start, end, prompt = hits[0]
            found.append(prompt)
            self._buf = self._buf[end:]


def _box_labels(box: bytes) -> list[str]:
    """The labels listed under "It is on any disc labelled:" in *box*."""
    i = box.find(TIER1_LABELS_LINE)
    if i < 0:
        return []
    labels: list[str] = []
    for line in box[i:].splitlines()[1:]:
        m = _LABEL_LINE.fullmatch(line.strip())
        if m is None:
            break
        labels.append(m.group(1).decode())
    return labels


# Answer to one prompt: the bytes to type (None: the default answer).
Answer = Callable[[Prompt, int], bytes | None]


def default_answer(prompt: Prompt) -> bytes:
    """ENTER at a disc question (after the disc is in), N at the others."""
    if prompt.kind in ("newest", "disc"):
        return ENTER
    return NO


def drive_restore(
    cmd: list[str],
    env: dict[str, str],
    answer: Answer,
    *,
    timeout: float = 300.0,
    echo: Callable[[bytes], None] | None = None,
) -> tuple[int, str]:
    """Run *cmd* under a pseudoterminal and answer each prompt.

    *answer* is called as ``answer(prompt, n)`` (n counts the prompts
    from 1) after the prompt is printed; it swaps discs as needed and
    returns what to type, or None for :func:`default_answer`.  Returns
    ``(exit code, output)``; the exit code is -1 on a timeout.
    """
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        try:
            os.execvpe(cmd[0], cmd, env)
        finally:
            os._exit(127)
    scanner = PromptScanner()
    chunks: list[bytes] = []
    n = 0
    deadline = time.monotonic() + timeout
    status: int | None = None
    try:
        while time.monotonic() < deadline:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except InterruptedError:
                continue
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    chunks.append(chunk)
                    if echo is not None:
                        echo(chunk)
                    for prompt in scanner.feed(chunk):
                        n += 1
                        reply = answer(prompt, n)
                        os.write(fd, default_answer(prompt)
                                 if reply is None else reply)
            wpid, wstatus = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = wstatus
                _drain(fd, chunks)
                break
            if ready and not chunk:
                _, status = os.waitpid(pid, 0)
                break
    finally:
        if status is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
    text = b"".join(chunks).decode(errors="replace")
    if status is None:
        return -1, text
    return os.waitstatus_to_exitcode(status), text


def _drain(fd: int, sink: list[bytes], timeout: float = 1.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ready, _, _ = select.select([fd], [], [], 0.2)
        except (InterruptedError, OSError):
            return
        if not ready:
            return
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        sink.append(chunk)


def disc_prompts(output: str) -> int:
    """How many disc questions tier 1 asked in *output*."""
    return output.count(TIER1_RETRY_LINE.decode())
