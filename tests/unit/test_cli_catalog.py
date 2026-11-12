"""Tests for the ``lcsas catalog`` command (``lcsas.cli.commands.catalog``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import get_copies_for_volume
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import create_volume, get_volume_by_label


def _build_fake_disc(
    disc_path,
    catalog_hashes,
    disc_file_hashes,
    *,
    volume_label="VOL_001",
    repo_name="family",
):
    """Build a minimal fake mounted-disc directory at *disc_path*.

    Writes a holographic catalog (``catalog.db``) that registers *repo_name*
    and one volume whose ``volume_packs`` rows reference every SHA in
    *catalog_hashes*, plus ``data/<hash>`` files for every SHA in
    *disc_file_hashes*.  The two sets need not match — that mismatch is
    exactly what ``cmd_catalog_validate`` is meant to surface.
    """
    import sqlite3

    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume
    from lcsas.utils.labels import generate_uuid

    disc_path.mkdir(parents=True, exist_ok=True)
    catalog_db = disc_path / "catalog.db"

    conn = sqlite3.connect(str(catalog_db))
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
        if catalog_hashes:
            packs = [register_pack(conn, sha, 1024, repo_name) for sha in catalog_hashes]
            vol = create_volume(
                conn,
                volume_label,
                generate_uuid(),
                "TEST_TINY",
                1_000_000,
                "Home_Shelf",
                "VERIFIED",
            )
            bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
        conn.commit()
    finally:
        conn.close()

    data_dir = disc_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for sha in disc_file_hashes:
        (data_dir / sha).write_bytes(b"x")


class TestCmdCatalogValidate:
    def test_catalog_validate_happy_path(self, tmp_path, capsys):
        """Disc with data files matching the embedded catalog exits 0."""
        disc = tmp_path / "disc"
        hashes = ["aa" * 32, "bb" * 32]
        _build_fake_disc(disc, catalog_hashes=hashes, disc_file_hashes=hashes)

        result = main(["catalog", "validate", str(disc)])
        assert result == 0
        out = capsys.readouterr().out
        assert "Catalog validation PASSED" in out
        assert "Catalog packs  : 2" in out
        assert "Disc packs     : 2" in out

    def test_catalog_validate_orphan_packs(self, tmp_path, capsys):
        """Disc with pack files NOT in the catalog is reported as orphaned."""
        disc = tmp_path / "disc"
        catalog_hashes = ["aa" * 32]
        # Disc has an extra file the catalog doesn't reference.
        disc_file_hashes = ["aa" * 32, "cc" * 32]
        _build_fake_disc(
            disc,
            catalog_hashes=catalog_hashes,
            disc_file_hashes=disc_file_hashes,
        )

        result = main(["catalog", "validate", str(disc)])
        assert result != 0
        out = capsys.readouterr().out
        assert "ORPHAN" in out
        assert "cc" * 32 in out
        assert "Catalog validation FAILED" in out

    def test_catalog_validate_missing_packs(self, tmp_path, capsys):
        """Catalog references packs that are absent on disc -> reported missing."""
        disc = tmp_path / "disc"
        catalog_hashes = ["aa" * 32, "bb" * 32]
        # Only one of the two cataloged packs is on the disc.
        disc_file_hashes = ["aa" * 32]
        _build_fake_disc(
            disc,
            catalog_hashes=catalog_hashes,
            disc_file_hashes=disc_file_hashes,
        )

        result = main(["catalog", "validate", str(disc)])
        assert result != 0
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "bb" * 32 in out
        assert "Catalog validation FAILED" in out

    def test_catalog_validate_unknown_path_errors(self, tmp_path, capsys):
        """`catalog validate /nonexistent` exits non-zero with a clear error."""
        missing = tmp_path / "no-such-disc"
        assert not missing.exists()

        result = main(["catalog", "validate", str(missing)])
        assert result != 0
        out = capsys.readouterr().out
        assert "does not exist" in out or "not a directory" in out
        assert str(missing) in out


def _build_fake_disc_for_rebuild(
    disc_path,
    *,
    repo_id,
    repo_name,
    volume_label,
    volume_uuid,
    pack_hashes,
):
    """Build a minimal fake mounted-disc directory holding a holographic catalog.

    Writes ``<disc_path>/catalog.db`` containing a single repository, a single
    volume, and a row for each SHA in *pack_hashes* linked to that volume.
    No on-disc ``data/`` files are required for catalog rebuild — rebuild
    only consults ``catalog.db``.
    """
    import sqlite3

    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    disc_path.mkdir(parents=True, exist_ok=True)
    catalog_db = disc_path / "catalog.db"

    conn = sqlite3.connect(str(catalog_db))
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        register_repo(conn, repo_id, repo_name, f"/mnt/mirror/{repo_name}", "")
        packs = [register_pack(conn, sha, 1024, repo_id) for sha in pack_hashes]
        vol = create_volume(
            conn,
            volume_label,
            volume_uuid,
            "TEST_TINY",
            1_000_000,
            "Home_Shelf",
            "VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
        conn.commit()
    finally:
        conn.close()


def _master_catalog_counts(db_path):
    """Return a dict of row counts for the key catalog tables."""
    from lcsas.db.connection import get_connection

    conn = get_connection(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "repositories",
                "volumes",
                "packs",
                "volume_packs",
            )
        }
    finally:
        conn.close()


class TestCmdCatalogRebuild:
    def test_catalog_rebuild_happy_path(self, tmp_path, capsys):
        """rebuild from two disc dirs produces a master catalog with the union."""
        disc1 = tmp_path / "disc1"
        disc2 = tmp_path / "disc2"

        _build_fake_disc_for_rebuild(
            disc1,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        _build_fake_disc_for_rebuild(
            disc2,
            repo_id="work",
            repo_name="work",
            volume_label="VOL_002",
            volume_uuid="uuid-volume-2",
            pack_hashes=["cc" * 32, "dd" * 32],
        )

        out_db = tmp_path / "master.db"
        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert result == 0

        out = capsys.readouterr().out
        assert "Catalog rebuild complete" in out
        assert str(out_db) in out
        assert "Discs processed  : 2" in out

        counts = _master_catalog_counts(str(out_db))
        assert counts["repositories"] == 2  # family + work
        assert counts["volumes"] == 2       # VOL_001 + VOL_002
        assert counts["packs"] == 4         # 2 + 2 distinct shas
        assert counts["volume_packs"] == 4  # 2 links per volume

    def test_catalog_rebuild_idempotent(self, tmp_path, capsys):
        """Running rebuild twice over the same discs is a no-op on the 2nd pass."""
        disc1 = tmp_path / "disc1"
        disc2 = tmp_path / "disc2"
        _build_fake_disc_for_rebuild(
            disc1,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        _build_fake_disc_for_rebuild(
            disc2,
            repo_id="work",
            repo_name="work",
            volume_label="VOL_002",
            volume_uuid="uuid-volume-2",
            pack_hashes=["cc" * 32, "dd" * 32],
        )
        out_db = tmp_path / "master.db"

        # First pass
        first = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert first == 0
        first_counts = _master_catalog_counts(str(out_db))
        capsys.readouterr()  # clear

        # Second pass over the same inputs — natural-key conflicts hit
        # INSERT OR IGNORE, so the master catalog should be unchanged.
        second = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert second == 0
        second_counts = _master_catalog_counts(str(out_db))

        assert first_counts == second_counts
        # Counts come from the happy-path expectation.
        assert second_counts == {
            "repositories": 2,
            "volumes": 2,
            "packs": 4,
            "volume_packs": 4,
        }

        out = capsys.readouterr().out
        # On the second pass nothing new should be merged.
        assert "Repositories     : 0 new" in out
        assert "Volumes          : 0 new" in out
        assert "Packs            : 0 new" in out

    def test_catalog_rebuild_unknown_dir_errors(self, tmp_path, capsys):
        """A non-existent disc dir trips the sanity check and exits non-zero."""
        missing = tmp_path / "no-such-disc"
        assert not missing.exists()
        out_db = tmp_path / "master.db"

        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(missing),
        ])
        assert result != 0

        out = capsys.readouterr().out
        assert "Not a directory" in out
        assert str(missing) in out
        # The handler bails BEFORE creating the master DB.
        assert not out_db.exists()

    def test_catalog_rebuild_partial_failure(self, tmp_path, capsys):
        """One valid disc + one disc missing catalog.db -> non-zero, but the
        valid disc is still merged into the master catalog."""
        good = tmp_path / "good_disc"
        bad = tmp_path / "bad_disc"

        _build_fake_disc_for_rebuild(
            good,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        # `bad` exists as a directory (so it survives the is_dir() sanity
        # check) but contains no catalog.db — rebuild_catalog records an
        # error for it and continues.
        bad.mkdir()

        out_db = tmp_path / "master.db"
        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(good), str(bad),
        ])
        # rebuild_catalog appends to result.errors -> handler returns 1.
        assert result != 0

        out = capsys.readouterr().out
        # The good disc was still processed.
        assert "Discs processed  : 1" in out
        assert "Discs skipped    : 1" in out
        # The error mentioning the bad disc is reported.
        assert "No catalog.db" in out
        assert str(bad) in out

        # The good disc's contents made it into the master catalog.
        counts = _master_catalog_counts(str(out_db))
        assert counts["repositories"] == 1
        assert counts["volumes"] == 1
        assert counts["packs"] == 2
        assert counts["volume_packs"] == 2


def _seed_ghost_volume(db: str, *, label: str = "GHOST_VOL",
                       age_hours: int = 48) -> None:
    """Seed a stale never-burned STAGING volume with two linked packs."""
    from lcsas.db.connection import get_connection
    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    conn = get_connection(db)
    try:
        register_repo(conn, "r1", "R1", "/mnt/r1")
        p1 = register_pack(conn, sha256="a" * 64, size_bytes=100,
                           repo_id="r1")
        p2 = register_pack(conn, sha256="b" * 64, size_bytes=200,
                           repo_id="r1")
        vol = create_volume(
            conn, label=label, uuid=f"uuid-{label}",
            media_type="TEST_TINY", capacity_bytes=2_097_152,
            status="STAGING",
        )
        bulk_link_packs(conn, vol.volume_id, [p1.pack_id, p2.pack_id])
        conn.execute(
            "UPDATE volumes SET created_at = datetime('now', ?) "
            "WHERE volume_id = ?",
            (f"-{int(age_hours)} hours", vol.volume_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_catalog_reconcile_reports_and_fixes_ghosts(tmp_path, capsys):
    """`catalog reconcile` lists ghosts; --fix --yes deletes them and the
    packs return to the unarchived pool; a second run reports clean.
    [FMA-01]"""
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_unarchived_packs

    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    capsys.readouterr()

    # Report-only: ghost listed, nonzero exit (needs attention), no change.
    rc = main(["--db", str(db), "catalog", "reconcile"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "GHOST_VOL" in out
    assert "--fix" in out

    # Fix: ghost volume deleted, packs reclaimed.
    rc = main(["--db", str(db), "catalog", "reconcile", "--fix", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 pack(s) returned to the unarchived pool" in out

    conn = get_connection(str(db))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM volumes").fetchone()[0] == 0
        assert {p.sha256 for p in get_unarchived_packs(conn)} == {
            "a" * 64, "b" * 64,
        }
    finally:
        conn.close()

    # A second run reports clean.
    rc = main(["--db", str(db), "catalog", "reconcile"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no ghost volumes" in out


def test_catalog_reconcile_spares_fresh_and_burned_volumes(tmp_path, capsys):
    """Mid-pipeline (fresh) STAGING volumes and volumes with copies are
    never treated as ghosts.  [FMA-01]"""
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import create_volume

    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db), label="FRESH_STAGING", age_hours=0)
    conn = get_connection(str(db))
    try:
        ensure_location(conn, "Home_Shelf")
        vol = create_volume(
            conn, label="OLD_WITH_COPY", uuid="uuid-old-copy",
            media_type="TEST_TINY", capacity_bytes=2_097_152,
            status="STAGING",
        )
        add_volume_copy(conn, volume_id=vol.volume_id,
                        location="Home_Shelf")
        conn.execute(
            "UPDATE volumes SET created_at = datetime('now', '-48 hours') "
            "WHERE volume_id = ?", (vol.volume_id,),
        )
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()

    rc = main(["--db", str(db), "catalog", "reconcile"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no ghost volumes" in out


def _give_staged_iso(db: str, tmp_path, label: str = "GHOST_VOL") -> object:
    """Record a burn session for ``label`` whose ISO exists on disk."""
    from lcsas.db.connection import get_connection
    from lcsas.db.sessions import add_session_volume, create_session
    from lcsas.db.volumes import get_volume_by_label

    session_dir = tmp_path / "staging" / f"sess-{label}"
    session_dir.mkdir(parents=True)
    iso = session_dir / f"{label}.iso"
    iso.write_bytes(b"\x00" * 64)
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        create_session(conn, media_type="TEST_TINY",
                       staging_dir=str(session_dir), session_id=f"sess-{label}")
        add_session_volume(conn, f"sess-{label}", vol.volume_id, str(iso),
                           iso_sha256="c" * 64, iso_size_bytes=64)
    finally:
        conn.close()
    return iso


def test_catalog_reconcile_fix_spares_an_old_volume_whose_iso_exists(
    tmp_path, capsys,
):
    """#647: a 48 h old STAGING volume whose ISO exists (burned elsewhere,
    receipt not yet imported) survives a default --fix; --include-waiting
    deletes it."""
    from lcsas.db.connection import get_connection

    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    _give_staged_iso(str(db), tmp_path)
    capsys.readouterr()

    rc = main(["--db", str(db), "catalog", "reconcile"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "waiting to be burned" in out and "GHOST_VOL" in out
    assert "no ghost volumes" in out

    assert main(["--db", str(db), "catalog", "reconcile", "--fix", "--yes"]) == 0
    conn = get_connection(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 1
    finally:
        conn.close()
    capsys.readouterr()

    rc = main(["--db", str(db), "catalog", "reconcile", "--fix", "--yes",
               "--include-waiting"])
    assert rc == 0
    assert "Deleting ghost volume GHOST_VOL" in capsys.readouterr().out
    conn = get_connection(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 0
    finally:
        conn.close()


def test_catalog_reconcile_confirmation_mentions_receipts(tmp_path, capsys,
                                                          monkeypatch):
    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    capsys.readouterr()
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "no"

    monkeypatch.setattr("builtins.input", fake_input)
    assert main(["--db", str(db), "catalog", "reconcile", "--fix"]) == 1
    assert len(prompts) == 1
    assert "receipt" in prompts[0] and "can no longer be imported" in prompts[0]


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
{extra}
""")
    return cfg


