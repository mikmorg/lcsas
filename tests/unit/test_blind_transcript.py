"""The blind-restore harness's transcript module (#889).

``tests/e2e/cdemu_blind_restore/transcript.py`` is the one place the
harness parses an agent's ``transcript.jsonl``.  In the rc10 sweep one new
event shape (a ``permission_denied`` system event whose ``message`` is a
string) crashed six checks and scored a correct restore 9/16 (#878).
These tests pin that ``load`` passes over every odd shape seen or feared,
and what it returns for each.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
sys.path.insert(0, str(HARNESS))
import transcript  # noqa: E402


def _bash(command: object, *, ts: str | None = "2026-11-18T10:00:00Z",
          description: object = "run it") -> dict:
    event: dict = {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": "Bash",
        "input": {"command": command, "description": description},
    }]}}
    if ts is not None:
        event["timestamp"] = ts
    return event


def _said(text: object) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _result(turns: object = 3, cost: object = 0.25, text: str = "done") -> dict:
    return {"type": "result", "subtype": "success", "num_turns": turns,
            "total_cost_usd": cost, "result": text}


def _write(tmp_path: Path, *lines: object) -> Path:
    """Each line is an event (dumped as JSON) or a str (written as is)."""
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n"
        for line in lines
    ))
    return path


# --- load: odd lines and odd event shapes ----------------------------------

# Each odd line, between two good Bash calls: load skips it and keeps both.
ODD_LINES = {
    "not json": "this is not json {",
    "blank line": "",
    "json array": "[1, 2, 3]",
    "json string": '"a string"',
    "json number": "42",
    "json null": "null",
    "deeply nested": "[" * 100_000 + "]" * 100_000,
    "permission_denied (#878)": {"type": "system", "subtype": "permission_denied",
                                 "message": "Permission to use Read was denied"},
    "message is a list": {"type": "assistant", "message": ["tool_use"]},
    "message is null": {"type": "assistant", "message": None},
    "no message": {"type": "system", "subtype": "init"},
    "content is a string": {"type": "assistant", "message": {"content": "hi"}},
    "content is null": {"type": "assistant", "message": {"content": None}},
    "block is a string": {"type": "assistant", "message": {"content": ["tool_use"]}},
    "block is null": {"type": "assistant", "message": {"content": [None]}},
    "input is null": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": None}]}},
    "input is a list": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": ["ls"]}]}},
    "no input": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "ToolSearch"}]}},
}


@pytest.mark.parametrize("odd", ODD_LINES.values(), ids=ODD_LINES.keys())
def test_load_passes_over_an_odd_line(tmp_path: Path, odd: object) -> None:
    t = transcript.load(_write(tmp_path, _bash("ls /media"), odd, _bash("df -h")))
    assert [c.command for c in t.bash()] == ["ls /media", "df -h"]


def test_a_call_with_no_usable_command_is_a_call_with_no_command(
    tmp_path: Path,
) -> None:
    """input None, a list, a missing input, a non-string command: the call
    is counted (the CLI's tool_calls) but carries no shell command."""
    t = transcript.load(_write(
        tmp_path,
        ODD_LINES["input is null"], ODD_LINES["input is a list"],
        ODD_LINES["no input"], _bash(None), _bash(["ls"]), _bash(""),
    ))
    assert len(t.calls) == 6
    assert [c.command for c in t.calls] == [""] * 6
    assert [c.tool for c in t.calls] == ["Bash", "Bash", "ToolSearch", "Bash", "Bash", "Bash"]
    assert list(t.bash()) == []


def test_a_truncated_last_line_is_dropped(tmp_path: Path) -> None:
    path = _write(tmp_path, _bash("ls"))
    with open(path, "a") as fh:
        fh.write(json.dumps(_bash("rustic restore latest"))[:40])  # no newline
    t = transcript.load(path)
    assert [c.command for c in t.calls] == ["ls"]


def test_bytes_that_are_not_utf8_do_not_stop_the_read(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(
        json.dumps(_bash("ls")).encode() + b"\n"
        + b"\xff\xfe garbage \x80\n"
        + json.dumps(_bash("df")).encode() + b"\n"
    )
    assert [c.command for c in transcript.load(path).calls] == ["ls", "df"]


def test_a_10_mb_tool_result_is_read(tmp_path: Path) -> None:
    big = {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "x", "content": "y" * 10_000_000}]}}
    t = transcript.load(_write(tmp_path, _bash("cat big"), big, _bash("ls")))
    assert [c.command for c in t.calls] == ["cat big", "ls"]
    assert len(t.events) == 3
    assert len(t.events[1]["message"]["content"][0]["content"]) == 10_000_000


