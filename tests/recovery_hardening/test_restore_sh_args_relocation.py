"""restore.sh reads its arguments before it copies anything off the META disc (#800).

Hardening for #800, #625, #807, #802 and #806.

* #800: run from the META disc, ``--help``, ``--version`` and
  ``--check-disc IMAGE`` copied about 130 MB of tools first and left the
  tools folder behind; ``--check-disc`` with a relative image path then
  failed, because the copy starts in ``/``.
* #625: a relative restore folder (and a relative ``--password-file FILE``) typed
  from the META disc landed under ``/`` for the same reason.
* #807: ``--restore-folder DIR`` could not be combined with a snapshot:
  ``--snapshot ID`` is the way now.
* #802: with one backup set on the discs, ``--repo NAME`` restored that
  set whatever NAME was.
* #806: every failing run left a ``/tmp/lcsas-restore-exit.*`` file.

The META copies here are read-only, as a mounted disc is, and named by
LCSAS_META_DISC, so restore.sh relocates exactly as it does off a disc.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from .test_restore_sh_tools_folder import (
    HOST_TARGET,
    RESTORE_SH,
    SET_ID,
    SET_NAME,
    _arg,
    _env,
    _make_writable,
    _meta_disc,
    _strip_write,
)

# The stub tier 1 of test_restore_sh_tools_folder, and it also checks
# that the password file it is given can be read (a relative --password-file FILE
# read from / could not be).
STUB = textwrap.dedent("""\
    #!/bin/sh
    [ "${1:-}" = "--help" ] && exit 0
    target=""
    pwfile=""
    prev=""
    for a in "$@"; do
        printf 'ARG: %s\\n' "$a"
        [ "$prev" = "--restore-folder" ] && target="$a"
        [ "$prev" = "--password-file" ] && pwfile="$a"
        prev="$a"
    done
    if [ -n "$pwfile" ] && [ ! -r "$pwfile" ]; then
        echo "stub: cannot read the password file $pwfile" >&2
        exit 9
    fi
    rc="${STUB_RC:-0}"
    [ "$rc" = 0 ] && printf 'restored\\n' > "$target/restored.txt"
    exit "$rc"
""")

# lcsas-ecc stub: `verify IMAGE` exits 0 when IMAGE can be read.
ECC_STUB = textwrap.dedent("""\
    #!/bin/sh
    printf 'ECC: %s %s\\n' "$1" "$2"
    [ -r "$2" ] || exit 3
    exit 0
""")


@pytest.fixture
def readonly() -> Iterator[Callable[[Path], None]]:
    made: list[Path] = []

    def make(root: Path) -> None:
        _strip_write(root)
        made.append(root)

    yield make
    for root in made:
        _make_writable(root)


def _meta(tmp_path: Path, stub: str = STUB) -> Path:
    """A META disc copy with the real restore.sh, the stub tier 1 and a
    stub lcsas-ecc; returns its root."""
    meta = tmp_path / "LCSAS_META"
    recovery = _meta_disc(meta, script=True)
    bin_dir = recovery / "bin" / HOST_TARGET
    (bin_dir / "lcsas-restore").write_text(stub)
    (bin_dir / "lcsas-ecc").write_text(ECC_STUB)
    (bin_dir / "lcsas-ecc").chmod(0o755)
    return meta


def _private(tmp_path: Path, **extra: str) -> dict[str, str]:
    """_env with a private HOME, TMPDIR and tools folder: the test then
    sees everything the run leaves behind."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return _env(tmp_path, HOME=str(home), LCSAS_TOOLS_DIR=str(tmp_path / "tools"),
                **extra)


def _left_behind(tmp_path: Path) -> list[str]:
    """What the run left in the tools folder, HOME and TMPDIR."""
    out = []
    if (tmp_path / "tools").exists():
        out.append("tools/")
    for sub in ("home", "tmpdir"):
        d = tmp_path / sub
        if d.exists():
            out += [f"{sub}/{p.name}" for p in d.iterdir()]
    return out


def _run_meta(meta: Path, args: list[str], env: dict[str, str], cwd: Path,
              timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(meta / "restore.sh"), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        env={**env, "LCSAS_META_DISC": str(meta)}, cwd=cwd, timeout=timeout,
    )


def _workdir(tmp_path: Path) -> Path:
    d = tmp_path / "start"
    d.mkdir(exist_ok=True)
    return d


# ── #800: --help, --version and --check-disc copy nothing ────────────


@pytest.mark.parametrize(("flag", "want"), [
    ("--help", "--snapshot ID"),
    ("-h", "usage:"),
    ("--version", "lcsas-restore.sh "),
])
def test_help_and_version_off_the_meta_disc_copy_nothing(
    tmp_path: Path, readonly: Callable[[Path], None], flag: str, want: str,
) -> None:
    meta = _meta(tmp_path)
    readonly(meta)
    res = _run_meta(meta, [flag], _private(tmp_path), _workdir(tmp_path))
    assert res.returncode == 0, res.stderr
    assert want in res.stdout
    assert "copying" not in res.stderr, res.stderr
    assert _left_behind(tmp_path) == []