def _import_locations(conn) -> None:
    """#669: a receipt must name a registered location."""
    from lcsas.db.locations import ensure_location
    for name in ("Home", "Offsite", "Vault", "Vault_A", "Vault_B"):
        ensure_location(conn, name)


class TestCmdCatalogImport:
    def test_import_receipts_from_json(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "uuid1", "TEST_TINY", 1000, "Home", "VERIFIED")
        _import_locations(conn)
        conn.close()

        receipt = tmp_path / "receipt.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_001",
            "volume_id": 1,
            "session_id": "s1",
            "location": "Offsite",
            "pack_count": 5,
            "verify_passed": True,
        }))

        result = main(["--config", str(cfg), "catalog", "import-receipts",
                        str(receipt)])
        assert result == 0

    def test_import_transitions_staging_to_verified(self, tmp_path, capsys):
        """Receipt with verify_passed=true promotes STAGING → VERIFIED and closes."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_100", "u1", "TEST_TINY", 1000, "Home", "STAGING")
        _import_locations(conn)
        # #669: VERIFIED needs the receipt's hash to match the staged ISO's.
        from lcsas.db.sessions import add_session_volume, create_session
        create_session(conn, "TEST_TINY", "/staging", session_id="s100")
        add_session_volume(conn, "s100", 1, "/staging/VOL_100.iso",
                           iso_sha256="e" * 64, iso_size_bytes=2048)
        conn.close()

        receipt = tmp_path / "r.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_100",
            "location": "Home",
            "verify_passed": True,
            "burn_date": "2026-04-14T12:00:00+00:00",
            "iso_sha256": "e" * 64,
        }))

        result = main(["--config", str(cfg), "catalog", "import-receipts",
                       str(receipt)])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_100")
        assert vol.status == "VERIFIED"
        assert vol.closed_at is not None
        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")
        assert len(events) == 1
        conn.close()

    def test_import_keeps_staging_on_verify_fail(self, tmp_path):
        """Receipt with verify_passed=false leaves the volume STAGING (#483,
        as burn_session does) and records the VERIFY_FAIL event."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_101", "u2", "TEST_TINY", 1000, "Home", "STAGING")
        _import_locations(conn)
        conn.close()

        receipt = tmp_path / "r.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_101",
            "location": "Home",
            "verify_passed": False,
        }))

        result = main(["--config", str(cfg), "catalog", "import-receipts",
                       str(receipt)])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_101")
        assert vol.status == "STAGING"
        assert vol.closed_at is None  # not closed on failure
        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_FAIL")
        assert len(events) == 1
        conn.close()

    @staticmethod
    def _import(tmp_path, status, receipt):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_514", "u514", "TEST_TINY", 1000, "Home", status)
        _import_locations(conn)
        conn.close()
        r = tmp_path / "r.json"
        r.write_text(json.dumps({"volume_label": "VOL_514", **receipt}))
        rc = main(["--config", str(cfg), "catalog", "import-receipts", str(r)])
        from lcsas.db.volume_copies import get_copies_for_volume
        from lcsas.db.volume_events import get_events_for_volume
        from lcsas.db.volumes import get_volume_by_label
        conn = get_connection(db)
        vol = get_volume_by_label(conn, "VOL_514")
        copies = [c.location for c in get_copies_for_volume(conn, vol.volume_id)]
        events = [e.event_type for e in get_events_for_volume(conn, vol.volume_id)]
        conn.close()
        return rc, vol, copies, events

    def test_import_failed_first_burn_records_no_copy(self, tmp_path, capsys):
        """#514 / BURN-05: a disc that failed verification is not a copy,
        and (#483) the volume stays STAGING so its packs stay unarchived."""
        rc, vol, copies, events = self._import(
            tmp_path, "STAGING", {"location": "Home", "verify_passed": False})
        assert rc == 0
        assert vol.status == "STAGING"
        assert copies == []
        assert "VERIFY_FAIL" in events
        out = capsys.readouterr()
        assert "FAILED verification" in out.out + out.err

    def test_import_failed_reburn_records_event_not_copy(self, tmp_path):
        rc, vol, copies, events = self._import(
            tmp_path, "VERIFIED", {"location": "Offsite", "verify_passed": False})
        assert rc == 0
        assert vol.status == "VERIFIED"
        assert "Offsite" not in copies
        assert "VERIFY_FAIL_REBURN" in events

    def test_import_unverified_burn_is_burned_not_verified(self, tmp_path):
        """#514: verify_passed null (burn-iso --no-verify) is recorded as a
        copy but never promotes the volume to VERIFIED."""
        rc, vol, copies, events = self._import(
            tmp_path, "STAGING", {"location": "Home", "verify_passed": None})
        assert rc == 0
        assert vol.status == "BURNED"
        assert vol.closed_at is None
        assert copies == ["Home"]
        assert "VERIFY_PASS" not in events and "VERIFY_FAIL" not in events

    def test_import_reburn_only_adds_copy(self, tmp_path):
        """Already-VERIFIED volume: no status change, just a new copy."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_102", "u3", "TEST_TINY", 1000, "Home", "VERIFIED")
        _import_locations(conn)
        conn.close()

        receipt = tmp_path / "r.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_102",
            "location": "Offsite",
            "verify_passed": True,
        }))

        result = main(["--config", str(cfg), "catalog", "import-receipts",
                       str(receipt)])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volume_copies import get_copies_for_volume
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_102")
        assert vol.status == "VERIFIED"
        copies = get_copies_for_volume(conn, vol.volume_id)
        assert any(c.location == "Offsite" for c in copies)
        conn.close()

    def test_import_persists_receipt_provenance(self, tmp_path):
        """Issue #18: imported receipts must persist iso_sha256, session_id,
        device, and pack_ids — currently silently dropped.
        """
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_PROV", "uuid-prov", "TEST_TINY", 1000,
                      "Vault_A", "STAGING")
        _import_locations(conn)
        conn.close()

        iso_hash = "a" * 64
        session_id = "sess-2026-04-14"
        device = "/dev/sr2"
        pack_ids = [11, 22, 33]

        receipt = tmp_path / "prov.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_PROV",
            "location": "Vault_A",
            "session_id": session_id,
            "device": device,
            "burn_date": "2026-04-14T13:00:00+00:00",
            "iso_sha256": iso_hash,
            "verify_passed": True,
            "pack_ids": pack_ids,
        }))

        # No staged ISO hash on record here (#669).
        result = main(["--config", str(cfg), "catalog", "import-receipts",
                       "--allow-unhashed", str(receipt)])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volume_events import get_events_for_volume
        from lcsas.db.volumes import get_volume_by_label

        vol = get_volume_by_label(conn, "VOL_PROV")
        events = get_events_for_volume(
            conn, vol.volume_id, "BURN_RECEIPT_IMPORTED"
        )
        assert len(events) == 1, (
            "expected exactly one BURN_RECEIPT_IMPORTED event row "
            "carrying the receipt provenance"
        )
        ev = events[0]
        # All four provenance fields must be recoverable from the event detail
        detail = json.loads(ev.detail)
        assert detail["iso_sha256"] == iso_hash
        assert detail["session_id"] == session_id
        assert detail["device"] == device
        assert detail["pack_ids"] == pack_ids
        conn.close()

    def test_import_rejects_hash_mismatch(self, tmp_path):
        """Issue #18: if a prior receipt persisted an iso_sha256 for a volume,
        a second receipt with a different iso_sha256 for the same volume must
        be rejected and must not write a spurious event row.
        """
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_HASH", "uuid-hash", "TEST_TINY", 1000,
                      "Vault_A", "STAGING")
        _import_locations(conn)
        conn.close()

        good_hash = "b" * 64
        bad_hash = "c" * 64

        r1 = tmp_path / "r1.json"
        r1.write_text(json.dumps({
            "volume_label": "VOL_HASH",
            "location": "Vault_A",
            "session_id": "s1",
            "device": "/dev/sr0",
            "burn_date": "2026-04-14T14:00:00+00:00",
            "iso_sha256": good_hash,
            "verify_passed": True,
            "pack_ids": [],
        }))
        # No staged ISO hash on record here (#669).
        rc1 = main(["--config", str(cfg), "catalog", "import-receipts",
                    "--allow-unhashed", str(r1)])
        assert rc1 == 0

        # Second receipt: same volume, *different* iso_sha256 — must reject.
        r2 = tmp_path / "r2.json"
        r2.write_text(json.dumps({
            "volume_label": "VOL_HASH",
            "location": "Vault_B",
            "session_id": "s1",
            "device": "/dev/sr1",
            "burn_date": "2026-04-14T15:00:00+00:00",
            "iso_sha256": bad_hash,
            "verify_passed": True,
            "pack_ids": [],
        }))
        rc2 = main(["--config", str(cfg), "catalog", "import-receipts",
                    str(r2)])
        assert rc2 != 0, "hash-mismatch receipt must cause non-zero exit"

        # And no second BURN_RECEIPT_IMPORTED event should have been written
        # for the rejected receipt.
        conn = get_connection(db)
        from lcsas.db.volume_events import get_events_for_volume
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_HASH")
        events = get_events_for_volume(
            conn, vol.volume_id, "BURN_RECEIPT_IMPORTED"
        )
        assert len(events) == 1, (
            "rejected receipt must not have written a second event row"
        )
        # The surviving event must reflect the ORIGINAL good hash.
        detail = json.loads(events[0].detail)
        assert detail["iso_sha256"] == good_hash
        conn.close()


def test_import_receipts_from_durable_dir(tmp_path):
    """`catalog import-receipts <db dir>/receipts/*.json` repopulates
    copy/location rows on a fresh hot DB from the durable receipts the
    burn pipeline wrote beside the catalog.  [BURN-08]"""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock

    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volumes import create_volume, get_volume_by_label
    from tests.unit.test_session_pipeline import (
        _make_config,
        _seed_packs,
        _wire_drive_identity,
        _zeros_device_reader,
    )

    # 1. Real burn pipeline against a file-backed hot DB writes the
    #    durable receipts into <db dir>/receipts/.
    config = _make_config(tmp_path, num_repos=2)
    conn = get_connection(config.db_path)
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=5, pack_size=50)

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_zeros_device_reader)
    result = orch.stage()
    orch.burn_session(result.session_id, "Offsite_Safe", skip_burn=True)
    conn.close()

    receipt_files = sorted(
        (config.db_path.parent / "receipts").glob("*.json")
    )
    assert receipt_files, "burn must leave durable receipts beside the DB"
    labels = [
        json.loads(rf.read_text())["volume_label"] for rf in receipt_files
    ]

    # 2. Disaster: the hot DB is lost and rebuilt — the volumes are
    #    known again (from the discs) but every copy/location is gone.
    config.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(config.db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    fresh = get_connection(config.db_path)
    create_all(fresh)
    for label in labels:
        create_volume(fresh, label, f"uuid-{label}", "TEST_TINY",
                      2_097_152, "Home_Shelf", "STAGING")
    # #669: the receipt's location must be a registered one.
    from lcsas.db.locations import ensure_location
    ensure_location(fresh, "Offsite_Safe")
    fresh.close()

    # 3. Replay the durable receipt directory through the CLI.
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'database = "{config.db_path}"\n'
        "\n"
        "[defaults]\n"
        'media_type = "TEST_TINY"\n'
        "metadata_reserve_mb = 0\n"
    )
    # #669: the rebuilt catalog has no staged ISO hash for these volumes,
    # and they were staged for Home_Shelf but burned for Offsite_Safe, so
    # the owner vouches for both.
    rc = main(["--config", str(cfg), "catalog", "import-receipts",
               "--allow-unhashed", "--allow-other-location",
               *[str(rf) for rf in receipt_files]])
    assert rc == 0

    # 4. Copy/location rows and statuses are back on the fresh DB.
    check = get_connection(config.db_path)
    try:
        for label in labels:
            vol = get_volume_by_label(check, label)
            assert vol.status == "VERIFIED"
            copies = get_copies_for_volume(check, vol.volume_id)
            assert [c.location for c in copies] == ["Offsite_Safe"]
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Error-path tests for ``lcsas catalog import-receipts`` (issue #517).
#
# ``docs/workflows/burn-iso-portable.md`` promises that a bad receipt is
# skipped with a warning and the rest of the batch still imports.  Nothing
# pinned that.  These tests cover:
#
# * malformed / unreadable receipt files (truncated, empty, garbage, missing,
#   a directory);
# * receipts missing ``volume_label`` / ``location``;
# * an orphan receipt (volume label not in the catalog);
# * mixed batches -- the bad receipt is skipped, the good one still imports,
#   whichever order they arrive in.
#
# They assert exit codes and, via a before/after dump of the catalog tables,
# that a skipped receipt writes nothing at all.  Since #669 every skipped or
# refused receipt makes the exit code non-zero, while the good receipts of
# the batch are still imported.  Nothing here depends on ``verify_passed``
# semantics: good receipts target an already-VERIFIED volume (the "re-burn to
# a new location" path), so only the copy row and the
# ``BURN_RECEIPT_IMPORTED`` event are asserted.
#
# The #666 / #669 / #670 / #671 cases are at the end of the file.
# ---------------------------------------------------------------------------


_TABLES = ("volumes", "volume_copies", "volume_events", "locations")


def _write_config_receipts(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{tmp_path / "archive.db"}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    return cfg


# #669: a receipt must name a registered location.
_LOCATIONS = ("Home", "Offsite", "Vault", "Vault_B", "Vault_C", "Bank_Box",
              "Home_Shelf")


def _make_catalog(tmp_path: Path, labels: dict[str, str],
                  staged_for: str = "Home") -> Path:
    from lcsas.db.locations import ensure_location

    db = tmp_path / "archive.db"
    conn = get_connection(db)
    create_all(conn)
    for name in _LOCATIONS:
        ensure_location(conn, name)
    for i, (label, status) in enumerate(labels.items()):
        create_volume(conn, label, f"uuid-{i}", "TEST_TINY", 1000, staged_for, status)
    conn.close()
    return db


def _dump(db: Path) -> dict[str, list[tuple]]:
    """Every row of the tables an import could touch, for before/after equality."""
    conn = get_connection(db)
    try:
        return {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()]
            for t in _TABLES
        }
    finally:
        conn.close()


def _receipt(tmp_path: Path, name: str, body: dict | str | bytes) -> Path:
    p = tmp_path / name
    if isinstance(body, bytes):
        p.write_bytes(body)
    elif isinstance(body, str):
        p.write_text(body)
    else:
        p.write_text(json.dumps(body))
    return p


def _good(label: str, location: str = "Offsite") -> dict:
    return {
        "volume_label": label,
        "location": location,
        "session_id": "s1",
        "device": "/dev/sr0",
        "burn_date": "2026-04-14T12:00:00+00:00",
        "iso_sha256": "a" * 64,
        "iso_size_bytes": 2048,
        "verify_passed": True,
    }


def _run(cfg: Path, *files: Path, flags: tuple[str, ...] = ()) -> int:
    return main(["--config", str(cfg), "catalog", "import-receipts",
                 *flags, *[str(f) for f in files]])


def _assert_imported(db: Path, label: str, location: str, expected_events: int = 1) -> None:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        copies = get_copies_for_volume(conn, vol.volume_id)
        assert [c.location for c in copies if c.location == location], (
            f"{label}: no copy recorded at {location}"
        )
        events = get_events_for_volume(conn, vol.volume_id, "BURN_RECEIPT_IMPORTED")
        assert len(events) == expected_events
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    """Config + catalog holding one VERIFIED volume, VOL_GOOD."""
    cfg = _write_config_receipts(tmp_path)
    db = _make_catalog(tmp_path, {"VOL_GOOD": "VERIFIED", "VOL_OTHER": "VERIFIED"})
    return tmp_path, cfg, db


_MALFORMED = {
    "truncated": '{"volume_label": "VOL_GOOD", "location": "Off',
    "empty-file": "",
    "not-json": "this is not a receipt",
    "trailing-comma": '{"volume_label": "VOL_GOOD", "location": "Offsite",}',
}


class TestMalformedReceiptFile:
    @pytest.mark.parametrize("kind", sorted(_MALFORMED))
    def test_malformed_json_is_skipped_with_warning(self, env, capsys, kind):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "bad.json", _MALFORMED[kind])
        before = _dump(db)

        rc = _run(cfg, bad)

        out = capsys.readouterr().out
        assert rc == 1  # #669: a skipped receipt is a non-zero exit
        assert "Failed to read receipt" in out
        assert str(bad) in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_nonexistent_file_is_skipped_with_warning(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, tmp_path / "never-written.json")

        out = capsys.readouterr().out
        assert rc == 1
        assert "Failed to read receipt" in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_a_directory_in_place_of_a_receipt_is_skipped(self, env, capsys):
        tmp_path, cfg, db = env
        (tmp_path / "adir").mkdir()
        before = _dump(db)

        rc = _run(cfg, tmp_path / "adir")

        assert rc == 1
        assert "Failed to read receipt" in capsys.readouterr().out
        assert _dump(db) == before

    @pytest.mark.parametrize(
        ("body", "message"),
        [("[]", "not a JSON object"), ('"just a string"', "not a JSON object"),
         ("{}", "missing keys")],
    )
    def test_json_that_is_not_a_receipt_object_is_skipped(
        self, env, capsys, body, message
    ):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "weird.json", body))

        out = capsys.readouterr().out
        assert rc == 1
        assert message in out
        assert _dump(db) == before

    @pytest.mark.parametrize(
        "raw",
        [b"\xff\xfe\x00\x01\x80garbage", '{"volume_label": "VOL_GOOD"}'.encode("utf-16")],
        ids=["binary-garbage", "utf16"],
    )
    def test_undecodable_file_is_skipped_and_later_receipts_still_import(
        self, env, capsys, raw
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "undecodable.json", raw)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    @pytest.mark.parametrize("body", ["null", "42", "true"])
    def test_scalar_json_is_skipped_and_later_receipts_still_import(
        self, env, capsys, body
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "scalar.json", body)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    @pytest.mark.parametrize("size", ["abc", -5, True, [2048], {"n": 1}, "2.5"])
    def test_invalid_size_is_skipped_and_writes_nothing(self, env, capsys, size):
        """#522: the bad receipt writes nothing -- not even its location row."""
        tmp_path, cfg, db = env
        before = _dump(db)
        bad = _receipt(tmp_path, "badsize.json",
                       {**_good("VOL_OTHER", "Vault_Q"), "iso_size_bytes": size})
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        assert _run(cfg, bad, good) == 1

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert "invalid iso_size_bytes" in out
        assert not [row for row in _dump(db)["locations"] if "Vault_Q" in row]
        assert _dump(db)["volume_copies"] != before["volume_copies"]
        _assert_imported(db, "VOL_GOOD", "Offsite")
        conn = get_connection(db)
        try:
            other = get_volume_by_label(conn, "VOL_OTHER")
            assert other is not None
            assert get_copies_for_volume(conn, other.volume_id) == []
        finally:
            conn.close()

    def test_digit_string_size_is_recorded_as_an_integer(self, env, capsys):
        tmp_path, cfg, db = env
        rc = _run(cfg, _receipt(tmp_path, "r.json",
                                {**_good("VOL_GOOD"), "iso_size_bytes": "4096"}))

        assert rc == 0
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_GOOD")
            assert vol is not None
            sizes = [c.iso_size_bytes for c in get_copies_for_volume(conn, vol.volume_id)]
        finally:
            conn.close()
        assert sizes == [4096]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("volume_label", 42), ("volume_label", ""), ("location", ["Offsite"]),
         ("location", "   "), ("location", None)],
    )
    def test_non_string_or_blank_label_or_location_is_skipped(
        self, env, capsys, field, value
    ):
        tmp_path, cfg, db = env
        before = _dump(db)
        bad = _receipt(tmp_path, "bad.json", {**_good("VOL_OTHER"), field: value})

        assert _run(cfg, bad) == 1

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert f"invalid {field}" in out
        assert _dump(db) == before


