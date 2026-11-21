"""Hardening tests: --restore-folder / --password-file flags on restore.sh [KEY-02].

The split-key instructions burned onto every disc tell the heir to run
``sh /mnt/restore.sh --restore-folder ~/restored`` and optionally ``--password-file
repo.key``.  These tests pin that contract:

* ``--restore-folder DIR`` sets the restore target (never misparsed as a
  positional RECOVERY_ROOT/SNAPSHOT pair);
* ``--password-file FILE`` feeds the password file (skips the Password: prompt)
  and fails fast on an unreadable path;
* any unknown ``-*`` flag exits 2 listing the valid flags — the
  silent-misparse class that produced uninterpretable
  snapshot-not-found errors is dead;
* a positional restore folder or snapshot (the old TARGET_DIR and
  SNAPSHOT_ID) is refused, exit 2: RECOVERY_ROOT is the only positional.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Default rust-triple the C build chooses on Linux x86_64; matches
# what detect_arch.sh emits.  Tests run on the host arch so this is
# the only target we need a stub for.
HOST_TARGET = "x86_64-unknown-linux-musl"


def _make_repo_skeleton(root: Path, name: str) -> Path:
    """Make a minimal restic-format-shaped repo dir at root/<name>."""
    repo = root / name
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    (repo / "snapshots").mkdir()
    (repo / "keys" / "stub_key").write_text("stub")
    return repo


def _install_stub_binary(recovery: Path, target: str, name: str) -> Path:
    """Install a stub recovery binary at ``recovery/bin/<target>/<name>``.

    The stub just prints its argv to stdout (one arg per line, prefixed
    ``ARG: ``) so tests can assert the script invoked it with the
    expected flags.  It exits 0.
    """
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _stub_args(stdout: str) -> list[str]:
    """Extract the ARG: lines printed by the stub binary."""
    return [
        line.removeprefix("ARG: ")
        for line in stdout.splitlines()
        if line.startswith("ARG: ")
    ]


def _arg_value(args: list[str], flag: str) -> str | None:
    """Return the value of ``--flag X`` in the arg list, or None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _recovery_fixture(tmp_path: Path) -> Path:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    return recovery


_BASE_ENV = {
    "LCSAS_MOUNT_DIRS": "",
    "LCSAS_NO_RELOCATE": "1",
}


# ── --restore-folder / --password-file appear in --help ─────────────────────────────────