def test_a_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        transcript.load(tmp_path / "nope.jsonl")


def test_an_empty_file_is_an_empty_transcript(tmp_path: Path) -> None:
    t = transcript.load(_write(tmp_path))
    assert (t.calls, t.texts, t.results, t.events) == ([], [], [], [])
    assert t.cost_usd() is None and t.turns() is None


# --- load: what it returns --------------------------------------------------

def test_calls_texts_results_and_events(tmp_path: Path) -> None:
    monitor = {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": "Monitor",
        "input": {"command": "tail -f log", "description": 7}}]}}
    t = transcript.load(_write(
        tmp_path,
        {"type": "system", "subtype": "init"},
        _said("I will look at the disc."),
        _bash("ls /media", ts=None),
        monitor,
        {"type": "user", "message": {"content": [{"type": "text", "text": "not the agent"}]}},
        _said(None),
        _result(),
    ))
    assert t.calls == [
        transcript.ToolCall(0, "Bash", "ls /media", "run it", None),
        transcript.ToolCall(1, "Monitor", "tail -f log", "", None),
    ]
    assert [c.tool for c in t.bash()] == ["Bash", "Monitor"]
    assert t.texts == ["I will look at the disc."]
    assert t.results == [_result()]
    assert len(t.events) == 7


def test_a_call_carries_its_event_timestamp(tmp_path: Path) -> None:
    t = transcript.load(_write(tmp_path, _bash("ls", ts="2026-11-18T10:00:00Z")))
    assert t.calls[0].timestamp == "2026-11-18T10:00:00Z"


def test_the_dataclasses_are_frozen(tmp_path: Path) -> None:
    t = transcript.load(_write(tmp_path, _bash("ls")))
    with pytest.raises(AttributeError):
        t.calls[0].command = "rm -rf /"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        t.calls = []  # type: ignore[misc]


# --- cost and turns ----------------------------------------------------------

def test_turns_is_the_most_of_any_result_not_the_last(tmp_path: Path) -> None:
    """rc10: the CLI counts turns afresh after each background-task wake,
    so a run that made 43 turns ended on a result event saying 1."""
    t = transcript.load(_write(tmp_path, _result(43, 0.28), _result(1, 0.29)))
    assert t.turns() == 43


def test_cost_is_the_running_total_of_the_last_result(tmp_path: Path) -> None:
    """total_cost_usd is cumulative across result events: summing them
    would count the first part of the run twice."""
    t = transcript.load(_write(tmp_path, _result(43, 0.28), _result(1, 0.29)))
    assert t.cost_usd() == 0.29


@pytest.mark.parametrize("bad", [None, "1", True, [3]])
def test_cost_and_turns_pass_over_values_that_are_not_numbers(
    tmp_path: Path, bad: object,
) -> None:
    t = transcript.load(_write(tmp_path, _result(5, 0.5), _result(bad, bad)))
    assert t.turns() == 5
    assert t.cost_usd() == 0.5


# --- clauses -----------------------------------------------------------------

def _words(text: str) -> list[list[str]]:
    return list(transcript.split_clauses(text))


