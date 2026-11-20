"""Hardening tests: the retired restore.sh names fail with the new one (#934).

ADR 0005: the names an heir types follow the glossary.  The old flags
and environment variables were not kept as aliases: each fails with exit
2 (usage) and one line, ``restore.sh: <old> was renamed: use <new>``,
before anything else happens.  The table is tests/contract/renames.py.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.contract.renames import renamed

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
RESTORE_AUTO = REPO_ROOT / "recovery" / "scripts" / "restore_auto.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _recovery(tmp_path: Path) -> Path:
    """A recovery tree whose tier 1 is a stub that records that it ran."""
    recovery = tmp_path / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "lcsas-restore"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "STUB RAN" >&2
        exit 0
    """))
    stub.chmod(0o755)
    for sub in ("keys", "index", "snapshots", "data"):
        (recovery / "metadata" / "alpha" / sub).mkdir(parents=True)
    (recovery / "metadata" / "alpha" / "keys" / "k").write_text("k")
    return recovery


def _run(argv: list[str], tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}
    base.update({"LCSAS_MOUNT_DIRS": "", "LCSAS_NO_RELOCATE": "1",
                 "HOME": str(tmp_path / "home")})
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(["sh", str(RESTORE_SH), *argv], input="pw\n",
                          capture_output=True, text=True, timeout=30,
                          env={**base, **env})


def _refused(res: subprocess.CompletedProcess[str], old: str, new: str) -> None:
    assert res.returncode == 2, (res.returncode, res.stdout, res.stderr)
    assert f"restore.sh: {old} was renamed: use {new}" in res.stderr, res.stderr
    assert "STUB RAN" not in res.stderr


@pytest.mark.parametrize("old", ["--key", "--target", "--repo"])
def test_old_flag_is_refused_with_the_new_name(tmp_path: Path, old: str) -> None:
    new = renamed(old).new
    recovery = _recovery(tmp_path)
    arg = str(tmp_path / "x")
    res = _run([old, arg, str(recovery)], tmp_path)
    _refused(res, old, new)


def test_old_flag_with_equals_is_refused(tmp_path: Path) -> None:
    res = _run(["--target=" + str(tmp_path / "x"), str(_recovery(tmp_path))], tmp_path)
    _refused(res, "--target", "--restore-folder")


def test_positional_restore_folder_is_refused(tmp_path: Path) -> None:
    """RECOVERY_ROOT is the only positional argument; a second one (the old
    TARGET_DIR) exits 2 and names the flags that replaced it."""
    recovery = _recovery(tmp_path)
    res = _run([str(recovery), str(tmp_path / "restored")], tmp_path)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "--restore-folder" in res.stderr and "--snapshot" in res.stderr
    assert "STUB RAN" not in res.stderr


def test_positional_snapshot_is_refused(tmp_path: Path) -> None:
    recovery = _recovery(tmp_path)
    res = _run([str(recovery), str(tmp_path / "restored"), "latest"], tmp_path)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "--snapshot" in res.stderr


def test_a_lone_positional_that_is_no_recovery_folder_is_refused(tmp_path: Path) -> None:
    """The old one-argument form, sh restore.sh TARGET_DIR."""
    res = _run([str(tmp_path / "restored")], tmp_path)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "--restore-folder" in res.stderr


@pytest.mark.parametrize("old", [
    "LCSAS_REPO", "LCSAS_PWFILE", "LCSAS_TARGET", "LCSAS_ALLOW_TMPFS_TARGET",
    "LCSAS_FORBID_TMPFS_TARGET", "LCSAS_FORCE_NONEMPTY_TARGET",
])
def test_old_environment_variable_is_refused(tmp_path: Path, old: str) -> None:
    recovery = _recovery(tmp_path)
    res = _run(["--restore-folder", str(tmp_path / "out"), str(recovery)],
               tmp_path, **{old: "1"})
    _refused(res, old, renamed(old).new)


def test_old_environment_variable_is_refused_even_when_empty(tmp_path: Path) -> None:
    """Set, not non-empty: an empty LCSAS_REPO= is still the old name."""
    recovery = _recovery(tmp_path)
    res = _run([str(recovery)], tmp_path, LCSAS_REPO="")
    _refused(res, "LCSAS_REPO", "LCSAS_BACKUP_SET")


def test_the_new_names_are_accepted(tmp_path: Path) -> None:
    """The refusal is of the old names only: the new flags run tier 1."""
    recovery = _recovery(tmp_path)
    pw = tmp_path / "pw"
    pw.write_text("pw")
    res = _run(["--backup-set", "alpha", "--password-file", str(pw),
                "--restore-folder", str(tmp_path / "out"), str(recovery)], tmp_path)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "was renamed" not in res.stderr


@pytest.mark.parametrize("old", ["LCSAS_REPO", "LCSAS_PWFILE", "LCSAS_TARGET"])
def test_restore_auto_refuses_the_old_environment_variables(tmp_path: Path, old: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}
    env[old] = "x"
    res = subprocess.run(["sh", str(RESTORE_AUTO), str(tmp_path), str(tmp_path / "o")],
                         capture_output=True, text=True, timeout=30, env=env)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert f"{old} was renamed: use {renamed(old).new}" in res.stderr
