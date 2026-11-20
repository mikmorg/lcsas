"""The script contract (#887): what restore.sh and restore.bat do with a
reader's exit code, stated once as data and run on both scripts.

``restore.sh`` and ``restore.bat`` turn what a reader did (its exit
code, the rows it wrote to ``RESTORE_FAILURES.txt``, what it printed)
into what the person sees: a message, the script's own exit code,
whether "how to carry on" is said, whether the tools folder is kept and
whether "Restore another backup set?" is asked.  That rule used to be
pinned by asserting lines of script source; here it is run.

- A :class:`ScriptCase` is data, keyed on what the reader did, not on a
  repository: which tier runs, its exit code, its failure rows.  It
  holds the :class:`ScriptOutcome` each script must produce (``sh`` and
  ``bat``; ``bat=None`` for a case restore.bat has no counterpart of,
  with the reason in ``bat_why``).
- :func:`run_restore_sh` runs ``restore.sh`` with stub readers (shell
  scripts that exit with the case's code), as
  ``tests/recovery_hardening/test_restore_sh_exit_outcomes.py`` does.
- :func:`run_restore_bat` runs ``restore.bat`` under wine with a stub
  ``.exe`` built from :data:`STUB_C` (one program for every tool: it
  reads its exit code and words from files beside it).
- Both return the :class:`ScriptOutcome` built from the script's real
  output, exit code and folders; :func:`mismatches` compares it with the
  expected one.

Why stubs here and real readers in ``restore_contract.py``: the
scripts' rule is a function of (reader exit code, failures file,
interactive or not).  Stubs make every row cheap and exact; the reader
table (#886) proves the readers produce those codes.

The test is ``tests/recovery_hardening/test_restore_script_outcomes.py``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tests.contract.exit_codes import ReaderExit, ScriptExit

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
RESTORE_BAT = REPO_ROOT / "recovery" / "scripts" / "restore.bat"

#: The triple each script runs the stubs as.
SH_TARGET = "x86_64-unknown-linux-musl"
BAT_TARGET = "x86_64-pc-windows-gnu"

#: The set restored; a second set makes "another set?" a question.
SET = "alpha"
OTHER_SET = "beta"
PASSWORD = "pw"

FAILURES_NAME = "RESTORE_FAILURES.txt"
FAILURES_HEADER = (
    "LCSAS restore: items that were NOT restored.\n"
    "Written by a stub.  Everything else was restored.\n"
    "\n"
    "path\treason\tblob_id\n"
)
#: What a next tier prints when it runs: a case that must not fall
#: through says it must not appear.
NEXT_TIER_RAN = "NEXT_TIER_RAN"

PACK_A = "a" * 64
PACK_B = "b" * 64


# ── What the reader did, and what the script must do ────────────────


@dataclass(frozen=True)
class ScriptOutcome:
    """What a script did.  An expected outcome pins only what it states:
    ``None`` = not compared.

    ``says`` / ``not_says``: regexes searched in everything the script
    printed (with the stub reader's own lines).  ``resume_advice``: the
    script said how to carry on (restore.sh names ``resume.sh``;
    restore.bat says "To carry on").  ``tools_kept``: the tools folder is
    there after the run.  ``another_set``: "Restore another backup set
    from these discs?" was asked.  ``log``: the ``result=`` of
    restore.sh's session log line (restore.bat keeps no log).
    """

    exit: int
    says: str = ""
    not_says: str = ""
    resume_advice: bool | None = None
    tools_kept: bool | None = None
    another_set: bool | None = None
    log: str | None = None
    #: Only on an outcome a driver returns: what the script printed.
    output: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True)
class ScriptCase:
    """What the reader did, and what each script must make of it."""

    name: str
    #: The tier whose stub runs ("1", "2", "2b" or "3"); None: no tier
    #: this computer can run.
    tier: str | None
    sh: ScriptOutcome
    bat: ScriptOutcome | None
    #: Why restore.bat has no such case (when ``bat`` is None).
    bat_why: str = ""
    #: Why wine cannot show restore.bat's outcome (a strict xfail there,
    #: so it goes red if wine starts to).
    wine_gap: str = ""
    reader_exit: int = 0
    #: Lines the stub prints on stderr.
    reader_says: tuple[str, ...] = ()
    #: Rows the stub writes to RESTORE_FAILURES.txt: (path, reason).
    failures: tuple[tuple[str, str], ...] = ()
    #: The set has its own data/ folder (so tiers 2 and 2b may run).
    data: bool = False
    #: A tier-2 stub that says NEXT_TIER_RAN and succeeds is there.
    next_tier: bool = False
    #: LCSAS_TIER_FALLBACK=1.
    fallback: bool = False
    #: The folder to restore into already holds a file of the person's.
    target_taken: bool = False
    #: The script is called wrongly (each script its own wrong call).
    usage_error: bool = False
    #: ``--check-disc``, and what ``lcsas-ecc verify`` exits with.
    check_disc: int | None = None
    #: restore.sh runs with LCSAS_INTERACTIVE=on (so "another set?" can
    #: be asked); False: off (a wrong password is then not asked again).
    interactive: bool = True


def _rows(*rows: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return rows


# Two errors: a special file and a link the file system cannot hold
# are listed, and not counted (failures_errors, :count_failures).
_FINISHED = _rows(("a/bad1", "name refused: test"),
                  ("a/bad2", "link refused: test"),
                  ("a/fifo", "special file not created: test"),
                  ("a/link", "link not created: the target file system "
                             "does not support symlinks"))
_DAMAGED = _rows(("a/one", f"pack damaged: {PACK_A}"),
                 ("a/two", f"pack unreadable: {PACK_B}"))
_NOT_FOUND = _rows(("a/one", f"pack not found: {PACK_A}"),
                   ("a/two", f"pack damaged: {PACK_B}"))
_NOT_ALL_SAID = ("[lcsas-restore] ERROR: 2 item(s) were NOT restored.",
                 "[lcsas-restore] finished with errors: every other item "
                 "was restored")

# Messages, per script.
_SH_FINISHED = r"The restore finished, but 2 item\(s\) could not be restored\."
_BAT_FINISHED = r"The restore finished, but 2 item\(s\) could not be restored\."
_SH_STOPPED = r"The restore stopped before it finished \(exit code {rc}\)"
_BAT_STOPPED = r"The restore stopped before it finished \(code {rc}\)"
_SH_FAILED_LINE = r"stopped with exit code"
_MISSING_DISC = (r"[\s\S]*1 of them could not be read: a data disc was not in a drive"
                 r"[\s\S]*Insert the data disc\(s\), then\s+run the same "
                 r"restore again, into the same folder:")
#: restore.bat's advice to carry on (restore.sh's names resume.sh).
_BAT_CARRY_ON = r"To carry on|run the same restore again"
_ANOTHER = "Restore another backup set"


def _stopped(rc: int, *, sh: bool) -> str:
    return (_SH_STOPPED if sh else _BAT_STOPPED).format(rc=rc)


SCRIPT_CASES: tuple[ScriptCase, ...] = (
    # ── Tier 1 ──────────────────────────────────────────────────────
    ScriptCase(
        name="tier 1 restored everything",
        tier="1", reader_exit=ReaderExit.OK,
        reader_says=("[lcsas-restore] restore complete",),
        sh=ScriptOutcome(exit=ScriptExit.OK, not_says=_SH_FAILED_LINE,
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="ok"),
        bat=ScriptOutcome(exit=ScriptExit.OK,
                          says=r"Restore complete\.  Files restored to: E:\\restored",
                          resume_advice=False, tools_kept=False,
                          another_set=True),
    ),
    ScriptCase(
        name="tier 1 failed, set read from the discs",
        # No data/ folder: tier 2 (there, and able to say it ran) cannot
        # read the discs, so neither script runs it (#615, #227).
        tier="1", reader_exit=ReaderExit.FAILED, next_tier=True,
        reader_says=("ERROR: tree restore failed",) + _NOT_ALL_SAID[1:],
        # Tier 1 also writes the list when it stops early; exit 1 is
        # still a failure (#872), whatever is listed.
        failures=_FINISHED,
        sh=ScriptOutcome(exit=ScriptExit.FAILED,
                         says=_stopped(1, sh=True),
                         not_says=rf"could not be restored|{NEXT_TIER_RAN}",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="failed-1"),
        bat=ScriptOutcome(exit=ScriptExit.FAILED,
                          says=_stopped(1, sh=False),
                          not_says=rf"could not be restored|{NEXT_TIER_RAN}|tier 2",
                          resume_advice=True, tools_kept=True,
                          another_set=False),
    ),
    ScriptCase(
        # A reader's own 2 is its usage error; the scripts' 2 means the
        # script was called wrongly, so they end with 1 (owner, #887).
        name="tier 1 usage error",
        tier="1", reader_exit=ReaderExit.USAGE,
        reader_says=("usage: lcsas-restore --repo DIR ...",),
        sh=ScriptOutcome(exit=ScriptExit.FAILED, says=_stopped(1, sh=True),
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="failed-2"),
        bat=ScriptOutcome(exit=ScriptExit.FAILED, says=_stopped(1, sh=False),
                          resume_advice=True, tools_kept=True,
                          another_set=False),
    ),
    ScriptCase(
        name="tier 1 finished, some items not restored",
        tier="1", reader_exit=ReaderExit.NOT_ALL_RESTORED,
        reader_says=_NOT_ALL_SAID, failures=_FINISHED,
        # Not redone by the next tier, even with the fallback on.
        data=True, next_tier=True, fallback=True,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED, says=_SH_FINISHED,
                         not_says=rf"{NEXT_TIER_RAN}|{_SH_FAILED_LINE}|"
                                  r"stopped before it finished",
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="not-all-restored"),
        bat=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED,
                          says=_BAT_FINISHED + r"[\s\S]*"
                               r"E:\\restored\\RESTORE_FAILURES\.txt",
                          not_says=rf"{NEXT_TIER_RAN}|stopped before it finished",
                          resume_advice=False, tools_kept=False,
                          another_set=True),
    ),
    ScriptCase(
        name="tier 1 finished, packs damaged",
        tier="1", reader_exit=ReaderExit.NOT_ALL_RESTORED,
        reader_says=_NOT_ALL_SAID, failures=_DAMAGED,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED, says=_SH_FINISHED,
                         not_says=r"insert|Insert",
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="not-all-restored"),
        bat=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED, says=_BAT_FINISHED,
                          not_says=r"insert|Insert",
                          resume_advice=False, tools_kept=False,
                          another_set=True),
    ),
    ScriptCase(
        # A disc that was not in a drive, with no disc prompt (#909):
        # the same restore, run again with it in, restores the rest, so
        # the tools folder and the resume helper stay (owner, #887).
        # The scripts cannot name the disc: the row holds a pack id, and
        # only tier 1's disc prompt looks it up in the catalog.
        name="tier 1 finished, a pack not found",
        tier="1", reader_exit=ReaderExit.NOT_ALL_RESTORED,
        reader_says=_NOT_ALL_SAID, failures=_NOT_FOUND,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED,
                         says=_SH_FINISHED + _MISSING_DISC
                              + r"[\s\S]*    sh \S*/tools/resume\.sh\n",
                         not_says=rf"{_SH_FAILED_LINE}|stopped before it finished",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="not-all-restored"),
        bat=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED,
                          says=_BAT_FINISHED + _MISSING_DISC
                               + r"[\s\S]*run restore\.bat again from the META disc",
                          not_says=r"stopped before it finished",
                          resume_advice=True, tools_kept=True,
                          another_set=False),
    ),
    ScriptCase(
        name="tier 1 disk full",
        tier="1", reader_exit=ReaderExit.DISK_FULL,
        data=True, next_tier=True, fallback=True,
        sh=ScriptOutcome(exit=ScriptExit.DISK_FULL,
                         says=r"\[tier 1\] the disk is full\.",
                         not_says=rf"{NEXT_TIER_RAN}|falling through",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="disk-full"),
        bat=ScriptOutcome(exit=ScriptExit.DISK_FULL,
                          says=_stopped(74, sh=False) + r"[\s\S]*The disk is full\.",
                          not_says=NEXT_TIER_RAN,
                          resume_advice=True, tools_kept=True,
                          another_set=False),
    ),
    ScriptCase(
        name="tier 1 q at the disc prompt",
        tier="1", reader_exit=ReaderExit.READER_QUIT,
        data=True, next_tier=True, fallback=True,
        sh=ScriptOutcome(exit=ScriptExit.READER_QUIT,
                         says=r"\[tier 1\] stopped at the disc prompt at your request\.",
                         not_says=rf"{NEXT_TIER_RAN}|falling through",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="stopped"),
        bat=ScriptOutcome(exit=ScriptExit.READER_QUIT,
                          says=r"You stopped it at the disc box\.",
                          not_says=NEXT_TIER_RAN,
                          resume_advice=True, tools_kept=True,
                          another_set=False),
    ),
    ScriptCase(
        name="tier 1 wrong password",
        tier="1", reader_exit=ReaderExit.WRONG_PASSWORD,
        data=True, next_tier=True, fallback=True,
        sh=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                         says=r"\[tier 1\] wrong password",
                         not_says=rf"{NEXT_TIER_RAN}|falling through",
                         tools_kept=True, another_set=False,
                         log="wrong-password"),
        bat=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                          says=r"could not decrypt the repository with that password",
                          not_says=NEXT_TIER_RAN,
                          tools_kept=True, another_set=False),
    ),
    # ── Tier 3, the Python reader (restore.sh only) ─────────────────
    ScriptCase(
        name="tier 3 restored everything",
        tier="3", reader_exit=ReaderExit.OK,
        sh=ScriptOutcome(exit=ScriptExit.OK,
                         not_says=rf"FAILED|{_SH_FAILED_LINE}",
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="ok"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        name="tier 3 failed",
        tier="3", reader_exit=ReaderExit.FAILED,
        reader_says=("[lcsas-restore] ERROR: no repository",),
        sh=ScriptOutcome(exit=ScriptExit.FAILED,
                         says=r"\[tier 3\] FAILED \(rc=1\)",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="failed-1"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        name="tier 3 usage error",
        tier="3", reader_exit=ReaderExit.USAGE,
        reader_says=("usage: standalone_restorer.py [-h]",),
        sh=ScriptOutcome(exit=ScriptExit.FAILED,
                         says=r"\[tier 3\] FAILED \(rc=2\)",
                         not_says=r"could not be restored",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="failed-2"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        # A Python reader on a disc made before rc10 (#875) said
        # "finished, not all restored" with exit 2, its usage error too.
        # Those discs are burned, so the words decide (owner, #887).
        name="tier 3 of an older disc, exit 2 with its words and a list",
        tier="3", reader_exit=2,
        reader_says=("[lcsas-restore] ERROR: 2 item(s) were NOT restored.",
                     "[lcsas-restore] 2 item(s) could NOT be restored. See x"),
        failures=_FINISHED,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED, says=_SH_FINISHED,
                         not_says=r"\[tier 3\] FAILED|stopped before it finished",
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="not-all-restored"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        # No "N item(s) were NOT restored" line: N comes from the list.
        name="tier 3 finished, some items not restored",
        tier="3", reader_exit=ReaderExit.NOT_ALL_RESTORED,
        reader_says=("[lcsas-restore] some item(s) could NOT be restored.",),
        failures=_FINISHED,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED, says=_SH_FINISHED,
                         not_says=r"\[tier 3\] FAILED|stopped before it finished",
                         resume_advice=False, tools_kept=False,
                         another_set=True, log="not-all-restored"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        # The same as tier 1's: the Python reader writes the same reason.
        name="tier 3 finished, a pack not found",
        tier="3", reader_exit=ReaderExit.NOT_ALL_RESTORED,
        reader_says=_NOT_ALL_SAID[:1], failures=_NOT_FOUND,
        sh=ScriptOutcome(exit=ScriptExit.NOT_ALL_RESTORED,
                         says=_SH_FINISHED + _MISSING_DISC
                              + r"[\s\S]*    sh \S*/tools/resume\.sh\n",
                         not_says=rf"\[tier 3\] FAILED|{_SH_FAILED_LINE}|"
                                  r"stopped before it finished",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="not-all-restored"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        name="tier 3 disk full",
        tier="3", reader_exit=ReaderExit.DISK_FULL,
        sh=ScriptOutcome(exit=ScriptExit.DISK_FULL,
                         says=r"\[tier 3\] the disk is full\.",
                         not_says=r"\[tier 3\] FAILED",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="disk-full"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        name="tier 3 q at the disc prompt",
        tier="3", reader_exit=ReaderExit.READER_QUIT,
        sh=ScriptOutcome(exit=ScriptExit.READER_QUIT,
                         says=r"\[tier 3\] stopped at the disc prompt at your request\.",
                         not_says=r"\[tier 3\] FAILED",
                         resume_advice=True, tools_kept=True,
                         another_set=False, log="stopped"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    ScriptCase(
        name="tier 3 wrong password",
        tier="3", reader_exit=ReaderExit.WRONG_PASSWORD, interactive=False,
        reader_says=("[lcsas-restore] wrong password: (1 key file(s) rejected it)",),
        sh=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                         says=r"\[tier 3\] wrong password",
                         not_says=r"\[tier 3\] FAILED",
                         tools_kept=True, another_set=False,
                         log="wrong-password"),
        bat=None, bat_why="restore.bat runs no tier 3",
    ),
    # ── Tiers 2 and 2b: rustic and restic ───────────────────────────
    ScriptCase(
        name="tier 2 rustic says wrong password",
        tier="2", reader_exit=1, data=True, interactive=False,
        reader_says=("error: The password that has been entered, seems to be "
                     "incorrect. No suitable key found for the given password.",),
        sh=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                         says=r"\[tier 2\] wrong password",
                         tools_kept=True, another_set=False,
                         log="wrong-password"),
        bat=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                          says=r"could not decrypt the repository with that password",
                          tools_kept=True, another_set=False),
        wine_gap=("restore.bat finds rustic's words with `... snapshots 2>&1 "
                  "<nul | findstr` inside the tier-2 ( ) block; wine's cmd does "
                  "not send the program's stderr into that pipe there (it does "
                  "at top level), so the words are not seen and the run ends "
                  "with exit 1.  windows-e2e does not run this case."),
    ),
    ScriptCase(
        name="tier 2b restic says wrong password (exit 12)",
        tier="2b", reader_exit=12, data=True, fallback=True, interactive=False,
        reader_says=("Fatal: wrong password or no key found",),
        sh=ScriptOutcome(exit=ScriptExit.WRONG_PASSWORD,
                         says=r"\[tier 2b\] wrong password",
                         not_says=r"falling through",
                         tools_kept=True, another_set=False,
                         log="wrong-password"),
        bat=None, bat_why="restore.bat runs no tier 2b",
    ),
    # ── The script's own codes ──────────────────────────────────────
    ScriptCase(
        name="no program here can restore the set",
        tier=None,
        sh=ScriptOutcome(exit=ScriptExit.NO_RECOVERY_BIN,
                         says=r"Exit code 64 = no_recovery_binary_available\.",
                         resume_advice=False),
        bat=ScriptOutcome(exit=ScriptExit.NO_RECOVERY_BIN,
                          says=r"ERROR: the restore program is missing:",
                          resume_advice=False, tools_kept=False),
    ),
    ScriptCase(
        name="no at the overwrite question",
        tier="1", reader_exit=ReaderExit.OK, target_taken=True,
        reader_says=(NEXT_TIER_RAN,),
        sh=ScriptOutcome(exit=ScriptExit.USER_ABORT,
                         says=r"refusing to overwrite",
                         not_says=NEXT_TIER_RAN,
                         resume_advice=False, another_set=False),
        bat=ScriptOutcome(exit=ScriptExit.USER_ABORT,
                          says=r"Aborting; nothing was written\.",
                          not_says=NEXT_TIER_RAN,
                          resume_advice=False, tools_kept=False,
                          another_set=False),
    ),
    ScriptCase(
        name="usage error",
        tier="1", usage_error=True, reader_says=(NEXT_TIER_RAN,),
        sh=ScriptOutcome(exit=ScriptExit.USAGE,
                         says=r"restore\.sh: unknown flag '--bogus'",
                         not_says=NEXT_TIER_RAN),
        bat=ScriptOutcome(exit=ScriptExit.USAGE,
                          says=r"ERROR: --snapshot needs a snapshot id",
                          not_says=NEXT_TIER_RAN),
    ),
    ScriptCase(
        # lcsas-ecc verify exits 2 for that; the scripts say 5, since 2
        # is their usage error (owner, #887).
        name="check-disc: the image has no ECC data",
        tier=None, check_disc=2,
        sh=ScriptOutcome(exit=ScriptExit.NO_ECC,
                         says=r"no RS03 ECC parity layer; nothing to repair\."),
        bat=ScriptOutcome(exit=ScriptExit.NO_ECC,
                          says=r"no RS03 ECC parity layer; nothing to repair\."),
    ),
)


# ── Comparing ────────────────────────────────────────────────────────


def mismatches(want: ScriptOutcome, got: ScriptOutcome) -> list[str]:
    """What in ``got`` differs from what ``want`` states."""
    out: list[str] = []
    if got.exit != want.exit:
        out.append(f"exit {got.exit}, want {want.exit}")
    if want.says and not re.search(want.says, got.output):
        out.append(f"does not say /{want.says}/")
    if want.not_says and re.search(want.not_says, got.output):
        out.append(f"says /{want.not_says}/ and must not")
    for name in ("resume_advice", "tools_kept", "another_set", "log"):
        w = getattr(want, name)
        g = getattr(got, name)
        if w is not None and w != g:
            out.append(f"{name} {g!r}, want {w!r}")
    return out


# ── restore.sh: shell stubs ─────────────────────────────────────────

# Sets $target from the readers' flags.
_SH_ARGS = """\
target=""
while [ $# -gt 0 ]; do
    case "$1" in --target) target="$2"; shift ;; esac
    shift
done
"""


def _sh_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _sh_reader_body(case: ScriptCase) -> str:
    """The restore call of a stub reader: write the rows, say the lines,
    exit with the case's code."""
    lines = []
    if case.failures:
        rows = FAILURES_HEADER + "".join(f"{p}\t{r}\t\n" for p, r in case.failures)
        lines.append(f"[ -z \"$target\" ] || printf '%s' '{rows}' "
                     f"> \"$target/{FAILURES_NAME}\"")
    for said in case.reader_says:
        lines.append(f"printf '%s\\n' '{said}' >&2")
    lines.append(f"exit {case.reader_exit}")
    return "\n".join(lines) + "\n"


def _sh_fixture(case: ScriptCase, work: Path) -> tuple[Path, Path]:
    """The recovery tree and the PATH folder of stubs for ``case``."""
    recovery = work / "recovery"
    bindir = recovery / "bin" / SH_TARGET
    bindir.mkdir(parents=True)
    for name in (SET, OTHER_SET):
        repo = recovery / "metadata" / name
        (repo / "keys").mkdir(parents=True)
        (repo / "index").mkdir()
        if case.data:
            (repo / "data").mkdir()
    stubs = work / "stubbin"
    stubs.mkdir()
    (work / "standalone_restorer.py").write_text("# stub\n")
    # No host python unless the case is tier 3: tier 3 would otherwise
    # be the fall-back of a case that has no tier at all.
    for name in ("python3", "python"):
        _sh_stub(stubs / name, "exit 127\n")
    body = _sh_reader_body(case)
    if case.tier == "1":
        _sh_stub(bindir / "lcsas-restore",
                 'case "$1" in --help) exit 0 ;; esac\n'
                 'for a in "$@"; do case "$a" in --list-*) exit 0 ;; esac; done\n'
                 + _SH_ARGS + body)
    elif case.tier == "3":
        _sh_stub(stubs / "python3",
                 'case "$1" in -c) exit 0 ;; esac\n' + _SH_ARGS + body)
    elif case.tier == "2":
        # rustic-static: --help loads; any other call says the words and
        # exits (rejects_password asks it again with `snapshots`).
        _sh_stub(bindir / "rustic-static",
                 'case "$1" in --help|-V|--version) exit 0 ;; esac\n' + body)
    elif case.tier == "2b":
        _sh_stub(bindir / "restic",
                 'case "$1" in --help|version) exit 0 ;; esac\n' + body)
    if case.next_tier:
        _sh_stub(bindir / "rustic-static",
                 'case "$1" in --help|-V|--version) exit 0 ;; esac\n'
                 f'case " $* " in *" restore "*) echo {NEXT_TIER_RAN} >&2 ;; esac\n'
                 "exit 0\n")
    return recovery, stubs


def _sh_ecc_stub(recovery: Path, rc: int) -> None:
    _sh_stub(recovery / "bin" / SH_TARGET / "lcsas-ecc",
             f'echo "ECC-CALL: $1" >&2\nexit {rc}\n')


def run_restore_sh(case: ScriptCase, work: Path) -> ScriptOutcome:
    """Run restore.sh on ``case`` (stub readers) in ``work``."""
    work.mkdir(parents=True, exist_ok=True)
    recovery, stubs = _sh_fixture(case, work)
    home = work / "home"
    home.mkdir()
    tools = work / "tools"
    target = work / "restored"
    if case.target_taken:
        target.mkdir()
        (target / "my-own-file.txt").write_text("mine\n")
    env = {
        **os.environ,
        # /usr/local/bin (a host restic/rustic) is left out: a stock
        # tool would be tier 2b.
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(home),
        "LCSAS_TOOLS_DIR": str(tools),
        "LCSAS_TARGET": SH_TARGET,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_NO_RELOCATE": "1",
        # On, so "another set?" can be asked (it is asked only when
        # someone can answer); its answer is the end of the input.
        "LCSAS_INTERACTIVE": "on" if case.interactive else "off",
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        "LCSAS_REPO": SET,
        "LCSAS_PASSWORD": PASSWORD,
        "LCSAS_TIER_FALLBACK": "1" if case.fallback else "0",
    }
    for name in ("LCSAS_PWFILE", "LCSAS_SHARES_DIR", "LCSAS_ANOTHER_SET",
                 "LCSAS_FORCE_NONEMPTY_TARGET", "LCSAS_RESUME",
                 "LCSAS_ANOTHER_PASS", "LCSAS_PACK_CACHE_DIR"):
        env.pop(name, None)
    if case.check_disc is not None:
        _sh_ecc_stub(recovery, case.check_disc)
        img = work / "disc.iso"
        img.write_bytes(b"\0" * 2048)
        argv = ["--check-disc", str(img), str(recovery)]
    elif case.usage_error:
        argv = ["--bogus", str(recovery), str(target)]
    else:
        argv = [str(recovery), str(target), "latest"]
    # Not a terminal: the overwrite question is then refused, not asked.
    res = subprocess.run(["sh", str(RESTORE_SH), *argv], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, env=env, timeout=60)
    log = home / ".lcsas-restore-log"
    result = None
    if log.exists():
        last = log.read_text().splitlines()[-1:]
        m = re.search(r"result=(\S+)", last[0]) if last else None
        result = m.group(1) if m else None
    output = res.stdout + res.stderr
    return ScriptOutcome(
        exit=res.returncode,
        resume_advice="resume.sh" in output,
        tools_kept=tools.exists(),
        another_set=_ANOTHER in output,
        log=result,
        output=output,
    )


# ── restore.bat: a stub .exe under wine ─────────────────────────────

#: One stub for every program restore.bat runs (lcsas-restore.exe,
#: rustic-static.exe, lcsas-ecc.exe).  ``--help`` and ``--list-*`` (the
#: password check, the size of the set) exit 0.  Any other call prints
#: <name>.say, writes <name>.failures to <--target>\RESTORE_FAILURES.txt
#: and exits with the number in <name>.rc (files beside the .exe).
STUB_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

static void copy_to(FILE *in, FILE *out) {
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, in)) > 0) fwrite(buf, 1, n, out);
}

