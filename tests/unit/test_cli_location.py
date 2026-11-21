"""Tests for the ``lcsas location`` command (``lcsas.cli.commands.location``)."""

from __future__ import annotations

from pathlib import Path

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.schema import create_all


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    db = tmp_path / "catalog.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
catalog = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
{extra}
""")
    return cfg


class TestCmdLocation:
    def test_location_requires_config(self, capsys):
        result = main(["location", "list"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_location_add_and_list(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        r1 = main(["--config", str(cfg), "location", "add", "Offsite_Safe"])
        assert r1 == 0
        capsys.readouterr()

        r2 = main(["--config", str(cfg), "location", "list"])
        assert r2 == 0
        out = capsys.readouterr().out
        assert "Offsite_Safe" in out

    def test_location_add_rejects_path_separator(self, tmp_path, capsys):
        """Sanitization rejects names with path separators."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "location", "add", "../../etc"])
        assert result == 1
        out = capsys.readouterr().out
        assert "unsafe" in out.lower() or "error" in out.lower()

    def test_location_status(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        from lcsas.db.locations import create_location
        create_location(conn, "Home_Shelf", "")
        conn.close()

        result = main(["--config", str(cfg), "location", "status", "Home_Shelf"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Home_Shelf" in out

    def test_location_move_nonexistent_volume(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main([
            "--config", str(cfg), "location", "move", "NONEXIST",
            "--from", "A", "--to", "B",
        ])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def _catalog_with_disc(self, tmp_path: Path) -> Path:
        from lcsas.db.locations import create_location
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volumes import create_volume

        cfg = _write_config(tmp_path)
        conn = get_connection(tmp_path / "catalog.db")
        create_all(conn)
        create_location(conn, "Home_Shelf", "")
        create_location(conn, "Offsite_Safe", "")
        vol = create_volume(
            conn, "LCSAS_TEST_2026_0001", "uuid-1", "TEST_TINY",
            1_000_000, "Home_Shelf", "VERIFIED",
        )
        add_volume_copy(conn, vol.volume_id, "Home_Shelf")
        conn.close()
        return cfg

    def test_location_move_to_unregistered_location(self, tmp_path, capsys):
        """#823: an unregistered --to says so and names 'lcsas location add'."""
        cfg = self._catalog_with_disc(tmp_path)

        result = main([
            "--config", str(cfg), "location", "move", "LCSAS_TEST_2026_0001",
            "--from", "Home_Shelf", "--to", "Bank_Box",
        ])

        assert result == 1
        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert "already recorded" not in out
        assert "'Bank_Box' is not registered" in out
        assert "lcsas location add Bank_Box" in out
        conn = get_connection(tmp_path / "catalog.db")
        rows = conn.execute("SELECT location FROM volume_copies").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["Home_Shelf"]

    def test_location_move_to_registered_location(self, tmp_path, capsys):
        cfg = self._catalog_with_disc(tmp_path)

        result = main([
            "--config", str(cfg), "location", "move", "LCSAS_TEST_2026_0001",
            "--from", "Home_Shelf", "--to", "Offsite_Safe",
        ])

        assert result == 0
        conn = get_connection(tmp_path / "catalog.db")
        rows = conn.execute("SELECT location FROM volume_copies").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["Offsite_Safe"]

    def test_location_move_from_wrong_location(self, tmp_path, capsys):
        """A --from with no disc of the volume is an error, not a crash."""
        cfg = self._catalog_with_disc(tmp_path)

        result = main([
            "--config", str(cfg), "location", "move", "LCSAS_TEST_2026_0001",
            "--from", "Offsite_Safe", "--to", "Home_Shelf",
        ])

        assert result == 1
        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert "No in-service disc" in out
