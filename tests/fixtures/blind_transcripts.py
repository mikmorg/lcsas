"""Small agent transcripts for the blind-restore harness's tests (#890).

The checks of ``tests/e2e/cdemu_blind_restore/checks.py`` (unit tests) and
verify.sh end to end (``tests/recovery_hardening/test_verify_*.py``) are
fed the same kind of ``transcript.jsonl``: one stream-json event per line.
These build those events, and hold the disc-swap loops the rc9 agents
saved to files (the #855 exemption cases), so the test files share one
copy of each.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

# --- events -------------------------------------------------------------------


def bash_event(command: str) -> dict:
    """Build a tool_use event the agent would have emitted."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": command, "description": "stub"},
                }
            ],
        },
        "session_id": "test",
    }


def result_event(text: str = "RESTORE COMPLETE") -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "num_turns": 5,
        "result": text,
        "duration_ms": 1000,
        "total_cost_usd": 0.01,
        "is_error": False,
    }


def bash(command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": command, "description": "stub"}},
    ]}}


def said(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text},
    ]}}


# --- transcript files -----------------------------------------------------------


def write_transcript(run_dir: Path, events: Iterable[dict]) -> None:
    """*run_dir*/transcript.jsonl holding *events*."""
    (run_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


def transcript_file(tmp_path: Path, commands: Iterable[str]) -> Path:
    """A transcript (*tmp_path*/t.jsonl) of one Bash call per command."""
    t = tmp_path / "t.jsonl"
    t.write_text("".join(json.dumps(bash(c)) + "\n" for c in commands))
    return t


# --- the agents' loops, as written in the rc9 runs ------------------------------

LABEL = "LCSAS_TEST_TINY_2026_{:04d}".format
PW_HEX = "00112233445566778899aabbccddeeff"  # stand-in, not a real password

# rc9b, run ...-2623073 (its own echo lines, /mnt, `|| true`).
HANDLE_RESTORE = (
    "cat > /tmp/handle_restore.sh << 'EOFSCRIPT'\n#!/bin/bash\n"
    "DATA_DISCS=( $(grep -v '^LCSAS_META$' ~/disc-labels.txt) )\n"
    "disc_idx=0\n\nfor iteration in $(seq 1 40); do\n"
    '    echo "Iteration $iteration: waiting for next prompt..."\n'
    "    out=$(restore-shell expect 'Insert the right disc|restore complete' 120) || break\n"
    '    echo "Got output:"\n    echo "$out"\n    echo "---"\n    \n'
    '    case "$out" in\n        *"restore complete"*)\n'
    '            echo "RESTORE COMPLETE"\n            break\n            ;;\n'
    '        *"Insert the right"*)\n            # Need to swap disc\n'
    '            echo "Swapping to disc: ${DATA_DISCS[$disc_idx]}"\n'
    "            sudo umount /mnt 2>/dev/null || true\n"
    '            disc-loader insert "${DATA_DISCS[$disc_idx]}"\n'
    "            sudo mount /dev/sr0 /mnt\n"
    "            disc_idx=$(( (disc_idx + 1) % ${#DATA_DISCS[@]} ))\n"
    '            restore-shell send ""\n            ;;\n    esac\ndone\n'
    "EOFSCRIPT\nchmod +x /tmp/handle_restore.sh\n/tmp/handle_restore.sh"
)

# rc9-final, run ...-3349390 (disc list written out, a sleep).
DISC_SWAP_LOOP = (
    "cat > /tmp/disc_swap_loop.sh << 'LOOP_EOF'\n#!/bin/bash\n"
    "DATA_DISCS=(LCSAS_TEST_TINY_2026_0002 LCSAS_TEST_TINY_2026_0003 "
    "LCSAS_TEST_TINY_2026_0004 LCSAS_TEST_TINY_2026_0005 "
    "LCSAS_TEST_TINY_2026_0006)\n"
    "disc_idx=0\nfor i in $(seq 1 40); do\n"
    "    out=$(restore-shell expect 'Insert the right disc|restore complete' 120) || break\n"
    '    case "$out" in\n        *"restore complete"*) \n'
    '            echo "RESTORE COMPLETE"\n            break \n            ;;\n'
    '        *"Insert the right"*) ;;\n    esac\n'
    "    sudo umount /media 2>/dev/null\n"
    '    disc-loader insert "${DATA_DISCS[$disc_idx]}"\n    sleep 2\n'
    "    sudo mount /dev/sr0 /media\n"
    "    disc_idx=$(( (disc_idx + 1) % ${#DATA_DISCS[@]} ))\n"
    '    restore-shell send ""\ndone\nLOOP_EOF\nbash /tmp/disc_swap_loop.sh'
)

# rc9, run ...-2521360: the prompt's loop typed inline (no file).
INLINE_LOOP = (
    "\nDATA_DISCS=( $(grep -v '^LCSAS_META$' ~/disc-labels.txt) )\n"
    "disc_idx=0\n\n"
    "for _ in $(seq 1 40); do\n"
    "    out=$(restore-shell expect 'Insert the right disc|restore complete' 120) || break\n"
    '    case "$out" in\n'
    '        *"restore complete"*) echo "RESTORE COMPLETE"; break ;;\n'
    '        *"Insert the right"*) ;;  # fall through to swap\n'
    "    esac\n    sudo umount /media 2>/dev/null\n    disc-loader eject\n"
    '    disc-loader insert "${DATA_DISCS[$disc_idx]}"\n'
    "    disc_idx=$(( (disc_idx + 1) % ${#DATA_DISCS[@]} ))\n"
    '    sudo mount /dev/sr0 /media\n    restore-shell send ""\ndone\n'
)

# single-key run ...-345048, cut down: a runner that starts the restore
# itself and watches a log file.  A wrapper, not the prompt's loop.
RESTORE_RUNNER = (
    "cat > /tmp/restore-runner.sh << 'SCRIPT'\n#!/bin/bash\nset -e\n"
    "sudo umount /media 2>/dev/null || true\n"
    "disc-loader insert LCSAS_TEST_TINY_2026_0001\nsleep 1\n"
    "sudo mount /dev/sr0 /media 2>&1 | grep -v WARNING\n"
    "sh /mnt/restore.sh ~/restored/ latest > /tmp/restore-progress.txt 2>&1 &\n"
    "RESTORE_PID=$!\n"
    "for swap_num in {1..20}; do\n"
    "    if ! kill -0 $RESTORE_PID 2>/dev/null; then break; fi\n"
    "done\nSCRIPT\nchmod +x /tmp/restore-runner.sh\n/tmp/restore-runner.sh"
)


def loop_with(extra: str, path: str = "/tmp/swap.sh") -> str:
    """The prompt's loop saved to *path*, with *extra* inside the loop."""
    return (
        f"cat > {path} <<'EOF'\n"
        "DATA_DISCS=( $(grep -v '^LCSAS_META$' ~/disc-labels.txt) )\n"
        "disc_idx=0\nfor _ in $(seq 1 40); do\n"
        "    out=$(restore-shell expect 'Insert the right disc|restore complete' 120) || break\n"
        '    case "$out" in *"restore complete"*) break ;; esac\n'
        f"{extra}"
        "    sudo umount /media 2>/dev/null\n"
        '    disc-loader insert "${DATA_DISCS[$disc_idx]}"\n'
        "    disc_idx=$(( (disc_idx + 1) % ${#DATA_DISCS[@]} ))\n"
        "    sudo mount /dev/sr0 /media\n"
        '    restore-shell send ""\n'
        f"done\nEOF\nbash {path}"
    )
