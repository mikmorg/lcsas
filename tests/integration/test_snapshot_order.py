"""#968 item 30: snapshots are ordered by their time, not their text.

``snapshots.timestamp`` holds rustic's time string, which carries the
local offset of the machine that took the backup.  ``list_snapshots``
ordered it as text, so snapshots taken in different time zones sorted
wrongly: a backup at 07:00+14:00 (17:00 UTC the day before) sorted after
one at 05:00-12:00 the same evening (17:00 UTC + a second).

Real rustic backups taken under two TZ settings, a real scan, a real
stage, and ``volume impact --snapshots`` through ``main()``.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from lcsas.db.connection import get_connection
from lcsas.db.snapshots import list_snapshots
from lcsas.utils.timestamps import parse_utc
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _backup_in(inst: ScratchInstall, tz: str, filename: str) -> None:
    """A backup of alpha by a machine whose clock is in *tz*."""
    (inst.data("alpha") / filename).write_bytes(os.urandom(6000))
    subprocess.run(
        ["rustic", "-r", str(inst.mirror("alpha")), "--password-file",
         str(inst.key("alpha")), "backup", str(inst.data("alpha")),
         "--as-path", "/alpha"],
        capture_output=True, text=True, check=True,
        env={**os.environ, "TZ": tz, "TMPDIR": str(inst.root / "tmp"),
             "HOME": str(inst.root / "tmp")},
    )


@pytest.fixture
def inst(tmp_path: Path) -> ScratchInstall:
    inst = ScratchInstall(tmp_path, repos=("alpha",))
    # UTC+14, then UTC-12 a moment later: the later backup's text is a
    # day earlier.
    _backup_in(inst, "Pacific/Kiritimati", "f1.bin")
    _backup_in(inst, "Etc/GMT+12", "f2.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def _taken_order(inst: ScratchInstall) -> list[str]:
    """Snapshot ids in the order rustic took them (by UTC)."""
    rows = inst.query("SELECT snapshot_id, timestamp FROM snapshots")
    assert len(rows) == 2
    stamps = {str(r[0]): str(r[1]) for r in rows}
    assert sorted(stamps.values()) != sorted(
        stamps.values(), key=lambda t: parse_utc(t) or 0), (
        "fixture: the text order must differ from the time order")
    return sorted(stamps, key=lambda i: parse_utc(stamps[i]))  # type: ignore[arg-type, return-value]


def test_list_snapshots_orders_by_time(inst: ScratchInstall) -> None:
    conn = get_connection(inst.db)
    try:
        listed = [s.snapshot_id for s in list_snapshots(conn)]
    finally:
        conn.close()
    assert listed == _taken_order(inst)


def test_volume_impact_lists_snapshots_oldest_first(
    inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
) -> None:
    assert inst.lcsas("stage") == 0
    (label,) = inst.labels()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("volume", "impact", "--snapshots", label) == 0
    first, second = _taken_order(inst)
    assert first in caplog.text and second in caplog.text, caplog.text
    assert caplog.text.index(first) < caplog.text.index(second)


def test_unreadable_timestamps_sort_last_and_stay_listed(tmp_path: Path) -> None:
    """A row whose time cannot be read is kept, after every readable one."""
    from lcsas.db.models import Snapshot
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import ensure_schema
    from lcsas.db.snapshots import bulk_upsert_snapshots

    conn = get_connection(tmp_path / "catalog.db")
    try:
        ensure_schema(conn)
        register_repo(conn, "r1", "alpha", str(tmp_path / "m"))
        rows = [
            ("bad", "not a time"),
            ("plus14", "2026-11-24T07:00:00.123456789+14:00"),
            ("blank", ""),
            ("minus12", "2026-11-23T05:00:01.5-12:00"),
            ("utc", "2026-11-23T16:00:00Z"),
        ]
        bulk_upsert_snapshots(conn, [
            Snapshot(snapshot_id=i, repo_id="r1", hostname="h", timestamp=t,
                     paths="[]", tags="[]", description="")
            for i, t in rows
        ])
        listed = [s.snapshot_id for s in list_snapshots(conn)]
    finally:
        conn.close()
    assert listed[:3] == ["utc", "plus14", "minus12"]
    assert sorted(listed[3:]) == ["bad", "blank"]