class TestMissingKeys:
    @pytest.mark.parametrize(
        ("body", "named"),
        [
            ({"location": "Offsite"}, ["volume_label"]),
            ({"volume_label": "VOL_GOOD"}, ["location"]),
            ({"session_id": "s1", "verify_passed": True}, ["volume_label", "location"]),
        ],
        ids=["no-volume_label", "no-location", "neither"],
    )
    def test_missing_required_key_is_skipped_and_named(self, env, capsys, body, named):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "incomplete.json", body))

        out = capsys.readouterr().out
        assert rc == 1
        assert "missing keys" in out
        for key in named:
            assert key in out
        # The key that IS present must not be reported as missing.
        present = {"volume_label", "location"} - set(named)
        line = next(ln for ln in out.splitlines() if "missing keys" in ln)
        for key in present:
            assert key not in line.split("missing keys:")[1]
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_optional_keys_are_not_required(self, env, capsys):
        """Only volume_label, location and (#669) verify_passed are needed;
        everything else defaults."""
        tmp_path, cfg, db = env
        minimal = _receipt(tmp_path, "min.json",
                           {"volume_label": "VOL_GOOD", "location": "Offsite",
                            "verify_passed": True})

        assert _run(cfg, minimal) == 0

        assert "Imported 1 receipt(s)." in capsys.readouterr().out
        _assert_imported(db, "VOL_GOOD", "Offsite")


