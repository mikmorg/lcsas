"""Tests for the ``lcsas stage`` command (``lcsas.cli.commands.stage``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
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


class TestCmdStage:
    def test_stage_requires_config(self, capsys):
        result = main(["stage"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_stage_unknown_media(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "stage", "--media", "BOGUS_TYPE"])
        assert result == 1
        out = capsys.readouterr().out
        assert "Unknown media type" in out

    def test_stage_dry_run(self, tmp_path, capsys):
        """Dry-run produces plan output but no DB/FS changes."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        register_repo(conn, "fam", "fam", str(tmp_path / "mirror"), "")

        from lcsas.db.packs import register_pack
        register_pack(conn, "aa" * 32, 5000, "fam")
        conn.close()

        mock_stage_result = MagicMock()
        mock_stage_result.session_id = "dry-run"
        mock_stage_result.manifests = []

        with patch("lcsas.burn.orchestrator.BurnOrchestrator.stage",
                    return_value=mock_stage_result) as mock_s:
            result = main(["--config", str(cfg), "stage", "--dry-run"])

        assert result == 0
        mock_s.assert_called_once()
        _, kwargs = mock_s.call_args
        assert kwargs.get("dry_run") is True
