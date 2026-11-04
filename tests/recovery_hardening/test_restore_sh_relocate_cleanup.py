"""Hardening (#508, #609, #612): relocation copies one target into the
tools folder, says so, and cleans up after a restore that succeeds.

relocate_to_ram used to copy the meta disc's whole recovery/bin tree
(every arch plus a CPython each, ~750 MB) into a temp dir and never
remove it, so each run left a copy in /tmp or the small /run/user tmpfs.
Now it copies only bin/<host triple>/ (all restore.sh ever reads), and
the copy is removed when a restore succeeds (#508).

Since rc6 the copy goes to the fixed tools folder ($LCSAS_TOOLS_DIR,
default $HOME/lcsas-restore-tools), which stays after any exit that is
not a success so the restore can carry on (#609); a temporary folder is
the fallback.  A line before the copy says it may take minutes (#612).
These tests were updated for that: they used to expect the copy under
$TMPDIR/lcsas-restore.XXXXXX.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

# The stub tier-1 binary lists the bin/ dir it was run from, so the test
# can see what the relocated copy holds while it still exists.
STUB = textwrap.dedent("""\
    #!/bin/sh
    printf 'BINDIR: %s\\n' "$(ls "$(dirname "$0")/..")"
    exit "${STUB_RC:-0}"
""")


@pytest.fixture
def shm_dir():
    shm = Path("/dev/shm")
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        pytest.skip("/dev/shm not available")
    d = shm / f"lcsas-508-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _stick(tmp_path: Path) -> Path:
    meta = tmp_path / "stick"
    (meta / "recovery" / "scripts").mkdir(parents=True)
    (meta / "restore.sh").write_text(RESTORE_SH.read_text())
    host = meta / "recovery" / "bin" / HOST_TARGET
    host.mkdir(parents=True)
    (host / "lcsas-restore").write_text(STUB)
    (host / "lcsas-restore").chmod(0o644)       # FAT-style: no exec bit
    other = meta / "recovery" / "bin" / "aarch64-apple-darwin"
    other.mkdir(parents=True)
    (other / "python-tree-stand-in").write_bytes(b"\0" * 4096)
    repo = meta / "metadata" / "alpha"
    for sub in ("keys", "index", "data"):
        (repo / sub).mkdir(parents=True)
    return meta


def _run(meta: Path, tmp_path: Path, relocate_into: Path,
         tools: Path | None = None, stub_rc: int = 0, repo: str = "alpha"):
    env = {
        "LCSAS_REPO": repo,
        **os.environ,
        "HOME": str(tmp_path),
        "TMPDIR": str(relocate_into),
        "LCSAS_MOUNT_DIRS": str(meta),
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_TARGET": HOST_TARGET,
        "LCSAS_TOOLS_DIR": str(tools or relocate_into / "tools"),
        "STUB_RC": str(stub_rc),
    }
    for k in ("LCSAS_RELOCATED", "LCSAS_NO_RELOCATE", "LCSAS_META_DISC"):
        env.pop(k, None)
    return subprocess.run(
        ["sh", str(meta / "restore.sh"), str(tmp_path / "out")],
        input="pw\n", capture_output=True, text=True, env=env, timeout=60,
    )


def test_only_the_host_target_is_copied(tmp_path: Path, shm_dir: Path) -> None:
    res = _run(_stick(tmp_path), tmp_path, shm_dir)
    tools = shm_dir / "tools"
    assert f"copied recovery binaries to {tools}\n" in res.stderr, res.stderr
    assert f"BINDIR: {HOST_TARGET}" in res.stdout, (
        f"relocated bin/ should hold only {HOST_TARGET}:\n{res.stdout}"
    )
    # #612: the copy is announced, with its size, BEFORE it starts.
    before = res.stderr.find("copying the recovery tools for this computer (about")
    after = res.stderr.find("copied recovery binaries to")
    assert -1 < before < after, res.stderr
    assert f"copying the backup-set metadata (about 1 MB) to {tools}/metadata" in (
        res.stderr
    ), res.stderr
    # /dev/shm is RAM, like $HOME on a live USB stick.
    assert f"note: {tools} is in RAM" in res.stderr, res.stderr


def test_relocated_copy_is_removed_on_exit(tmp_path: Path, shm_dir: Path) -> None:
    res = _run(_stick(tmp_path), tmp_path, shm_dir)
    assert res.returncode == 0, res.stderr
    assert "[tier 1] using prebuilt lcsas-restore" in res.stderr, res.stderr
    left = sorted(p.name for p in shm_dir.iterdir())
    assert left == [], f"relocated copy left behind: {left}"


def test_tools_folder_stays_after_a_failure_and_is_reused(
    tmp_path: Path, shm_dir: Path,
) -> None:
    """#609: a run that does not succeed keeps the copy (with the disc's
    metadata/ and a resume.sh); the next run from the disc reuses it
    instead of copying again."""
    meta = _stick(tmp_path)
    tools = shm_dir / "tools"
    first = _run(meta, tmp_path, shm_dir, stub_rc=1)
    assert first.returncode == 1, first.stderr
    assert (tools / "recovery" / "bin" / HOST_TARGET / "lcsas-restore").is_file()
    assert (tools / "metadata" / "alpha" / "keys").is_dir()
    assert (tools / "recovery" / "scripts" / "restore.sh").read_text() == (
        meta / "restore.sh"
    ).read_text()
    assert f"sh {tools}/resume.sh" in first.stderr, first.stderr

    # A set the copy lacks is added; a set an earlier run brought up to
    # date from a data disc (tagged in metadata/.from/) is kept as it is;
    # any other set is taken from the META disc again.  (This used to keep
    # every set the copy had, so a half-copied set, or one from an older
    # META disc, was never refreshed.)
    (tools / "metadata" / "alpha" / "keys" / "updated-by-run-1").write_text("")
    (tools / "metadata" / ".from").mkdir()
    (tools / "metadata" / ".from" / "alpha").write_text("LCSAS_TEST_TINY_2026_0002\n")
    for sub in ("keys", "index"):
        (meta / "metadata" / "bravo" / sub).mkdir(parents=True)
    (tools / "metadata" / "bravo" / "keys").mkdir(parents=True)  # half a copy
    again = _run(meta, tmp_path, shm_dir, stub_rc=1)
    assert again.returncode == 1, again.stderr
    assert (
        f"copied recovery binaries to {tools} on an earlier run; using that copy"
    ) in again.stderr, again.stderr
    assert "copying the recovery tools" not in again.stderr, again.stderr
    assert (tools / "metadata" / "bravo" / "index").is_dir()
    assert (tools / "metadata" / "alpha" / "keys" / "updated-by-run-1").exists()

    last = _run(meta, tmp_path, shm_dir, stub_rc=0)
    assert last.returncode == 0, last.stderr
    assert not tools.exists(), "the tools folder must go after a success"


def _old_tools(tools: Path, build_line: str) -> None:
    """A tools folder left by another meta disc's restore.sh."""
    (tools / "recovery" / "scripts").mkdir(parents=True)
    (tools / "recovery" / "scripts" / "restore.sh").write_text(
        f"#!/bin/sh\n{build_line}\n# an older restore.sh\n"
    )
    (tools / "pack-cache" / "data").mkdir(parents=True)
    (tools / "pack-cache" / "data" / "cached-pack").write_text("pack")
    (tools / ".lcsas-restore-tools").write_text("")