int main(int argc, char **argv) {
    char self[MAX_PATH], path[MAX_PATH + 32], out[2 * MAX_PATH];
    const char *target = NULL;
    char *dot;
    int i, rc = 0;
    FILE *f, *o;
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--help") || !strncmp(argv[i], "--list-", 7))
            return 0;
        if (!strcmp(argv[i], "--target") && i + 1 < argc) target = argv[i + 1];
    }
    if (!GetModuleFileNameA(NULL, self, MAX_PATH)) return 99;
    dot = strrchr(self, '.');
    if (dot) *dot = '\0';
    sprintf(path, "%s.rc", self);
    if ((f = fopen(path, "r")) != NULL) {
        if (fscanf(f, "%d", &rc) != 1) rc = 98;
        fclose(f);
    }
    sprintf(path, "%s.say", self);
    if ((f = fopen(path, "rb")) != NULL) { copy_to(f, stderr); fclose(f); }
    sprintf(path, "%s.failures", self);
    if (target && (f = fopen(path, "rb")) != NULL) {
        sprintf(out, "%s\\RESTORE_FAILURES.txt", target);
        if ((o = fopen(out, "wb")) != NULL) { copy_to(f, o); fclose(o); }
        fclose(f);
    }
    return rc;
}
"""


class NoStub(Exception):  # noqa: N818 - a reason, raised as pytest.skip
    """No Windows C compiler here to build the stub."""


def build_stub_exe(dest: Path) -> Path:
    """Compile :data:`STUB_C` to ``dest`` (a Windows .exe) with mingw or
    ``zig cc``; :class:`NoStub` when neither is there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = dest.with_suffix(".c")
    src.write_text(STUB_C)
    tried = []
    for cmd in (["x86_64-w64-mingw32-gcc", "-O1", "-o", str(dest), str(src)],
                ["zig", "cc", "-target", "x86_64-windows-gnu", "-O1",
                 "-o", str(dest), str(src)]):
        if not shutil.which(cmd[0]):
            continue
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                             env={**os.environ,
                                  "ZIG_GLOBAL_CACHE_DIR": str(dest.parent / "zig-cache"),
                                  "ZIG_LOCAL_CACHE_DIR": str(dest.parent / "zig-cache")})
        if res.returncode == 0 and dest.is_file():
            return dest
        tried.append(f"{cmd[0]}: {res.stderr.strip()[:300]}")
    raise NoStub("no Windows C compiler built the stub .exe "
                 "(x86_64-w64-mingw32-gcc or zig): " + ("; ".join(tried) or "none found"))


