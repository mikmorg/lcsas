"""Unit tests for database catalog rebuild functionality."""

from __future__ import annotations

import os
from pathlib import Path

from lcsas.db import rebuild, schema
from lcsas.db.connection import get_connection
from lcsas.restore.planner import RestorePlanner

# Shared fixtures for the recency-aware merge tests (FMA-06).
# VOLUME_CREATED_AT is 2026-01-01 00:00:00 UTC == epoch 1767225600;
# both mtimes lie after it so freshness is mtime-driven unless a test
# deliberately equalises the mtimes.
VOLUME_CREATED_AT = "2026-01-01 00:00:00"
STALE_MTIME = 1_770_000_000.0  # 2026-02-02
FRESH_MTIME = 1_780_000_000.0  # 2026-05-29

_MERGED_TABLES = (
    "repositories",
    "locations",
    "packs",
    "volumes",
    "snapshots",
    "volume_packs",
    "volume_copies",
)


def _build_disc(
    base: Path,
    name: str,
    mtime: float,
    volumes: list[tuple[str, str, str, str]],
    packs: tuple[tuple[str, int, int, str], ...] = (),
) -> Path:
    """Create a disc dir with a catalog.db holding *volumes* and *packs*.

    volumes: (label, uuid, status, created_at) tuples.
    packs:   (sha256, size_bytes, is_pruned, volume_uuid) tuples.
    The catalog file's mtime is forced to *mtime* (the freshness signal).
    """
    disc_dir = base / name
    disc_dir.mkdir()
    catalog_db = disc_dir / "catalog.db"
    conn = get_connection(catalog_db)
    schema.create_all(conn)
    conn.execute(
        "INSERT INTO repositories (repo_id, name, mirror_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("repo1", "Test", "/mnt/mirror", VOLUME_CREATED_AT),
    )
    for label, uuid_, status, created_at in volumes:
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (label, uuid_, "BD25", 25_000_000_000, status, created_at),
        )
    for sha, size_bytes, is_pruned, volume_uuid in packs:
        conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id, is_pruned, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (sha, size_bytes, "repo1", is_pruned, VOLUME_CREATED_AT),
        )
        conn.execute(
            "INSERT INTO volume_packs (volume_id, pack_id) "
            "SELECT v.volume_id, p.pack_id FROM volumes v, packs p "
            "WHERE v.uuid = ? AND p.sha256 = ?",
            (volume_uuid, sha),
        )
    conn.commit()
    conn.close()
    os.utime(catalog_db, (mtime, mtime))
    return disc_dir


def _dump_merged_tables(db_path: Path) -> dict[str, list[tuple]]:
    """Full contents of every merged table, for order-independence checks."""
    conn = get_connection(db_path)
    dump = {
        table: [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")
        ]
        for table in _MERGED_TABLES
    }
    conn.close()
    return dump


