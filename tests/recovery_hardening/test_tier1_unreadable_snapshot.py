"""Issue #662 -- tier 1 must not silently restore an older snapshot.

When a snapshot file cannot be read (damaged sectors, a truncated copy),
tier 1 skipped it with a warning and "latest" quietly became the newest
snapshot it COULD read: older data, exit 0.  Now a restore of "latest"
stops when nobody is at the keyboard, and asks when someone is.

The test makes two snapshots with rustic, damages the newest snapshot
file, and checks:

  1. ``--interactive off``: non-zero exit, the damaged file is named,
     and nothing is restored.
  2. ``--interactive on`` answering "y": the older snapshot is restored.
  3. ``--snapshot <older id>``: restores without asking.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
    non_marker_files,
)

pytestmark = pytest.mark.integration


def _snapshots(repo: Path, pwfile: Path) -> list[dict[str, str]]:
    res = subprocess.run(
        ["rustic", "-r", str(repo), "snapshots", "--json",
         "--password-file", str(pwfile)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    data = json.loads(res.stdout)
    # rustic groups snapshots: [[group, [snap, ...]], ...]
    out: list[dict[str, str]] = []
    for group in data:
        snaps = group[1] if isinstance(group, list) else group["snapshots"]
        out.extend(snaps)
    return sorted(out, key=lambda s: s["time"])


def test_latest_with_unreadable_snapshot(tmp_path: Path) -> None:
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("first\n")
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    build_rustic_repo(src, repo, pwfile)
    (src / "b.txt").write_text("second\n")
    subprocess.run(
        ["rustic", "-r", str(repo), "backup", str(src),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=120,
    )
    snaps = _snapshots(repo, pwfile)
    assert len(snaps) == 2
    older, newest = snaps[0]["id"], snaps[1]["id"]

    # Damage the newest snapshot file: authentication now fails.
    newest_file = repo / "snapshots" / newest
    newest_file.write_bytes(newest_file.read_bytes()[:20])

    def run(target: Path, *extra: str,
            stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(bin_path), "--repo", str(repo), "--target", str(target),
             "--password-file", str(pwfile), *extra],
            input=stdin, capture_output=True, text=True, timeout=300,
        )

    # 1. Nobody at the keyboard: stop, name the file, restore nothing.
    t1 = tmp_path / "t1"
    res = run(t1, "--interactive", "off")
    assert res.returncode != 0, res.stderr
    assert newest in res.stderr
    assert older[:8] in res.stderr
    assert not t1.exists() or non_marker_files(t1) == []

    # 2. Someone answers "y": the older snapshot is restored.
    t2 = tmp_path / "t2"
    res = run(t2, "--interactive", "on", stdin="y\n")
    assert res.returncode == 0, res.stderr
    names = {p.name for p in non_marker_files(t2)}
    assert "a.txt" in names and "b.txt" not in names

    # 3. An explicit snapshot id needs no answer.
    t3 = tmp_path / "t3"
    res = run(t3, "--interactive", "off", "--snapshot", older[:8])
    assert res.returncode == 0, res.stderr
    names = {p.name for p in non_marker_files(t3)}
    assert "a.txt" in names and "b.txt" not in names
