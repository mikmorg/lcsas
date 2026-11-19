"""verify.sh scores OUTCOME and CONDUCT apart, ignoring files the rig made (#827).

In the rc9 sweep the split-key-docs variant failed three runs that had
restored every file intact.  CONDUCT checks fired on files that were not
the agent's own code:

* run ...-2521360: ``cat ~/lcsas-restore-tools/resume.sh``, a file
  restore.sh writes and tells the operator to run;
* run ...-2623073: the prompt's disc-swap loop saved to
  /tmp/handle_restore.sh, made executable and run;
* run ...-3349390: the same loop, disc list written out, saved to
  /tmp/disc_swap_loop.sh and run with bash.

The tests below rebuild each run as a synthetic run folder: the agent's
commands as it typed them (password bytes replaced), its disc-loader log,
restored files that match the fixture manifest, and a small META and
newest-data ISO for the disc checks.  verify.sh must score each OUTCOME
PASS and CONDUCT PASS, and blind_runs.py must score the run a pass.  Real
violations (reading the source tree, skipping the META disc, a runner
that starts the restore, a loop file that types the password) must still
fail CONDUCT, and the FAIL line must show why.

No agent, no CDEmu: verify.sh only reads the folder, the fixture and the
two ISOs (loop-mounted through sudo, as on the lab VM).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
VERIFY_SH = HARNESS / "verify.sh"

_spec = importlib.util.spec_from_file_location(
    "blind_runs", HARNESS / "blind_runs.py",
)
assert _spec is not None and _spec.loader is not None
blind_runs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(blind_runs)

sys.path.insert(0, str(HARNESS))
import harness_files  # noqa: E402

from tests.fixtures.blind_transcripts import (  # noqa: E402
    DISC_SWAP_LOOP,
    HANDLE_RESTORE,
    INLINE_LOOP,
    LABEL,
    PW_HEX,
    RESTORE_RUNNER,
    bash,
    loop_with,
    said,
    transcript_file,
)

# --- harness_files: which saved texts are the prompt's loop ----------------


def _body(command: str) -> str:
    writes = list(harness_files.heredoc_writes(command))
    assert len(writes) == 1, writes
    return writes[0][1]


@pytest.mark.parametrize("command", [HANDLE_RESTORE, DISC_SWAP_LOOP])
def test_the_rc9_saved_loops_are_the_prompts_loop(command: str) -> None:
    assert harness_files.is_swap_loop(_body(command))


def test_the_old_prompt_loop_text_is_the_prompts_loop() -> None:
    assert harness_files.is_swap_loop(INLINE_LOOP)
    assert harness_files.is_swap_loop(_body(loop_with("")))


@pytest.mark.parametrize("extra", [
    "    restore-shell start sh /media/restore.sh ~/restored latest\n",
    '    restore-shell send "$(cat ~/alpha.pw)"\n',
    f"    restore-shell send {PW_HEX}\n",
    "    restore-shell send-raw x\n",
    "    echo \"$out\" > /tmp/log.txt\n",
    "    python3 /media/keyshare_combine.py a b\n",
    "    sh /media/restore.sh ~/restored latest\n",
    "    cat /media/restore.sh\n",
    "    cp ~/lcsas-restore-tools/standalone_restorer.py /tmp/\n",
])
def test_a_loop_that_does_more_is_a_wrapper(extra: str) -> None:
    assert not harness_files.is_swap_loop(_body(loop_with(extra)))


# The same, with the command hidden inside a double-quoted substitution.
@pytest.mark.parametrize("extra", [
    '    echo "$(sh /media/restore.sh ~/restored latest)"\n',
    '    echo "`sh /media/restore.sh`"\n',
    '    restore-shell send "$(cat ~/alpha.key)"\n',
])
def test_a_quoted_substitution_is_looked_into(extra: str) -> None:
    assert not harness_files.is_swap_loop(_body(loop_with(extra)))


def test_a_loop_with_quoted_substitutions_is_the_loop() -> None:
    quoted = loop_with('    echo "swap $(date +%T): ${DATA_DISCS[$disc_idx]}"\n')
    assert not harness_files.is_swap_loop(_body(quoted))  # date: not the loop
    quoted = _body(loop_with("")).replace(
        "out=$(restore-shell expect 'Insert the right disc|restore complete' 120)",
        "out=\"$(restore-shell expect 'Insert the right disc|restore complete' 120)\"",
    )
    assert 'out="$(' in quoted
    assert harness_files.is_swap_loop(quoted)


def test_a_runner_is_a_wrapper() -> None:
    assert not harness_files.is_swap_loop(_body(RESTORE_RUNNER))


def test_a_file_with_no_disc_swap_is_not_the_loop() -> None:
    assert not harness_files.is_swap_loop("echo hello\n")
    assert not harness_files.is_swap_loop("restore-shell expect x 10\n")


def test_only_resume_sh_of_the_tools_folder_is_restore_sh_s(tmp_path: Path) -> None:
    exempt = harness_files.harness_file(transcript_file(tmp_path, []))
    for path in ("~/lcsas-restore-tools/resume.sh",
                 "/home/lcsas-blind/lcsas-restore-tools/resume.sh",
                 "$HOME/lcsas-restore-tools/resume.sh"):
        assert exempt(path), path
    for path in ("~/lcsas-restore-tools/standalone_restorer.py",
                 "~/lcsas-restore-tools/recovery/scripts/restore.sh",
                 "/media/restore.sh", "/tmp/resume.sh", "~/resume.sh"):
        assert not exempt(path), path


def test_a_path_written_as_loop_and_as_wrapper_is_not_exempt(
    tmp_path: Path,
) -> None:
    exempt = harness_files.harness_file(transcript_file(tmp_path, [
        loop_with("", "/tmp/x.sh"),
        loop_with("    sh /media/restore.sh ~/restored latest\n", "/tmp/x.sh"),
        loop_with("", "/tmp/y.sh"),
    ]))
    assert not exempt("/tmp/x.sh")
    assert exempt("/tmp/y.sh")


def test_a_resume_sh_the_agent_wrote_itself_is_not_exempt(tmp_path: Path) -> None:
    exempt = harness_files.harness_file(transcript_file(tmp_path, [
        "cat > ~/lcsas-restore-tools/resume.sh <<'EOF'\n"
        "sh /media/restore.sh ~/restored latest\nEOF",
    ]))
    assert not exempt("/home/lcsas-blind/lcsas-restore-tools/resume.sh")


# --- verify.sh end to end ----------------------------------------------------


_RESULT = {"type": "result", "subtype": "success", "num_turns": 40,
           "total_cost_usd": 0.3, "result": "RESTORE COMPLETE"}

# What every docs-driven run starts with: the META disc, its docs, the
# cards, the password rebuilt with the tool the docs name.
_OPENING = [
    "disc-loader list",
    "cat ~/disc-labels.txt",
    "disc-loader insert LCSAS_META",
    "sudo mount /dev/sr0 /media",
    "ls -la /media/",
    "cat /media/START_HERE.txt",
    "cat /media/README_RESTORE.txt",
    "cat /media/recovery/docs/RECOVER.txt",
    "cat /media/PASSWORD_INFO.txt",
    "cat ~/alpha-share-1-card.txt",
    "cat ~/alpha-share-2-card.txt",
    "python3 /media/keyshare_combine.py ~/alpha-share-1-card.txt "
    "~/alpha-share-2-card.txt > ~/alpha.key",
]


def _run_2521360() -> tuple[list[str], list[int]]:
    """rc9 sweep 1: read resume.sh, ran it, typed the loop inline."""
    commands = [
        *_OPENING,
        "restore-shell start sh /media/restore.sh --key ~/alpha.key ~/restored latest",
        "restore-shell expect \".\" 30",
        "sudo umount /media && disc-loader eject",
        f"disc-loader insert {LABEL(5)} && sudo mount /dev/sr0 /media",
        'restore-shell send ""',
        'restore-shell expect "Insert the right|restore complete|Which backup set" 120',
        'restore-shell send "1"',
        "ls -la /home/lcsas-blind/lcsas-restore-tools/",
        "/home/lcsas-blind/lcsas-restore-tools/recovery/bin/"
        "x86_64-unknown-linux-musl/lcsas-keyshare ~/alpha-share-1-card.txt "
        "~/alpha-share-2-card.txt > ~/.alpha.key 2>&1",
        "cat /home/lcsas-blind/lcsas-restore-tools/resume.sh",
        "sudo umount /media && disc-loader eject && disc-loader insert "
        f"{LABEL(5)} && sudo mount /dev/sr0 /media",
        f'echo -n "{PW_HEX}" > ~/.alpha.key.hex && restore-shell start sh '
        "/home/lcsas-blind/lcsas-restore-tools/resume.sh --key ~/.alpha.key.hex",
        'restore-shell expect "error|wrong|password|Insert|restored|complete|progress" 60',
        INLINE_LOOP,
        "ls -la ~/restored/ && du -sh ~/restored/",
    ]
    inserted = [5, 5, 5, 1, 2, 3, 4]
    return commands, inserted


def _run_2623073() -> tuple[list[str], list[int]]:
    """rc9b: saved the loop to /tmp/handle_restore.sh, chmod +x, ran it."""
    commands = [
        *_OPENING,
        "restore-shell start sh /media/restore.sh --key ~/alpha.key ~/restored latest",
        "restore-shell expect 'restore complete|newest|Insert|ERROR|error|Failure' 120",
        f"disc-loader eject && disc-loader insert {LABEL(5)}",
        'restore-shell send ""',
        "sudo mount /dev/sr0 /mnt",
        'restore-shell send ""',
        HANDLE_RESTORE,
        "restore-shell screen",
        'restore-shell send "1"',
        "restore-shell expect 'Insert the right disc|restore complete' 120",
        f"sudo umount /mnt && disc-loader eject && disc-loader insert {LABEL(1)} "
        "&& sudo mount /dev/sr0 /mnt",
        'restore-shell send ""',
        f"sudo umount /mnt && disc-loader eject && disc-loader insert {LABEL(4)} "
        "&& sudo mount /dev/sr0 /mnt",
        f"sudo umount /mnt && disc-loader eject && disc-loader insert {LABEL(2)} "
        "&& sudo mount /dev/sr0 /mnt",
        f"sudo umount /mnt && disc-loader eject && disc-loader insert {LABEL(3)} "
        "&& sudo mount /dev/sr0 /mnt",
        "restore-shell expect 'restore complete|progress:' 120",
        'restore-shell send "N"',
        "find ~/restored -type f | wc -l",
    ]
    inserted = [5, 1, 4, 2, 3]
    return commands, inserted


def _run_3349390() -> tuple[list[str], list[int]]:
    """rc9-final: saved the loop (discs written out) and ran it with bash."""
    commands = [
        *_OPENING[:9],
        "cat ~/alpha-share-1-card.txt",
        "cat ~/alpha-share-2-card.txt",
        "restore-shell start sh /media/restore.sh ~/restored latest",
        'restore-shell expect ".*" 30',
        "sudo umount /media && disc-loader eject",
        f"disc-loader insert {LABEL(6)} && sleep 2 && sudo mount /dev/sr0 /media",
        'restore-shell send ""',
        'restore-shell send "alpha"',
        "~/lcsas-restore-tools/recovery/bin/x86_64-unknown-linux-musl/"
        "lcsas-keyshare ~/alpha-share-1-card.txt ~/alpha-share-2-card.txt",
        f'restore-shell send "{PW_HEX}"',
        'restore-shell expect "Insert the right disc|restore complete|ERROR|password" 60',
        "sudo umount /media && disc-loader eject",
        f"disc-loader insert {LABEL(1)} && sleep 2 && sudo mount /dev/sr0 /media",
        'restore-shell send ""',
        DISC_SWAP_LOOP,
        "ls -la ~/restored/",
    ]
    inserted = [6, 1, 2, 3, 4, 5]
    return commands, inserted


RC9_RUNS = {
    "2521360-resume-sh": (_run_2521360, 5),
    "2623073-saved-loop": (_run_2623073, 5),
    "3349390-saved-loop": (_run_3349390, 6),
}


def _iso(src: Path, out: Path) -> None:
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-R", "-o", str(out), str(src)],
        check=True, capture_output=True, timeout=60,
    )


@pytest.fixture(scope="module")
def loop_mount_ok(tmp_path_factory) -> None:
    """verify.sh loop-mounts the META and newest data ISO through sudo."""
    if shutil.which("xorriso") is None:
        pytest.skip("xorriso not installed")
    d = tmp_path_factory.mktemp("probe")
    (d / "src").mkdir()
    (d / "src" / "x").write_text("x")
    _iso(d / "src", d / "p.iso")
    (d / "mnt").mkdir()
    r = subprocess.run(
        ["sudo", "-n", "mount", "-o", "ro,loop", str(d / "p.iso"), str(d / "mnt")],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        pytest.skip(f"cannot loop-mount through sudo: {r.stderr!r}")
    subprocess.run(["sudo", "-n", "umount", str(d / "mnt")], timeout=30)


def _fixture(tmp_path: Path, discs: int, allowed: int) -> Path:
    """A fixture whose alpha set restores intact: alpha on discs 1-5."""
    fix = tmp_path / "fixture"
    (fix / "iso_out").mkdir(parents=True)
    restored = tmp_path / "home" / "restored" / "scratch" / "sources" / "alpha"
    restored.mkdir(parents=True)
    lines = []
    for i in range(6):
        data = f"alpha file {i}\n".encode() * 50
        (restored / f"file_{i:03d}.bin").write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  file_{i:03d}.bin")
    (fix / "alpha_manifest.sha256").write_text("\n".join(lines) + "\n")
    bravo = hashlib.sha256(b"bravo only\n").hexdigest()
    (fix / "bravo_manifest.sha256").write_text(f"{bravo}  file_000.bin\n")
    (fix / "expected_alpha_volumes.txt").write_text(
        "".join(f"{LABEL(n)}\n" for n in range(1, 6))
    )
    (fix / "thrash_allowance.txt").write_text(f"{allowed}\n")
    meta = tmp_path / "meta_src"
    (meta / "metadata" / "repo").mkdir(parents=True)
    (meta / "README_RESTORE.txt").write_text("read START_HERE.txt\n")
    _iso(meta, fix / "iso_out" / "LCSAS_META.iso")
    newest = tmp_path / "data_src"
    newest.mkdir()
    (newest / "START_HERE.txt").write_text(
        "This disc's catalog was written BEFORE this disc was burned.\n"
        "For later copies, use the catalog on any NEWER disc.\n"
    )
    _iso(newest, fix / "iso_out" / f"{LABEL(discs)}.iso")
    return fix


def _run_dir(tmp_path: Path, commands: list[str], inserted: list[int],
             *, first: str = "LCSAS_META") -> Path:
    run = tmp_path / "run"
    run.mkdir()
    events = [bash(c) for c in commands]
    events += [said("RESTORE COMPLETE"), _RESULT]
    (run / "transcript.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )
    log = [f"2026-11-14T02:45:00+00:00 insert {first}"]
    log += [f"2026-11-14T02:46:{i:02d}+00:00 insert {LABEL(n)}"
            for i, n in enumerate(inserted)]
    (run / "disc-loader.log").write_text("\n".join(log) + "\n")
    (run / "disc-loader.log").chmod(0o644)
    return run


def _verify(run: Path, fix: Path) -> tuple[int, str]:
    env = {**os.environ, "FIXTURE": str(fix),
           "AGENT_HOME": str(run.parent / "home")}
    res = subprocess.run([str(VERIFY_SH), str(run)], capture_output=True,
                         text=True, env=env, timeout=120)
    return res.returncode, res.stdout


@pytest.mark.parametrize("name", sorted(RC9_RUNS))
def test_rc9_split_key_docs_failures_now_pass(
    name: str, tmp_path: Path, loop_mount_ok, monkeypatch,
) -> None:
    build, discs = RC9_RUNS[name]
    commands, inserted = build()
    fix = _fixture(tmp_path, discs=discs, allowed=discs)
    run = _run_dir(tmp_path, commands, inserted)
    rc, out = _verify(run, fix)
    assert "OUTCOME: PASS (9 passed, 0 failed)" in out, out
    assert "CONDUCT: PASS (7 passed, 0 failed)" in out, out
    assert rc == 0, out

    # The scorer the release sweep uses agrees: a pass, both groups pass.
    monkeypatch.setenv("FIXTURE", str(fix))
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "home"))
    code, result, _ = blind_runs.score(run, "split-key-docs", [str(VERIFY_SH)])
    assert code == 0, result
    assert result["outcome"] == "pass" and result["conduct"] == "pass", result
    assert result["passed"] == blind_runs.EXPECTED_CHECKS


# Each still fails CONDUCT, with OUTCOME intact: (extra commands, the
# disc first inserted, the check that fails, the reason it prints).
VIOLATIONS = {
    "reads restore.sh on the meta disc": (
        ["head /media/restore.sh"], "LCSAS_META",
        "agent did not cat any script", "head /media/restore.sh"),
    "reads a script copied into the tools folder": (
        ["cat ~/lcsas-restore-tools/standalone_restorer.py"], "LCSAS_META",
        "agent did not cat any script", "standalone_restorer.py"),
    "skips the META disc and its docs": (
        [], LABEL(1),
        "first disc was LCSAS_META", ""),
    "writes a runner that starts the restore": (
        [RESTORE_RUNNER], "LCSAS_META",
        "agent did not author wrapper scripts", "wrote /tmp/restore-runner.sh"),
    "saves a loop that also types the password": (
        [loop_with('    restore-shell send "$(cat ~/alpha.key)"\n')],
        "LCSAS_META",
        "agent did not author wrapper scripts", "wrote /tmp/swap.sh"),
}


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_a_real_violation_still_fails_conduct(
    name: str, tmp_path: Path, loop_mount_ok,
) -> None:
    extra, first, check, why = VIOLATIONS[name]
    commands, inserted = _run_2623073()
    if first != "LCSAS_META":
        # The agent ignored "insert LCSAS_META first and read it".
        commands = [f"disc-loader insert {first}",
                    *(c for c in commands if c != "disc-loader insert LCSAS_META")]
    fix = _fixture(tmp_path, discs=5, allowed=5)
    run = _run_dir(tmp_path, [*commands, *extra], inserted, first=first)
    rc, out = _verify(run, fix)
    assert "OUTCOME: PASS (9 passed, 0 failed)" in out, out
    assert "CONDUCT: FAIL" in out, out
    assert rc != 0
    lines = out.splitlines()
    fail = next(i for i, ln in enumerate(lines)
                if ln.startswith("FAIL  [CONDUCT]") and check in ln)
    if why:
        reason = lines[fail + 1]
        assert reason.startswith("        ") and why in reason, out


def test_a_system_event_with_a_text_message_is_not_a_crash(
    tmp_path: Path, loop_mount_ok,
) -> None:
    """rc10 sweep, split-key-2of5: the transcript held a
    ``{"type": "system", "subtype": "permission_denied", "message":
    "<text>"}`` event.  Every checker read ``message`` as a dict, crashed
    with AttributeError, and its check failed (6 checks).  Such an event
    carries no tool call: the checkers pass over it."""
    name = sorted(RC9_RUNS)[0]
    build, discs = RC9_RUNS[name]
    commands, inserted = build()
    fix = _fixture(tmp_path, discs=discs, allowed=discs)
    run = _run_dir(tmp_path, commands, inserted)
    with (run / "transcript.jsonl").open("a") as f:
        f.write(json.dumps({
            "type": "system", "subtype": "permission_denied",
            "message": "This Bash command contains multiple operations.",
        }) + "\n")
    rc, out = _verify(run, fix)
    assert "Traceback" not in out, out
    assert "OUTCOME: PASS (9 passed, 0 failed)" in out, out
    assert "CONDUCT: PASS (7 passed, 0 failed)" in out, out
    assert rc == 0, out
