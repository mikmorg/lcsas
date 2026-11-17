"""How restore.sh reports what a tier's exit code means (#799, #805, #803, #804).

Each test runs restore.sh against stub tiers, so it needs no real
repository and no binaries:

* #799 / #783: a restore that FINISHED but could not restore some items
  (tier 1 exit 1 saying "finished with errors", tier 3 exit 2), with the
  list in RESTORE_FAILURES.txt written by this run, is reported as
  "finished, N item(s) could not be restored", exits 4 (not 1 or 2), and
  gives no resume advice.  Tier 1's exit 1 for a damaged disc (no
  "finished with errors") stays a failure that can be carried on.  A
  RESTORE_FAILURES.txt an earlier run left does not count.  Exit 75 (q at
  tier 1's disc prompt) says "stopped at your request"; exit 74 (the
  disk is full) says so and keeps the resume advice.
* #805: a wrong password gives exit 77 on tiers 2, 2b and 3 too, and the
  password is asked again on a terminal; tier 3 honours
  LCSAS_INTERACTIVE; the session log line is written after the tier ran,
  with its outcome.
* #803: on an ARMv6 processor no bundled ARMv7 program is run, and the
  message says what still works.
* #804: the tier-3 probe names each reason, and its Python floor equals
  the reader's.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
BUILDER = REPO_ROOT / "src" / "lcsas" / "restore" / "standalone_builder.py"
HOST_TARGET = "x86_64-unknown-linux-musl"

HEADER = (
    "LCSAS restore: items that were NOT restored.\n"
    "Written by a stub.  Everything else was restored.\n"
    "\n"
    "path\treason\tblob_id\n"
)
ROWS = (
    "a/bad1\tname refused: test\t\n"
    "a/bad2\tlink refused: test\t\n"
    "a/fifo\tspecial file not created: test\t\n"
)


def _fixture(tmp_path: Path, *, data: bool = False) -> Path:
    recovery = tmp_path / "recovery"
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    if data:
        (repo / "data").mkdir()
    return recovery


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


# Shell that sets $target and $pwfile from the tiers' flags.
ARGS = """\
target=""; pwfile=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) target="$2"; shift ;;
        --password-file) pwfile="$2"; shift ;;
    esac
    shift
