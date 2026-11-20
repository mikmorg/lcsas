"""Hardening (#605): "latest" is the newest data disc's, with the real tier 1.

The META disc copies each backup set's index and snapshots when it is
burned, so a backup made after that is not in its copy.  Drill #476
restored "latest" from that copy.  restore.sh now ranks every copy of a
set by its disc's label and uses the newest data disc's (C2).  The
journey tests pin that ranking with a stub tier 1; this test restores
for real: a rustic repository with two snapshots, the META disc's copy
taken after the first, the data disc's after the second, and the real
tier-1 binary (recovery/bin/x86_64/lcsas-restore).  The restored files
must be the second snapshot's.  Read off the META copy they would be
the first one's (the test checks the two copies differ that way).

Run in place from the META copy, and with LCSAS_RELOCATED standing in
for the relocated copy (then the newest disc's copy is also merged into
the tools folder first).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
TIER1 = REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore"
HOST_TARGET = "x86_64-unknown-linux-musl"
SET_ID = "5d2c7a41-9e83-4f06-b1c4-7a9e2d6f3b18"  # invented
PASSWORD = "not-a-real-password-1618"  # invented
LABEL = "LCSAS_TEST_TINY_2026_0002"

pytestmark = [
    pytest.mark.skipif(shutil.which("rustic") is None,
                       reason="rustic not on PATH; need a real repository"),
    pytest.mark.skipif(
        not (sys.platform.startswith("linux") and platform.machine() == "x86_64"
             and TIER1.is_file()),
        reason="the committed tier-1 binary is an x86_64 Linux one"),
]


def _rustic(repo: Path, pwfile: Path, *args: str) -> None:
    subprocess.run(["rustic", "-r", str(repo), "--password-file", str(pwfile), *args],
                   capture_output=True, check=True, timeout=120)


def _metadata(repo: Path, dst: Path) -> None:
    """The set's metadata as a disc carries it: no data/."""
    dst.mkdir(parents=True)
    shutil.copy2(repo / "config", dst / "config")
    for sub in ("keys", "index", "snapshots"):
        shutil.copytree(repo / sub, dst / sub)


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    """(the META copy's recovery/, the folder the data discs are under)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "letter.txt").write_text("the first version\n")
    (src / "kept.bin").write_bytes(bytes(range(256)) * 64)
    pwfile = tmp_path / "pw"
    pwfile.write_text(PASSWORD + "\n")
    repo = tmp_path / "rustic_repo"
    _rustic(repo, pwfile, "init")
    _rustic(repo, pwfile, "backup", str(src))

    meta = tmp_path / "meta"
    _metadata(repo, meta / "metadata" / SET_ID)

    (src / "letter.txt").write_text("the second version, after the META disc\n")
    (src / "added.txt").write_text("made after the META disc was burned\n")
    _rustic(repo, pwfile, "backup", str(src))

    media = tmp_path / "media"
    disc = media / LABEL
    _metadata(repo, disc / "metadata" / SET_ID)
    for pack in (repo / "data").rglob("*"):
        if pack.is_file():
            dst = disc / "data" / pack.parent.name / pack.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pack, dst)
    (disc / "VOLUME_INFO.txt").write_text(
        f"ABOUT THIS DISC\n===============\n\n  Label:         {LABEL}\n")

    recovery = meta / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    shutil.copy2(TIER1, bin_dir / "lcsas-restore")
    (bin_dir / "lcsas-restore").chmod(0o755)
    (recovery / "scripts").mkdir()
    (recovery / "scripts" / "restore.sh").write_text("# the META disc\n")

    # The teeth: the META copy knows only the first snapshot.
    meta_snaps = list((meta / "metadata" / SET_ID / "snapshots").iterdir())
    disc_snaps = list((disc / "metadata" / SET_ID / "snapshots").iterdir())
    assert len(meta_snaps) == 1 and len(disc_snaps) == 2
    return recovery, media


def _restored(target: Path, name: str) -> Path:
    found = sorted(target.rglob(name))
    assert len(found) == 1, (name, found)
    return found[0]


@pytest.mark.parametrize("run", ["in-place", "relocated"])
def test_latest_is_the_newest_data_discs_snapshot(tmp_path: Path, run: str) -> None:
    recovery, media = _archive(tmp_path)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": str(media),
        "LCSAS_BACKUP_SET": SET_ID,
        "LCSAS_PASSWORD": PASSWORD,
        "LCSAS_TOOLS_DIR": str(tmp_path / "tools"),
        "LCSAS_PLATFORM": HOST_TARGET,
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_ANOTHER_SET": "0",
        "TMPDIR": str(tmpdir),
    }
    for var in ("LCSAS_PASSWORD_FILE", "LCSAS_SHARES_DIR", "LCSAS_RELOCATED",
                "LCSAS_META_DISC", "LCSAS_INTERACTIVE", "LCSAS_PACK_CACHE_DIR",
                "LCSAS_TIER_FALLBACK", "LCSAS_NEWEST_DISC_PROMPT"):
        env.pop(var, None)
    if run == "relocated":
        env["LCSAS_RELOCATED"] = str(recovery.parent)
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--restore-folder", str(target), str(recovery)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
        timeout=180,
    )
    assert res.returncode == 0, res.stderr
    assert "[tier 1]" in res.stderr, res.stderr
    assert _restored(target, "letter.txt").read_text() == (
        "the second version, after the META disc\n")
    assert _restored(target, "added.txt").read_text() == (
        "made after the META disc was burned\n")
    assert _restored(target, "kept.bin").read_bytes() == bytes(range(256)) * 64
    assert sorted(p.name for p in tmpdir.iterdir()) == []