def test_tools_from_another_build_are_replaced(tmp_path: Path, shm_dir: Path) -> None:
    """C6: a tools folder whose restore.sh differs is replaced; its pack
    cache goes too, because the builds differ."""
    meta = _stick(tmp_path)
    tools = shm_dir / "tools"
    _old_tools(tools, 'LCSAS_RESTORE_BUILD_SHA="0123abcd"')
    res = _run(meta, tmp_path, shm_dir, stub_rc=1)
    assert f"{tools} holds an older copy of the recovery tools" in res.stderr, res.stderr
    assert (tools / "recovery" / "scripts" / "restore.sh").read_text() == (
        meta / "restore.sh"
    ).read_text()
    assert not (tools / "pack-cache" / "data" / "cached-pack").exists()


def test_same_build_keeps_the_pack_cache(tmp_path: Path, shm_dir: Path) -> None:
    """C6: the same build (LCSAS_RESTORE_BUILD_SHA line) keeps the cache."""
    meta = _stick(tmp_path)
    tools = shm_dir / "tools"
    _old_tools(tools, 'LCSAS_RESTORE_BUILD_SHA="@@BUILD_SHA@@"')
    res = _run(meta, tmp_path, shm_dir, stub_rc=1)
    assert f"{tools} holds an older copy of the recovery tools" in res.stderr, res.stderr
    assert (tools / "pack-cache" / "data" / "cached-pack").read_text() == "pack"


def test_unusable_tools_folder_falls_back_to_a_temporary_one(
    tmp_path: Path, shm_dir: Path,
) -> None:
    """A tools folder that holds other files is never used (nor deleted):
    the copy goes to a temporary folder, removed after a success."""
    meta = _stick(tmp_path)
    mine = tmp_path / "my-files"
    mine.mkdir()
    (mine / "letter.txt").write_text("mine")
    res = _run(meta, tmp_path, shm_dir, tools=mine)
    assert res.returncode == 0, res.stderr
    assert f"cannot use {mine} for the recovery tools" in res.stderr, res.stderr
    assert f"copied recovery binaries to {shm_dir}/lcsas-restore." in res.stderr, (
        res.stderr
    )
    assert sorted(p.name for p in mine.iterdir()) == ["letter.txt"]
    assert sorted(p.name for p in shm_dir.iterdir()) == []


def test_run_from_recovery_scripts_copies_the_docs_and_root_metadata(
    tmp_path: Path, shm_dir: Path,
) -> None:
    """Run as <disc>/recovery/scripts/restore.sh: the copy still carries
    the restore manuals (C6) and the disc ROOT's metadata/ (#616), which
    is two levels up from the script there."""
    meta = _stick(tmp_path)
    (meta / "recovery" / "scripts" / "restore.sh").write_text(RESTORE_SH.read_text())
    (meta / "recovery" / "docs").mkdir()
    (meta / "recovery" / "docs" / "RECOVER.txt").write_text("manual\n")
    tools = shm_dir / "tools"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TMPDIR": str(shm_dir),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_TARGET": HOST_TARGET,
        "LCSAS_TOOLS_DIR": str(tools),
        "STUB_RC": "1",
    }
    for k in ("LCSAS_RELOCATED", "LCSAS_NO_RELOCATE", "LCSAS_META_DISC", "LCSAS_REPO"):
        env.pop(k, None)
    res = subprocess.run(
        ["sh", str(meta / "recovery" / "scripts" / "restore.sh"), str(tmp_path / "out")],
        input="pw\n", capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 1, res.stderr
    assert (tools / "recovery" / "docs" / "RECOVER.txt").read_text() == "manual\n"
    assert (tools / "metadata" / "alpha" / "keys").is_dir(), res.stderr
    # With no data disc mounted, the set comes from the copy.
    assert f"using repository {tools}/metadata/alpha" in res.stderr, res.stderr
