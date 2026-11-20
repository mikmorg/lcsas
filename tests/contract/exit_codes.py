"""The readers' exit codes, named once for the restore contract (#886).

A module of its own so a test can name a code without importing the
case table (which builds trees and needs POSIX).

Beside each set of codes: the key words a row of an exit-code table in
the recovery docs must contain for that code (#888; ``doc_tables.py``
reads the tables).  Any one of a code's words will do, matched without
regard to case: the docs word each table for its own reader, so
restore.bat's say "disc box" where restore.sh's say "disc prompt".  A
key word catches a row whose meaning moved to another number; it is
not the row's wording.
"""
from __future__ import annotations

import enum


class ReaderExit(enum.IntEnum):
    """A reader's own exit codes; tier 1 and the Python reader share them.

    Tier 1: ``recovery/src/lcsas-restore/main.c``.  Python reader:
    ``src/lcsas/restore/restic_fallback.py`` and the ``sys.exit`` calls
    in ``standalone_builder.py``.  1 and 2 are bare literals in both.
    """

    OK = 0
    FAILED = 1               # the walk stopped early: run again
    USAGE = 2                # a usage error (argparse's 2 in tier 3)
    NOT_ALL_RESTORED = 4     # finished; items listed in RESTORE_FAILURES.txt
    DISK_FULL = 74           # the target file system is full
    READER_QUIT = 75         # 'q' (or end of input) at the disc prompt
    WRONG_PASSWORD = 77      # a key file rejected the password


class ScriptExit(enum.IntEnum):
    """restore.sh's and restore.bat's own exit codes (#887).

    Their headers and the EXIT CODES tables of ``RESTORE.txt`` and
    ``RESTORE_WINDOWS.txt`` state them; ``script_contract.py`` runs them.
    Any other code is the one the tier that ran stopped with.
    """

    OK = 0
    FAILED = 1               # the message above it names the failure
    USAGE = 2                # the script was called wrongly
    CHECK_DISC_ERROR = 3     # --check-disc: no image or no tool, a read error
    NOT_ALL_RESTORED = 4     # finished; items listed in RESTORE_FAILURES.txt
    NO_ECC = 5               # --check-disc: the image has no ECC data (#887)
    NO_RECOVERY_BIN = 64     # no program here can restore the set
    USER_ABORT = 65          # the person stopped at a question the script asked
    DISK_FULL = 74           # the disk restored to is full
    READER_QUIT = 75         # q at a reader's disc prompt
    WRONG_PASSWORD = 77      # a key rejected the password, or q at it


class EccExit(enum.IntEnum):
    """``lcsas-ecc``'s exit codes (``recovery/src/lcsas-ecc/main.c``).

    restore.sh and restore.bat run it for ``--check-disc`` and report
    its 2 as their 5 (#887).
    """

    OK = 0                   # verify: no damage; fix: fully repaired
    DAMAGE = 1               # verify: damage found; fix: some left
    NO_ECC = 2               # no RS03 header: not an augmented image
    USAGE = 3                # a usage or I/O error


class StopExit(enum.IntEnum):
    """128 + the signal: a program stopped from outside.

    restore.sh exits with these from its HUP, INT and TERM traps; the
    Python reader exits 130 on Ctrl-C (``standalone_builder.py``).
    Tier 1 and restore.bat do not choose them, so their tables do not
    list them.
    """

    HANGUP = 129             # the terminal closed
    INTERRUPT = 130          # Ctrl-C
    TERMINATED = 143         # killed with TERM


#: restore.sh's ``--check-disc`` codes, a subset of :class:`ScriptExit`.
CHECK_DISC_EXITS = frozenset({ScriptExit.OK, ScriptExit.FAILED,
                              ScriptExit.CHECK_DISC_ERROR, ScriptExit.NO_ECC})

# ── Key words (#888) ────────────────────────────────────────────────

READER_WORDS: dict[int, tuple[str, ...]] = {
    ReaderExit.OK: ("restored",),
    ReaderExit.FAILED: ("failed",),
    ReaderExit.USAGE: ("usage",),
    ReaderExit.NOT_ALL_RESTORED: ("RESTORE_FAILURES.txt",),
    ReaderExit.DISK_FULL: ("full",),
    ReaderExit.READER_QUIT: ("disc prompt",),
    ReaderExit.WRONG_PASSWORD: ("password",),
}

SCRIPT_WORDS: dict[int, tuple[str, ...]] = {
    ScriptExit.OK: ("success", "finished"),
    ScriptExit.FAILED: ("failure",),
    # restore.bat: an argument it does not take, --repo without a name, a
    # bad --snapshot (#925).
    ScriptExit.USAGE: ("usage", "wrongly", "--snapshot"),
    ScriptExit.CHECK_DISC_ERROR: ("check-disc",),
    ScriptExit.NOT_ALL_RESTORED: ("RESTORE_FAILURES.txt",),
    ScriptExit.NO_ECC: ("no ECC",),
    # restore.bat has one recovery program to miss: lcsas-restore.exe.
    ScriptExit.NO_RECOVERY_BIN: ("no recovery", "missing"),
    ScriptExit.USER_ABORT: ("question",),
    ScriptExit.DISK_FULL: ("full",),
    # restore.bat's disc prompt is a box.
    ScriptExit.READER_QUIT: ("disc prompt", "disc box"),
    ScriptExit.WRONG_PASSWORD: ("password",),
}

#: ``--check-disc``: the codes say what lcsas-ecc found.
CHECK_DISC_WORDS: dict[int, tuple[str, ...]] = {
    ScriptExit.OK: ("clean",),
    ScriptExit.FAILED: ("damage",),
    ScriptExit.CHECK_DISC_ERROR: ("I-O",),
    ScriptExit.NO_ECC: ("no RS03",),
}

ECC_WORDS: dict[int, tuple[str, ...]] = {
    EccExit.OK: ("clean",),
    EccExit.DAMAGE: ("damage",),
    EccExit.NO_ECC: ("RS03",),
    EccExit.USAGE: ("usage",),
}

STOP_WORDS: dict[int, tuple[str, ...]] = {
    StopExit.HANGUP: ("terminal",),
    StopExit.INTERRUPT: ("Ctrl-C",),
    StopExit.TERMINATED: ("kill",),
}
