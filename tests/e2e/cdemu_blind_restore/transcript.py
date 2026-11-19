"""The agent's transcript, read once (#889).

The blind-restore gate scores a run by reading the agent's transcript
(``transcript.jsonl``, the CLI's stream-json output: one JSON event per
line).  This module is the only harness code that parses that file.
Every check and report asks it for what it needs:

    t = transcript.load(path)
    for call in t.bash():           # each shell command the agent ran
        ...
    for clause in t.clauses():      # each command split into clauses
        clause.words, clause.call
    t.texts                         # what the agent said
    t.cost_usd(), t.turns()         # from the CLI's 'result' events
    t.events                        # every event, for the timeline

``load`` never raises on a malformed line or an odd event shape: a line
that is not JSON, an event that is not an object, a ``message`` that is
not an object (the ``permission_denied`` event of #878 carries a string),
a content block that is not an object, or a tool call whose ``input`` is
``None`` is passed over, not fatal.  A file that cannot be opened still
raises ``OSError``; each caller decides what a missing transcript means.

``split_clauses`` splits a piece of shell text (a command, or the body of
a heredoc) into the words of each clause.  It is the one clause splitter
of the harness's Python modules.

The harness runs these scripts with the system Python as root against a
run folder, so this file sits beside them (not under ``src/``) and they
import it from their own folder, with no PYTHONPATH.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from os import PathLike
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One tool call by the agent (a ``tool_use`` block), in order.

    ``command`` is the call's ``input.command`` when that is a string,
    else "": Bash calls carry one, and so does any other tool that runs a
    shell command (Monitor).  ``description`` likewise; ``timestamp`` is
    the event's, when it has one."""

    index: int
    tool: str
    command: str
    description: str
    timestamp: str | None


@dataclass(frozen=True)
class Clause:
    """One clause of a call's command: the command word and its
    arguments (see ``split_clauses``), and the call it came from."""

    words: list[str]
    call: ToolCall


@dataclass(frozen=True)
class Transcript:
    calls: list[ToolCall]          # every tool call, in order
    texts: list[str]               # what the agent said (assistant text)
    results: list[dict[str, Any]]  # the 'result' events, in order
    events: list[dict[str, Any]]   # every event that is a JSON object

    def bash(self) -> Iterator[ToolCall]:
        """The calls that ran a shell command, in order: every call with a
        non-empty ``command`` (Bash, and Monitor, which also runs one)."""
        return (call for call in self.calls if call.command)

    def clauses(self) -> Iterator[Clause]:
        """Each shell command split once into clauses, in order."""
        for call in self.bash():
            for words in split_clauses(call.command):
                yield Clause(words, call)

    def cost_usd(self) -> float | None:
        """What the run cost.  ``total_cost_usd`` is a running total (each
        'result' event carries the cost so far), so the run's cost is the
        last one given, not a sum."""
        for event in reversed(self.results):
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                return cost
        return None

    def turns(self) -> int | None:
        """The run's turn count: the most ``num_turns`` of any 'result'
        event.  The CLI counts turns afresh each time it wakes for a
        background task, so the last event can say 1 after 30 calls."""
        counts = [
            event["num_turns"] for event in self.results
            if isinstance(event.get("num_turns"), int)
            and not isinstance(event.get("num_turns"), bool)
        ]
        return max(counts) if counts else None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def load(path: str | PathLike[str]) -> Transcript:
    """Read a transcript.jsonl.  Raises OSError only (no such file, no
    permission); anything inside the file that is not the expected shape
    is passed over."""
    calls: list[ToolCall] = []
    texts: list[str] = []
    results: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if event.get("type") == "result":
                results.append(event)
            msg = event.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            stamp = event.get("timestamp")
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    inp = block.get("input")
                    if not isinstance(inp, dict):
                        inp = {}
                    calls.append(ToolCall(
                        index=len(calls),
                        tool=_str(block.get("name")),
                        command=_str(inp.get("command")),
                        description=_str(inp.get("description")),
                        timestamp=stamp if isinstance(stamp, str) else None,
                    ))
                elif kind == "text" and event.get("type") == "assistant":
                    text = block.get("text")
                    if isinstance(text, str):
                        texts.append(text)
    return Transcript(calls, texts, results, events)


# --- splitting shell text into clauses -----------------------------------

# Shell words that open a command or a block; the next word is the
# command.  `for` and `case` headers name no command and are skipped.
_LEADING_KEYWORDS = frozenset({
    "if", "then", "else", "elif", "do", "while", "until", "!", "{", "}",
    "time",
})
_IGNORED_KEYWORDS = frozenset({"fi", "done", "esac", "in"})
_HEADER_KEYWORDS = frozenset({"for", "case", "select"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=")

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


def blank(text: str) -> str:
    """*text* with quoted strings, ${...} and $((...)) replaced by
    placeholders, so the shell operators inside them do not split it.
    An empty string stays "" so a send of ENTER can be told apart."""
    text = _blank_quotes(text)
    text = re.sub(r"\$\{[^}]*\}", "V", text)
    text = re.sub(r"\$\(\((?:[^()]|\([^()]*\))*\)\)", "A", text)
    # An array written out: NAME=(word word ...).
    text = re.sub(r"\b([A-Za-z_]\w*)=\([^()$`]*\)", r"\1=A", text)
    return text


def clause_texts(text: str) -> Iterator[str]:
    """The clauses of *text* (blanked), split on newlines, ``;``, ``;;``,
    ``&&``, ``||``, ``|``, ``&``, parentheses, ``$(`` and backticks, with
    comments and ``case`` patterns dropped."""
    for line in blank(text).split("\n"):
        line = re.sub(r"(?:^|\s)#.*$", "", line)          # comment, shebang
        line = re.sub(r"^\s*[^\s()]+\)", "", line)        # case pattern)
        yield from re.split(r";;|&&|\|\||\$\(|[;|&()`]", line)


def command_words(clause: str) -> list[str]:
    """The command and its arguments in one clause, or [] if none.
    Leading keywords (``if``, ``then``, ``do``, ...) and variable
    assignments are dropped, and so is ``sudo`` with its flags."""
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


def split_clauses(text: str) -> Iterator[list[str]]:
    """The words of each clause of *text* that runs a command."""
    for clause in clause_texts(text):
        words = command_words(clause)
        if words:
            yield words
