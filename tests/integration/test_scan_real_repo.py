"""Integration: ``lcsas scan`` against a REAL rustic repository (issue #517).

Pins the "cmd_scan against a real repo" gap from
``docs/workflows/backup-and-scan.md`` (Gaps, section 7).  Every other scan
test runs against a fake mirror (random bytes in ``data/``, a ``{}`` config
file) with ``password_file = ""``, so two things were never exercised
against what rustic really writes:

* the pack scanner on a real repo layout (pack names must be the SHA-256 of
  the pack file, sizes must match the files on disk), and
* the snapshot-persistence branch of ``cmd_scan``: ``rustic snapshots
  --json`` -> ``parse_snapshots_output`` -> ``bulk_upsert_snapshots`` ->
  the ``snapshots`` table.

The flow is the documented one: ``rustic init`` + two backups into a tmp
mirror, an ``lcsas`` config with ``[repos.family]`` (``mirror_path`` +
``password_file``), ``lcsas repo add``, ``lcsas scan``; then a third backup
and a re-scan must add only the new packs / the new snapshot.

Requires: ``rustic`` on PATH (self-skips otherwise).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection

# `make test-integration` selects with `-m integration`; without this the
# module is deselected and runs in no gate target at all (#426).
pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic]

_REPO_NAME = "family"


class _World:
    """A tmp rustic mirror plus an lcsas config/catalog pointing at it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.src = root / "source"
        self.mirror = root / "mirror" / _REPO_NAME
        self.password_file = root / "family.key"
        self.db = root / "archive.db"
        self.config = root / "config.toml"
        self.rng = random.Random(517)
        self._backups = 0
        self._env = {**os.environ, "TMPDIR": str(root)}

        self.src.mkdir()
        self.mirror.parent.mkdir()
        self.password_file.write_text("scan-real-repo-test-password\n")
        self.config.write_text(
            f'[paths]\nmirror_base = "{root / "mirror"}"\n'
            f'staging = "{root / "staging"}"\n'
            f'database = "{self.db}"\n'
            "\n[defaults]\n"
            'media_type = "TEST_TINY"\n'
            "ecc_redundancy_pct = 0\n"
            'location = "Home_Shelf"\n'
            'optical_device = "/dev/null"\n'
            'label_prefix = "TEST"\n'
            "metadata_reserve_mb = 0\n"
            f"\n[repos.{_REPO_NAME}]\n"
            f'mirror_path = "{self.mirror}"\n'
            f'password_file = "{self.password_file}"\n'
        )
        self._rustic("init")

    # -- rustic ---------------------------------------------------------

    def _rustic(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["rustic", "-r", str(self.mirror),
             "--password-file", str(self.password_file), *args],
            capture_output=True, text=True, check=True, env=self._env,
        )

    def backup(self) -> None:
        """Add one new random file to the source tree and back it up."""
        self._backups += 1
        (self.src / f"file_{self._backups:02d}.bin").write_bytes(
            self.rng.randbytes(4096)
        )
        self._rustic("backup", str(self.src))

    def rustic_snapshots(self) -> list[dict]:
        """Every snapshot rustic itself reports, flattened out of its groups."""
        groups = json.loads(self._rustic("snapshots", "--json").stdout)
        return [snap for group in groups for snap in group["snapshots"]]

    def pack_files(self) -> dict[str, int]:
        """``{pack file name: size}`` for every file under the mirror's data/."""
        return {
            p.name: p.stat().st_size
            for p in (self.mirror / "data").rglob("*") if p.is_file()
        }

    # -- lcsas ----------------------------------------------------------

    def lcsas(self, *argv: str) -> int:
        return main(["--config", str(self.config), *argv])

    def register(self) -> None:
        assert self.lcsas("init") == 0
        assert self.lcsas("repo", "add", _REPO_NAME, str(self.mirror)) == 0

    def query(self, sql: str) -> list[sqlite3.Row]:
        conn = get_connection(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


@pytest.fixture
def world(tmp_path) -> _World:
    w = _World(tmp_path)
    w.backup()
    w.backup()
    w.register()
    return w


# ---------------------------------------------------------------------------
# Packs: what the scanner makes of a real repo
# ---------------------------------------------------------------------------


class TestScanRegistersRealPacks:
    def test_first_scan_registers_every_pack_rustic_wrote(self, world, capsys):
        on_disk = world.pack_files()
        assert on_disk, "two real backups must have produced pack files"
        # LCSAS trusts the pack file name as the pack's SHA-256 (staging
        # re-verifies content against it); prove rustic really names packs so.
        for name in on_disk:
            data = next((world.mirror / "data").rglob(name)).read_bytes()
            assert hashlib.sha256(data).hexdigest() == name

        assert world.lcsas("scan") == 0

        out = capsys.readouterr().out
        assert f"Newly registered: {len(on_disk)}" in out
        rows = world.query("SELECT sha256, size_bytes, is_pruned FROM packs")
        assert {r["sha256"]: r["size_bytes"] for r in rows} == on_disk
        assert not any(r["is_pruned"] for r in rows)

    def test_packs_are_attributed_to_the_registered_repo(self, world):
        assert world.lcsas("scan") == 0

        (repo,) = world.query("SELECT repo_id, name FROM repositories")
        assert repo["name"] == _REPO_NAME
        owners = {r["repo_id"] for r in world.query("SELECT repo_id FROM packs")}
        assert owners == {repo["repo_id"]}

    def test_rescan_without_new_backups_registers_nothing(self, world, capsys):
        assert world.lcsas("scan") == 0
        before = world.query("SELECT pack_id, sha256 FROM packs ORDER BY pack_id")
        assert len(before) == len(world.pack_files()) > 0
        capsys.readouterr()

        assert world.lcsas("scan") == 0

        assert "Newly registered: 0" in capsys.readouterr().out
        after = world.query("SELECT pack_id, sha256 FROM packs ORDER BY pack_id")
        assert [tuple(r) for r in after] == [tuple(r) for r in before]

    def test_third_backup_adds_only_the_new_packs(self, world, capsys):
        assert world.lcsas("scan") == 0
        first_scan = {r["sha256"]: r["pack_id"] for r in world.query(
            "SELECT sha256, pack_id FROM packs"
        )}
        assert set(first_scan) == set(world.pack_files())
        capsys.readouterr()

        world.backup()
        new_on_disk = set(world.pack_files()) - set(first_scan)
        assert new_on_disk, "the third backup must have written new pack files"

        assert world.lcsas("scan") == 0

        out = capsys.readouterr().out
        assert f"Newly registered: {len(new_on_disk)}" in out
        now = {r["sha256"]: r["pack_id"] for r in world.query(
            "SELECT sha256, pack_id FROM packs"
        )}
        assert set(now) == set(world.pack_files())
        assert set(now) - set(first_scan) == new_on_disk
        # The packs seen by the first scan were left alone (same rows).
        assert {s: now[s] for s in first_scan} == first_scan

    def test_no_snapshots_flag_registers_packs_and_leaves_snapshots_empty(
        self, world, capsys,
    ):
        """``--no-snapshots`` against the real repo: packs yes, rustic not asked."""
        assert world.lcsas("scan", "--no-snapshots") == 0

        assert f"Newly registered: {len(world.pack_files())}" in (
            capsys.readouterr().out
        )
        assert world.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Snapshots: rustic snapshots --json -> snapshots table
# ---------------------------------------------------------------------------


class TestScanPersistsRealSnapshots:
    def test_scan_with_snapshots_enabled_succeeds_against_real_rustic(
        self, world,
    ):
        """The snapshot branch runs (rustic found, password file read) without
        failing the scan; packs are registered either way."""
        assert world.lcsas("scan") == 0

        assert world.query("SELECT COUNT(*) AS n FROM packs")[0]["n"] == len(
            world.pack_files()
        )

    def test_every_rustic_snapshot_is_persisted_with_its_fields(self, world):
        expected = world.rustic_snapshots()
        assert len(expected) == 2

        assert world.lcsas("scan") == 0

        rows = world.query(
            "SELECT snapshot_id, repo_id, hostname, timestamp, paths, tags "
            "FROM snapshots ORDER BY timestamp"
        )
        assert [r["snapshot_id"] for r in rows] == [
            s["id"] for s in sorted(expected, key=lambda s: s["time"])
        ]
        assert len({r["snapshot_id"] for r in rows}) == 2
        (repo,) = world.query("SELECT repo_id FROM repositories")
        by_id = {s["id"]: s for s in expected}
        for r in rows:
            snap = by_id[r["snapshot_id"]]
            assert len(r["snapshot_id"]) == 64
            assert r["repo_id"] == repo["repo_id"]
            assert r["hostname"] == snap["hostname"] != ""
            assert r["timestamp"] == snap["time"]
            assert json.loads(r["paths"]) == snap["paths"]
            assert json.loads(r["tags"]) == snap["tags"]

    def test_rescan_after_third_backup_adds_only_the_new_snapshot(self, world):
        assert world.lcsas("scan") == 0
        first = {r["snapshot_id"] for r in world.query("SELECT snapshot_id FROM snapshots")}
        assert first == {s["id"] for s in world.rustic_snapshots()}
        assert len(first) == 2

        world.backup()
        all_ids = {s["id"] for s in world.rustic_snapshots()}
        assert len(all_ids) == 3
        assert world.lcsas("scan") == 0

        now = {r["snapshot_id"] for r in world.query("SELECT snapshot_id FROM snapshots")}
        assert now == all_ids
        assert first < now  # the two original rows are still there

    def test_scan_removes_the_empty_id_row_an_older_scan_left(self, world, capsys):
        """#536: scans before the parser fix stored one row keyed ''."""
        (repo,) = world.query("SELECT repo_id FROM repositories")
        conn = get_connection(world.db)
        try:
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, repo_id, hostname, timestamp,"
                " paths, tags, description) VALUES ('', ?, '', '', '[]', '[]', '')",
                (repo["repo_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        assert world.lcsas("scan") == 0

        ids = {r["snapshot_id"] for r in world.query("SELECT snapshot_id FROM snapshots")}
        assert ids == {s["id"] for s in world.rustic_snapshots()}
        assert "Removed 1 snapshot row(s) with an empty id" in capsys.readouterr().out
