"""Hardening (#508): relocation copies one target and cleans up after itself.

relocate_to_ram used to copy the meta disc's whole recovery/bin tree
(every arch plus a CPython each, ~750 MB) into a temp dir and never
remove it, so each run left a copy in /tmp or the small /run/user tmpfs.
Now it copies only bin/<host triple>/ (all restore.sh ever reads), and
the relocated script removes its own directory when it exits.
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
    exit 0
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


def _run(meta: Path, tmp_path: Path, relocate_into: Path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TMPDIR": str(relocate_into),
        "LCSAS_MOUNT_DIRS": str(meta),
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_TARGET": HOST_TARGET,
    }
    for k in ("LCSAS_RELOCATED", "LCSAS_NO_RELOCATE", "LCSAS_META_DISC"):
        env.pop(k, None)
    return subprocess.run(
        ["sh", str(meta / "restore.sh"), str(tmp_path / "out")],
        input="pw\n", capture_output=True, text=True, env=env, timeout=60,
    )


def test_only_the_host_target_is_copied(tmp_path: Path, shm_dir: Path) -> None:
    res = _run(_stick(tmp_path), tmp_path, shm_dir)
    assert f"copied recovery binaries to {shm_dir}/lcsas-restore." in res.stderr, (
        res.stderr
    )
    assert f"BINDIR: {HOST_TARGET}" in res.stdout, (
        f"relocated bin/ should hold only {HOST_TARGET}:\n{res.stdout}"
    )


def test_relocated_copy_is_removed_on_exit(tmp_path: Path, shm_dir: Path) -> None:
    res = _run(_stick(tmp_path), tmp_path, shm_dir)
    assert "[tier 1] using prebuilt lcsas-restore" in res.stderr, res.stderr
    left = sorted(p.name for p in shm_dir.iterdir())
    assert left == [], f"relocated copy left behind: {left}"
