"""Regression test for issue #194.

When `lcsas-restore` is run without `--snapshot` (or with
`--snapshot latest`), it must select the chronologically newest
snapshot — matching what `rustic restore latest` would pick.

Before the fix, snapshots were loaded in `readdir()` order, then sorted
by file_name (a content-addressed hash, effectively random); the LATEST
selector therefore returned an arbitrary snapshot whenever the repo
contained more than one.  The fix sorts by the ISO-8601 `time` field
so `items[count-1]` is genuinely the newest.

This test builds a real rustic-format repo with THREE backups separated
by `time.sleep(2)`, exercises:

  1. `lcsas-restore --list-snapshots` — output rows must be ordered by
     time, with the newest row last.
  2. `lcsas-restore --target ...` (no `--snapshot`) — the restored
     content must match what the THIRD backup contained.

Skips when `rustic` isn't on PATH or `lcsas-restore` hasn't been built.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import find_restore_bin, find_restored_root

pytestmark = pytest.mark.integration



def _rustic(*args: str, repo: Path, pwfile: Path,
            check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rustic", "-r", str(repo), *args,
         "--password-file", str(pwfile)],
        capture_output=True, text=True, check=check, timeout=180,
    )


def test_tier1_latest_is_newest_by_time(tmp_path: Path) -> None:
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")

    # Three real backups separated by 2 s so rustic stamps each
    # snapshot with a distinct, monotonically increasing ISO-8601
    # `time` field.
    _rustic("init", repo=repo, pwfile=pwfile)

    backup_ids: list[str] = []
    contents = ["alpha-v1\n", "beta-v2\n", "gamma-v3-newest\n"]
    for i, body in enumerate(contents):
        if i > 0:
            time.sleep(2)
        (src / "marker.txt").write_text(body)
        res = _rustic("backup", str(src), repo=repo, pwfile=pwfile)
        # rustic prints e.g. "snapshot <8-hex> successfully saved."
        m = re.search(r"snapshot ([0-9a-f]{8,})\b", res.stdout + res.stderr)
        assert m, f"could not find snapshot id in rustic output:\n{res.stdout}\n{res.stderr}"
        backup_ids.append(m.group(1)[:8])

    # 1) --list-snapshots: rows ascending by time, newest last.
    list_res = subprocess.run(
        [str(bin_path),
         "--repo", str(repo),
         "--password-file", str(pwfile),
         "--list-snapshots"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    # The binary writes the listing to stdout.  Each data row looks like:
    #   <8-hex>  <ISO-8601 time>  <path>
    rows: list[tuple[str, str]] = []
    for line in list_res.stdout.splitlines():
        m = re.match(r"\s*([0-9a-f]{8})\s+(\S+)\s+", line)
        if m:
            rows.append((m.group(1), m.group(2)))

    assert len(rows) == 3, (
        f"expected 3 snapshot rows in --list-snapshots output, got {len(rows)}:\n"
        f"stdout:\n{list_res.stdout}\nstderr:\n{list_res.stderr}"
    )

    times = [t for _, t in rows]
    assert times == sorted(times), (
        f"tier-1 --list-snapshots not sorted by time (issue #194):\n"
        f"rows={rows}"
    )

    # The newest row (last in the listing) must be the third backup
    # we made.  Compare by 8-char id prefix.
    newest_id = rows[-1][0]
    assert newest_id == backup_ids[-1], (
        f"tier-1 'latest' = {newest_id}, expected last backup id "
        f"{backup_ids[-1]}; full order={[r[0] for r in rows]}, "
        f"backup ids in time order={backup_ids}"
    )

    # 2) --target without --snapshot must restore the THIRD (newest)
    # backup's content, not an arbitrary one.
    target = tmp_path / "tier1_out"
    target.mkdir()
    subprocess.run(
        [str(bin_path),
         "--repo", str(repo),
         "--target", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=180,
    )

    root = find_restored_root(target)
    restored = (root / "marker.txt").read_text()
    assert restored == contents[-1], (
        f"tier-1 default restore did not pick the newest snapshot "
        f"(issue #194): got {restored!r}, want {contents[-1]!r}"
    )