@dataclass
class WineRig:
    """A wine prefix and the stub .exe, shared by every bat case."""

    prefix: Path
    env: dict[str, str]
    stub: Path
    runs: int = 0


def _bat_place(stub: Path, dest: Path, rc: int, says: tuple[str, ...] = (),
               failures: tuple[tuple[str, str], ...] = ()) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stub, dest)
    base = dest.with_suffix("")
    base.with_suffix(".rc").write_text(f"{rc}\n")
    if says:
        base.with_suffix(".say").write_bytes(
            "".join(s + "\r\n" for s in says).encode())
    if failures:
        rows = FAILURES_HEADER + "".join(f"{p}\t{r}\t\n" for p, r in failures)
        base.with_suffix(".failures").write_bytes(rows.replace("\n", "\r\n").encode())


def _bat_fixture(case: ScriptCase, meta: Path, stub: Path) -> None:
    """A META disc for ``case``: restore.bat, two sets, the stubs."""
    scripts = meta / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    bindir = meta / "recovery" / "bin" / BAT_TARGET
    bindir.mkdir(parents=True)
    for name in (SET, OTHER_SET):
        repo = meta / "metadata" / name
        (repo / "keys").mkdir(parents=True)
        (repo / "index").mkdir()
        if case.data:
            (repo / "data").mkdir()
    if case.tier == "1":
        _bat_place(stub, bindir / "lcsas-restore.exe", case.reader_exit,
                   case.reader_says, case.failures)
    elif case.tier == "2":
        _bat_place(stub, bindir / "rustic-static.exe", case.reader_exit,
                   case.reader_says)
    if case.next_tier:
        _bat_place(stub, bindir / "rustic-static.exe", 0, (NEXT_TIER_RAN,))
    if case.check_disc is not None:
        _bat_place(stub, bindir / "lcsas-ecc.exe", case.check_disc)
        (meta / "disc.iso").write_bytes(b"\0" * 2048)
    if case.target_taken:
        (meta / "restored").mkdir()
        (meta / "restored" / "my-own-file.txt").write_text("mine\n")


