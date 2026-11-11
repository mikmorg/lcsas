"""Issue #662 -- tier 1 must not silently restore an older snapshot.

When a snapshot file cannot be read (damaged sectors, a truncated copy),
tier 1 skipped it with a warning and "latest" quietly became the newest
snapshot it COULD read: older data, exit 0.  Now a restore of "latest"
stops when nobody is at the keyboard, and asks when someone is.

Uses the committed fixture repository (two snapshots, no rustic needed,
so the audit gate's coverage run exercises these paths).  The newest
snapshot file is damaged in a copy, then:

  1. ``--list`` says one snapshot file could not be read.
  2. ``--interactive off``: non-zero exit, the damaged file and the
     --snapshot to use are named, and nothing is restored.
  3. ``--interactive on`` answering "n": non-zero exit, nothing restored.
  4. ``--interactive on`` answering "y": the older snapshot is chosen.
  5. ``--snapshot <older id>``: chosen without asking.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo"
RESTORE_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]


def _find_bin() -> Path:
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        pytest.skip(f"LCSAS_RESTORE_BIN={path} not executable")
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    pytest.skip("no lcsas-restore binary; run `make -C recovery`")


def _files(target: Path) -> list[Path]:
    if not target.exists():
        return []
    return [p for p in target.rglob("*") if p.is_file()]


def test_latest_with_unreadable_snapshot(tmp_path: Path) -> None:
    bin_path = _find_bin()
    if not (FIXTURE_REPO / "keys").is_dir():
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    pw = tmp_path / "pw"
    pw.write_text("test\n")

    def run(*extra: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(bin_path), "--repo", str(repo), "--password-file", str(pw),
             *extra],
            input=stdin, capture_output=True, text=True, timeout=60,
        )

    listing = run("--list-snapshots")
    assert listing.returncode == 0, listing.stderr
    ids = [line.split()[0] for line in listing.stdout.splitlines()[1:]
           if line.strip()]
    assert len(ids) >= 2, listing.stdout
    older, newest = ids[-2], ids[-1]  # sorted oldest -> newest
    (newest_file,) = [p for p in (repo / "snapshots").iterdir()
                      if p.name.startswith(newest)]
    newest_file.write_bytes(newest_file.read_bytes()[:20])

    # 1. --list names the problem.
    res = run("--list-snapshots")
    assert res.returncode == 0, res.stderr
    assert "1 snapshot file(s) could not be read" in res.stdout
    assert newest not in res.stdout

    # 2. Nobody at the keyboard: stop, name the file and the choice.
    t = tmp_path / "t-off"
    res = run("--target", str(t), "--interactive", "off")
    assert res.returncode != 0, res.stderr
    assert newest_file.name in res.stderr
    assert f"--snapshot {older}" in res.stderr
    assert _files(t) == []

    # 3. Someone answers "n": not restored.
    t = tmp_path / "t-no"
    res = run("--target", str(t), "--interactive", "on", stdin="n\n")
    assert res.returncode != 0, res.stderr
    assert "Not restoring." in res.stderr
    assert _files(t) == []

    # 4. Someone answers "y": the older snapshot is restored.  (The
    # fixture's older snapshot references a deliberately missing blob,
    # so the restore itself ends in an error; what matters here is that
    # the older snapshot was chosen and the walk began.)
    t = tmp_path / "t-yes"
    res = run("--target", str(t), "--interactive", "on", stdin="y\n")
    assert f"restoring snapshot {older}" in res.stderr, res.stderr
    assert "Not restoring." not in res.stderr

    # 5. An explicit snapshot id needs no answer.
    t = tmp_path / "t-id"
    res = run("--target", str(t), "--interactive", "off", "--snapshot", older)
    assert f"restoring snapshot {older}" in res.stderr, res.stderr
    assert "could not be read" not in res.stderr.split("restoring")[1]