def test_check_disc_off_the_meta_disc_copies_nothing_and_takes_a_relative_image(
    tmp_path: Path, readonly: Callable[[Path], None],
) -> None:
    meta = _meta(tmp_path)
    readonly(meta)
    work = _workdir(tmp_path)
    (work / "disc.iso").write_bytes(b"\0" * 2048)
    res = _run_meta(meta, ["--check-disc", "disc.iso"], _private(tmp_path), work)
    assert res.returncode == 0, res.stderr
    assert "ECC: verify disc.iso" in res.stdout
    assert "no damage found" in res.stderr
    assert "copying" not in res.stderr, res.stderr
    assert _left_behind(tmp_path) == []


def test_check_disc_runs_a_tool_without_its_run_permission_from_a_temporary_copy(
    tmp_path: Path, readonly: Callable[[Path], None],
) -> None:
    """#496 with #800: a META copy on a FAT stick has lcsas-ecc without
    its run permission.  The check copies that one file to a temporary
    folder, runs it, and removes the folder."""
    meta = _meta(tmp_path)
    ecc = meta / "recovery" / "bin" / HOST_TARGET / "lcsas-ecc"
    ecc.chmod(0o644)
    readonly(meta)
    work = _workdir(tmp_path)
    (work / "disc.iso").write_bytes(b"\0" * 2048)
    res = _run_meta(meta, ["--check-disc", "disc.iso"], _private(tmp_path), work)
    assert res.returncode == 0, res.stderr
    assert "ECC: verify disc.iso" in res.stdout
    assert "/lcsas-check-disc." in res.stderr, res.stderr
    assert _left_behind(tmp_path) == []


def test_a_mistyped_command_line_off_the_meta_disc_copies_nothing(
    tmp_path: Path, readonly: Callable[[Path], None],
) -> None:
    meta = _meta(tmp_path)
    readonly(meta)
    res = _run_meta(meta, ["--bogus"], _private(tmp_path), _workdir(tmp_path))
    assert res.returncode == 2
    assert "unknown flag" in res.stderr
    assert _left_behind(tmp_path) == []


# ── #625: relative paths are taken from the starting folder ──────────


@pytest.mark.parametrize("form", ["positional", "flag"])
def test_a_relative_target_off_the_meta_disc_lands_under_the_starting_folder(
    tmp_path: Path, readonly: Callable[[Path], None], form: str,
) -> None:
    """The run is started from a folder reached through a link (as a
    macOS /var path is): the target keeps that form (no pwd -P)."""
    meta = _meta(tmp_path)
    readonly(meta)
    real = _workdir(tmp_path)
    link = tmp_path / "start-link"
    link.symlink_to(real)
    args = ["restored"] if form == "positional" else ["--restore-folder", "restored"]
    env = {**_private(tmp_path), "PWD": str(link)}
    res = _run_meta(meta, args, env, link)
    assert res.returncode == 0, res.stderr
    assert "copied recovery binaries to" in res.stderr, "the run must relocate"
    assert _arg(res.stdout, "--restore-folder") == f"{link}/restored"
    assert (real / "restored" / "restored.txt").read_text() == "restored\n"


def test_a_relative_key_file_and_snapshot_off_the_meta_disc(
    tmp_path: Path, readonly: Callable[[Path], None],
) -> None:
    """--password-file FILE relative to the starting folder is read after the
    relocation, and --snapshot reaches tier 1 through it (#807)."""
    meta = _meta(tmp_path)
    readonly(meta)
    work = _workdir(tmp_path)
    (work / "pw.txt").write_text("not-a-real-password\n")
    env = _private(tmp_path)
    env.pop("LCSAS_PASSWORD")
    res = _run_meta(
        meta, ["--password-file", "pw.txt", "--restore-folder", "./out", "--snapshot", "a1b2c3d4"],
        env, work,
    )
    assert res.returncode == 0, res.stderr
    assert "copied recovery binaries to" in res.stderr, "the run must relocate"
    assert _arg(res.stdout, "--snapshot") == "a1b2c3d4"
    assert _arg(res.stdout, "--restore-folder") == f"{work}/out"
    assert (work / "out" / "restored.txt").exists()


def test_a_relative_recovery_root_off_the_meta_disc(
    tmp_path: Path, readonly: Callable[[Path], None],
) -> None:
    """RECOVERY_ROOT TARGET_DIR SNAPSHOT, both paths relative: the copy
    still finds the recovery root and the target."""
    meta = _meta(tmp_path)
    readonly(meta)
    res = _run_meta(meta, ["LCSAS_META/recovery", "out", "latest"],
                    _private(tmp_path), tmp_path)
    assert res.returncode == 0, res.stderr
    assert "copied recovery binaries to" in res.stderr, "the run must relocate"
    assert _arg(res.stdout, "--restore-folder") == f"{tmp_path}/out"
    assert (tmp_path / "out" / "restored.txt").exists()


# ── #807: --snapshot ID ──────────────────────────────────────────────