class TestOrphanReceipt:
    def test_unknown_volume_label_is_skipped_and_writes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)
        orphan = _receipt(tmp_path, "orphan.json", _good("VOL_NOT_IN_CATALOG", "Vault_Z"))

        rc = _run(cfg, orphan)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Volume 'VOL_NOT_IN_CATALOG' not found" in out
        assert "Imported 0 receipt(s)." in out
        after = _dump(db)
        assert after == before
        # In particular the receipt's location was NOT created as a side effect.
        assert not [row for row in after["locations"] if "Vault_Z" in row]
        # ... and no volume was invented for the orphan label.
        conn = get_connection(db)
        try:
            assert get_volume_by_label(conn, "VOL_NOT_IN_CATALOG") is None
        finally:
            conn.close()

    def test_label_match_is_exact_not_prefix(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        # VOL_GOOD exists; VOL_GOO and VOL_GOOD2 do not.
        _run(cfg, _receipt(tmp_path, "a.json", _good("VOL_GOO")),
             _receipt(tmp_path, "b.json", _good("VOL_GOOD2")))

        out = capsys.readouterr().out
        assert out.count("not found, skipping") == 2
        assert _dump(db) == before


_BAD_FILES = {
    "malformed": lambda tp: _receipt(tp, "bad_malformed.json", '{"volume_label": '),
    "missing-keys": lambda tp: _receipt(tp, "bad_keys.json", {"volume_label": "VOL_OTHER"}),
    "orphan": lambda tp: _receipt(tp, "bad_orphan.json", _good("VOL_GHOST", "Nowhere")),
    "nonexistent": lambda tp: tp / "bad_missing_file.json",
}


class TestMixedBatch:
    @pytest.mark.parametrize("kind", sorted(_BAD_FILES))
    @pytest.mark.parametrize("bad_first", [True, False], ids=["bad-first", "bad-last"])
    def test_bad_receipt_is_skipped_good_one_still_imports(self, env, capsys, kind, bad_first):
        tmp_path, cfg, db = env
        bad = _BAD_FILES[kind](tmp_path)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD", "Offsite"))
        files = (bad, good) if bad_first else (good, bad)

        rc = _run(cfg, *files)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_GOOD", "Offsite")
        # The bad receipt left no trace: no event on the volume it named, and
        # nothing for the other seeded volume either.
        conn = get_connection(db)
        try:
            other = get_volume_by_label(conn, "VOL_OTHER")
            assert get_copies_for_volume(conn, other.volume_id) == []
            assert get_events_for_volume(conn, other.volume_id, "BURN_RECEIPT_IMPORTED") == []
            locs = {row[0] for row in conn.execute("SELECT name FROM locations").fetchall()}
            assert "Nowhere" not in locs
        finally:
            conn.close()

    def test_bad_receipts_sandwiching_two_good_ones(self, env, capsys):
        tmp_path, cfg, db = env
        files = [
            _BAD_FILES["malformed"](tmp_path),
            _receipt(tmp_path, "g1.json", _good("VOL_GOOD", "Offsite")),
            _BAD_FILES["orphan"](tmp_path),
            _receipt(tmp_path, "g2.json", _good("VOL_OTHER", "Vault")),
            _BAD_FILES["missing-keys"](tmp_path),
        ]

        rc = _run(cfg, *files)

        assert rc == 1
        assert "Imported 2 receipt(s)." in capsys.readouterr().out
        _assert_imported(db, "VOL_GOOD", "Offsite")
        _assert_imported(db, "VOL_OTHER", "Vault")

    def test_all_bad_batch_exits_nonzero_and_changes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, *[make(tmp_path) for make in _BAD_FILES.values()])

        out = capsys.readouterr().out
        assert rc == 1  # #669: any refused receipt is a non-zero exit
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_hash_mismatch_exits_nonzero_but_does_not_stop_the_batch(self, env, capsys):
        """A rejected receipt flips the exit code, yet the good receipt after
        it is still committed and the rejected one wrote nothing."""
        tmp_path, cfg, db = env
        first = _receipt(tmp_path, "first.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, first) == 0
        capsys.readouterr()

        tampered = dict(_good("VOL_GOOD", "Vault_B"), iso_sha256="b" * 64)
        rejected = _receipt(tmp_path, "tampered.json", tampered)
        fine = _receipt(tmp_path, "fine.json", _good("VOL_OTHER", "Vault_C"))

        rc = _run(cfg, rejected, fine)

        out = capsys.readouterr().out
        assert rc == 1
        assert "does not match previously recorded hash" in out
        assert "Rejected 1 receipt(s)" in out
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_OTHER", "Vault_C")
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_GOOD")
            # Still exactly the one event from the first, honest import.
            assert len(get_events_for_volume(conn, vol.volume_id, "BURN_RECEIPT_IMPORTED")) == 1
            assert [c.location for c in get_copies_for_volume(conn, vol.volume_id)] == ["Offsite"]
        finally:
            conn.close()


class TestReceiptLastVerified:
    def _last_verified(self, db: Path, label: str, location: str) -> str | None:
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, label)
            assert vol is not None
            (copy,) = [c for c in get_copies_for_volume(conn, vol.volume_id)
                       if c.location == location]
            return copy.last_verified_at
        finally:
            conn.close()

    def test_verified_receipt_sets_last_verified_and_status_is_quiet(
        self, env, capsys,
    ):
        from datetime import UTC, datetime

        from lcsas.db.volume_copies import find_stale_copies

        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"),
                    burn_date=datetime.now(UTC).isoformat())
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") == body["burn_date"]
        conn = get_connection(db)
        try:
            assert find_stale_copies(conn) == []
        finally:
            conn.close()
        capsys.readouterr()
        assert main(["--config", str(cfg), "status"]) == 0
        assert "not verified" not in capsys.readouterr().out

    def test_unverified_receipt_leaves_it_null(self, env):
        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"), verify_passed=None)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is None

    def test_reburn_without_verify_clears_the_old_verification(self, env):
        """The row now describes a new, unread disc.  (#666: the re-burn has
        a later burn date; the same burn date and hash would be the same
        receipt imported again.)"""
        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "a.json", _good("VOL_GOOD", "Bank_Box"))) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is not None
        again = dict(_good("VOL_GOOD", "Bank_Box"), verify_passed=None,
                     burn_date="2026-05-01T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "b.json", again)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is None

    @pytest.mark.parametrize("burn_date, expected", [
        ("2026-04-14T14:00:00+02:00", "2026-04-14T12:00:00+00:00"),
        ("2026-04-14T12:00:00", "2026-04-14T12:00:00+00:00"),
    ])
    def test_burn_date_is_normalised_to_utc(self, env, burn_date, expected):
        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"), burn_date=burn_date)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") == expected

    @pytest.mark.parametrize("burn_date", ["", "not a date", "2999-01-01T00:00:00+00:00"])
    def test_missing_bad_or_future_burn_date_uses_the_import_time(
        self, env, burn_date,
    ):
        from datetime import UTC, datetime

        tmp_path, cfg, db = env
        before = datetime.now(UTC).isoformat()
        body = dict(_good("VOL_GOOD", "Bank_Box"), burn_date=burn_date)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        got = self._last_verified(db, "VOL_GOOD", "Bank_Box")
        assert got is not None and before <= got <= datetime.now(UTC).isoformat()


