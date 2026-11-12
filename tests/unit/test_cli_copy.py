"""Tests for the ``lcsas copy`` command (``lcsas.cli.commands.copy``)."""

from __future__ import annotations

from lcsas.cli.main import main


def _setup_copy_db(db_path, *, locations=("Home_Shelf",)):
    """Create a catalog with one VERIFIED volume and one ACTIVE copy
    per location in *locations*."""
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import create_location
    from lcsas.db.schema import create_all
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import create_volume
    from lcsas.utils.labels import generate_uuid

    conn = get_connection(str(db_path))
    try:
        create_all(conn)
        vol = create_volume(
            conn, label="VOL_GONE", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
            status="VERIFIED",
        )
        for loc in locations:
            create_location(conn, loc)
            add_volume_copy(conn, vol.volume_id, loc)
        return vol.volume_id
    finally:
        conn.close()


class TestCmdCopy:
    def _volume_status(self, db_path, volume_id):
        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_id

        conn = get_connection(str(db_path))
        try:
            return get_volume_by_id(conn, volume_id).status
        finally:
            conn.close()

    def test_copy_destroy_cli(self, tmp_path, capsys):
        """Destroying the last disc demotes the volume, calls it lost and
        prints the re-burn hint (BURN-10, Q33 wire-level)."""
        db = tmp_path / "test.db"
        vol_id = _setup_copy_db(db)

        result = main(["--db", str(db), "copy", "destroy",
                       "VOL_GONE", "Home_Shelf"])
        assert result == 0
        out = capsys.readouterr().out
        assert "DESTROYED" in out
        assert "VOL_GONE is now a lost volume" in out
        assert "stage --for-location Home_Shelf" in out
        assert self._volume_status(db, vol_id) == "DEPRECATED"

    def test_copy_destroy_with_surviving_copy_prints_no_hint(self, tmp_path, capsys):
        """A surviving ACTIVE copy elsewhere → no demotion, no hint."""
        db = tmp_path / "test.db"
        vol_id = _setup_copy_db(db, locations=("Home_Shelf", "Offsite_Safe"))

        result = main(["--db", str(db), "copy", "destroy",
                       "VOL_GONE", "Home_Shelf"])
        assert result == 0
        out = capsys.readouterr().out
        assert "DESTROYED" in out
        assert "now a lost volume" not in out
        assert self._volume_status(db, vol_id) == "VERIFIED"

    def test_copy_deprecate_cli(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        vol_id = _setup_copy_db(db)

        result = main(["--db", str(db), "copy", "deprecate",
                       "VOL_GONE", "Home_Shelf"])
        assert result == 0
        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        assert "VOL_GONE is now a lost volume" in out
        assert self._volume_status(db, vol_id) == "DEPRECATED"

    def test_copy_destroy_unknown_volume_errors(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _setup_copy_db(db)

        result = main(["--db", str(db), "copy", "destroy",
                       "NO_SUCH_VOL", "Home_Shelf"])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out

    def test_copy_destroy_unknown_location_errors(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _setup_copy_db(db)

        result = main(["--db", str(db), "copy", "destroy",
                       "VOL_GONE", "Bank_Vault"])
        assert result == 1
        out = capsys.readouterr().out
        assert "No disc" in out
