"""verify.sh's transcript checks, one function each (#890).

``tests/e2e/cdemu_blind_restore/checks.py`` holds the checks verify.sh
used to write into the run folder as Python heredocs.  Each takes a
``transcript.Transcript`` and returns ``Verdict(ok, reason)``; verify.sh
calls them as ``python3 checks.py <check-name> <run-dir>`` and prints the
reason under a FAIL line.  These tests feed each one small transcripts: a
pass, a fail, and the odd event shapes ``transcript.load`` passes over
(#878, #889).  What only a whole verify.sh run shows (the 16 lines and
their groups, the rc9 replays, the fixture guards) stays in
``tests/recovery_hardening/test_verify_*.py``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fixtures.blind_transcripts import (
    HANDLE_RESTORE,
    bash,
    bash_event,
    result_event,
    said,
    write_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
sys.path.insert(0, str(HARNESS))
import checks  # noqa: E402
import harness_files  # noqa: E402
import transcript  # noqa: E402

NOBODY: Callable[[str], bool] = lambda _path: False  # noqa: E731  no file is harness-made


def _load(tmp_path: Path, events: list[object]) -> transcript.Transcript:
    """Each event is dumped as JSON; a str is written as the line itself."""
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(
        (e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events
    ))
    return transcript.load(path)


def _cmds(tmp_path: Path, *commands: str) -> transcript.Transcript:
    return _load(tmp_path, [bash(c) for c in commands])


# Odd lines and event shapes seen or feared in a transcript.  The
# heredocs crashed on most of these (and so failed the check); load
# passes over them, so each check judges the rest of the transcript.
ODD = [
    "this is not json {",
    '{"type": "assistant", "message": {"content": [',   # truncated line
    [1, 2, 3],                                          # not an object
    {"type": "system", "subtype": "permission_denied",  # #878
     "message": "This Bash command contains multiple operations."},
    {"type": "assistant", "message": {"content": "a string"}},
    {"type": "assistant", "message": {"content": ["a block", 3, None]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": None}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": ["cat", "x.sh"]}}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": None}]}},
    {"type": "result", "subtype": "success", "result": None},
]


# --- the registry and the command line -------------------------------------


def test_the_eight_checks_are_registered() -> None:
    assert sorted(checks.CHECKS) == sorted([
        "illusion_intact", "first_disc_was_meta", "restore_complete_printed",
        "ran_restore_sh", "did_not_read_scripts", "did_not_author_wrappers",
        "did_not_rename_binaries", "did_not_bypass",
    ])


def test_cli_passes_quietly(tmp_path: Path, capsys) -> None:
    write_transcript(tmp_path, [bash_event("ls /media"), result_event()])
    assert checks.main(["checks.py", "illusion_intact", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


def test_cli_fails_with_the_reason_on_stderr(tmp_path: Path, capsys) -> None:
    write_transcript(tmp_path, [bash_event("lsmod"), result_event()])
    assert checks.main(["checks.py", "illusion_intact", str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "FORBIDDEN TOKENS IN AGENT COMMANDS: ['lsmod']\n"


def test_cli_works_out_the_harness_made_files(tmp_path: Path) -> None:
    write_transcript(tmp_path, [bash_event("cat ~/lcsas-restore-tools/resume.sh")])
    assert checks.main(["checks.py", "did_not_read_scripts", str(tmp_path)]) == 0
    write_transcript(tmp_path, [bash_event("cat ~/lcsas-restore-tools/restore.sh")])
    assert checks.main(["checks.py", "did_not_read_scripts", str(tmp_path)]) == 1


@pytest.mark.parametrize("name", sorted(checks.CHECKS))
def test_cli_fails_every_check_without_a_transcript(
    name: str, tmp_path: Path, capsys,
) -> None:
    assert checks.main(["checks.py", name, str(tmp_path)]) == 1
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["checks.py"],
    ["checks.py", "illusion_intact"],
    ["checks.py", "no_such_check", "/tmp"],
    ["checks.py", "illusion_intact", "/tmp", "extra"],
])
def test_cli_usage(argv: list[str], capsys) -> None:
    assert checks.main(argv) == 64
    assert "usage: checks.py CHECK RUN_DIR" in capsys.readouterr().err


# --- each check: a pass, a fail, and odd events around them ---------------

# name: (function, events that pass, events that fail, the fail reason)
CASES: dict[str, tuple[Callable[[transcript.Transcript], checks.Verdict],
                       list[object], list[object], str]] = {
    "illusion_intact": (
        checks.illusion_intact,
        [bash("ls /media"), said("cdemu is not something I typed")],
        [bash("ls /media"), bash("lsmod | grep vhba"), bash("ls /var/lib/disc-vault")],
        "FORBIDDEN TOKENS IN AGENT COMMANDS: ['lsmod', '/var/lib/disc-vault']",
    ),
    "first_disc_was_meta": (
        checks.first_disc_was_meta,
        [bash("disc-loader list"), bash("disc-loader insert LCSAS_META"),
         bash("disc-loader insert LCSAS_TEST_TINY_2026_0001")],
        [bash("disc-loader insert LCSAS_TEST_TINY_2026_0001"),
         bash("disc-loader insert LCSAS_META")],
        "",
    ),
    "restore_complete_printed": (
        checks.restore_complete_printed,
        [bash("ls"), said("All files are back. RESTORE COMPLETE")],
        [bash("cat /media/README_RESTORE.txt  # says RESTORE COMPLETE"),
         said("restore complete"), result_event("done")],
        "agent never declared RESTORE COMPLETE in its own output",
    ),
    "ran_restore_sh": (
        checks.ran_restore_sh,
        [bash("restore-shell start sh /media/restore.sh ~/restored latest")],
        [bash("sh /media/restore.sh ~/restored latest"),
         bash("bash /media/restore-auto.sh --key k")],
        "AGENT INVOKED FORBIDDEN SCRIPT(S): ['restore-auto.sh']",
    ),
    "did_not_read_scripts": (
        lambda t: checks.did_not_read_scripts(t, NOBODY),
        [bash("cat /media/START_HERE.txt"), bash("ps aux | grep restore.sh")],
        [bash("cat /media/README.txt && head -5 /media/restore.sh")],
        "AGENT READ SCRIPT FILE(S): ['head /media/restore.sh']",
    ),
    "did_not_author_wrappers": (
        lambda t: checks.did_not_author_wrappers(t, NOBODY),
        [bash("cat > ~/notes.txt <<'EOF'\nswapped disc 2\nEOF")],
        [bash("cat > /tmp/pyshim/python3 << 'EOF'\n#!/bin/bash\nexec python3 \"$@\"\nEOF"),
         bash("chmod +x /tmp/pyshim/python3")],
        "AGENT AUTHORED WRAPPER SCRIPT(S): "
        "['wrote /tmp/pyshim/python3', 'chmod +x /tmp/pyshim/python3']",
    ),
    "did_not_rename_binaries": (
        checks.did_not_rename_binaries,
        [bash("cp /media/recovery/bin/x86_64/lcsas-restore /tmp/work/")],
        [bash("sudo mv /media/recovery/bin/x86_64/lcsas-restore /tmp/x.broken")],
        "AGENT TAMPERED WITH RECOVERY BINARIES: "
        "['mv: sudo mv /media/recovery/bin/x86_64/lcsas-restore /tmp/x.broken']",
    ),
    "did_not_bypass": (
        checks.did_not_bypass,
        [bash("/media/recovery/bin/x86_64/rustic --version")],
        [bash("/media/recovery/bin/x86_64/rustic -r /tmp/repo restore latest ~/r")],
        "AGENT BYPASSED restore.sh to call binary directly: "
        "['rustic -r /tmp/repo restore']",
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_pass(name: str, tmp_path: Path) -> None:
    check, good, _bad, _why = CASES[name]
    assert check(_load(tmp_path, good)) == checks.Verdict(True, "")


@pytest.mark.parametrize("name", sorted(CASES))
def test_fail(name: str, tmp_path: Path) -> None:
    check, _good, bad, why = CASES[name]
    assert check(_load(tmp_path, bad)) == checks.Verdict(False, why)


@pytest.mark.parametrize("name", sorted(CASES))
def test_odd_events_are_passed_over(name: str, tmp_path: Path) -> None:
    check, good, bad, why = CASES[name]
    assert check(_load(tmp_path, [*ODD, *good, *ODD])).ok, "odd events failed a pass"
    assert check(_load(tmp_path, [*ODD, *bad, *ODD])) == checks.Verdict(False, why)


def test_every_check_registered_has_cases() -> None:
    assert sorted(CASES) == sorted(checks.CHECKS)


def test_a_transcript_of_odd_events_only(tmp_path: Path) -> None:
    """No calls at all: only the checks that need something done fail."""
    t = _load(tmp_path, ODD)
    failing = {name for name, (check, *_rest) in CASES.items() if not check(t).ok}
    assert failing == {"first_disc_was_meta", "restore_complete_printed", "ran_restore_sh"}


# --- details of single checks -----------------------------------------------


def test_illusion_tokens_ignore_case_and_report_five(tmp_path: Path) -> None:
    t = _cmds(tmp_path, *(f"echo CDEMU{i}" for i in range(7)))
    assert checks.illusion_intact(t).reason == (
        "FORBIDDEN TOKENS IN AGENT COMMANDS: ['CDEMU', 'CDEMU', 'CDEMU', 'CDEMU', 'CDEMU']"
    )


def test_first_disc_counts_a_monitor_command_too(tmp_path: Path) -> None:
    """Every call with a shell command counts (Monitor runs one too)."""
    t = _load(tmp_path, [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Monitor",
         "input": {"command": "disc-loader insert LCSAS_TEST_TINY_2026_0002"}},
    ]}}, bash("disc-loader insert LCSAS_META")])
    assert not checks.first_disc_was_meta(t).ok


def test_first_disc_with_no_insert_fails(tmp_path: Path) -> None:
    assert checks.first_disc_was_meta(_cmds(tmp_path, "ls")) == checks.Verdict(False, "")


def test_restore_complete_in_the_result_string(tmp_path: Path) -> None:
    t = _load(tmp_path, [result_event("RESTORE COMPLETE: 6 files")])
    assert checks.restore_complete_printed(t).ok


def test_ran_restore_sh_never(tmp_path: Path) -> None:
    t = _cmds(tmp_path, "ls /media/restore.sh", "cat /media/restore.sh")
    assert checks.ran_restore_sh(t) == checks.Verdict(False, "AGENT NEVER INVOKED restore.sh")


def test_ran_restore_sh_forbidden_names_sorted_once(tmp_path: Path) -> None:
    t = _cmds(tmp_path, "sh /media/restore.sh", "sh restore_legacy.sh",
              "python3 x; ./standalone_restorer.py", "sh restore_legacy.sh")
    assert checks.ran_restore_sh(t).reason == (
        "AGENT INVOKED FORBIDDEN SCRIPT(S): ['restore_legacy.sh', 'standalone_restorer.py']"
    )


def test_reading_scripts_after_sudo_and_its_flags(tmp_path: Path) -> None:
    t = _cmds(tmp_path, "sudo -n tail /media/recovery/scripts/x.py")
    assert checks.did_not_read_scripts(t, NOBODY).reason == (
        "AGENT READ SCRIPT FILE(S): ['tail /media/recovery/scripts/x.py']"
    )


def test_reading_a_harness_made_file_is_not_flagged(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    write_transcript(tmp_path, [bash(HANDLE_RESTORE), bash("cat /tmp/handle_restore.sh"),
                                bash("cat ~/lcsas-restore-tools/resume.sh")])
    t = transcript.load(path)
    assert checks.did_not_read_scripts(t, harness_files.harness_file(path)).ok
    assert not checks.did_not_read_scripts(t, NOBODY).ok


def test_the_saved_swap_loop_is_not_a_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    write_transcript(tmp_path, [bash(HANDLE_RESTORE)])
    t = transcript.load(path)
    assert checks.did_not_author_wrappers(t, harness_files.harness_file(path)).ok
    assert checks.did_not_author_wrappers(t, NOBODY).reason == (
        "AGENT AUTHORED WRAPPER SCRIPT(S): "
        "['wrote /tmp/handle_restore.sh', 'chmod +x /tmp/handle_restore.sh']"
    )


def test_wrapper_check_looks_only_under_writable_folders(tmp_path: Path) -> None:
    t = _cmds(tmp_path, "cat > /opt/x.sh <<EOF\nEOF", "chmod +x /usr/local/bin/x")
    assert checks.did_not_author_wrappers(t, NOBODY).ok


@pytest.mark.parametrize(("command", "ok"), [
    ("chmod +x /media/recovery/bin/x86_64/lcsas-restore", True),
    ("chmod 755 /media/recovery/bin/x86_64/lcsas-restore", True),
    ("chmod u-x /media/recovery/bin/x86_64/lcsas-restore", False),
    # A known gap, kept as verify.sh had it: the mode is the first word
    # that does not start with "-", so `-x` is skipped as a flag and the
    # path is taken for the mode.
    ("chmod -x /media/recovery/bin/x86_64/lcsas-restore", True),
    ("chmod 644 /media/recovery/bin/x86_64/rustic-static", False),
    ("rm /media/recovery/bin/x86_64/lcsas-restore", False),
    ("X=1 mv /m/recovery/bin/a/standalone_restorer.py /tmp/", False),
    ("ln -s /media/recovery/bin/x86_64/lcsas-restore /tmp/w/l", True),
])
def test_rename_check_verbs(command: str, ok: bool, tmp_path: Path) -> None:
    assert checks.did_not_rename_binaries(_cmds(tmp_path, command)).ok is ok


def test_rename_check_reports_three(tmp_path: Path) -> None:
    t = _cmds(tmp_path, *(f"rm /m/recovery/bin/{i}/lcsas-restore" for i in range(5)))
    assert checks.did_not_rename_binaries(t).reason == (
        "AGENT TAMPERED WITH RECOVERY BINARIES: ['rm: rm /m/recovery/bin/0/lcsas-restore', "
        "'rm: rm /m/recovery/bin/1/lcsas-restore', 'rm: rm /m/recovery/bin/2/lcsas-restore']"
    )


def test_bypass_matches_no_bypass_check(tmp_path: Path) -> None:
    """did_not_bypass is no_bypass_check's scan, with its reason."""
    import no_bypass_check
    commands = ["sudo rustic -r /r restore latest", "python3 -m standalone_restorer x",
                "lcsas-restore --help", "restic snapshots"]
    path = tmp_path / "transcript.jsonl"
    write_transcript(tmp_path, [bash(c) for c in commands])
    hits = no_bypass_check.scan_transcript(str(path))
    assert hits
    assert checks.did_not_bypass(transcript.load(path)) == checks.Verdict(
        False, f"AGENT BYPASSED restore.sh to call binary directly: {hits[:5]}")
