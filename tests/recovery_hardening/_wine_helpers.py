"""Shared wine teardown for the recovery-hardening wine tests (#453).

Each wine test boots a throwaway WINEPREFIX, and wine starts one
``wineserver`` per prefix.  Nothing stopped it: pytest deleted the prefix
directory while its server was still shutting down, and the next test
booted a fresh prefix next to it.  In a full-tier run that showed up as
intermittent setup errors and ``wineboot`` overrunning its 90 s guard,
while every test passed in isolation.

``stop_wineserver`` asks the server for ONE prefix to exit
(``wineserver -k``) and waits for it (``wineserver -w``).  Both talk to
that prefix's own server through its socket, so they never touch another
prefix, another user's wine, or anything matched by name.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path


def stop_wineserver(prefix: Path | str, timeout: int = 20) -> None:
    """Stop the wineserver of ``prefix`` and wait for it; best effort."""
    if not shutil.which("wineserver") or not Path(prefix).is_dir():
        return
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all"}
    for flag in ("-k", "-w"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["wineserver", flag], env=env,
                           capture_output=True, timeout=timeout)
