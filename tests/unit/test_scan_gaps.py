"""Gap tests for ``lcsas scan`` and the pack-registration internals (issue #517).

Pins behaviour that ``docs/workflows/backup-and-scan.md`` lists as untested:

* ``--no-prune-sync`` really skips prune detection (the detector is never
  consulted and the mass-prune guard never fires);
* ``--no-snapshots`` really skips the snapshot block (no rustic version
  check, no runner, existing ``snapshots`` rows untouched) -- with a control
  run proving the patched seams are live;
* the >900-item SQLite-variable batching in ``DeltaAnalyzer``,
  ``bulk_register``, ``bulk_mark_pruned`` and the restore queries.  Modern
  SQLite allows 32 766 variables, so an *unbatched* 1 000-placeholder query
  would still succeed and a naive test would pass vacuously; the
  ``sqlite_999_vars`` fixture lowers the connection limit to the classic 999
  so removing the batching makes these tests fail loudly;
* the ``bulk_register`` size-mismatch warning.

No real rustic binary is needed: the snapshot seams are patched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.packs import bulk_mark_pruned, bulk_register, list_packs, register_pack
from lcsas.db.queries import get_missing_packs, get_pick_list
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.snapshots import list_snapshots, upsert_snapshot
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.exceptions import BinaryError
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.rustic.types import SnapshotInfo

# ---------------------------------------------------------------------------
# Helpers (small copies of the patterns in test_cli_scan.py)
# ---------------------------------------------------------------------------


def _sha(i: int) -> str:
    return f"{i:064x}"


def _make_mirror(tmp_path: Path, repo_name: str, pack_hashes: list[str],
                 pack_size: int = 64) -> Path:
    """Create a fake Rustic mirror with pack files in two-level layout."""
    mirror = tmp_path / "mirror" / repo_name
    data_dir = mirror / "data"
    for sha in pack_hashes:
        prefix_dir = data_dir / sha[:2]
        prefix_dir.mkdir(parents=True, exist_ok=True)
        (prefix_dir / sha).write_bytes(b"x" * pack_size)
    (mirror / "config").write_text("{}")
    for sub in ("keys", "index", "snapshots"):
        (mirror / sub).mkdir(exist_ok=True)
    return mirror


def _write_config(tmp_path: Path, db_path: Path, repos: dict[str, Path],
                  password_files: dict[str, Path] | None = None) -> Path:
    config_path = tmp_path / "config.toml"
    repo_blocks = ""
    for name, mirror_path in repos.items():
        repo_blocks += f'\n[repos.{name}]\nmirror_path = "{mirror_path}"\n'
        if password_files and name in password_files:
            repo_blocks += f'password_file = "{password_files[name]}"\n'
    config_path.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{db_path}"\n'
        "\n[defaults]\n"
        'media_type = "TEST_TINY"\n'
        "ecc_redundancy_pct = 0\n"
        'location = "Home_Shelf"\n'
        'optical_device = "/dev/null"\n'
        'label_prefix = "TEST"\n'
        "metadata_reserve_mb = 0\n"
        f"{repo_blocks}"
    )
    return config_path


class _Env:
    """A configured catalog + mirror for one repo called ``family``."""

    def __init__(self, tmp_path: Path, on_disk: list[str],
                 known_in_db: list[str] | None = None, *, with_password: bool = False) -> None:
        self.db_path = tmp_path / "archive.db"
        self.mirror = _make_mirror(tmp_path, "family", on_disk)
        self.password_file: Path | None = None
        if with_password:
            self.password_file = tmp_path / "family.pw"
            self.password_file.write_text("hunter2\n")
        self.config = _write_config(
            tmp_path, self.db_path, {"family": self.mirror},
            {"family": self.password_file} if self.password_file else None,
        )
        main(["init", "--db-path", str(self.db_path)])
        conn = get_connection(self.db_path)
        create_all(conn)
        register_repo(conn, "family", "family", str(self.mirror), "")
        for sha in known_in_db or []:
            register_pack(conn, sha256=sha, size_bytes=1024, repo_id="family")
        conn.close()

    def scan(self, *flags: str) -> int:
        return main(["--config", str(self.config), "--db", str(self.db_path),
                     "scan", *flags])

    def active_packs(self) -> list:
        conn = get_connection(self.db_path)
        try:
            return list_packs(conn, include_pruned=False)
        finally:
            conn.close()

    def snapshots(self) -> list:
        conn = get_connection(self.db_path)
        try:
            return list_snapshots(conn)
        finally:
            conn.close()


@pytest.fixture
def sqlite_999_vars(monkeypatch):
    """Make every new sqlite3 connection enforce the classic 999-variable cap.

    Without this, SQLite >= 3.32 accepts 32 766 variables and the batching
    code is never *required* for correctness at test sizes.  The fixture
    proves the cap is live before yielding, so it can never go vacuous.
    """
    real_connect = sqlite3.connect

    def limited_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return conn

    monkeypatch.setattr(sqlite3, "connect", limited_connect)

    probe = sqlite3.connect(":memory:")
    try:
        placeholders = ",".join("?" * 1000)
        with pytest.raises(sqlite3.OperationalError, match="too many SQL variables"):
            probe.execute(f"SELECT 1 WHERE 1 IN ({placeholders})", list(range(1000)))
    finally:
        probe.close()
    yield


@pytest.fixture
def db(sqlite_999_vars):
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "r1", "r1", "/mirror/r1")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# --no-prune-sync
# ---------------------------------------------------------------------------


class TestNoPruneSync:
    """``--no-prune-sync`` skips the whole prune-reconciliation block."""

    def test_flag_keeps_packs_missing_from_mirror_active(self, tmp_path, capsys):
        shas = [_sha(i) for i in range(100)]
        env = _Env(tmp_path, on_disk=shas[:98], known_in_db=shas)  # 2 "pruned"

        assert env.scan("--no-snapshots", "--no-prune-sync") == 0

        out = capsys.readouterr().out
        assert "Pruned packs" not in out
        assert "Refusing" not in out
        assert len(env.active_packs()) == 100  # nothing flipped to is_pruned

    def test_control_same_setup_without_flag_does_prune(self, tmp_path, capsys):
        """Proves the setup above would prune -- so the flag is what stopped it."""
        shas = [_sha(i) for i in range(100)]
        env = _Env(tmp_path, on_disk=shas[:98], known_in_db=shas)

        assert env.scan("--no-snapshots") == 0

        assert "Pruned packs:   2" in capsys.readouterr().out
        assert len(env.active_packs()) == 98

    def test_flag_means_detect_pruned_is_never_consulted(self, tmp_path):
        shas = [_sha(i) for i in range(20)]
        env = _Env(tmp_path, on_disk=shas[:19], known_in_db=shas)

        real = DeltaAnalyzer.detect_pruned
        with patch.object(DeltaAnalyzer, "detect_pruned", autospec=True,
                          side_effect=real) as spy:
            assert env.scan("--no-snapshots", "--no-prune-sync") == 0
            assert spy.call_count == 0

            assert env.scan("--no-snapshots") == 0  # control: not skipped
            assert spy.call_count == 1

    def test_flag_bypasses_the_mass_prune_guard_too(self, tmp_path, capsys):
        """With the block skipped there is no 'Refusing ...' warning either, and
        --yes-prune cannot resurrect it: the skip wins over the confirmation."""
        shas = [_sha(i) for i in range(100)]
        env = _Env(tmp_path, on_disk=shas[:1], known_in_db=shas)  # 99% "pruned"

        assert env.scan("--no-snapshots", "--no-prune-sync") == 0
        out = capsys.readouterr().out
        assert "Refusing" not in out
        assert len(env.active_packs()) == 100

        assert env.scan("--no-snapshots", "--no-prune-sync", "--yes-prune") == 0
        assert "Pruned packs" not in capsys.readouterr().out
        assert len(env.active_packs()) == 100

    def test_flag_still_registers_new_packs(self, tmp_path, capsys):
        known = [_sha(i) for i in range(5)]
        fresh = [_sha(100), _sha(101), _sha(102)]
        env = _Env(tmp_path, on_disk=fresh, known_in_db=known)  # known all "gone"

        assert env.scan("--no-snapshots", "--no-prune-sync") == 0

        assert "Newly registered: 3" in capsys.readouterr().out
        assert len(env.active_packs()) == len(known) + len(fresh)


# ---------------------------------------------------------------------------
# --no-snapshots
# ---------------------------------------------------------------------------


_SEAMS = {
    "runner": "lcsas.rustic.wrapper.SubprocessRusticRunner",
    "version": "lcsas.utils.subprocess.check_binary_version",
}


class TestNoSnapshots:
    """``--no-snapshots`` skips rustic entirely.

    ``password_file`` is configured (and exists) in every test here: without
    one the snapshot loop would skip the repo anyway and "rustic not called"
    would be vacuous.  The control test proves the seams are live.
    """

    def test_control_without_flag_checks_version_and_lists_snapshots(self, tmp_path, capsys):
        env = _Env(tmp_path, on_disk=[_sha(1)], with_password=True)
        snap = SnapshotInfo(
            snapshot_id="a" * 8, timestamp="2026-01-02T03:04:05Z",
            hostname="nas", paths=["/data"], tags=["weekly"],
        )
        with patch(_SEAMS["runner"]) as runner_cls, patch(_SEAMS["version"]) as check:
            runner_cls.return_value.snapshots.return_value = [snap]
            assert env.scan() == 0

        check.assert_called_once()
        assert check.call_args.args[0] == "rustic"
        runner_cls.return_value.snapshots.assert_called_once_with(
            repo_path=env.mirror, password_file=env.password_file,
        )
        # cmd_scan persisted what the runner returned.
        rows = env.snapshots()
        assert [r.snapshot_id for r in rows] == ["a" * 8]
        assert rows[0].repo_id == "family"
        assert rows[0].hostname == "nas"
        assert json.loads(rows[0].paths) == ["/data"]
        assert json.loads(rows[0].tags) == ["weekly"]
        assert "Snapshots persisted: 1" in capsys.readouterr().out

    def test_flag_never_touches_rustic(self, tmp_path, capsys):
        env = _Env(tmp_path, on_disk=[_sha(1), _sha(2)], with_password=True)
        with patch(_SEAMS["runner"]) as runner_cls, patch(_SEAMS["version"]) as check:
            assert env.scan("--no-snapshots") == 0

        check.assert_not_called()
        runner_cls.assert_not_called()  # not even constructed
        out = capsys.readouterr().out
        assert "Snapshots persisted" not in out
        assert "Newly registered: 2" in out  # the pack scan itself still ran

    def test_flag_leaves_existing_snapshot_rows_untouched(self, tmp_path):
        env = _Env(tmp_path, on_disk=[_sha(1)], with_password=True)
        conn = get_connection(env.db_path)
        upsert_snapshot(conn, "old12345", "family", hostname="oldhost",
                        timestamp="2020-01-01T00:00:00Z", paths='["/old"]', tags="[]")
        conn.close()
        before = [(s.snapshot_id, s.hostname, s.timestamp, s.paths) for s in env.snapshots()]
        assert before  # sanity: something to preserve

        with patch(_SEAMS["runner"]) as runner_cls, patch(_SEAMS["version"]):
            # If the snapshot block ran it would rewrite 'old12345' (INSERT OR
            # REPLACE) and add 'new67890'; the flag must keep both from happening.
            runner_cls.return_value.snapshots.return_value = [
                SnapshotInfo("old12345", "2026-06-06T00:00:00Z", "NEWHOST", ["/new"], []),
                SnapshotInfo("new67890", "2026-06-07T00:00:00Z", "NEWHOST", ["/new"], []),
            ]
            assert env.scan("--no-snapshots") == 0

        after = [(s.snapshot_id, s.hostname, s.timestamp, s.paths) for s in env.snapshots()]
        assert after == before

    def test_flag_works_when_rustic_check_would_fail(self, tmp_path):
        """The docs promise ``scan --no-snapshots`` works with no rustic installed."""
        env = _Env(tmp_path, on_disk=[_sha(1)], with_password=True)
        with patch(_SEAMS["version"], side_effect=BinaryError("rustic not found")) as check:
            assert env.scan("--no-snapshots") == 0
        check.assert_not_called()

    def test_rustic_version_check_failure_aborts_scan_with_rc1(self, tmp_path, capsys):
        env = _Env(tmp_path, on_disk=[_sha(1)], with_password=True)
        with patch(_SEAMS["runner"]) as runner_cls, \
                patch(_SEAMS["version"], side_effect=BinaryError("rustic 0.8.0 is too old")):
            assert env.scan() == 1

        runner_cls.assert_not_called()
        assert "rustic 0.8.0 is too old" in capsys.readouterr().out
        assert env.snapshots() == []

    def test_repo_without_password_file_is_skipped_even_without_flag(self, tmp_path):
        """Why the other tests configure a password file: with none, the loop
        never calls the runner and the flag would be unobservable."""
        env = _Env(tmp_path, on_disk=[_sha(1)], with_password=False)
        with patch(_SEAMS["runner"]) as runner_cls, patch(_SEAMS["version"]):
            assert env.scan() == 0
        runner_cls.return_value.snapshots.assert_not_called()


# ---------------------------------------------------------------------------
# >900-item batching (SQLite variable limit)
# ---------------------------------------------------------------------------

# Boundaries around _SQLITE_BATCH == 900, plus multi-batch sizes.
_SIZES = [899, 900, 901, 1800, 1801, 2500]


class TestBatchingBeyond900:
    @pytest.mark.parametrize("n", _SIZES)
    def test_delta_register_new_packs_registers_every_pack(self, db, n):
        scanned = {_sha(i): 100 + i for i in range(n)}
        new = DeltaAnalyzer(db, scanned, "r1").register_new_packs()

        assert [p.sha256 for p in new] == list(scanned)  # all of them, in order
        assert db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == n
        # Sizes survived the round trip batch by batch.
        assert {p.sha256: p.size_bytes for p in new} == scanned

    @pytest.mark.parametrize("n", [901, 2500])
    def test_delta_second_pass_filters_existing_across_batches(self, db, n):
        scanned = {_sha(i): 100 for i in range(n)}
        assert len(DeltaAnalyzer(db, scanned, "r1").register_new_packs()) == n
        assert DeltaAnalyzer(db, scanned, "r1").register_new_packs() == []

    def test_delta_registers_only_the_unknown_ones_in_a_big_scan(self, db):
        n = 1801
        scanned = {_sha(i): 100 for i in range(n)}
        for i in range(0, n, 2):  # every other pack already known, spans all batches
            register_pack(db, sha256=_sha(i), size_bytes=100, repo_id="r1")

        new = DeltaAnalyzer(db, scanned, "r1").register_new_packs()

        assert [p.sha256 for p in new] == [_sha(i) for i in range(1, n, 2)]
        assert db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == n

    @pytest.mark.parametrize("n", _SIZES)
    def test_bulk_register_returns_every_pack_in_input_order(self, db, n):
        packs = [(_sha(i), 10 + i, "r1") for i in range(n)]
        result = bulk_register(db, packs)
        assert [p.sha256 for p in result] == [t[0] for t in packs]
        assert [p.size_bytes for p in result] == [t[1] for t in packs]

    @pytest.mark.parametrize("n", _SIZES)
    def test_bulk_mark_pruned_updates_every_pack(self, db, n):
        packs = bulk_register(db, [(_sha(i), 10, "r1") for i in range(n)])

        updated = bulk_mark_pruned(db, [p.pack_id for p in packs])

        assert updated == n
        assert list_packs(db, repo_id="r1", include_pruned=False) == []
        assert len(list_packs(db, repo_id="r1", include_pruned=True)) == n

    def test_get_missing_packs_batches_its_lookups(self, db):
        n = 1801
        packs = bulk_register(db, [(_sha(i), 10, "r1") for i in range(n)])
        vol = create_volume(db, "VOL_1", "u1", "TEST_TINY", 1_000_000, "Home", "VERIFIED")
        # The first 1000 packs are on an active volume; the rest are catalogued
        # but unassigned.  Plus 5 shas the catalog has never heard of.
        bulk_link_packs(db, vol.volume_id, [p.pack_id for p in packs[:1000]])
        unknown = [_sha(10_000 + i) for i in range(5)]

        missing = get_missing_packs(db, [p.sha256 for p in packs] + unknown)

        assert sorted(missing) == sorted([p.sha256 for p in packs[1000:]] + unknown)

    def test_get_pick_list_batches_its_lookups(self, db):
        n = 1801
        packs = bulk_register(db, [(_sha(i), 10, "r1") for i in range(n)])
        v1 = create_volume(db, "VOL_1", "u1", "TEST_TINY", 1_000_000, "Home", "VERIFIED")
        v2 = create_volume(db, "VOL_2", "u2", "TEST_TINY", 1_000_000, "Home", "VERIFIED")
        bulk_link_packs(db, v1.volume_id, [p.pack_id for p in packs[:1200]])
        bulk_link_packs(db, v2.volume_id, [p.pack_id for p in packs[1200:]])

        pick = get_pick_list(db, [p.sha256 for p in packs])

        assert set(pick) == {"VOL_1", "VOL_2"}
        assert len(pick["VOL_1"]) == 1200
        assert len(pick["VOL_2"]) == n - 1200
        assert {p.sha256 for v in pick.values() for p in v} == {p.sha256 for p in packs}

    def test_cli_scan_of_1000_packs_registers_then_prunes_them_all(
        self, tmp_path, capsys, sqlite_999_vars
    ):
        """End to end through ``lcsas scan`` on a connection capped at 999
        variables: registration (delta + bulk_register batches), then a
        confirmed mass-prune of 950 packs (bulk_mark_pruned across two batches)."""
        shas = [_sha(i) for i in range(1000)]
        env = _Env(tmp_path, on_disk=shas)

        assert env.scan("--no-snapshots") == 0
        out = capsys.readouterr().out
        assert "Newly registered: 1000" in out
        assert len(env.active_packs()) == 1000

        # 950 packs vanish from the mirror; 50 remain.
        for sha in shas[:950]:
            (env.mirror / "data" / sha[:2] / sha).unlink()
        assert env.scan("--no-snapshots", "--yes-prune") == 0
        out = capsys.readouterr().out
        assert "Pruned packs:   950" in out
        assert sorted(p.sha256 for p in env.active_packs()) == shas[950:]


# ---------------------------------------------------------------------------
# bulk_register size-mismatch warning
# ---------------------------------------------------------------------------


def _mismatch_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "size mismatch" in r.getMessage()]


class TestBulkRegisterSizeMismatch:
    def test_warns_and_keeps_the_original_size(self, memory_db, caplog):
        sha = "ab" * 32
        register_pack(memory_db, sha256=sha, size_bytes=1024, repo_id="_test")

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            result = bulk_register(memory_db, [(sha, 2048, "_test")])

        hits = _mismatch_records(caplog)
        assert len(hits) == 1
        msg = hits[0].getMessage()
        assert sha[:16] in msg
        assert "DB has 1024 bytes" in msg
        assert "found 2048 bytes" in msg
        # The catalog trusts the first-recorded size (documented, intentional).
        assert [p.size_bytes for p in result] == [1024]
        row = memory_db.execute("SELECT size_bytes FROM packs WHERE sha256 = ?",
                                (sha,)).fetchone()
        assert row["size_bytes"] == 1024

    def test_matching_size_is_silent(self, memory_db, caplog):
        sha = "cd" * 32
        register_pack(memory_db, sha256=sha, size_bytes=1024, repo_id="_test")

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            bulk_register(memory_db, [(sha, 1024, "_test")])

        assert _mismatch_records(caplog) == []

    def test_brand_new_pack_is_silent(self, memory_db, caplog):
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            bulk_register(memory_db, [("ef" * 32, 7, "_test")])
        assert _mismatch_records(caplog) == []

    def test_only_the_mismatching_pack_is_reported(self, memory_db, caplog):
        good, bad = "11" * 32, "22" * 32
        register_pack(memory_db, sha256=good, size_bytes=100, repo_id="_test")
        register_pack(memory_db, sha256=bad, size_bytes=200, repo_id="_test")

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            bulk_register(memory_db, [(good, 100, "_test"), (bad, 999, "_test")])

        hits = _mismatch_records(caplog)
        assert len(hits) == 1
        assert bad[:16] in hits[0].getMessage()

    def test_mismatch_is_detected_in_a_later_batch(self, db, caplog):
        """A mismatching pack at index 950 sits in the second 900-batch."""
        register_pack(db, sha256=_sha(950), size_bytes=111, repo_id="r1")
        packs = [(_sha(i), 111 if i == 950 else 5, "r1") for i in range(1000)]
        packs[950] = (_sha(950), 222, "r1")  # disagrees with the stored 111

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            result = bulk_register(db, packs)

        assert len(result) == 1000
        hits = _mismatch_records(caplog)
        assert len(hits) == 1
        assert _sha(950)[:16] in hits[0].getMessage()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: backup-and-scan.md section 6 says a re-scan 'logs a warning on "
            "size mismatch', but DeltaAnalyzer.register_new_packs() drops packs "
            "already in the catalog BEFORE calling bulk_register(), so the "
            "warning is unreachable from `lcsas scan`: a pack truncated/altered "
            "on the mirror after its first scan is never flagged. See #517 report."
        ),
    )
    def test_rescan_after_mirror_pack_changed_size_warns(self, tmp_path, capsys):
        sha = _sha(7)
        env = _Env(tmp_path, on_disk=[sha])
        assert env.scan("--no-snapshots") == 0
        capsys.readouterr()

        # Same content-addressed name, different length: mirror corruption.
        (env.mirror / "data" / sha[:2] / sha).write_bytes(b"y" * 7)
        assert env.scan("--no-snapshots") == 0

        assert "size mismatch" in capsys.readouterr().out
