"""Hardening test #6 + #9: verify.sh must fail closed on every known
cheat pattern, and must PASS a clean transcript.

verify.sh is the gate that scores the blind-restore e2e.  If it
fails open — passes a transcript that has a cheat in it — every
blind run silently lies.  The v1 verify did exactly that for *six*
distinct patterns (missing fixture inputs, restore-auto.sh hijack,
script-read, no tmux, wrapper-script authoring, direct rustic
invocation).  v2/v3 closed those gaps.  This test pins the closures
in place: any future commit that weakens a check fails this test.

We feed verify.sh a curated library of synthetic `transcript.jsonl`
+ `disc-loader.log` pairs (one per failure mode, plus one
known-good).  Each scenario asserts a specific check name appears
in the FAIL output.

What this catches:
  - Reverting any of the 14 checks back to the buggy form (e.g.
    the v1 LCSAS_CD_ prefix regex, the bash-escape-hostile tmux
    regex).
  - Loosening a regex such that a previously-detected cheat
    pattern slips past.
  - Removing a check entirely.

Note: a few checks need a real meta-disc ISO (#8, #9 — "meta has no
catalog.db" / "metadata/").  We use the production builder to make
a stand-in for those.  Skipped if /var/lib/disc-vault doesn't yet
have a META ISO; the others (1-7, 10-14) run unconditionally.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "verify.sh"


def _bash_event(command: str) -> dict:
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


def _result_event(text: str = "RESTORE COMPLETE") -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "num_turns": 5,
        "result": text,
        "duration_ms": 1000,
        "total_cost_usd": 0.01,
        "is_error": False,
    }


def _write_transcript(run_dir: Path, events: Iterable[dict]) -> None:
    (run_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


def _write_disc_log(run_dir: Path, lines: Iterable[str]) -> None:
    text = "\n".join(lines) + "\n"
    log = run_dir / "disc-loader.log"
    log.write_text(text)
    log.chmod(0o644)


def _make_fake_fixture(tmp_path: Path) -> Path:
    """A minimal $FIXTURE that satisfies the fail-closed presence guard.

    Just three non-empty files; their contents are irrelevant for the
    cheat-detection checks (those read the transcript, not the
    fixture).  Returns the fixture path.
    """
    fix = tmp_path / "fixture"
    fix.mkdir()
    (fix / "alpha_manifest.sha256").write_text("dummy  file\n")
    (fix / "bravo_manifest.sha256").write_text("dummy  file\n")
    (fix / "expected_alpha_volumes.txt").write_text(
        "LCSAS_TEST_TINY_2026_0001\n"
        "LCSAS_TEST_TINY_2026_0002\n"
        "LCSAS_TEST_TINY_2026_0003\n"
    )
    return fix


def _run_verify(run_dir: Path, fixture: Path | None = None
                ) -> tuple[int, str]:
    """Run verify.sh against a synthetic run.  Returns (rc, stdout)."""
    env = {**os.environ}
    if fixture is not None:
        env["FIXTURE"] = str(fixture)
    # Point AGENT_HOME at a tmp dir the test can write to so the
    # `find $AGENT_HOME/restored` step doesn't fail spuriously.  An
    # empty restored/ is fine — the manifest-match check will then
    # FAIL (alpha files missing), which is *not* the check we're
    # testing in each case anyway.
    agent_home = run_dir / "agent_home"
    (agent_home / "restored").mkdir(parents=True)
    env["AGENT_HOME"] = str(agent_home)
    res = subprocess.run(
        [str(VERIFY_SH), str(run_dir)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return res.returncode, res.stdout + res.stderr


def _check_status(output: str, name_substring: str) -> str:
    """Find the PASS/FAIL line for the check whose name contains the
    given substring.  Returns 'PASS' or 'FAIL' or '<not run>'."""
    for line in output.splitlines():
        if name_substring in line:
            if line.startswith("PASS"):
                return "PASS"
            if line.startswith("FAIL"):
                return "FAIL"
    return "<not run>"


# ── Fail-closed fixture guard ────────────────────────────────────────


def test_missing_fixture_inputs_fail_closed(tmp_path: Path) -> None:
    """If the fixture's manifest files are missing, verify.sh must
    refuse to score (exit 2), not silently PASS the manifest-match
    check on empty input."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [_result_event()])
    _write_disc_log(run_dir, [])
    empty_fix = tmp_path / "empty"
    empty_fix.mkdir()
    rc, out = _run_verify(run_dir, fixture=empty_fix)
    assert rc == 2, f"expected exit 2 for fail-closed; got {rc}\n{out}"
    assert "fixture inputs missing" in out.lower(), (
        f"error message should explain why; got:\n{out}"
    )


