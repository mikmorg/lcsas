"""The readers' exit codes, named once for the restore contract (#886).

A module of its own so a test can name a code without importing the
case table (which builds trees and needs POSIX).
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

    Their headers and the EXIT CODES tables of ``RECOVER.txt`` and
    ``RECOVER_WINDOWS.txt`` state them; ``script_contract.py`` runs them.
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