@pytest.mark.parametrize(("text", "want"), [
    ("ls /media && cat README.txt", [["ls", "/media"], ["cat", "README.txt"]]),
    ("a; b || c | d & e", [["a"], ["b"], ["c"], ["d"], ["e"]]),
    ("cd /x\nls", [["cd", "/x"], ["ls"]]),
    ("sudo -E mount /dev/sr0 /media", [["mount", "/dev/sr0", "/media"]]),
    ("X=1 Y=2 restore-shell send ''", [["restore-shell", "send", ""]]),
    ("if test -f x; then echo y; fi", [["test", "-f", "x"], ["echo", "y"]]),
    ("for d in a b; do disc-loader insert $d; done",
     [["disc-loader", "insert", "$d"]]),
    ("out=$(restore-shell expect 'a|b' 120)", [["restore-shell", "expect", "Q", "120"]]),
    ('echo "x; rm -rf /"', [["echo", "Q"]]),
    ('echo "$(cat ~/pw)"', [["echo"], ["cat", "~/pw"]]),
    ("ls # rustic restore", [["ls"]]),
    ("(cd /x && ls)", [["cd", "/x"], ["ls"]]),
    ('case "$x" in\n  *done*) break ;;\nesac', [["break"]]),
    ("echo `uname -r`", [["echo"], ["uname", "-r"]]),
])
def test_split_clauses(text: str, want: list[list[str]]) -> None:
    assert _words(text) == want


# The cases the harness's other clause splitters handled, which the one
# splitter must too (#889): no_bypass_check's CLAUSE_SPLIT + shlex, and
# verify.sh's clause_args (script_read_check, no_binary_rename_check).
@pytest.mark.parametrize(("text", "want"), [
    # shlex took the quotes off a word: a quoted path is still that path.
    ('"$META/recovery/bin/lcsas-restore" restore',
     [["$META/recovery/bin/lcsas-restore", "restore"]]),
    ('"/media/LCSAS META/recovery/bin/lcsas-restore" --repo a',
     [["/media/LCSAS META/recovery/bin/lcsas-restore", "--repo", "a"]]),
    ("cat '/media/restore.sh'", [["cat", "/media/restore.sh"]]),
    ('mv "/mnt/recovery/bin/lcsas-restore" /tmp/x.broken',
     [["mv", "/mnt/recovery/bin/lcsas-restore", "/tmp/x.broken"]]),
    ('"${META}/recovery/bin/rustic" restore', [["V/recovery/bin/rustic", "restore"]]),
    # clause_args: every leading sudo, then VAR=val assignments.
    ("sudo sudo mv a b", [["mv", "a", "b"]]),
    ("sudo RUSTIC_PASSWORD=x rustic restore", [["rustic", "restore"]]),
    ("RUSTIC_PASSWORD=x rustic restore", [["rustic", "restore"]]),
    # CLAUSE_SPLIT's operators, with no spaces around them.
    ("a&&b||c;d|e&f", [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]),
    # An operator inside quotes does not split (CLAUSE_SPLIT did split it,
    # which flagged `restore-shell expect '...|lcsas-restore|...'`).
    ("restore-shell expect 'Password:|lcsas-restore|Insert' 120",
     [["restore-shell", "expect", "Q", "120"]]),
    ('echo "a && rustic restore"', [["echo", "Q"]]),
    # A quoted case pattern with a space is still a case pattern.
    ('case "$out" in\n  *"restore complete"*) break ;;\nesac', [["break"]]),
])
def test_split_clauses_covers_the_old_splitters(text: str, want: list[list[str]]) -> None:
    assert _words(text) == want


def test_clauses_split_each_shell_command_once(tmp_path: Path) -> None:
    t = transcript.load(_write(tmp_path, _bash("ls && df"), _bash("sudo umount /media")))
    got = [(c.words, c.call.index) for c in t.clauses()]
    assert got == [(["ls"], 0), (["df"], 0), (["umount", "/media"], 1)]