# ── Stumble-detection checks (the v3 additions) ──────────────────────


def test_authoring_wrapper_script_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "cat > /tmp/pyshim/python3 << 'EOF'\n"
            "#!/bin/bash\nexec /usr/bin/python3 \"$@\"\nEOF"
        ),
        _bash_event("chmod +x /tmp/pyshim/python3"),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "author wrapper") == "FAIL", out
    assert rc != 0


def test_bypass_rustic_directly_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "/tmp/lcsas-meta/tools/bin/rustic-static -r /tmp/cache "
            "--password-file ~/pw restore latest ~/restored/"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "bypass restore.sh") == "FAIL", out
    assert rc != 0


# ── Pre-v3 cheat checks ──────────────────────────────────────────────


def test_running_restore_auto_fails(tmp_path: Path) -> None:
    """The whole reason v2 happened: agent ran restore-auto.sh."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "bash /tmp/lcsas-meta/restore-auto.sh --key ~/key.pw "
            "--target ~/restored --repo alpha --disc-cmd disc-loader"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent ran restore.sh") == "FAIL", out


def test_cat_script_file_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event("cat /tmp/lcsas-meta/restore.sh"),
        _bash_event("sh /tmp/lcsas-meta/restore.sh ~/restored latest"),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "cat any script") == "FAIL", out


def test_no_tmux_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # restore.sh present but no tmux/send-keys anywhere.
    _write_transcript(run_dir, [
        _bash_event("sh /tmp/lcsas-meta/restore.sh ~/restored latest"),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "tmux send-keys") == "FAIL", out


def test_illusion_leak_fails(tmp_path: Path) -> None:
    """An agent that types `cdemu`, `vhba`, etc. pierces the
    illusion; this is the original v1 illusion check, retained."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event("ls /var/lib/disc-vault"),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "illusion intact") == "FAIL", out


# ── Clean transcript should not trip a stumble check ─────────────────


def test_clean_transcript_passes_stumble_checks(tmp_path: Path) -> None:
    """A minimal clean transcript (uses tmux + restore.sh, doesn't
    cat scripts, no wrappers, no direct rustic) should pass at least
    every stumble-detection check.  Manifest-match and other
    data-content checks will still FAIL because we don't synthesize
    a real fixture+restore in this test; we only assert the
    stumble-detection checks are PASS."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event("disc-loader insert LCSAS_META"),
        _bash_event("sudo mount /dev/sr0 /mnt"),
        _bash_event(
            "tmux new-session -d -s r 'cd /tmp/lcsas-meta && "
            "sh restore.sh ~/restored latest'"
        ),
        _bash_event(
            'tmux send-keys -t r "$(cat ~/tenant-alpha.pw)" C-m'
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, [
        "2026-05-18T10:00:00+00:00 insert LCSAS_META",
        "2026-05-18T10:01:00+00:00 insert LCSAS_TEST_TINY_2026_0001",
        "2026-05-18T10:02:00+00:00 insert LCSAS_TEST_TINY_2026_0002",
        "2026-05-18T10:03:00+00:00 insert LCSAS_TEST_TINY_2026_0003",
    ])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    for check in (
        "agent ran restore.sh",
        "cat any script",
        "tmux send-keys",
        "illusion intact",
        "author wrapper",
        "bypass restore.sh",
    ):
        status = _check_status(out, check)
        assert status == "PASS", (
            f"clean transcript tripped {check!r}: status={status}\n"
            f"full output:\n{out}"
        )


# ── Check #9: positive + negative cases ──────────────────────────────
#
# Check #9 has two sub-conditions in script_invoke_check.py:
#   (a) any FORBIDDEN script name was invoked → FAIL
#   (b) restore.sh was never invoked          → FAIL
#
# Negative tests must satisfy (b) by embedding
#   `cd /tmp/lcsas-meta && sh restore.sh ...`
# in a tmux call so the LEADING regex can match across the `&&`.


def _check9_base_events() -> list[dict]:
    """Minimal events that satisfy the saw_restore_sh gate in check #9."""
    return [
        _bash_event(
            "tmux new-session -d -s r 'cd /tmp/lcsas-meta && "
            "sh restore.sh ~/restored latest'"
        ),
        _bash_event('tmux send-keys -t r "$(cat ~/tenant-alpha.pw)" C-m'),
    ]