done
"""


def _tier1(recovery: Path, body: str) -> Path:
    return _stub(recovery / "bin" / HOST_TARGET / "lcsas-restore",
                 'case "$1" in --help) exit 0 ;; esac\n'
                 'for a in "$@"; do case "$a" in --list-*) exit 0 ;; esac; done\n'
                 + ARGS + textwrap.dedent(body))


def _python(stub_dir: Path, body: str) -> Path:
    """A host python3 that passes the probe, then runs BODY."""
    return _stub(stub_dir / "python3",
                 'case "$1" in -c) exit 0 ;; esac\n'
                 'printf "%s\\n" "$@" > "$(dirname "$0")/argv"\n'
                 + ARGS + textwrap.dedent(body))


def _run(recovery: Path, tmp_path: Path, stdin: str = "pw\n",
         **extra: str) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    (tmp_path / "standalone_restorer.py").write_text("# stub\n")
    env = {
        **os.environ,
        # /usr/local/bin (a host restic/rustic) is left out: a stock
        # tool would be tier 2b.
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LCSAS_TARGET": HOST_TARGET,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_INTERACTIVE": "off",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(extra)
    env = {k: v for k, v in env.items() if v != "<unset>"}
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored"),
         "latest"],
        input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )


def _log(tmp_path: Path) -> list[str]:
    log = tmp_path / "home" / ".lcsas-restore-log"
    return log.read_text().splitlines() if log.exists() else []


def _tools(tmp_path: Path) -> Path:
    return tmp_path / "home" / "lcsas-restore-tools"


# ── #799: finished, but not every item restored ─────────────────────

FINISHED_WITH_ERRORS = f"""\
printf '%s%s' '{HEADER}' '{ROWS}' > "$target/RESTORE_FAILURES.txt"
echo '[lcsas-restore] ERROR: 2 item(s) were NOT restored.' >&2
echo '[lcsas-restore] finished with errors: every other item was restored' >&2
exit 1
"""


def test_tier1_finished_with_errors_is_reported_as_finished(
        tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    _tier1(recovery, FINISHED_WITH_ERRORS)

    res = _run(recovery, tmp_path)

    assert res.returncode == 4, res.stderr
    target = tmp_path / "restored"
    assert ("The restore finished, but 2 item(s) could not be restored."
            in res.stderr), res.stderr
    assert f"{target}/RESTORE_FAILURES.txt" in res.stderr, res.stderr
    assert "ITEMS NOT RESTORED" in res.stderr, res.stderr
    assert "resume.sh" not in res.stderr, res.stderr
    assert "stopped before it finished" not in res.stderr, res.stderr
    assert "stopped with exit code" not in res.stderr, res.stderr
    # The set is done: nothing to carry on, the tools folder goes.
    assert not _tools(tmp_path).exists()
    assert _log(tmp_path)[-1].endswith("tier=1  discs=0  result=not-all-restored")


def test_tier1_exit_1_without_finished_with_errors_is_a_failure(
        tmp_path: Path) -> None:
    """Tier 1 also writes the list when it stops on a damaged disc; that
    restore did not finish, so the resume advice stays."""
    recovery = _fixture(tmp_path)
    _tier1(recovery, f"""\
        printf '%s%s' '{HEADER}' '{ROWS}' > "$target/RESTORE_FAILURES.txt"
        echo 'ERROR: tree restore failed' >&2
        exit 1
    """)

    res = _run(recovery, tmp_path)

    assert res.returncode == 1, res.stderr
    assert "could not be restored" not in res.stderr, res.stderr
    assert "resume.sh" in res.stderr, res.stderr
    assert _log(tmp_path)[-1].endswith("result=failed-1")


def test_old_failures_file_does_not_count(tmp_path: Path) -> None:
    """A RESTORE_FAILURES.txt an earlier run left is removed before the
    tier runs, so a later failure is not taken for "finished"."""
    recovery = _fixture(tmp_path)
    target = tmp_path / "restored"
    target.mkdir()
    (target / ".lcsas-restore-marker").write_text("alpha\n")
    (target / "RESTORE_FAILURES.txt").write_text(HEADER + ROWS)
    _tier1(recovery, """\
        test -e "$target/RESTORE_FAILURES.txt" && echo STALE_STILL_THERE
        echo '[lcsas-restore] finished with errors: every other item' >&2
        exit 1
    """)

    res = _run(recovery, tmp_path, LCSAS_FORCE_NONEMPTY_TARGET="1")

    assert res.returncode == 1, res.stderr
    assert "STALE_STILL_THERE" not in res.stdout
    assert "could not be restored" not in res.stderr, res.stderr


def test_users_own_failures_file_is_kept(tmp_path: Path) -> None:
    """Only a file that starts with the tiers' header is removed."""
    recovery = _fixture(tmp_path)
    target = tmp_path / "restored"
    target.mkdir()
    own = target / "RESTORE_FAILURES.txt"
    own.write_text("my own notes\n")
    _tier1(recovery, "exit 0\n")

    res = _run(recovery, tmp_path, LCSAS_FORCE_NONEMPTY_TARGET="1")

    assert res.returncode == 0, res.stderr
    assert own.read_text() == "my own notes\n"