def _stage_record(db: Path, label: str, sha: str | None, size: int | None,
                  session_id: str = "sess-1") -> None:
    """Record a burn session for ``label`` as `lcsas stage` would."""
    from lcsas.db.sessions import add_session_volume, create_session

    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        create_session(conn, "TEST_TINY", "/staging", session_id=session_id)
        add_session_volume(conn, session_id, vol.volume_id, f"/staging/{label}.iso",
                           iso_sha256=sha, iso_size_bytes=size)
    finally:
        conn.close()


@pytest.fixture
def staged_env(tmp_path):
    """One STAGING volume whose ISO was recorded as ("a"*64, 2048 bytes)."""
    cfg = _write_config_receipts(tmp_path)
    db = _make_catalog(tmp_path, {"VOL_NEW": "STAGING"}, staged_for="Bank_Box")
    _stage_record(db, "VOL_NEW", "a" * 64, 2048)
    return tmp_path, cfg, db


def _status(db: Path, label: str) -> str:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        return vol.status
    finally:
        conn.close()


class TestStagedIsoCheck:
    def test_matching_hash_imports_and_verifies(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "SKIPPED" not in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_first_receipt_with_a_different_hash_is_rejected(self, staged_env, capsys):
        """The case #644 is about: no earlier receipt to compare with."""
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="c" * 64)
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad))
        out = capsys.readouterr().out
        assert rc == 1
        assert "does not match the ISO staged" in out
        assert "c" * 64 in out and "a" * 64 in out
        assert "Rejected 1 receipt(s)" in out
        assert _dump(db) == before
        assert _status(db, "VOL_NEW") == "STAGING"

    def test_different_size_is_rejected(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_size_bytes=4096)
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad))
        out = capsys.readouterr().out
        assert rc == 1
        assert "iso_size_bytes 4096" in out
        assert _dump(db) == before

    def test_mismatch_does_not_stop_the_batch(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="c" * 64)
        good = _good("VOL_NEW", "Bank_Box")
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad),
                  _receipt(tmp_path, "good.json", good))
        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")

    def test_latest_session_wins(self, staged_env, capsys):
        """A volume re-mastered in a later session is checked against the newer ISO."""
        tmp_path, cfg, db = staged_env
        conn = get_connection(db)
        try:
            conn.execute("UPDATE burn_sessions SET created_at = '2000-01-01T00:00:00'")
            conn.commit()
        finally:
            conn.close()
        _stage_record(db, "VOL_NEW", "d" * 64, 2048, session_id="sess-2")
        rc = _run(cfg, _receipt(tmp_path, "old.json", _good("VOL_NEW", "Bank_Box")))
        assert rc == 1
        assert "does not match the ISO staged" in capsys.readouterr().out
        newer = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="d" * 64)
        assert _run(cfg, _receipt(tmp_path, "new.json", newer)) == 0

    def test_receipt_without_hash_warns_that_the_check_was_skipped(
        self, staged_env, capsys,
    ):
        tmp_path, cfg, db = staged_env
        body = _good("VOL_NEW", "Bank_Box")
        del body["iso_sha256"]
        nohash = _receipt(tmp_path, "nohash.json", body)
        # #669: without a hash the volume does not become VERIFIED ...
        before = _dump(db)
        assert _run(cfg, nohash) == 1
        out = capsys.readouterr().out
        assert "carries no iso_sha256" in out and "--allow-unhashed" in out
        assert _dump(db) == before
        # ... unless the owner says so.
        rc = _run(cfg, nohash, flags=("--allow-unhashed",))
        out = capsys.readouterr().out
        assert rc == 0
        assert "carries no iso_sha256" in out and "SKIPPED" in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_volume_without_staged_hash_warns_that_the_check_was_skipped(
        self, env, capsys,
    ):
        """env's volumes have no burn session at all (e.g. an older catalog)."""
        tmp_path, cfg, db = env
        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Bank_Box")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "no ISO hash was recorded" in out and "SKIPPED" in out
        _assert_imported(db, "VOL_GOOD", "Bank_Box")

    def test_empty_staged_hash_counts_as_none(self, tmp_path, capsys):
        """session_volumes rows start with iso_sha256='' before the ISO exists."""
        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": "STAGING"}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "", None)
        r = _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box"))
        # #665: a never-burned volume with no ISO hash is a stage that did
        # not finish; its receipt needs --allow-incomplete-staging too.
        assert _run(cfg, r) == 1
        assert "did not finish" in capsys.readouterr().out
        assert _run(cfg, r, flags=("--allow-unhashed",
                                   "--allow-incomplete-staging")) == 0
        assert "no ISO hash was recorded" in capsys.readouterr().out