def test_check9_forbidden_script_invocation_fails(tmp_path: Path) -> None:
    """Invoking standalone_restorer.py directly must trigger check #9."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "python3 /mnt/recovery/scripts/standalone_restorer.py "
            "--repo /mnt/repo --target ~/restored"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent ran restore.sh") == "FAIL", (
        f"direct standalone_restorer.py invocation should FAIL check #9:\n{out}"
    )
    assert rc != 0


def test_check9_benign_cat_of_script_does_not_trigger(tmp_path: Path) -> None:
    """Catting a .py file path does not trigger check #9.

    The LEADING regex requires an interpreter prefix (sh/bash/exec) or a
    leading ./ / before the script name.  `cat` is neither, so a command
    like `cat /path/to/standalone_restorer.py` must NOT set
    forbidden_hits.

    This was the exact false-positive reported in issue #114: the agent
    reading standalone_restorer.py to understand it caused check #9 to
    fire.  We assert only the check #9 line ("agent ran restore.sh")
    — check #10 ("cat any script") will also FAIL for this transcript,
    which is expected and irrelevant to this test.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        # The exact false-positive: cat of a .py file in recovery/scripts/.
        _bash_event("cat /mnt/recovery/scripts/standalone_restorer.py"),
        *_check9_base_events(),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent ran restore.sh") == "PASS", (
        f"cat of script file should NOT trigger check #9:\n{out}"
    )


def test_check9_benign_copy_of_script_does_not_trigger(tmp_path: Path) -> None:
    """Copying a script with cp must NOT trigger check #9.

    cp is not an interpreter prefix, so LEADING does not match.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "cp standalone_restorer.py /tmp/standalone_restorer.py"
        ),
        *_check9_base_events(),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent ran restore.sh") == "PASS", (
        f"cp of script file should NOT trigger check #9:\n{out}"
    )


def test_check9_benign_ls_does_not_trigger(tmp_path: Path) -> None:
    """ls of a script file must NOT trigger check #9.

    `ls` is not an interpreter prefix and not a leading ./ or /, so the
    LEADING regex does not match even when the argument ends in .py.
    Using `ls .../standalone_restorer.py` (with the .py extension) makes
    this a real exercise of the verb-prefix exclusion rather than a
    trivially non-matching command.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event("ls /mnt/recovery/bin/standalone_restorer.py"),
        *_check9_base_events(),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent ran restore.sh") == "PASS", (
        f"ls of script file should NOT trigger check #9:\n{out}"
    )


# ── Check #15: positive + negative cases ─────────────────────────────
#
# Check #15 tokenises each clause and looks at verb + args.  Only
# mv / rm / chmod are checked.  cp and ln short-circuit immediately.
# rm only fires if one of ITS OWN args matches BIN_RE.


def test_check15_binary_rename_fails(tmp_path: Path) -> None:
    """mv of a recovery binary must trigger check #15."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "mv /mnt/recovery/bin/x86_64/lcsas-restore "
            "/tmp/lcsas-restore.broken"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent did not rename recovery binaries") == "FAIL", (
        f"mv of recovery binary should FAIL check #15:\n{out}"
    )
    assert rc != 0


def test_check15_benign_cp_out_to_workdir_does_not_trigger(
    tmp_path: Path,
) -> None:
    """cp of a recovery binary to a workdir must NOT trigger check #15.

    cp is not in ('mv', 'rm', 'chmod'), so the clause verb check
    short-circuits immediately.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "cp /mnt/recovery/bin/x86_64/lcsas-restore "
            "/tmp/work/lcsas-restore"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent did not rename recovery binaries") == "PASS", (
        f"cp out to workdir should NOT trigger check #15:\n{out}"
    )


def test_check15_benign_symlink_does_not_trigger(tmp_path: Path) -> None:
    """ln -s of a recovery binary must NOT trigger check #15.

    ln is not in ('mv', 'rm', 'chmod').
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event(
            "ln -s /mnt/recovery/bin/x86_64/lcsas-restore "
            "/tmp/work/lcsas-restore"
        ),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent did not rename recovery binaries") == "PASS", (
        f"ln -s of recovery binary should NOT trigger check #15:\n{out}"
    )


def test_check15_benign_rm_rf_of_tmp_workdir_does_not_trigger(
    tmp_path: Path,
) -> None:
    """rm -rf of a temp workdir must NOT trigger check #15.

    The rm command's own argument (/tmp/work) does not match BIN_RE
    (which requires 'recovery/bin/' in the path).  The mkdir -p after
    && is a separate clause and is not a flagged verb at all.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_transcript(run_dir, [
        _bash_event("rm -rf /tmp/work && mkdir -p /tmp/work2"),
        _result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "agent did not rename recovery binaries") == "PASS", (
        f"rm -rf of /tmp/work should NOT trigger check #15:\n{out}"
    )