def test_tier3_exit_2_with_list_is_finished(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    _python(tmp_path / "stubbin", f"""\
        printf '%s%s' '{HEADER}' '{ROWS}' > "$target/RESTORE_FAILURES.txt"
        echo '[lcsas-restore] ERROR: 2 item(s) were NOT restored.' >&2
        echo '[lcsas-restore] 2 item(s) could NOT be restored. See x' >&2
        exit 2
    """)

    res = _run(recovery, tmp_path)

    assert res.returncode == 4, res.stderr
    assert ("The restore finished, but 2 item(s) could not be restored."
            in res.stderr), res.stderr
    assert "[tier 3] FAILED" not in res.stderr, res.stderr
    assert "resume.sh" not in res.stderr, res.stderr
    assert _log(tmp_path)[-1].endswith("tier=3  discs=0  result=not-all-restored")


def test_tier3_count_from_the_list_when_not_said(tmp_path: Path) -> None:
    """N comes from the tier's own line, else from the error rows of the
    list (special files and links the file system cannot hold are not
    errors)."""
    recovery = _fixture(tmp_path)
    _python(tmp_path / "stubbin", f"""\
        printf '%s%s' '{HEADER}' '{ROWS}' > "$target/RESTORE_FAILURES.txt"
        echo '[lcsas-restore] some item(s) could NOT be restored.' >&2
        exit 2
    """)

    res = _run(recovery, tmp_path)

    assert res.returncode == 4, res.stderr
    assert "but 2 item(s) could not be restored" in res.stderr, res.stderr


def test_tier3_usage_error_is_still_a_failure(tmp_path: Path) -> None:
    """Exit 2 without a list (argparse's usage error) stays a failure."""
    recovery = _fixture(tmp_path)
    _python(tmp_path / "stubbin", """\
        echo 'usage: standalone_restorer.py [-h]' >&2
        exit 2
    """)

    res = _run(recovery, tmp_path)

    assert res.returncode == 2, res.stderr
    assert "[tier 3] FAILED (rc=2)" in res.stderr, res.stderr
    assert "could not be restored" not in res.stderr, res.stderr


def test_tier1_q_at_disc_prompt_says_stopped(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    _tier1(recovery, "exit 75\n")

    res = _run(recovery, tmp_path)

    assert res.returncode == 75, res.stderr
    assert ("[tier 1] stopped at the disc prompt at your request."
            in res.stderr), res.stderr
    assert "resume.sh" in res.stderr, res.stderr
    assert _log(tmp_path)[-1].endswith("result=stopped")


@pytest.mark.parametrize("tier", ["1", "3"])
def test_disk_full_exit_74(tmp_path: Path, tier: str) -> None:
    """Exit 74 (tiers 1 and 3: the disk filled up) ends the run with 74,
    says the disk is full and keeps the resume advice -- also under
    LCSAS_TIER_FALLBACK=1, where another tier would only fill it again."""
    recovery = _fixture(tmp_path)
    if tier == "1":
        _tier1(recovery, "exit 74\n")
    else:
        _python(tmp_path / "stubbin", "exit 74\n")

    res = _run(recovery, tmp_path, LCSAS_TIER_FALLBACK="1")

    assert res.returncode == 74, res.stderr
    assert f"[tier {tier}] the disk is full." in res.stderr, res.stderr
    assert "resume.sh" in res.stderr, res.stderr
    assert "falling through" not in res.stderr, res.stderr
    assert _log(tmp_path)[-1].endswith("result=disk-full")


def test_finished_with_errors_does_not_fall_through(tmp_path: Path) -> None:
    """Under LCSAS_TIER_FALLBACK=1 a finished restore is not redone by
    tier 2."""
    recovery = _fixture(tmp_path, data=True)
    _tier1(recovery, FINISHED_WITH_ERRORS)
    _stub(recovery / "bin" / HOST_TARGET / "rustic-static",
          "echo TIER2_RAN\nexit 0\n")

    res = _run(recovery, tmp_path, LCSAS_TIER_FALLBACK="1")

    assert res.returncode == 4, res.stderr
    assert "TIER2_RAN" not in res.stdout


# ── #805: a wrong password on every tier, LCSAS_INTERACTIVE, the log ──

def _rustic(path: Path) -> Path:
    """rustic: the password "good" opens the keys; any other is refused
    in rustic's words (exit 1)."""
    return _stub(path, """\
        case "$1" in --help|-V|--version) exit 0 ;; esac
        pw=""; prev=""
        for a in "$@"; do
            [ "$prev" = --password-file ] && pw="$(cat "$a")"
            prev="$a"
        done
        if [ "$pw" != good ]; then
            printf 'error: The password that has been entered, seems to be ' >&2
            printf 'incorrect. No suitable key found for the given password.\\n' >&2
            exit 1
        fi
        case " $* " in *" restore "*) echo RUSTIC_RESTORED ;; esac
        exit 0
    """)


def _restic(path: Path) -> Path:
    """restic: "good" opens the keys; any other: exit 12."""
    return _stub(path, """\
        case "$1" in --help|version) exit 0 ;; esac
        if [ "$(cat "$RESTIC_PASSWORD_FILE")" != good ]; then
            echo 'Fatal: wrong password or no key found' >&2
            exit 12
        fi
        case " $* " in *" restore "*) echo RESTIC_RESTORED ;; esac
        exit 0
    """)


def test_tier2_wrong_password_exits_77(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path, data=True)
    _rustic(recovery / "bin" / HOST_TARGET / "rustic-static")

    res = _run(recovery, tmp_path, stdin="bad\n")

    assert res.returncode == 77, res.stderr
    assert "[tier 2] wrong password" in res.stderr, res.stderr
    assert _log(tmp_path)[-1].endswith("tier=2  discs=0  result=wrong-password")


def test_tier2b_wrong_password_exits_77_and_falls_no_further(
        tmp_path: Path) -> None:
    recovery = _fixture(tmp_path, data=True)
    _restic(recovery / "bin" / HOST_TARGET / "restic")
    _python(tmp_path / "stubbin", "echo TIER3_RAN\nexit 0\n")

    res = _run(recovery, tmp_path, stdin="bad\n", LCSAS_TIER_FALLBACK="1")

    assert res.returncode == 77, res.stderr
    assert "[tier 2b] wrong password" in res.stderr, res.stderr
    assert "TIER3_RAN" not in res.stdout


def test_tier3_wrong_password_exits_77(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    _python(tmp_path / "stubbin", """\
        echo '[lcsas-restore] wrong password: (1 key file(s) rejected it)' >&2
        exit 77
    """)

    res = _run(recovery, tmp_path)

    assert res.returncode == 77, res.stderr
    assert "[tier 3] wrong password" in res.stderr, res.stderr
    assert "[tier 3] FAILED" not in res.stderr, res.stderr


@pytest.mark.parametrize("tier", ["2", "2b", "3"])
def test_wrong_password_is_asked_again(tmp_path: Path, tier: str) -> None:
    """On a terminal (LCSAS_INTERACTIVE=on) a wrong password is asked for
    again on tiers 2, 2b and 3, as on tier 1, and the restore goes on
    with the right one."""
    recovery = _fixture(tmp_path, data=tier != "3")
    if tier == "2":
        _rustic(recovery / "bin" / HOST_TARGET / "rustic-static")
        done = "RUSTIC_RESTORED"
    elif tier == "2b":
        _restic(recovery / "bin" / HOST_TARGET / "restic")
        done = "RESTIC_RESTORED"
    else:
        _python(tmp_path / "stubbin", """\
            if [ "$(cat "$pwfile")" != good ]; then exit 77; fi
            echo PYTHON_RESTORED
            exit 0
        """)
        done = "PYTHON_RESTORED"

    res = _run(recovery, tmp_path, stdin="bad\ngood\n",
               LCSAS_INTERACTIVE="on", LCSAS_ANOTHER_SET="0")

    assert res.returncode == 0, res.stderr
    assert done in res.stdout, res.stdout
    assert res.stdout.count(done) == 1, res.stdout
    assert "That password was not accepted for alpha." in res.stderr, res.stderr


@pytest.mark.parametrize(("value", "passed"), [
    ("off", "off"), ("auto", "auto"), ("on", "on"), ("<unset>", "on"),
])
def test_tier3_gets_lcsas_interactive(tmp_path: Path, value: str,
                                      passed: str) -> None:
    recovery = _fixture(tmp_path)
    _python(tmp_path / "stubbin", "exit 0\n")

    res = _run(recovery, tmp_path, LCSAS_INTERACTIVE=value,
               LCSAS_ANOTHER_SET="0")

    assert res.returncode == 0, res.stderr
    argv = (tmp_path / "stubbin" / "argv").read_text().splitlines()
    i = argv.index("--interactive")
    assert argv[i + 1] == passed, argv


@pytest.mark.parametrize("tier", ["1", "3"])
def test_session_log_written_after_the_run(tmp_path: Path, tier: str) -> None:
    """The log line is written once the tier has run, with its outcome."""
    recovery = _fixture(tmp_path)
    log = tmp_path / "home" / ".lcsas-restore-log"
    body = f"""\
        test -e '{log}' && echo LOG_WRITTEN_BEFORE_THE_RUN
        exit 17
    """
    if tier == "1":
        _tier1(recovery, body)
    else:
        _python(tmp_path / "stubbin", body)

    res = _run(recovery, tmp_path)

    assert res.returncode == 17, res.stderr
    assert "LOG_WRITTEN_BEFORE_THE_RUN" not in res.stdout
    assert _log(tmp_path)[-1].endswith(f"tier={tier}  discs=0  result=failed-17")


# ── #803: ARMv6 ──────────────────────────────────────────────────────

def _armv6(tmp_path: Path) -> dict[str, str]:
    d = tmp_path / "stubbin"
    _stub(d / "uname", """\
        case "$1" in -m) echo armv6l ;; *) echo Linux ;; esac
    """)
    return {"LCSAS_TARGET": "<unset>"}


ARMV7 = "armv7-unknown-linux-gnueabihf"


def test_armv6_skips_the_armv7_programs_and_uses_host_python(
        tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    for name in ("lcsas-restore", "rustic-static", "lcsas-keyshare"):
        _stub(recovery / "bin" / ARMV7 / name,
              f"echo {name}_RAN_ON_ARMV6\nexit 0\n")
    _stub(recovery / "bin" / ARMV7 / "python" / "bin" / "python3",
          "echo BUNDLED_PYTHON_RAN_ON_ARMV6\nexit 0\n")
    _python(tmp_path / "stubbin", "echo HOST_PYTHON_RAN\nexit 0\n")

    res = _run(recovery, tmp_path, LCSAS_ANOTHER_SET="0", **_armv6(tmp_path))

    assert res.returncode == 0, res.stderr
    assert "_RAN_ON_ARMV6" not in res.stdout, res.stdout
    assert "HOST_PYTHON_RAN" in res.stdout, res.stdout
    assert "This computer has an ARMv6 processor (armv6l" in res.stderr
    assert "[tier 1] skipped: lcsas-restore needs an ARMv7 or newer" in res.stderr
    assert ("the bundled Python cannot run on this host (it needs an ARMv7 "
            "or newer processor); using the host python3") in res.stderr


def test_armv6_without_host_python_says_so(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    _stub(recovery / "bin" / ARMV7 / "lcsas-restore", "exit 0\n")
    stub_dir = tmp_path / "stubbin"
    for name in ("python3", "python"):
        _stub(stub_dir / name, "exit 127\n")

    res = _run(recovery, tmp_path, **_armv6(tmp_path))

    assert res.returncode == 64, res.stderr
    assert "nothing on this disc can run on this computer" in res.stderr
    assert "need ARMv7 or newer" in res.stderr, res.stderr
    assert "needs Python 3.10 or newer on this" in res.stderr, res.stderr
    assert "Password:" not in res.stderr


def test_armv6_check_disc_refuses(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    img = tmp_path / "disc.iso"
    img.write_bytes(b"\0" * 2048)
    env = {**os.environ, "PATH": f"{tmp_path / 'stubbin'}:/usr/bin:/bin"}
    env.pop("LCSAS_TARGET", None)
    _armv6(tmp_path)
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--check-disc", str(img), str(recovery)],
        capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 3, res.stderr
    assert "lcsas-ecc needs ARMv7 or newer" in res.stderr, res.stderr


def test_sigill_binary_fails_preflight(tmp_path: Path) -> None:
    """A tier binary that dies with an illegal instruction (a program for
    a newer processor) is skipped like a wrong-architecture one."""
    recovery = _fixture(tmp_path)
    _stub(recovery / "bin" / HOST_TARGET / "lcsas-restore",
          "echo TIER1_RAN\nkill -ILL $$\n")
    _python(tmp_path / "stubbin", "echo HOST_PYTHON_RAN\nexit 0\n")

    res = _run(recovery, tmp_path, LCSAS_ANOTHER_SET="0")

    assert "failed pre-flight" in res.stderr, res.stderr
    assert "HOST_PYTHON_RAN" in res.stdout, res.stdout
    assert res.returncode == 0, res.stderr


# ── #804: the tier-3 probe ──────────────────────────────────────────

def test_bundled_python_not_executable_is_named(tmp_path: Path) -> None:
    """A bundled interpreter without its run permission is "not
    executable", never "no bundled CPython"."""
    recovery = _fixture(tmp_path)
    py = recovery / "bin" / HOST_TARGET / "python" / "bin" / "python3"
    _stub(py, "exit 0\n").chmod(0o644)
    _python(tmp_path / "stubbin", "echo HOST_PYTHON_RAN\nexit 0\n")

    res = _run(recovery, tmp_path, LCSAS_ANOTHER_SET="0")

    assert res.returncode == 0, res.stderr
    assert ("the bundled Python cannot run on this host (it is not "
            "executable here") in res.stderr, res.stderr


def test_host_python_too_old_is_named(tmp_path: Path) -> None:
    recovery = _fixture(tmp_path)
    stub_dir = tmp_path / "stubbin"
    _stub(stub_dir / "python3", """\
        case "$2" in *version_info\\[:2\\]*) echo 3.9; exit 0 ;; esac
        exit 1
    """)
    _stub(stub_dir / "python", "exit 127\n")

    res = _run(recovery, tmp_path)

    assert res.returncode == 64, res.stderr
    assert ("no bundled Python at" in res.stderr), res.stderr
    assert ("python3 (Python 3.9) is there, but the reader needs Python 3.10 "
            "or newer.") in res.stderr, res.stderr


def test_probe_floor_is_the_readers_floor() -> None:
    """restore.sh accepts exactly the Pythons the reader runs on."""
    reader = re.search(r"sys\.version_info < \((\d+), (\d+)\)",
                       BUILDER.read_text())
    probe = re.search(r"sys\.exit\(sys\.version_info < \((\d+), (\d+)\)\)",
                      RESTORE_SH.read_text())
    assert reader and probe
    assert probe.groups() == reader.groups(), (probe.groups(), reader.groups())
    major, minor = reader.groups()
    assert f"Python {major}.{minor} or newer" in RESTORE_SH.read_text()
