"""LCSAS CLI entry point."""

import sys

from lcsas.cli.main import main

if __name__ == "__main__":
    # Propagate the handler's return value as the process exit code. Without
    # this, `python -m lcsas` always exited 0 even when a command failed
    # (e.g. `key combine` with too few shares) — the console-script entry
    # point already does this; `python -m` did not.
    sys.exit(main())
