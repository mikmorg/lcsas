"""Tests for the ``lcsas session`` command (``lcsas.cli.commands.session``)."""

from __future__ import annotations

from lcsas.cli.main import main


def _write_session_config(tmp_path) -> tuple[str, str]:
    """Create a minimal TOML config + initialized DB and return (cfg, db) paths."""
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
""")
    # Initialize the catalog DB so list_sessions has tables to query.
    main(["init", "--db-path", str(db)])
    return str(cfg), str(db)


class TestCmdSessionList:
    def test_session_list_empty_db(self, tmp_path, capsys):
        """Fresh DB with no sessions: handler returns 0 and prints a friendly
        'no sessions' message."""
        cfg, _db = _write_session_config(tmp_path)
        capsys.readouterr()  # clear init output

        result = main(["--config", cfg, "session", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No sessions found" in out

    def test_session_list_shows_sessions(self, tmp_path, capsys):
        """Two sessions in different statuses both appear with their IDs and statuses."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session, update_session_status

        cfg, db = _write_session_config(tmp_path)
        conn = get_connection(db)
        try:
            s1 = create_session(conn, media_type="TEST_TINY",
                                staging_dir="/tmp/s1", session_id="sess-staged-001")
            s2 = create_session(conn, media_type="TEST_TINY",
                                staging_dir="/tmp/s2", session_id="sess-complete-001")
            # s1 stays at the default STAGED status; advance s2 to COMPLETE
            # (schema CHECK constrains status to STAGED/PARTIAL/COMPLETE/CLEANED,
            # so 'COMPLETE' is the closest valid analogue of the issue's "BURNED").
            update_session_status(conn, s2.session_id, "COMPLETE")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list"])
        assert result == 0
        out = capsys.readouterr().out
        # Both session IDs are present in the listing.
        assert s1.session_id in out
        assert s2.session_id in out
        # Both statuses are visible.
        assert "STAGED" in out
        assert "COMPLETE" in out

    def test_session_list_status_filter(self, tmp_path, capsys):
        """--status STAGED shows only the STAGED session."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session, update_session_status

        cfg, db = _write_session_config(tmp_path)
        conn = get_connection(db)
        try:
            staged = create_session(conn, media_type="TEST_TINY",
                                    staging_dir="/tmp/staged",
                                    session_id="sess-staged-only")
            other = create_session(conn, media_type="TEST_TINY",
                                   staging_dir="/tmp/other",
                                   session_id="sess-complete-only")
            update_session_status(conn, other.session_id, "COMPLETE")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list", "--status", "STAGED"])
        assert result == 0
        out = capsys.readouterr().out
        assert staged.session_id in out
        assert other.session_id not in out

    def test_session_list_invalid_status_errors(self, tmp_path, capsys):
        """Passing a bogus --status value: argparse has no `choices=`, so the
        handler accepts the filter, the DB returns no rows, and the handler
        prints a 'No sessions found ...' notice (with the bad status echoed)
        and exits 0. This documents the current handler contract."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session

        cfg, db = _write_session_config(tmp_path)
        # Seed a real session so the empty result is unambiguously caused by
        # the filter, not by an empty table.
        conn = get_connection(db)
        try:
            create_session(conn, media_type="TEST_TINY",
                           staging_dir="/tmp/real", session_id="sess-real-001")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list", "--status", "BOGUS"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No sessions found" in out
        assert "BOGUS" in out
        # The real session must NOT be listed because the filter excluded it.
        assert "sess-real-001" not in out

    def test_session_list_prints_volume_labels(self, tmp_path, capsys):
        """#748: the volumes line names each volume by its label (as the
        owner and heir know it), not by its catalog volume id."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volumes import create_volume

        cfg, db = _write_session_config(tmp_path)
        conn = get_connection(db)
        try:
            create_session(conn, media_type="TEST_TINY",
                           staging_dir="/tmp/labels",
                           session_id="sess-labels-001")
            for label in ("FAMILY_BD25_2026_0007", "FAMILY_BD25_2026_0008"):
                vol = create_volume(conn, label=label, uuid=f"uuid-{label}",
                                    media_type="TEST_TINY",
                                    capacity_bytes=2_097_152)
                add_session_volume(conn, "sess-labels-001", vol.volume_id,
                                   f"/tmp/labels/{label}.iso")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        assert main(["--config", cfg, "session", "list"]) == 0
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if "volumes(2):" in ln)
        assert line.split("volumes(2):")[1].strip() == (
            "FAMILY_BD25_2026_0007, FAMILY_BD25_2026_0008"
        )


class TestCmdSessionAbort:
    """`lcsas session abort` — reclaim packs from a never-burned session.
    [BURN-03]"""

    def _seed_unburned_session(self, tmp_path, db):
        """Seed a STAGING volume + session with linked packs and on-disk
        artifacts, mimicking a staged-but-never-burned session."""
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume

        session_dir = tmp_path / "staging" / "sess-abort-001"
        session_dir.mkdir(parents=True)
        iso_path = session_dir / "TEST_VOL.iso"
        iso_path.write_bytes(b"\x00" * 64)

        conn = get_connection(db)
        try:
            register_repo(conn, "r1", "R1", "/mnt/r1")
            p1 = register_pack(conn, sha256="a" * 64, size_bytes=100,
                               repo_id="r1")
            p2 = register_pack(conn, sha256="b" * 64, size_bytes=200,
                               repo_id="r1")
            vol = create_volume(
                conn, label="TEST_VOL", uuid="uuid-abort-1",
                media_type="TEST_TINY", capacity_bytes=2_097_152,
                status="STAGING",
            )
            bulk_link_packs(conn, vol.volume_id, [p1.pack_id, p2.pack_id])
            create_session(conn, media_type="TEST_TINY",
                           staging_dir=str(session_dir),
                           session_id="sess-abort-001")
            add_session_volume(conn, "sess-abort-001", vol.volume_id,
                               str(iso_path), iso_sha256="")
        finally:
            conn.close()
        return session_dir, iso_path

    def test_session_abort_cli(self, tmp_path, capsys):
        """Wire-level: abort reclaims the packs and prints the summary."""
        from lcsas.db.connection import get_connection
        from lcsas.db.queries import get_unarchived_packs
        from lcsas.db.sessions import get_session

        cfg, db = _write_session_config(tmp_path)
        session_dir, iso_path = self._seed_unburned_session(tmp_path, db)
        capsys.readouterr()  # clear init output

        result = main(["--config", cfg, "session", "abort", "sess-abort-001"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Aborted session sess-abort-001" in out
        assert "1 volume(s)" in out
        assert "2 pack(s)" in out
        assert "300 bytes" in out

        # Catalog: volume gone, packs back in the unarchived pool,
        # session CLEANED.
        conn = get_connection(db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM volumes").fetchone()[0] == 0
            unarchived = {p.sha256 for p in get_unarchived_packs(conn)}
            assert unarchived == {"a" * 64, "b" * 64}
            assert get_session(conn, "sess-abort-001").status == "CLEANED"
        finally:
            conn.close()

        # Disk: ISO and staging tree removed.
        assert not iso_path.exists()
        assert not session_dir.exists()

    def test_stage_clean_unburned_refuses_then_force(self, tmp_path, capsys):
        """`stage --clean` refuses on an unburned session; --force reclaims."""
        from lcsas.db.connection import get_connection
        from lcsas.db.queries import get_unarchived_packs

        cfg, db = _write_session_config(tmp_path)
        self._seed_unburned_session(tmp_path, db)
        capsys.readouterr()

        rc = main(["--config", cfg, "stage", "--clean",
                   "--session", "sess-abort-001"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "never burned" in out
        assert "session abort" in out

        rc = main(["--config", cfg, "stage", "--clean", "--force",
                   "--session", "sess-abort-001"])
        assert rc == 0

        conn = get_connection(db)
        try:
            assert len(get_unarchived_packs(conn)) == 2
        finally:
            conn.close()