def _in_place(tmp_path: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    recovery = _meta_disc(tmp_path / "disc")
    (recovery / "bin" / HOST_TARGET / "lcsas-restore").write_text(STUB)
    return subprocess.run(
        ["sh", str(RESTORE_SH), *[str(recovery) if a == "@REC" else a for a in args]],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        env=_private(tmp_path, **extra),
    )


@pytest.mark.parametrize("args", [
    ["--restore-folder", "@OUT", "--snapshot", "a1b2c3d4", "@REC"],
    ["--snapshot", "a1b2c3d4", "@REC", "@OUT"],
    ["--snapshot", "a1b2c3d4", "@REC", "@OUT", "a1b2c3d4"],
    ["--restore-folder", "@OUT", "--snapshot", "a1b2c3d4", "--", "@REC"],
])
def test_snapshot_flag_reaches_tier_1(tmp_path: Path, args: list[str]) -> None:
    out = str(tmp_path / "out")
    res = _in_place(tmp_path, [out if a == "@OUT" else a for a in args])
    assert res.returncode == 0, res.stderr
    assert _arg(res.stdout, "--snapshot") == "a1b2c3d4"
    assert _arg(res.stdout, "--restore-folder") == out


def test_two_different_snapshots_exit_2(tmp_path: Path) -> None:
    res = _in_place(tmp_path, ["--snapshot", "a1b2", "@REC", str(tmp_path / "o"), "c3d4"])
    assert res.returncode == 2
    assert "give the snapshot once" in res.stderr
    assert "ARG: " not in res.stdout


def test_target_flag_with_a_positional_snapshot_points_at_snapshot_flag(
    tmp_path: Path,
) -> None:
    """The reviewer's `--restore-folder DIR latest`: still refused (latest could
    be a folder), and the message says how to name the snapshot."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--restore-folder", str(tmp_path / "o"), "latest"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        env=_private(tmp_path),
    )
    assert res.returncode == 2
    assert "give the target once" in res.stderr
    assert "--snapshot ID" in res.stderr


def test_snapshot_flag_needs_a_value() -> None:
    res = subprocess.run(["sh", str(RESTORE_SH), "--snapshot"],
                         capture_output=True, text=True, timeout=10)
    assert res.returncode == 2, res.stderr
    assert "--snapshot requires an ID" in res.stderr


@pytest.mark.parametrize("shell", ["dash", "bash"])
@pytest.mark.parametrize("args", [
    ["--repo"], ["--restore-folder"], ["--snapshot", ""], ["--password-file"],
    ["--check-disc"], ["--password-file", "/nonexistent/lcsas-key"],
])
def test_a_flag_without_its_value_is_a_usage_error(shell: str,
                                                   args: list[str]) -> None:
    """#887: restore.sh's 2 means it was called wrongly, in every shell.
    ${2:?} exited 2 in dash but 1 in bash."""
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    res = subprocess.run([shell, str(RESTORE_SH), *args],
                         stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=10)
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "restore.sh: --" in res.stderr, res.stderr


# ── #802: --repo with one backup set ─────────────────────────────────


def test_an_unknown_repo_with_one_set_stops_and_lists_the_set(tmp_path: Path) -> None:
    res = _in_place(tmp_path, ["--repo", "no-such-set", "@REC", str(tmp_path / "o")])
    assert res.returncode == 1
    assert "not among available repositories" in res.stderr
    assert f"{SET_NAME} (id {SET_ID})" in res.stderr
    assert "ARG: " not in res.stdout
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("name", [SET_NAME, SET_ID])
def test_the_one_set_by_its_name_or_id(tmp_path: Path, name: str) -> None:
    res = _in_place(tmp_path, ["--repo", name, "@REC", str(tmp_path / "o")])
    assert res.returncode == 0, res.stderr
    assert _arg(res.stdout, "--repo").endswith(f"/metadata/{SET_ID}")


def test_an_unknown_lcsas_repo_with_one_set_stops_too(tmp_path: Path) -> None:
    res = _in_place(tmp_path, ["@REC", str(tmp_path / "o")], LCSAS_BACKUP_SET="other")
    assert res.returncode == 1
    assert "LCSAS_BACKUP_SET=other not among available repositories" in res.stderr


# ── #806: no exit file under /tmp ────────────────────────────────────


def test_a_failed_run_leaves_no_exit_file(tmp_path: Path) -> None:
    """A failing run says its exit code on the terminal and leaves no
    file behind in TMPDIR (it used to leave /tmp/lcsas-restore-exit.*,
    which also ignored TMPDIR)."""
    res = _in_place(tmp_path, ["@REC", str(tmp_path / "o")], STUB_RC="1")
    assert res.returncode == 1, res.stderr
    assert "[restore.sh] stopped with exit code 1" in res.stderr
    assert "exit log" not in res.stderr
    assert list((tmp_path / "tmpdir").iterdir()) == []
    assert "lcsas-restore-exit" not in RESTORE_SH.read_text()
    assert "/tmp/lcsas-restore" not in RESTORE_SH.read_text()