def run_restore_bat(case: ScriptCase, work: Path, rig: WineRig) -> ScriptOutcome:
    """Run restore.bat under wine on ``case`` (stub .exe programs).

    The META disc is ``work/meta`` as drive E:, the folder restored into
    E:\\restored, the tools folder C:\\lcsas-tools-N (one per run).  The
    run does not relocate (LCSAS_NO_RELOCATE=1): wine cannot end a batch
    at ``(goto)``, so the relocated copy cannot run here (see the
    ``:relaunch`` comment in restore.bat); the tools folder is then
    TOOLS\\in-place, kept or removed by the same branches.
    """
    from tests.recovery_hardening.test_restore_bat_wine_smoke import _map_drive

    meta = work / "meta"
    _bat_fixture(case, meta, rig.stub)
    _map_drive(rig.prefix, "e", meta)
    rig.runs += 1
    tools_name = f"lcsas-tools-{rig.runs}"
    tools = rig.prefix / "drive_c" / tools_name
    env = {
        **rig.env,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_TARGET": BAT_TARGET,
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        "LCSAS_TOOLS_DIR": f"C:\\{tools_name}",
        "LCSAS_REPO": SET,
        "LCSAS_PW": PASSWORD,
        "LCSAS_INTERACTIVE": "off",
        "LCSAS_SKIP_SPACE_CHECK": "1",
    }
    for name in ("LCSAS_PWFILE", "LCSAS_PASSWORD", "LCSAS_SHARES_DIR",
                 "LCSAS_ANOTHER_SET", "LCSAS_FORCE_NONEMPTY_TARGET",
                 "LCSAS_PACK_CACHE_DIR", "LCSAS_TIER_FALLBACK"):
        env.pop(name, None)
    bat = "E:\\recovery\\scripts\\restore.bat"
    if case.check_disc is not None:
        cmdline = f"{bat} --check-disc E:\\disc.iso"
    elif case.usage_error:
        cmdline = f"{bat} --snapshot not-an-id"
    else:
        cmdline = bat
    # The folder question only: wine's `set /p` reads all of a piped
    # input at once, so one line is all a pipe can answer.  The end of
    # the input then answers the rest: no at the overwrite warning and
    # at "another set?", and each pause goes on.
    answers = "E:\\restored\n"
    res = subprocess.run(["wine", "cmd", "/c", cmdline], input=answers,
                         env=env, capture_output=True, text=True,
                         errors="replace", timeout=180)
    output = res.stdout + res.stderr
    return ScriptOutcome(
        exit=res.returncode,
        resume_advice=re.search(_BAT_CARRY_ON, output) is not None,
        tools_kept=tools.exists(),
        another_set=_ANOTHER in output,
        log=None,
        output=output,
    )
