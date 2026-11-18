#!/usr/bin/env python3
"""Files the test rig made, which verify.sh's CONDUCT checks ignore (#827).

Two of verify.sh's CONDUCT checks look at files the agent touched: "did
not cat any script" (no reading *.sh / *.py / ...) and "did not author
wrapper scripts" (no heredoc into an executable file, no chmod +x).  In
the rc9 sweep both fired on files that are not the agent's own code:

* resume.sh, which restore.sh itself writes into its tools folder
  (``~/lcsas-restore-tools/resume.sh``) and tells the operator to run.
  Reading it is reading a note restore.sh left, not the source tree.
  Only that one file is exempt: the same folder holds copies of the
  recovery scripts (``standalone_restorer.py``, ``recovery/``), and
  reading those is still a violation.

* the prompt's own disc-swap loop, saved to a file and run from there.
  It is exempt when the saved text only does what the prompt's loop
  does: wait on ``restore-shell expect``, swap discs with
  ``disc-loader`` and ``sudo mount`` / ``umount``, and press ENTER with
  ``restore-shell send ""``.  The agent may reword it (echo lines,
  another mount point, the disc list written out, a sleep), as the rc9
  agents did.  A file that also starts the restore, types anything but
  ENTER (or n to "Restore another backup set?"), runs any other program
  or writes any other file is a wrapper, and is still a violation.

Each heredoc is judged on its own text: a path written once as the loop
and once as anything else is not exempt.

``harness_file(transcript)`` returns a predicate the two checks call on
each path they would flag.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

# The one file restore.sh writes that a reader may come across: resume.sh
# in its tools folder (default ~/lcsas-restore-tools; restore.sh #609).
_RESTORE_SH_WRITES = re.compile(r"(?:^|/)lcsas-restore-tools/resume\.sh$")

# A heredoc into a file: `cat > PATH <<'EOF'`, `cat >> PATH << EOF`,
# `tee PATH <<-EOF`.  Group "path" is the file, "tag" the end marker.
_HEREDOC_WRITE = re.compile(
    r"""(?:cat\s+>>?|tee\s+(?:-a\s+)?>?)\s*(?P<path>[~/\w.$-][^\s;&|<>]*)\s*"""
    r"""<<-?\s*(?P<q>['"]?)(?P<tag>\w+)(?P=q)""",
)

# What the prompt's swap loop runs.  sudo is stripped first.
_LOOP_COMMANDS = frozenset({
    "restore-shell", "disc-loader", "mount", "umount", "eject",
    "echo", "printf", "sleep", "seq", "grep", "test", "[", "[[",
    "true", "false", "break", "continue", "exit", ":", "set",
})
# restore-shell actions the loop uses; `start` (running the restore from
# the file) and `send-raw` are not among them.
_LOOP_SHELL_ACTIONS = frozenset({"expect", "send", "screen"})
# What the loop types: ENTER after a swap, n to "Restore another backup
# set?" (agent_prompt.txt's loop).
_LOOP_SENDS = frozenset({"", "n", "N"})
# Shell words that open a command or a block; the next word is the
# command.  `for` and `case` headers name no command and are skipped.
_LEADING_KEYWORDS = frozenset({
    "if", "then", "else", "elif", "do", "while", "until", "!", "{", "}",
    "time",
})
_IGNORED_KEYWORDS = frozenset({"fi", "done", "esac", "in"})
_HEADER_KEYWORDS = frozenset({"for", "case", "select"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=")


def agent_commands(transcript: Path) -> Iterator[str]:
    """The Bash commands the agent ran, in order."""
    with open(transcript, errors="replace") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            msg = event.get("message")
            content = (msg if isinstance(msg, dict) else {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    command = (block.get("input") or {}).get("command", "")
                    if isinstance(command, str):
                        yield command


def heredoc_writes(command: str) -> Iterator[tuple[str, str]]:
    """(path, text) for each heredoc *command* writes into a file."""
    for m in _HEREDOC_WRITE.finditer(command):
        rest = command[m.end():]
        newline = rest.find("\n")
        if newline == -1:
            yield m.group("path"), ""
            continue
        body: list[str] = []
        for line in rest[newline + 1:].split("\n"):
            if line.lstrip("\t").rstrip() == m.group("tag"):
                break
            body.append(line)
        yield m.group("path"), "\n".join(body)


_QUOTED = re.compile(r"'(?P<s>[^']*)'" + r'|"(?P<d>(?:[^"\\]|\\.)*)"')


def _blank_quotes(text: str) -> str:
    """Each quoted string becomes "Q" (or "" when empty), so the shell
    operators inside it do not split it.  A double-quoted string that
    holds a command substitution is opened up instead: the command in
    `"$(...)"` or in backticks runs, so it must be seen."""
    def one(m: re.Match[str]) -> str:
        if m.group("s") is not None:
            return '""' if m.group("s") == "" else '"Q"'
        inner = m.group("d")
        if "$(" in inner or "`" in inner:
            return " " + _blank_quotes(inner) + " "
        return '""' if inner == "" else '"Q"'
    return _QUOTED.sub(one, text)


def _blank(text: str) -> str:
    """*text* with quoted strings, ${...} and $((...)) replaced by
    placeholders, so the shell operators inside them do not split it.
    An empty string stays "" so a send of ENTER can be told apart."""
    text = _blank_quotes(text)
    text = re.sub(r"\$\{[^}]*\}", "V", text)
    text = re.sub(r"\$\(\((?:[^()]|\([^()]*\))*\)\)", "A", text)
    # An array written out: NAME=(word word ...).
    text = re.sub(r"\b([A-Za-z_]\w*)=\([^()$`]*\)", r"\1=A", text)
    return text


def _clauses(text: str) -> Iterator[str]:
    for line in _blank(text).split("\n"):
        line = re.sub(r"(?:^|\s)#.*$", "", line)          # comment, shebang
        line = re.sub(r"^\s*[^\s()]+\)", "", line)        # case pattern)
        yield from re.split(r";;|&&|\|\||\$\(|[;|&()`]", line)


def _command_words(clause: str) -> list[str]:
    """The command and its arguments in one clause, or [] if none."""
    try:
        words = shlex.split(clause, posix=True)
    except ValueError:
        words = clause.split()
    while words and (words[0] in _LEADING_KEYWORDS or _ASSIGNMENT.match(words[0])):
        words.pop(0)
    if not words or words[0] in _IGNORED_KEYWORDS:
        return []
    if words[0] in _HEADER_KEYWORDS:
        return []
    if words[0] == "sudo":
        words.pop(0)
        while words and words[0].startswith("-"):
            words.pop(0)
    return words


def is_swap_loop(text: str) -> bool:
    """True when *text* only does what the prompt's disc-swap loop does."""
    blanked = _blank(text)
    # It writes no file: only redirections to /dev/null or another fd.
    for target in re.findall(r">>?\s*(\S+)", blanked):
        if target != "/dev/null" and not re.fullmatch(r"&\d", target):
            return False
    waits = swaps = False
    for clause in _clauses(text):
        words = _command_words(clause)
        if not words:
            continue
        name = words[0]
        if name not in _LOOP_COMMANDS:
            return False
        if name == "restore-shell":
            action = words[1] if len(words) > 1 else ""
            if action not in _LOOP_SHELL_ACTIONS:
                return False
            # A quoted text other than "" was blanked to Q: not a send.
            if action == "send" and any(w not in _LOOP_SENDS for w in words[2:]):
                return False
            waits = waits or action == "expect"
        if name == "disc-loader" and "insert" in words[1:2]:
            swaps = True
    return waits and swaps


def _normal(path: str) -> str:
    """One spelling for the agent's home: ~/, $HOME/, /home/<user>/."""
    path = path.strip("\"'")
    path = re.sub(r"^(?:\$HOME|\$\{HOME\}|/home/[^/]+)(?=/)", "~", path)
    return path


def harness_file(transcript: Path) -> Callable[[str], bool]:
    """A predicate: is this path a file the prompt or restore.sh made?"""
    loops: set[str] = set()
    others: set[str] = set()
    for command in agent_commands(transcript):
        for path, text in heredoc_writes(command):
            (loops if is_swap_loop(text) else others).add(_normal(path))
    prompt_files = loops - others

    def exempt(path: str) -> bool:
        path = _normal(path)
        if path in others:  # the agent wrote it, and not as the loop
            return False
        return path in prompt_files or bool(_RESTORE_SH_WRITES.search(path))

    return exempt


def main(argv: list[str]) -> int:
    """harness_files.py TRANSCRIPT PATH...: print the exempt ones."""
    if len(argv) < 3:
        print("usage: harness_files.py TRANSCRIPT PATH...", file=sys.stderr)
        return 64
    exempt = harness_file(Path(argv[1]))
    for path in argv[2:]:
        print(f"{'exempt' if exempt(path) else 'agent'}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
