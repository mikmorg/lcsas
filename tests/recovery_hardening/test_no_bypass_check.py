"""Unit tests for the blind-restore #13 bypass check.

`tests/e2e/cdemu_blind_restore/no_bypass_check.py` scans an agent
transcript for shell commands that invoke a recovery binary
directly, bypassing `restore.sh`.

History:

  * PR #235 / #236 / #238 noted that the original prefix-strip
    handled `sudo|sh|bash|exec` only.  An agent could route around
    the check by invoking `python3 /mnt/standalone_restorer.py`
    directly; the python wrapper isn't peeled and `python3` itself
    is not in BINARIES, so the bypass registered as a silent PASS.

Issue #241 extended the prefix-strip to cover `python` /
`python3` / `python3.<minor>` and the `python -m <module>` shape.
This file pins the new behavior plus the pre-existing
sudo/sh/bash/exec cases so a future refactor cannot silently
regress them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = (
    REPO_ROOT
    / "tests"
    / "e2e"
    / "cdemu_blind_restore"
    / "no_bypass_check.py"
)


def _load_check_module():
    spec = importlib.util.spec_from_file_location(
        "no_bypass_check", CHECK_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


def _write_transcript(tmp_path: Path, commands: list[str]) -> Path:
    """Build a minimal transcript.jsonl carrying the given Bash
    tool_use commands."""
    transcript = tmp_path / "transcript.jsonl"
    lines = []
    for cmd in commands:
        lines.append(
            json.dumps(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "input": {"command": cmd},
                            }
                        ]
                    }
                }
            )
        )
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


# -----------------------------------------------------------------
# Pre-existing wrappers (sudo / sh / bash / exec) — regression
# guard.  These are the cases the original implementation handled
# and the rewrite must continue to handle.
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "lcsas-restore --repo alpha --target /tmp/x",
        "sudo lcsas-restore --repo alpha --target /tmp/x",
        "sh /mnt/lcsas-restore --repo alpha",
        "bash /mnt/lcsas-restore --repo alpha",
        "exec lcsas-restore --repo alpha",
        "sudo -E lcsas-restore --repo alpha",
        "rustic restore latest --target /tmp/r",
        "rustic-static restore latest --target /tmp/r",
        "restic restore latest --target /tmp/r",
        "standalone_restorer.py --repo alpha --target /tmp/x",
    ],
    ids=[
        "bare-lcsas-restore",
        "sudo-lcsas-restore",
        "sh-lcsas-restore",
        "bash-lcsas-restore",
        "exec-lcsas-restore",
        "sudo-flag-lcsas-restore",
        "bare-rustic",
        "bare-rustic-static",
        "bare-restic",
        "bare-standalone-restorer",
    ],
)
def test_classic_bypass_shapes_flagged(check, cmd):
    assert check.scan_command(cmd), (
        f"expected {cmd!r} to register as bypass"
    )


# -----------------------------------------------------------------
# Issue #241: python / python3 / python3.<minor> as wrappers.
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 /mnt/standalone_restorer.py",
        "python3 /mnt/standalone_restorer.py --repo X --target Y",
        "python3.12 /tmp/standalone_restorer.py --repo X --target Y",
        "python /mnt/standalone_restorer.py",
        "python -m standalone_restorer --repo X",
        "python3 -m standalone_restorer --repo X --target Y",
        "sudo python3 /mnt/standalone_restorer.py",
        "sudo python3 -m standalone_restorer --repo X",
    ],
    ids=[
        "python3-script",
        "python3-script-args",
        "python3.12-script-args",
        "python-script",
        "python-m-module",
        "python3-m-module-args",
        "sudo-python3-script",
        "sudo-python3-m-module",
    ],
)
def test_python_bypass_shapes_flagged(check, cmd):
    assert check.scan_command(cmd), (
        f"expected {cmd!r} to register as bypass"
    )


# -----------------------------------------------------------------
# Legitimate / unrelated python invocations must NOT be flagged.
# `python3 -c "..."` is a smoke test; `python3 foo.py` where foo
# isn't standalone_restorer is benign; the legit
# `sh /mnt/restore.sh` driver invocation is the canonical path
# and must remain green.
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        'python3 -c "print(1)"',
        "python3 /tmp/some_other_script.py",
        "python3 -m pip --version",
        "python3 -m json.tool /tmp/data.json",
        "sh /mnt/restore.sh",
        "sh /mnt/restore.sh --repo alpha",
        "bash /mnt/restore.sh",
        "sudo /mnt/restore.sh",
        "/mnt/restore.sh",
        "ls -la /mnt",
        "cat /etc/hostname",
    ],
    ids=[
        "python-dash-c-print",
        "python-other-script",
        "python-m-pip-version",
        "python-m-json-tool",
        "sh-restore-sh",
        "sh-restore-sh-args",
        "bash-restore-sh",
        "sudo-restore-sh",
        "bare-restore-sh",
        "ls",
        "cat",
    ],
)
def test_benign_commands_not_flagged(check, cmd):
    assert not check.scan_command(cmd), (
        f"did not expect {cmd!r} to register as bypass; got "
        f"{check.scan_command(cmd)!r}"
    )


# -----------------------------------------------------------------
# Probing invocations (--version / --help / -V / -h) are exempt
# even when run directly against a recovery binary - the agent is
# allowed to sniff "does this binary exist?" without triggering
# the bypass alarm.
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "rustic --version",
        "rustic -V",
        "rustic --help",
        "rustic -h",
        "rustic help",
        "lcsas-restore --version",
        "lcsas-restore version",
        "python3 /mnt/standalone_restorer.py --version",
        "python3 -m standalone_restorer --help",
        "sudo lcsas-restore --version",
    ],
    ids=[
        "rustic-version-long",
        "rustic-V-short",
        "rustic-help-long",
        "rustic-h-short",
        "rustic-help-subcmd",
        "lcsas-restore-version",
        "lcsas-restore-version-subcmd",
        "python3-standalone-version",
        "python3-m-standalone-help",
        "sudo-lcsas-restore-version",
    ],
)
def test_probe_invocations_not_flagged(check, cmd):
    assert not check.scan_command(cmd), (
        f"did not expect probe {cmd!r} to register as bypass; got "
        f"{check.scan_command(cmd)!r}"
    )


# -----------------------------------------------------------------
# Compound shell commands: bypass anywhere in a `;` / `&&` / `||`
# / `|` chain is still a bypass.
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "ls /mnt && python3 /mnt/standalone_restorer.py --repo X",
        "true; python3 -m standalone_restorer --repo X",
        "echo go || lcsas-restore --repo X",
        "lcsas-restore --repo X | tee /tmp/out.log",
    ],
    ids=[
        "and-then-python3-script",
        "semi-then-python3-m",
        "or-then-lcsas-restore",
        "lcsas-restore-piped",
    ],
)
def test_compound_commands_flagged(check, cmd):
    assert check.scan_command(cmd), (
        f"expected compound {cmd!r} to register as bypass"
    )


# -----------------------------------------------------------------
# End-to-end: scan_transcript over a built jsonl matches
# scan_command's verdict.
# -----------------------------------------------------------------


def test_scan_transcript_flags_python_bypass(check, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            "ls /mnt",
            "python3 /mnt/standalone_restorer.py --repo alpha "
            "--target /tmp/out",
        ],
    )
    hits = check.scan_transcript(str(transcript))
    assert hits, "expected at least one bypass hit"


def test_scan_transcript_clean_run(check, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            "ls /mnt",
            "sh /mnt/restore.sh",
            'python3 -c "print(1)"',
            "rustic --version",
        ],
    )
    hits = check.scan_transcript(str(transcript))
    assert not hits, f"expected no hits, got {hits!r}"


def test_main_exit_code_clean(check, tmp_path):
    transcript = _write_transcript(tmp_path, ["sh /mnt/restore.sh"])
    assert check.main(["no_bypass_check.py", str(transcript)]) == 0


def test_main_exit_code_bypass(check, tmp_path):
    transcript = _write_transcript(
        tmp_path,
        ["python3 /mnt/standalone_restorer.py --repo X"],
    )
    assert check.main(["no_bypass_check.py", str(transcript)]) == 1


def test_main_usage_error(check):
    assert check.main(["no_bypass_check.py"]) == 64
