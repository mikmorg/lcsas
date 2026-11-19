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
    FAILED = 1               # a damaged disc, a missing pack: run again
    USAGE = 2                # a usage error (argparse's 2 in tier 3)
    NOT_ALL_RESTORED = 4     # finished; items listed in RESTORE_FAILURES.txt
    DISK_FULL = 74           # the target file system is full
    READER_QUIT = 75         # 'q' (or end of input) at the disc prompt
    WRONG_PASSWORD = 77      # a key file rejected the password