def _copy(db: Path, label: str, location: str):
    from lcsas.db.volume_copies import get_copy_at_location

    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        return get_copy_at_location(conn, vol.volume_id, location)
    finally:
        conn.close()


class TestReimport:
    def test_same_receipt_twice_changes_nothing_the_second_time(self, env, capsys):
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        # Something the owner wrote on the disc record since.
        conn = get_connection(db)
        try:
            conn.execute("UPDATE volume_copies SET notes = 'shelf 3'")
            conn.commit()
        finally:
            conn.close()
        capsys.readouterr()
        before = _dump(db)

        assert _run(cfg, r) == 0

        out = capsys.readouterr().out
        assert "already imported" in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before  # last_verified_at and notes untouched
        assert _copy(db, "VOL_GOOD", "Offsite").notes == "shelf 3"

    def test_receipt_imported_before_burn_date_was_recorded_is_seen(self, env, capsys):
        """A receipt imported before #666 has no burn_date in its event; the
        disc record's burn date stands in."""
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT event_id, detail FROM volume_events "
                "WHERE event_type = 'BURN_RECEIPT_IMPORTED'"
            ).fetchone()
            detail = json.loads(row["detail"])
            del detail["burn_date"]
            conn.execute("UPDATE volume_events SET detail = ? WHERE event_id = ?",
                         (json.dumps(detail), row["event_id"]))
            conn.commit()
        finally:
            conn.close()
        capsys.readouterr()
        before = _dump(db)
        assert _run(cfg, r) == 0
        assert "already imported" in capsys.readouterr().out
        assert _dump(db) == before

    def test_lost_disc_stays_destroyed_when_its_receipt_is_imported_again(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        capsys.readouterr()
        before = _dump(db)

        assert _run(cfg, r) == 0

        out = capsys.readouterr().out
        assert "already imported" in out and "stays so" in out
        assert _dump(db) == before
        assert _copy(db, "VOL_GOOD", "Offsite").status == "DESTROYED"

    def test_other_receipt_not_newer_than_the_loss_is_refused(self, env, capsys):
        """A receipt that was never imported, for a disc burned before the
        loss was recorded, cannot bring the lost disc back either."""
        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        capsys.readouterr()
        before = _dump(db)
        older = dict(_good("VOL_GOOD", "Offsite"),
                     burn_date="2026-04-15T12:00:00+00:00")

        assert _run(cfg, _receipt(tmp_path, "older.json", older)) == 1

        out = capsys.readouterr().out
        assert "is not later" in out and "It stays DESTROYED" in out
        assert _dump(db) == before

    def test_reburn_after_the_loss_puts_a_disc_back_in_service(self, env, capsys):
        from datetime import UTC, datetime

        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        reburn = dict(_good("VOL_GOOD", "Offsite"),
                      burn_date=datetime.now(UTC).isoformat())

        assert _run(cfg, _receipt(tmp_path, "reburn.json", reburn)) == 0

        copy = _copy(db, "VOL_GOOD", "Offsite")
        assert copy.status == "ACTIVE"
        assert copy.burn_date == reburn["burn_date"]


class TestStrictReceipt:
    @pytest.mark.parametrize("value", ["false", "true", "", 0, 1, [], {}])
    def test_non_boolean_verify_passed_is_refused(self, staged_env, capsys, value):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=value)

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 1

        out = capsys.readouterr().out
        assert "verify_passed must be JSON true, false or null" in out
        assert _dump(db) == before
        assert _status(db, "VOL_NEW") == "STAGING"

    def test_missing_verify_passed_is_refused(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        body = _good("VOL_NEW", "Bank_Box")
        del body["verify_passed"]

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 1

        assert "got nothing" in capsys.readouterr().out
        assert _dump(db) == before

    def test_unknown_location_is_refused_and_known_ones_are_listed(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Ofsite")))

        out = capsys.readouterr().out
        assert rc == 1
        assert "unknown location 'Ofsite'" in out
        assert "Did you mean: Offsite?" in out
        assert "Known locations: " + ", ".join(sorted(_LOCATIONS)) in out
        assert "lcsas location add Ofsite" in out
        assert _dump(db) == before  # no location was created

    def test_refused_receipt_exits_nonzero_but_valid_ones_still_import(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "bad.json",
                       dict(_good("VOL_OTHER", "Vault"), verify_passed="false"))
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD", "Offsite"))

        rc = _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out and "Rejected 1 receipt(s)" in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    def test_location_other_than_staged_for_is_refused_unless_allowed(
        self, staged_env, capsys,
    ):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        r = _receipt(tmp_path, "r.json", _good("VOL_NEW", "Vault"))

        assert _run(cfg, r) == 1

        out = capsys.readouterr().out
        assert "was staged for location Bank_Box" in out
        assert "--allow-other-location" in out
        assert _dump(db) == before

        assert _run(cfg, r, flags=("--allow-other-location",)) == 0
        _assert_imported(db, "VOL_NEW", "Vault")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_volume_without_a_staged_for_location_is_not_checked(self, tmp_path, capsys):
        """Older volumes may have no volumes.location: nothing to compare."""
        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_OLD": "STAGING"}, staged_for="")
        _stage_record(db, "VOL_OLD", "a" * 64, 2048)
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_OLD", "Vault"))) == 0
        assert _status(db, "VOL_OLD") == "VERIFIED"


class TestFirstDiscStates:
    @pytest.mark.parametrize("start", ["STAGING", "BURNING", "BURNED"])
    def test_passing_receipt_verifies_and_closes_the_volume(self, tmp_path, start):
        from lcsas.db.packs import register_pack
        from lcsas.db.queries import get_unarchived_packs
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": start}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        conn = get_connection(db)
        try:
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        finally:
            conn.close()

        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box"))) == 0

        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_NEW")
            assert vol.status == "VERIFIED"
            assert vol.closed_at is not None
            assert len(get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")) == 1
            # `lcsas stage` does not select its packs again.
            assert pack.pack_id not in {p.pack_id for p in get_unarchived_packs(conn)}
        finally:
            conn.close()

    @pytest.mark.parametrize("start", ["BURNING", "BURNED"])
    def test_failed_receipt_without_a_disc_returns_the_volume_to_staging(
        self, tmp_path, start,
    ):
        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": start}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0

        assert _status(db, "VOL_NEW") == "STAGING"
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_NEW")
            assert len(get_events_for_volume(conn, vol.volume_id, "VERIFY_FAIL")) == 1
        finally:
            conn.close()

    def test_unverified_receipt_for_a_burned_volume_keeps_it_burned(self, tmp_path):
        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": "BURNED"}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=None)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert _status(db, "VOL_NEW") == "BURNED"


class TestFailedBurnNextStep:
    def test_fail_status_reburn_pass_status(self, staged_env, capsys):
        from lcsas.db.packs import register_pack
        from lcsas.db.queries import get_unarchived_or_missing_at_location
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        tmp_path, cfg, db = staged_env
        # The staged ISO is still in the staging area.
        iso = tmp_path / "VOL_NEW.iso"
        iso.write_bytes(b"\0" * 2048)
        conn = get_connection(db)
        try:
            conn.execute("UPDATE session_volumes SET iso_path = ?", (str(iso),))
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
            conn.commit()
        finally:
            conn.close()
        next_step = "lcsas stage --for-location Bank_Box"

        # 1. The laptop burn failed its verify.
        failed = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        out = capsys.readouterr().out
        assert "FAILED verification" in out and next_step in out

        # 2. status lists it under needs re-burn, with the same next step,
        #    and not as waiting to be burned.
        assert main(["--config", str(cfg), "status"]) == 0
        out = capsys.readouterr().out
        assert "VOL_NEW needs re-burn at Bank_Box" in out
        assert next_step in out
        assert "waiting to be burned" not in out
        assert "catalog reconcile" not in out  # no second, different step

        # ... and `stage --for-location` would stage its packs again.
        conn = get_connection(db)
        try:
            missing = get_unarchived_or_missing_at_location(conn, "Bank_Box")
            assert pack.pack_id in {p.pack_id for p in missing}
        finally:
            conn.close()

        # 3. The re-burn passes.
        passed = dict(_good("VOL_NEW", "Bank_Box"),
                      burn_date="2026-04-15T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "pass.json", passed)) == 0
        assert _status(db, "VOL_NEW") == "VERIFIED"
        capsys.readouterr()

        # 4. status no longer lists it.
        assert main(["--config", str(cfg), "status"]) == 0
        assert "needs re-burn" not in capsys.readouterr().out

    def test_doing_the_named_step_clears_the_warning(self, staged_env, capsys):
        """After `stage --for-location` put the packs on a new volume, the
        failed one is no longer listed: the location needs nothing from it."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        tmp_path, cfg, db = staged_env
        conn = get_connection(db)
        try:
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        finally:
            conn.close()
        failed = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        capsys.readouterr()
        assert main(["--config", str(cfg), "status"]) == 0
        assert "VOL_NEW needs re-burn at Bank_Box" in capsys.readouterr().out

        # What `lcsas stage --for-location Bank_Box` does: a new volume,
        # staged for Bank_Box, holding the same pack.
        conn = get_connection(db)
        try:
            new = create_volume(conn, "VOL_NEXT", "uuid-next", "TEST_TINY", 1000,
                                "Bank_Box", "STAGING")
            bulk_link_packs(conn, new.volume_id, [pack.pack_id])
        finally:
            conn.close()

        assert main(["--config", str(cfg), "status"]) == 0
        assert "needs re-burn" not in capsys.readouterr().out

    def test_pass_at_another_location_does_not_clear_a_failure(self, env, capsys):
        """The newest verify result counts location by location."""
        tmp_path, cfg, db = env
        failed = dict(_good("VOL_GOOD", "Vault"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        passed = dict(_good("VOL_GOOD", "Offsite"),
                      burn_date="2026-04-15T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "pass.json", passed)) == 0
        capsys.readouterr()

        assert main(["--config", str(cfg), "status"]) == 0

        out = capsys.readouterr().out
        assert "VOL_GOOD needs re-burn at Vault" in out
        assert "needs re-burn at Offsite" not in out


class TestIncompleteStaging:
    """#665: a volume whose staging never finished (no ISO hash recorded)
    may have been burned from an ISO without RS03 data; its receipt is
    refused unless --allow-incomplete-staging."""

    @pytest.fixture
    def staged(self, tmp_path):
        from lcsas.db.sessions import add_session_volume, create_session

        cfg = _write_config_receipts(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_S": "STAGING"}, staged_for="Offsite")
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_S")
            assert vol is not None
            create_session(conn, media_type="TEST_TINY",
                           staging_dir=str(tmp_path / "staging" / "s1"),
                           session_id="s1")
            # The stage was cut short: the session row has no ISO hash.
            add_session_volume(conn, "s1", vol.volume_id,
                               str(tmp_path / "staging" / "s1" / "VOL_S.iso"),
                               iso_sha256="")
        finally:
            conn.close()
        return tmp_path, cfg, db

    def test_receipt_is_refused(self, staged, capsys):
        tmp_path, cfg, db = staged
        before = _dump(db)
        r = _receipt(tmp_path, "r.json", _good("VOL_S", "Offsite"))
        assert _run(cfg, r) == 1
        cap = capsys.readouterr()
        out = cap.out + cap.err
        assert "did not finish" in out
        assert "--allow-incomplete-staging" in out
        assert _dump(db) == before

    def test_override_imports(self, staged):
        tmp_path, cfg, db = staged
        r = _receipt(tmp_path, "r.json", _good("VOL_S", "Offsite"))
        assert _run(cfg, r, flags=("--allow-incomplete-staging",
                                   "--allow-unhashed")) == 0
        _assert_imported(db, "VOL_S", "Offsite")
