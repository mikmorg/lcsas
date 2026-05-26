#!/usr/bin/env python3
"""no_bypass_check.py - blind-restore verify check #13.

Scans a transcript.jsonl emitted by the agent harness for shell
commands that bypass restore.sh and invoke a recovery binary
directly.

Bypass shapes flagged:

    rustic ...
    rustic-static ...
    lcsas-restore ...
    restic ...
    standalone_restorer.py ...
    python3 .../standalone_restorer.py ...
    python -m standalone_restorer ...

Wrapper prefixes stripped before checking argv[0]:

    sudo, sh, bash, exec, python, python3, python3.12, ...

If the agent invoked python (any flavor) with a `-m <module>` form,
the `-m` flag and module name are folded together so the module
name itself is examined as argv[0].

Probing invocations (`--version` / `--help` / `-V` / `-h` /
`version` / `help`) are exempt - those just sniff the binary, they
don't drive a restore through it.

Exit codes:
    0 - no bypass detected
    1 - at least one bypass found (prints up to 5 to stderr)
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# Binary names (without path) that are considered recovery drivers.
# argv[0] (after stripping wrappers) is matched against this set
# both as-given and with a trailing `.py` stripped, so
# `standalone_restorer` and `standalone_restorer.py` both hit.
BINARIES = {
    'rustic',
    'rustic-static',
    'lcsas-restore',
    'standalone_restorer.py',
    'standalone_restorer',
    'restic',
}

CLAUSE_SPLIT = re.compile(r'\s*(?:&&|\|\||;|\||&)\s*')

# Tokens that wrap another command and should be peeled off before
# inspecting argv[0].  `python` matches `python`, `python3`,
# `python3.12`, etc.  `sh`/`bash`/`exec`/`sudo` are the classic
# wrappers.
WRAPPER_RE = re.compile(r'^(?:sudo|sh|bash|exec|python(?:[0-9.]*)?)$')

# Probing flags that are not a real restore.
PROBE_FLAGS = {
    '--version', '-V', '--help', '-h', 'version', 'help',
}


def _strip_wrappers(tokens: list[str]) -> int:
    """Advance past wrapper tokens and their flags.  Returns the
    index of the first non-wrapper token (argv[0] of the actual
    command).

    For python wrappers, `-m <module>` is consumed by promoting the
    module name into the next position so argv[0] becomes the
    module itself (i.e. `python3 -m standalone_restorer` => argv[0]
    == 'standalone_restorer').
    """
    i = 0
    n = len(tokens)
    while i < n and WRAPPER_RE.match(tokens[i]):
        is_python = tokens[i].startswith('python')
        i += 1
        # For python wrappers, watch specifically for `-m <module>`
        # and rewrite the token stream so argv[0] becomes the
        # module name.  Other flags (`-u`, `-O`, `-E`, ...) are
        # skipped along with their values where needed.
        if is_python:
            while i < n and tokens[i].startswith('-'):
                if tokens[i] == '-m' and i + 1 < n:
                    # Promote module name to argv[0] and return.
                    return i + 1
                # Skip standalone flag (e.g. `-u`, `-O`).  We do
                # not attempt to model every option that takes a
                # value; the common bypass shapes do not use them.
                i += 1
            # Non-python wrappers also accept short flags.
        else:
            while i < n and tokens[i].startswith('-'):
                i += 1
    return i


def _argv0_matches_binary(argv0: str) -> bool:
    name = argv0.rsplit('/', 1)[-1]
    if name in BINARIES:
        return True
    # Also catch `standalone_restorer` (no .py suffix) reaching the
    # BINARIES set as `standalone_restorer.py`.
    return name + '.py' in BINARIES


def scan_command(cmd: str) -> list[str]:
    """Return a list of bypass-shape descriptions for one command
    string.  Empty list means no bypass.
    """
    hits: list[str] = []
    for clause in CLAUSE_SPLIT.split(cmd):
        try:
            tokens = shlex.split(clause, posix=True)
        except ValueError:
            tokens = clause.split()
        if not tokens:
            continue
        i = _strip_wrappers(tokens)
        if i >= len(tokens):
            continue
        argv0 = tokens[i]
        if not _argv0_matches_binary(argv0):
            continue
        rest = tokens[i + 1:]
        # Probing invocations (`--version`, `--help`, ...) are not
        # a bypass.
        if rest and rest[0] in PROBE_FLAGS:
            continue
        name = argv0.rsplit('/', 1)[-1]
        hits.append(f'{name} {" ".join(rest[:3])}'.strip())
    return hits


def scan_transcript(path: str) -> list[str]:
    """Walk a transcript.jsonl and return all bypass hits across
    every tool_use command.
    """
    hits: list[str] = []
    with open(path) as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            content = d.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue
            for c in content:
                if c.get('type') != 'tool_use':
                    continue
                cmd = c.get('input', {}).get('command', '')
                hits.extend(scan_command(cmd))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('usage: no_bypass_check.py <transcript.jsonl>',
              file=sys.stderr)
        return 64
    hits = scan_transcript(argv[1])
    if hits:
        print('AGENT BYPASSED restore.sh to call binary directly:',
              hits[:5], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