class TestRebuildMerge:
    """Test merging disc catalogs into a master database."""

    def test_merge_simple_volumes(self, tmp_path):
        """Merge a simple set of volumes from a source disc."""
        # Create target DB
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        # Create source DB with one volume
        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)
        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test Repo", "/mnt/mirror"),
        )
        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-001", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        assert result["repositories"] == 1
        assert result["volumes"] == 1

        # Verify data was copied
        vol = target_conn.execute(
            "SELECT label, status FROM volumes WHERE uuid = ?", ("uuid-001",)
        ).fetchone()
        assert vol[0] == "VOL001"
        assert vol[1] == "VERIFIED"

        target_conn.close()

    def _status_conflict_dbs(self, tmp_path, target_status, source_status):
        """Target DB holding *target_status*, source DB holding *source_status*
        for the same volume uuid.  Returns (target_conn, source_db_path)."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        target_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "same-uuid", "BD25", 25000000000, target_status),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "same-uuid", "BD25", 25000000000, source_status),
        )
        source_conn.commit()
        source_conn.close()
        return target_conn, source_db

    def test_merge_status_fresher_source_wins_including_downgrade(self, tmp_path):
        """Recency wins (FMA-06): a fresher catalog's status is taken even
        when it is a downgrade (VERIFIED → DESTROYED)."""
        target_conn, source_db = self._status_conflict_dbs(
            tmp_path, "VERIFIED", "DESTROYED"
        )

        warnings: list[str] = []
        rebuild._merge_one_disc(
            target_conn,
            source_db,
            source_freshness=200.0,
            status_freshness={"same-uuid": 100.0},
            warnings=warnings,
        )

        vol = target_conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", ("same-uuid",)
        ).fetchone()
        assert vol[0] == "DESTROYED"
        assert warnings == []

        target_conn.close()

    def test_merge_status_staler_source_cannot_resurrect(self, tmp_path):
        """A stale catalog claiming a destroyed volume is VERIFIED must not
        win — the fresh status is kept and a warning names the volume."""
        target_conn, source_db = self._status_conflict_dbs(
            tmp_path, "DESTROYED", "VERIFIED"
        )

        warnings: list[str] = []
        rebuild._merge_one_disc(
            target_conn,
            source_db,
            source_freshness=100.0,
            status_freshness={"same-uuid": 200.0},
            warnings=warnings,
        )

        vol = target_conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", ("same-uuid",)
        ).fetchone()
        assert vol[0] == "DESTROYED"
        assert len(warnings) == 1
        assert "VOL001" in warnings[0]
        assert "lcsas verify VOL001 --disc" in warnings[0]

        target_conn.close()

    def test_merge_status_staler_lower_rank_keeps_target_quietly(self, tmp_path):
        """A stale catalog with a LESS-alive status is ignored without a
        warning — only the resurrection direction is alarming."""
        target_conn, source_db = self._status_conflict_dbs(
            tmp_path, "VERIFIED", "BURNED"
        )

        warnings: list[str] = []
        rebuild._merge_one_disc(
            target_conn,
            source_db,
            source_freshness=100.0,
            status_freshness={"same-uuid": 200.0},
            warnings=warnings,
        )

        vol = target_conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", ("same-uuid",)
        ).fetchone()
        assert vol[0] == "VERIFIED"
        assert warnings == []

        target_conn.close()

    def test_merge_packs_deduplicates_by_sha256(self, tmp_path):
        """Packs are merged with natural-key deduplication (INSERT OR IGNORE)."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Create a repo first
        target_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        source_conn.commit()

        # Create packs in both DBs
        pack_sha = "a" * 64  # 64-char SHA-256
        target_conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            (pack_sha, 1000, "repo1"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            (pack_sha, 1000, "repo1"),  # Same pack
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        rebuild._merge_one_disc(target_conn, source_db)

        # Pack should not be duplicated (INSERT OR IGNORE)
        count = target_conn.execute(
            "SELECT COUNT(*) FROM packs WHERE sha256 = ?", (pack_sha,)
        ).fetchone()[0]
        assert count == 1

        target_conn.close()

    def test_rebuild_catalog_skip_missing_disc(self, tmp_path):
        """Skip discs that don't have a catalog.db file."""
        output_db = tmp_path / "master.db"

        # Create a disc directory without catalog.db
        disc_dir = tmp_path / "disc1"
        disc_dir.mkdir()

        result = rebuild.rebuild_catalog([disc_dir], output_db)

        assert result.discs_skipped == 1
        assert result.discs_processed == 0
        assert len(result.errors) == 1
        assert "No catalog.db" in result.errors[0]

    def test_rebuild_catalog_processes_multiple_discs(self, tmp_path):
        """Process multiple discs and merge their catalogs."""
        output_db = tmp_path / "master.db"

        # Create two disc directories with catalogs
        disc1_dir = tmp_path / "disc1"
        disc1_dir.mkdir()
        disc1_cat = disc1_dir / "catalog.db"
        disc1_conn = get_connection(disc1_cat)
        schema.create_all(disc1_conn)
        disc1_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Repo 1", "/mnt/mirror1"),
        )
        disc1_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-1", "BD25", 25000000000, "VERIFIED"),
        )
        disc1_conn.commit()
        disc1_conn.close()

        disc2_dir = tmp_path / "disc2"
        disc2_dir.mkdir()
        disc2_cat = disc2_dir / "catalog.db"
        disc2_conn = get_connection(disc2_cat)
        schema.create_all(disc2_conn)
        disc2_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo2", "Repo 2", "/mnt/mirror2"),
        )
        disc2_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL002", "uuid-2", "BD25", 25000000000, "BURNED"),
        )
        disc2_conn.commit()
        disc2_conn.close()

        # Rebuild from both discs
        result = rebuild.rebuild_catalog([disc1_dir, disc2_dir], output_db)

        assert result.discs_processed == 2
        assert result.discs_skipped == 0
        assert result.repositories_merged >= 2
        assert result.volumes_merged >= 2

        # Verify merged data
        output_conn = get_connection(output_db)
        repos = output_conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        vols = output_conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
        assert repos >= 2
        assert vols >= 2
        output_conn.close()

    def test_rebuild_handles_corrupt_source(self, tmp_path):
        """Handle corrupted source database gracefully."""
        output_db = tmp_path / "master.db"

        disc_dir = tmp_path / "disc"
        disc_dir.mkdir()
        catalog_file = disc_dir / "catalog.db"

        # Create a file that looks like a DB but is truncated/corrupt
        catalog_file.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        result = rebuild.rebuild_catalog([disc_dir], output_db)

        assert result.discs_skipped == 1
        assert len(result.errors) == 1

    def test_merge_snapshots(self, tmp_path):
        """Test step 5: snapshots are merged correctly."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Create repo first
        target_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        source_conn.commit()

        # Add snapshots to source
        source_conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, repo_id, hostname, timestamp, paths, tags, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("snap-001", "repo1", "myhost", "2026-01-01T00:00:00",
             '[\"/\"]', "[]", "Snapshot 1"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        # Verify snapshot was merged
        assert result["snapshots"] == 1
        snap = target_conn.execute(
            "SELECT snapshot_id, hostname FROM snapshots WHERE snapshot_id = ?",
            ("snap-001",),
        ).fetchone()
        assert snap is not None
        assert snap[0] == "snap-001"
        assert snap[1] == "myhost"

        target_conn.close()

    def test_merge_volume_packs_with_id_translation(self, tmp_path):
        """Test step 6: volume_packs with ID translation across DBs."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Setup repos in both
        target_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        source_conn.commit()

        # Add volume with pack in source
        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-001", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            ("a" * 64, 5000, "repo1"),
        )
        # Get auto-incremented IDs
        src_vol_id = source_conn.execute(
            "SELECT volume_id FROM volumes WHERE uuid = ?", ("uuid-001",)
        ).fetchone()[0]
        src_pack_id = source_conn.execute(
            "SELECT pack_id FROM packs WHERE sha256 = ?", ("a" * 64,)
        ).fetchone()[0]

        # Link pack to volume
        source_conn.execute(
            "INSERT INTO volume_packs (volume_id, pack_id) VALUES (?, ?)",
            (src_vol_id, src_pack_id),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        # Verify volume_packs was created with correct ID translation
        assert result["volume_packs"] == 1
        vp = target_conn.execute(
            """SELECT vp.volume_id, vp.pack_id, v.uuid, p.sha256
               FROM volume_packs vp
               JOIN volumes v ON v.volume_id = vp.volume_id
               JOIN packs p ON p.pack_id = vp.pack_id""",
        ).fetchone()
        assert vp is not None
        assert vp[2] == "uuid-001"  # Volume UUID
        assert vp[3] == "a" * 64  # Pack SHA-256

        target_conn.close()

    def test_merge_volume_copies_preserves_all_fields(self, tmp_path):
        """Test step 7: volume_copies with all fields preserved."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Setup
        target_conn.execute(
            "INSERT INTO locations (name, description) VALUES (?, ?)",
            ("LOC1", "Location 1"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO locations (name, description) VALUES (?, ?)",
            ("LOC1", "Location 1"),
        )
        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-001", "BD25", 25000000000, "VERIFIED"),
        )
        src_vol_id = source_conn.execute(
            "SELECT volume_id FROM volumes WHERE uuid = ?", ("uuid-001",)
        ).fetchone()[0]

        # Add volume_copy with all fields
        source_conn.execute(
            "INSERT INTO volume_copies "
            "(volume_id, location, status, burn_date, notes, iso_sha256, "
            "last_verified_at, media_serial) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (src_vol_id, "LOC1", "ACTIVE", "2026-01-15", "Test copy",
             "b" * 64, "2026-02-01T12:00:00", "SERIAL123"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        # Verify volume_copies with all fields
        assert result["volume_copies"] == 1
        vc = target_conn.execute(
            "SELECT location, status, burn_date, notes, iso_sha256, last_verified_at, media_serial "
            "FROM volume_copies WHERE location = ?",
            ("LOC1",),
        ).fetchone()
        assert vc is not None
        assert vc[0] == "LOC1"
        assert vc[1] == "ACTIVE"
        assert vc[2] == "2026-01-15"
        assert vc[3] == "Test copy"
        assert vc[4] == "b" * 64
        assert vc[5] == "2026-02-01T12:00:00"
        assert vc[6] == "SERIAL123"

        target_conn.close()

    def test_rebuild_merges_events_and_sessions(self, tmp_path):
        """FMA-10: a source catalog carrying volume_events + session rows
        ends up in the rebuilt master with the volume_id translated via
        uuid (autoincrement ids differ between databases)."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Bump the source volume's surrogate id so a naive (untranslated)
        # merge would attach the audit rows to the wrong/absent volume.
        source_conn.execute(
            "INSERT INTO volumes (volume_id, label, uuid, media_type, "
            "capacity_bytes, status) VALUES (?, ?, ?, ?, ?, ?)",
            (77, "VOL001", "uuid-evt", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.execute(
            "INSERT INTO burn_sessions (session_id, media_type, status, "
            "staging_dir) VALUES (?, ?, ?, ?)",
            ("sess-abc", "BD25", "COMPLETE", "/staging/sess-abc"),
        )
        source_conn.execute(
            "INSERT INTO session_volumes (session_id, volume_id, iso_path, "
            "iso_sha256, iso_size_bytes) VALUES (?, ?, ?, ?, ?)",
            ("sess-abc", 77, "/staging/sess-abc/VOL001.iso", "c" * 64, 4096),
        )
        source_conn.execute(
            "INSERT INTO volume_events (volume_id, event_type, event_date, "
            "detail) VALUES (?, ?, ?, ?)",
            (77, "VERIFY_PASS", "2026-03-01 00:00:00", "post-burn read-back"),
        )
        source_conn.commit()
        source_conn.close()

        result = rebuild._merge_one_disc(target_conn, source_db)
        assert result["volume_events"] == 1
        assert result["burn_sessions"] == 1
        assert result["session_volumes"] == 1

        tgt_vol_id = target_conn.execute(
            "SELECT volume_id FROM volumes WHERE uuid = ?", ("uuid-evt",)
        ).fetchone()[0]

        evt = tuple(target_conn.execute(
            "SELECT volume_id, event_type, detail FROM volume_events"
        ).fetchone())
        assert evt == (tgt_vol_id, "VERIFY_PASS", "post-burn read-back")

        sess = tuple(target_conn.execute(
            "SELECT session_id, status, staging_dir FROM burn_sessions"
        ).fetchone())
        assert sess == ("sess-abc", "COMPLETE", "/staging/sess-abc")

        sv = tuple(target_conn.execute(
            "SELECT session_id, volume_id, iso_sha256, iso_size_bytes "
            "FROM session_volumes"
        ).fetchone())
        assert sv == ("sess-abc", tgt_vol_id, "c" * 64, 4096)

        target_conn.close()

    def test_rebuild_events_dedupe_across_two_discs(self, tmp_path):
        """The same event present on two discs must land once (the natural
        triple is volume-uuid + type + date + detail)."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        def _make_source(path: Path) -> Path:
            conn = get_connection(path)
            schema.create_all(conn)
            conn.execute(
                "INSERT INTO volumes (label, uuid, media_type, "
                "capacity_bytes, status) VALUES (?, ?, ?, ?, ?)",
                ("VOL001", "uuid-dup", "BD25", 25000000000, "VERIFIED"),
            )
            vid = conn.execute(
                "SELECT volume_id FROM volumes WHERE uuid = ?", ("uuid-dup",)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO volume_events (volume_id, event_type, "
                "event_date, detail) VALUES (?, ?, ?, ?)",
                (vid, "VERIFY_PASS", "2026-03-01 00:00:00", "rb"),
            )
            conn.commit()
            conn.close()
            return path

        src1 = _make_source(tmp_path / "s1.db")
        src2 = _make_source(tmp_path / "s2.db")

        r1 = rebuild._merge_one_disc(target_conn, src1)
        r2 = rebuild._merge_one_disc(target_conn, src2)
        assert r1["volume_events"] == 1
        assert r2["volume_events"] == 0  # already present, deduped

        count = target_conn.execute(
            "SELECT COUNT(*) FROM volume_events"
        ).fetchone()[0]
        assert count == 1
        target_conn.close()

    def test_rebuild_tolerates_catalog_without_event_tables(self, tmp_path):
        """A v3-era-shaped source (no volume_events/burn_sessions/
        session_volumes) merges cleanly; other tables stay intact and the
        provenance counts are zero rather than raising."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        # A v3-era catalog: the live schema minus the three provenance
        # tables that arrived later (volume_events / burn_sessions /
        # session_volumes).  Dropping them off a real schema keeps every
        # other table shaped exactly as the rebuild SELECTs expect.
        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)
        for tbl in ("session_volumes", "burn_sessions", "volume_events"):
            source_conn.execute(f"DROP TABLE {tbl}")
        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
            "status) VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-old", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.commit()
        source_conn.close()

        result = rebuild._merge_one_disc(target_conn, source_db)
        assert result["volumes"] == 1
        assert result["volume_events"] == 0
        assert result["burn_sessions"] == 0
        assert result["session_volumes"] == 0

        vol = target_conn.execute(
            "SELECT label FROM volumes WHERE uuid = ?", ("uuid-old",)
        ).fetchone()
        assert vol[0] == "VOL001"
        target_conn.close()


class TestRecencyAwareRebuild:
    """FMA-06: rebuild merges newest-first; stale discs cannot resurrect
    deprecated/destroyed volumes, and feed order never changes the output."""

    def test_destroyed_volume_not_resurrected_either_order(self, tmp_path):
        """A stale catalog says VERIFIED, a fresh one says DESTROYED:
        DESTROYED must survive in both feed orders, with exactly one
        resurrection warning, and the rebuilt catalog must route the
        volume's packs through the planner's deprecated channel."""
        sha = "a" * 64
        stale = _build_disc(
            tmp_path,
            "stale",
            STALE_MTIME,
            volumes=[("VOL001", "uuid-001", "VERIFIED", VOLUME_CREATED_AT)],
            packs=((sha, 1000, 0, "uuid-001"),),
        )
        fresh = _build_disc(
            tmp_path,
            "fresh",
            FRESH_MTIME,
            volumes=[("VOL001", "uuid-001", "DESTROYED", VOLUME_CREATED_AT)],
            packs=((sha, 1000, 0, "uuid-001"),),
        )

        dumps = []
        for idx, order in enumerate(([stale, fresh], [fresh, stale])):
            output_db = tmp_path / f"master-{idx}.db"
            result = rebuild.rebuild_catalog(order, output_db)
            assert result.ok

            conn = get_connection(output_db)
            status = conn.execute(
                "SELECT status FROM volumes WHERE uuid = ?", ("uuid-001",)
            ).fetchone()[0]
            assert status == "DESTROYED"

            # Exactly one resurrection warning (emitted when the stale
            # disc was processed), naming the volume and the remedy.
            assert len(result.warnings) == 1
            warning = result.warnings[0]
            assert "VOL001" in warning
            assert "VERIFIED" in warning
            assert "DESTROYED" in warning
            assert "lcsas verify VOL001 --disc" in warning

            # Restore planning on the rebuilt catalog must NOT offer the
            # destroyed volume as a pick — its packs go through the
            # deprecated_disc_labels warning channel instead.
            pick = RestorePlanner(conn).generate_pick_list([sha])
            assert pick.volumes == {}
            assert sha in pick.missing_packs
            assert pick.deprecated_disc_labels == {"VOL001": [sha]}
            conn.close()

            dumps.append(_dump_merged_tables(output_db))

        # Order-independence: all merged tables byte-identical.
        assert dumps[0] == dumps[1]

    def test_pack_fields_prefer_freshest_catalog(self, tmp_path):
        """Sources disagree on is_pruned/size_bytes: both feed orders must
        yield the freshest catalog's view, plus one summary warning."""
        sha = "b" * 64
        stale = _build_disc(
            tmp_path,
            "stale",
            STALE_MTIME,
            volumes=[("VOL001", "uuid-001", "VERIFIED", VOLUME_CREATED_AT)],
            packs=((sha, 1111, 0, "uuid-001"),),
        )
        fresh = _build_disc(
            tmp_path,
            "fresh",
            FRESH_MTIME,
            volumes=[("VOL001", "uuid-001", "VERIFIED", VOLUME_CREATED_AT)],
            packs=((sha, 2222, 1, "uuid-001"),),
        )

        dumps = []
        for idx, order in enumerate(([stale, fresh], [fresh, stale])):
            output_db = tmp_path / f"master-{idx}.db"
            result = rebuild.rebuild_catalog(order, output_db)
            assert result.ok

            conn = get_connection(output_db)
            row = conn.execute(
                "SELECT size_bytes, is_pruned FROM packs WHERE sha256 = ?",
                (sha,),
            ).fetchone()
            conn.close()
            assert (row[0], row[1]) == (2222, 1)

            # One summary warning about the is_pruned disagreement —
            # statuses agree, so it is the only warning.
            assert len(result.warnings) == 1
            assert "is_pruned" in result.warnings[0]

            dumps.append(_dump_merged_tables(output_db))

        assert dumps[0] == dumps[1]

    def test_freshness_falls_back_to_max_created_at(self, tmp_path):
        """Equal mtimes (older than every row): the row-derived
        MAX(volumes.created_at) must decide which catalog is fresher."""
        equal_mtime = 1000.0
        stale = _build_disc(
            tmp_path,
            "stale",
            equal_mtime,
            volumes=[("VOL001", "uuid-001", "VERIFIED", "2026-01-01 00:00:00")],
        )
        # The fresh catalog knows a volume created months later — its
        # MAX(created_at) outranks the stale catalog's despite the mtimes.
        fresh = _build_disc(
            tmp_path,
            "fresh",
            equal_mtime,
            volumes=[
                ("VOL001", "uuid-001", "DESTROYED", "2026-01-01 00:00:00"),
                ("VOL002", "uuid-002", "VERIFIED", "2026-04-01 00:00:00"),
            ],
        )

        for idx, order in enumerate(([stale, fresh], [fresh, stale])):
            output_db = tmp_path / f"master-{idx}.db"
            result = rebuild.rebuild_catalog(order, output_db)
            assert result.ok

            conn = get_connection(output_db)
            status = conn.execute(
                "SELECT status FROM volumes WHERE uuid = ?", ("uuid-001",)
            ).fetchone()[0]
            conn.close()
            assert status == "DESTROYED"
            assert len(result.warnings) == 1