def test_target_and_key_flags_in_help() -> None:
    """``sh restore.sh --help`` output must document --restore-folder and --password-file."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, (
        f"--help exited {res.returncode}.\nstderr:\n{res.stderr}"
    )
    assert "--restore-folder" in res.stdout, (
        f"--help must document the --restore-folder flag; got:\n{res.stdout}"
    )
    assert "--password-file" in res.stdout, (
        f"--help must document the --password-file flag; got:\n{res.stdout}"
    )


# ── --restore-folder sets TARGET (and is never a positional misparse) ─────────


def test_target_flag_sets_target_dir(tmp_path: Path) -> None:
    """``--restore-folder DIR RECOVERY`` passes DIR as the tier-1 --restore-folder."""
    recovery = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"

    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--restore-folder", str(target),
            str(recovery),
        ],
        input="stub-pw\n",
        capture_output=True, text=True,
        env={**os.environ, **_BASE_ENV}, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh --restore-folder failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--restore-folder") == str(target), (
        f"--restore-folder {target} should reach the binary as --restore-folder; got "
        f"{_arg_value(args, '--restore-folder')!r}.  full argv: {args}"
    )
    # The misparse class: a literal './--restore-folder' directory was the old
    # failure mode.  It must never exist.
    assert not Path("./--restore-folder").exists()
    assert _arg_value(args, "--snapshot") == "latest", (
        f"the restore folder must not be misparsed as a snapshot; argv: {args}"
    )


# ── --password-file feeds the password file and skips the prompt ───────────────


def test_key_flag_skips_password_prompt(tmp_path: Path) -> None:
    """``--password-file FILE`` exports LCSAS_PASSWORD_FILE; no Password: prompt fires."""
    recovery = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"
    pwfile = tmp_path / "repo.key"
    pwfile.write_text("hunter2\n")

    env = {**os.environ, **_BASE_ENV}
    env.pop("LCSAS_PASSWORD_FILE", None)
    env.pop("LCSAS_PASSWORD", None)
    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--restore-folder", str(target),
            "--password-file", str(pwfile),
            str(recovery),
        ],
        stdin=subprocess.DEVNULL,  # a prompt would read empty, not hang
        capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh --password-file failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "Password:" not in res.stderr, (
        f"--password-file must skip the Password: prompt.\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--password-file") == str(pwfile), (
        f"--password-file {pwfile} should reach the binary as --password-file; got "
        f"{_arg_value(args, '--password-file')!r}.  full argv: {args}"
    )
    assert _arg_value(args, "--restore-folder") == str(target)


# ── --password-file with an unreadable path fails fast ──────────────────────────


def test_key_flag_unreadable_path_exits_2(tmp_path: Path) -> None:
    """``--password-file /nonexistent`` → rc 2, message names the path, no prompts."""
    recovery = _recovery_fixture(tmp_path)
    missing = tmp_path / "no-such.key"

    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--password-file", str(missing),
            "--restore-folder", str(tmp_path / "restored"),
            str(recovery),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env={**os.environ, **_BASE_ENV}, timeout=10,
    )
    assert res.returncode == 2, (
        f"--password-file with unreadable path should exit 2; got {res.returncode}.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert str(missing) in res.stderr, (
        f"error must name the unreadable path.\nstderr:\n{res.stderr}"
    )


# ── unknown flags are rejected loudly (regression guard) ──────────────


def test_unknown_flag_exits_2_listing_valid_flags(tmp_path: Path) -> None:
    """``--bogus`` → rc 2 + valid-flag list; never demoted to a positional."""
    recovery = _recovery_fixture(tmp_path)

    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--bogus",
            "--restore-folder", str(tmp_path / "restored"),
            str(recovery),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env={**os.environ, **_BASE_ENV}, timeout=10,
    )
    assert res.returncode == 2, (
        f"unknown flag should exit 2; got {res.returncode}.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "--bogus" in res.stderr, (
        f"error must name the offending flag.\nstderr:\n{res.stderr}"
    )
    for known in ("--backup-set", "--restore-folder", "--password-file", "--version", "--help"):
        assert known in res.stderr, (
            f"error must list valid flag {known}.\nstderr:\n{res.stderr}"
        )
    # The old silent-misparse class: '--bogus' must never have been
    # treated as a restore folder (the stub would have printed ARG: lines).
    assert "ARG: " not in res.stdout, (
        f"binary must not run on unknown flags.\nstdout:\n{res.stdout}"
    )
    assert not Path("./--bogus").exists()


# ── a positional restore folder is refused ────────────────────────────────


def test_restore_folder_flag_plus_positional_folder_exits_2(tmp_path: Path) -> None:
    """``--restore-folder X RECOVERY Y`` → rc 2: Y would be the old positional."""
    recovery = _recovery_fixture(tmp_path)

    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--restore-folder", str(tmp_path / "a"),
            str(recovery), str(tmp_path / "b"),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env={**os.environ, **_BASE_ENV}, timeout=10,
    )
    assert res.returncode == 2, (
        f"--restore-folder plus a positional folder should exit 2; got "
        f"{res.returncode}.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "--restore-folder" in res.stderr and "--snapshot" in res.stderr, (
        f"the error must name the flags that replaced it.\nstderr:\n{res.stderr}"
    )
    assert "ARG: " not in res.stdout


# ── `--` ends flag parsing ─────────────────────────────────────────────


def test_double_dash_ends_flag_parsing(tmp_path: Path) -> None:
    """``--restore-folder X -- RECOVERY`` treats everything after ``--`` as positional."""
    recovery = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"

    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--restore-folder", str(target),
            "--",
            str(recovery),
        ],
        input="stub-pw\n",
        capture_output=True, text=True,
        env={**os.environ, **_BASE_ENV}, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh --restore-folder X -- RECOVERY failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--restore-folder") == str(target), (
        f"--restore-folder before -- should reach the binary; argv: {args}"
    )


# ── --restore-folder / --password-file with no argument exit non-zero ───────────────────


def test_target_flag_missing_arg_exits_nonzero() -> None:
    """``sh restore.sh --restore-folder`` with no DIR following exits non-zero."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--restore-folder"],
        capture_output=True, text=True,
        env={**os.environ, "LCSAS_NO_RELOCATE": "1"}, timeout=10,
    )
    assert res.returncode != 0, (
        f"restore.sh --restore-folder (no argument) should exit non-zero; got 0.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_key_flag_missing_arg_exits_nonzero() -> None:
    """``sh restore.sh --password-file`` with no FILE following exits non-zero."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--password-file"],
        capture_output=True, text=True,
        env={**os.environ, "LCSAS_NO_RELOCATE": "1"}, timeout=10,
    )
    assert res.returncode != 0, (
        f"restore.sh --password-file (no argument) should exit non-zero; got 0.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
