"""Hardening #6 + #9: verify.sh fails closed on every cheat pattern and passes a clean run.

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

Since #890 the checks that read the transcript are functions in
checks.py, and their cheat patterns (wrapper authoring, direct rustic,
restore-auto.sh, script reads, illusion tokens, checks #9 and #15) are
unit-tested there (tests/unit/test_blind_checks.py).  What stays here
is what only a whole verify.sh run shows: the fixture guards, the
bypass check (verify.sh still calls no_bypass_check.py for it), the
managed-terminal grep, the disc-count checks, the 16 grouped verdict
lines with the reason under a FAIL, and a clean run passing every
transcript check through verify.sh.

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

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from tests.fixtures.blind_transcripts import bash_event, result_event, write_transcript

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "verify.sh"


def _write_disc_log(run_dir: Path, lines: Iterable[str]) -> None:
    text = "\n".join(lines) + "\n"
    log = run_dir / "disc-loader.log"
    log.write_text(text)
    log.chmod(0o644)


def _make_fake_fixture(tmp_path: Path) -> Path:
    """A minimal $FIXTURE that satisfies the fail-closed presence guard.

    Just four non-empty files; their contents are irrelevant for the
    cheat-detection checks (those read the transcript, not the
    fixture).  thrash_allowance.txt is what setup.py writes (#836):
    needed + 1 on its layout.  Returns the fixture path.
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
    (fix / "thrash_allowance.txt").write_text("4\n")
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
    write_transcript(run_dir, [result_event()])
    _write_disc_log(run_dir, [])
    empty_fix = tmp_path / "empty"
    empty_fix.mkdir()
    rc, out = _run_verify(run_dir, fixture=empty_fix)
    assert rc == 2, f"expected exit 2 for fail-closed; got {rc}\n{out}"
    assert "fixture inputs missing" in out.lower(), (
        f"error message should explain why; got:\n{out}"
    )


# ── The bypass check (verify.sh calls no_bypass_check.py, not checks.py) ──


def test_bypass_rustic_directly_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_transcript(run_dir, [
        bash_event(
            "/tmp/lcsas-meta/tools/bin/rustic-static -r /tmp/cache "
            "--password-file ~/pw restore latest ~/restored/"
        ),
        result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "bypass restore.sh") == "FAIL", out
    assert rc != 0


# ── The managed-terminal check (a grep in verify.sh, not checks.py) ──


def test_no_tmux_fails(tmp_path: Path) -> None:
    """Despite the name (kept for git-blame continuity), this really
    asserts: with no managed terminal session — neither legacy tmux
    nor the restore-shell facade introduced by PR #206 — the
    managed-terminal check must FAIL.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # restore.sh present but no tmux/send-keys or restore-shell anywhere.
    write_transcript(run_dir, [
        bash_event("sh /tmp/lcsas-meta/restore.sh ~/restored latest"),
        result_event(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    fix = _make_fake_fixture(tmp_path)
    rc, out = _run_verify(run_dir, fixture=fix)
    # Check renamed from "tmux send-keys" to the facade-aware name by PR #206
    # (restore-shell facade); the check now accepts either pattern.
    assert _check_status(out, "managed terminal session") == "FAIL", out


def _make_fixture_n_discs(tmp_path: Path, n: int, suffix: str = "",
                          allowed: int | None = None) -> Path:
    """Create a fixture with *n* expected alpha volumes and a thrash
    allowance of *allowed* (default n + 1, as setup.py's layout gives).

    alpha_manifest.sha256 and bravo_manifest.sha256 are present (non-empty)
    to satisfy the fail-closed presence guard; their contents don't matter
    for check #6.
    """
    fix = tmp_path / f"fixture_{n}{suffix}"
    fix.mkdir()
    (fix / "alpha_manifest.sha256").write_text("dummy  file\n")
    (fix / "bravo_manifest.sha256").write_text("dummy  file\n")
    lines = "\n".join(
        f"LCSAS_TEST_TINY_2026_{i:04d}" for i in range(1, n + 1)
    ) + "\n"
    (fix / "expected_alpha_volumes.txt").write_text(lines)
    (fix / "thrash_allowance.txt").write_text(
        f"{n + 1 if allowed is None else allowed}\n"
    )
    return fix


def _clean_disc_log_lines(labels: list[str]) -> list[str]:
    """Build a disc-loader.log that starts with LCSAS_META then the given labels."""
    base = ["2026-05-18T10:00:00+00:00 insert LCSAS_META"]
    for i, label in enumerate(labels, start=1):
        ts = f"2026-05-18T10:{i:02d}:00+00:00"
        base.append(f"{ts} insert {label}")
    return base


def _clean_transcript_events() -> list[dict]:
    """Minimal transcript that passes all stumble-detection checks."""
    return [
        bash_event("disc-loader insert LCSAS_META"),
        bash_event("sudo mount /dev/sr0 /mnt"),
        bash_event(
            "tmux new-session -d -s r 'cd /tmp/lcsas-meta && "
            "sh restore.sh ~/restored latest'"
        ),
        bash_event('tmux send-keys -t r "$(cat ~/tenant-alpha.pw)" C-m'),
        result_event(),
    ]


# ── Check #6: unique-disc-insert threshold ───────────────────────────


def test_check6_same_disc_repeated_passes(tmp_path: Path) -> None:
    """Old behaviour: 10 total inserts of disc 0001 with NEEDED=1 → FAIL at 5×.
    New behaviour: 1 unique disc inserted with NEEDED=1 → PASS (unique ≤ NEEDED+1=2).

    This is the old-failing / new-passing case.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_transcript(run_dir, _clean_transcript_events())
    # Disc 0001 inserted ten times; NEEDED is 1.
    repeated = ["LCSAS_TEST_TINY_2026_0001"] * 10
    _write_disc_log(run_dir, _clean_disc_log_lines(repeated))
    fix = _make_fixture_n_discs(tmp_path, n=1)
    _rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "unique discs inserted") == "PASS", (
        "repeated single disc should PASS the unique-insert check\n"
        f"full output:\n{out}"
    )


def test_check6_many_unique_discs_fails(tmp_path: Path) -> None:
    """Old behaviour: 5 unique inserts with NEEDED=2 → PASS (5 ≤ 10 = NEEDED×5).
    New behaviour: 5 unique discs with NEEDED=2 → FAIL (5 > NEEDED+1=3).

    This is the old-passing / new-failing case.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_transcript(run_dir, _clean_transcript_events())
    # 5 unique discs inserted; only the first two are expected (NEEDED=2).
    five_unique = [
        "LCSAS_TEST_TINY_2026_0001",
        "LCSAS_TEST_TINY_2026_0002",
        "LCSAS_TEST_TINY_2026_0003",
        "LCSAS_TEST_TINY_2026_0004",
        "LCSAS_TEST_TINY_2026_0005",
    ]
    _write_disc_log(run_dir, _clean_disc_log_lines(five_unique))
    fix = _make_fixture_n_discs(tmp_path, n=2)
    _rc, out = _run_verify(run_dir, fixture=fix)
    assert _check_status(out, "unique discs inserted") == "FAIL", (
        "5 unique discs inserted when NEEDED=2 should FAIL the unique-insert check\n"
        f"full output:\n{out}"
    )


def test_check6_allowance_comes_from_the_fixture(tmp_path: Path) -> None:
    """#836: the bound is setup.py's thrash_allowance.txt, not a fixed
    needed + 1.  5 distinct discs with 2 needed pass on an allowance of
    5 and fail on 4."""
    five = [f"LCSAS_TEST_TINY_2026_{i:04d}" for i in range(1, 6)]
    for allowed, want in ((5, "PASS"), (4, "FAIL")):
        run_dir = tmp_path / f"run{allowed}"
        run_dir.mkdir()
        write_transcript(run_dir, _clean_transcript_events())
        _write_disc_log(run_dir, _clean_disc_log_lines(five))
        fix = _make_fixture_n_discs(tmp_path, n=2, suffix=str(allowed),
                                    allowed=allowed)
        _rc, out = _run_verify(run_dir, fixture=fix)
        assert _check_status(out, "unique discs inserted") == want, out
        assert f"allowed={allowed}" in out, out


def test_missing_or_bad_allowance_refuses_to_score(tmp_path: Path) -> None:
    for name, text in (("missing", None), ("bad", "3; echo injected\n")):
        run_dir = tmp_path / f"run-{name}"
        run_dir.mkdir()
        write_transcript(run_dir, [result_event()])
        _write_disc_log(run_dir, [])
        fix = _make_fixture_n_discs(tmp_path, n=2, suffix=name)
        if text is None:
            (fix / "thrash_allowance.txt").unlink()
        else:
            (fix / "thrash_allowance.txt").write_text(text)
        rc, out = _run_verify(run_dir, fixture=fix)
        assert rc == 2, out
        assert "thrash_allowance.txt" in out, out
        assert not any(ln.startswith(("PASS", "FAIL  [")) for ln in out.splitlines())


# ── OUTCOME / CONDUCT groups and the reason under a FAIL (#827) ──────


def test_every_check_has_a_group_and_a_failure_shows_why(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_transcript(run_dir, [
        bash_event("cat /tmp/lcsas-meta/restore.sh"),
        *_clean_transcript_events(),
    ])
    _write_disc_log(run_dir, ["2026-05-18T10:00:00+00:00 insert LCSAS_META"])
    rc, out = _run_verify(run_dir, fixture=_make_fake_fixture(tmp_path))
    lines = out.splitlines()
    verdicts = [ln for ln in lines if ln.startswith(("PASS", "FAIL"))]
    assert len(verdicts) == 16, out
    assert all(ln.split()[1] in ("[OUTCOME]", "[CONDUCT]") for ln in verdicts), out
    i = next(i for i, ln in enumerate(lines) if "cat any script" in ln)
    assert lines[i].startswith("FAIL  [CONDUCT]"), out
    assert lines[i + 1].startswith("        AGENT READ SCRIPT FILE(S):"), out
    assert "CONDUCT: FAIL" in out and "OUTCOME: FAIL" in out, out
    assert rc != 0


# ── Clean transcript should not trip a stumble check ─────────────────


def test_clean_transcript_passes_stumble_checks(tmp_path: Path) -> None:
    """A minimal clean transcript (uses the restore-shell facade +
    restore.sh, doesn't cat scripts, no wrappers, no direct rustic)
    should pass every stumble-detection check.  Manifest-match and
    other data-content checks will still FAIL because we don't
    synthesize a real fixture+restore in this test; we only assert
    the stumble-detection checks are PASS.

    Updated for PR #206 (restore-shell facade): canonical clean
    transcripts now invoke `restore-shell start sh restore.sh ...`
    rather than hand-rolled tmux idioms.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_transcript(run_dir, [
        bash_event("disc-loader insert LCSAS_META"),
        bash_event("sudo mount /dev/sr0 /mnt"),
        bash_event(
            "restore-shell start sh /mnt/restore.sh ~/restored/ latest"
        ),
        bash_event(
            'restore-shell send "$(cat ~/tenant-alpha.pw)"'
        ),
        result_event(),
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
        # Check renamed from "tmux send-keys" by PR #206 (restore-shell
        # facade).  The clean transcript above invokes the facade
        # directly so this asserts the facade satisfies the check.
        "managed terminal session",
        "illusion intact",
        "author wrapper",
        "bypass restore.sh",
        # With the above, every check that reads the transcript: each
        # line that calls checks.py (#890) passes a clean run.
        "first disc was LCSAS_META",
        "RESTORE COMPLETE printed",
        "rename recovery binaries",
    ):
        status = _check_status(out, check)
        assert status == "PASS", (
            f"clean transcript tripped {check!r}: status={status}\n"
            f"full output:\n{out}"
        )
