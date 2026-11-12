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


class TestCmdLocation:
    def test_location_requires_config(self, capsys):
        result = main(["location", "list"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_location_add_and_list(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
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
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "location", "add", "../../etc"])
        assert result == 1
        out = capsys.readouterr().out
        assert "unsafe" in out.lower() or "error" in out.lower()

    def test_location_status(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
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
        db = tmp_path / "archive.db"
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
