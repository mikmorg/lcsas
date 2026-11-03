"""Tests for db/locations.py — location CRUD."""

from __future__ import annotations

import pytest

from lcsas.db.locations import (
    create_location,
    delete_location,
    ensure_location,
    get_location,
    list_locations,
)


class TestLocationCRUD:
    def test_create_and_get(self, conn):
        loc = create_location(conn, "Home_Shelf", "Main bookshelf")
        assert loc.name == "Home_Shelf"
        assert loc.description == "Main bookshelf"

        fetched = get_location(conn, "Home_Shelf")
        assert fetched.name == loc.name

    def test_get_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            get_location(conn, "Nonexistent")

    def test_ensure_creates_if_missing(self, conn):
        loc = ensure_location(conn, "Bank_Vault", "Safe deposit box")
        assert loc.name == "Bank_Vault"

    def test_ensure_returns_existing(self, conn):
        create_location(conn, "Offsite", "Remote safe")
        loc = ensure_location(conn, "Offsite")
        assert loc.name == "Offsite"

    def test_ensure_without_commit_rolls_back_with_the_caller(self, conn):
        """#522: import-receipts keeps the insert in its per-receipt transaction."""
        loc = ensure_location(conn, "Vault_Q", commit=False)
        assert loc.name == "Vault_Q"
        conn.rollback()
        with pytest.raises(ValueError, match="not found"):
            get_location(conn, "Vault_Q")

    def test_ensure_commits_by_default(self, conn):
        ensure_location(conn, "Vault_R")
        conn.rollback()
        assert get_location(conn, "Vault_R").name == "Vault_R"

    def test_list_locations(self, conn):
        create_location(conn, "A_Location")
        create_location(conn, "B_Location")
        locs = list_locations(conn)
        assert len(locs) == 2
        assert locs[0].name == "A_Location"

    def test_delete_location(self, conn):
        create_location(conn, "Temp")
        delete_location(conn, "Temp")
        assert list_locations(conn) == []

    def test_duplicate_raises(self, conn):
        import sqlite3 as _sqlite3
        create_location(conn, "Home_Shelf")
        with pytest.raises(_sqlite3.IntegrityError):
            create_location(conn, "Home_Shelf")


class TestLocationRenameRemove:
    """#491 item 8: `location rename` / `location remove`."""

    @staticmethod
    def _disc_at(conn, location: str) -> int:
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volume_events import add_event
        from lcsas.db.volumes import create_volume

        ensure_location(conn, location)
        vol = create_volume(conn, "LCSAS_BD25_2026_0001", "uuid-1", "BD25",
                            25_000_000_000, location=location)
        add_volume_copy(conn, vol.volume_id, location)
        add_event(conn, vol.volume_id, "VERIFY_PASS", location=location)
        return vol.volume_id

    def test_rename_carries_discs_events_and_volumes(self, conn):
        from lcsas.db.locations import rename_location

        create_location(conn, "Shelf", "the hall shelf")
        vid = self._disc_at(conn, "Shelf")
        moved = rename_location(conn, "Shelf", "Hall_Shelf")
        assert moved == 3
        names = [loc.name for loc in list_locations(conn)]
        assert names == ["Hall_Shelf"]
        assert get_location(conn, "Hall_Shelf").description == "the hall shelf"
        for table in ("volume_copies", "volume_events", "volumes"):
            rows = conn.execute(
                f"SELECT DISTINCT location FROM {table} WHERE volume_id = ?",
                (vid,),
            ).fetchall()
            assert [r[0] for r in rows] == ["Hall_Shelf"], table
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_rename_refuses_missing_or_taken_names(self, conn):
        from lcsas.db.locations import rename_location

        create_location(conn, "A")
        create_location(conn, "B")
        with pytest.raises(ValueError, match="not found"):
            rename_location(conn, "Nope", "C")
        with pytest.raises(ValueError, match="already exists"):
            rename_location(conn, "A", "B")
        assert [loc.name for loc in list_locations(conn)] == ["A", "B"]

    def test_remove_refuses_a_location_with_history(self, conn):
        self._disc_at(conn, "Vault")
        with pytest.raises(ValueError, match="1 disc record"):
            delete_location(conn, "Vault")
        assert [loc.name for loc in list_locations(conn)] == ["Vault"]

    def test_remove_unknown_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            delete_location(conn, "Nope")


def test_cli_location_rename_remove_and_unknown_status(tmp_path, capsys):
    from lcsas.cli.main import main

    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{tmp_path}"\nstaging = "{tmp_path}"\n'
        f'database = "{tmp_path / "archive.db"}"\n'
    )
    c = ["--config", str(cfg)]
    assert main([*c, "init"]) == 0
    assert main([*c, "location", "add", "Old_Shelf"]) == 0
    assert main([*c, "location", "rename", "Old_Shelf", "New_Shelf"]) == 0
    assert main([*c, "location", "status", "Old_Shelf"]) == 1
    assert "not found" in capsys.readouterr().out
    assert main([*c, "location", "status", "New_Shelf"]) == 0
    assert main([*c, "location", "remove", "New_Shelf"]) == 0
    assert main([*c, "location", "remove", "New_Shelf"]) == 1
