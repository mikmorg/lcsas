"""Tests for the ``lcsas staging`` command (``lcsas.cli.commands.staging``)."""

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


class TestCmdStagingClean:
    def test_staging_clean_no_orphans(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "staging", "clean", "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No orphaned" in out

    def test_staging_clean_detects_orphan(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        # Create an orphan directory in staging (must match session-id pattern)
        orphan = tmp_path / "staging" / "2025-01-01T00-00-00.000000+00-00-deadbeef"
        orphan.mkdir(parents=True)

        result = main(["--config", str(cfg), "staging", "clean", "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed 1" in out
        assert not orphan.exists()

    def test_staging_clean_requires_config(self, capsys):
        result = main(["staging", "clean"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_staging_clean_rechecks_after_prompt(
        self, tmp_path, capsys, monkeypatch
    ):
        """FUP-02: a session committed during the confirm prompt is spared.

        Two orphan dirs are flagged; while the (mocked) input() blocks, a new
        burn_sessions row is committed pointing at one of them.  The re-check
        under the held lock must remove only the still-orphaned dir.
        """
        from lcsas.db.sessions import create_session

        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        staging = tmp_path / "staging"
        keep = staging / "2025-01-01T00-00-00.000000+00-00-aaaaaaaa"
        gone = staging / "2025-01-02T00-00-00.000000+00-00-bbbbbbbb"
        keep.mkdir(parents=True)
        gone.mkdir(parents=True)

        def _confirm(_prompt):
            # Simulate a concurrent `lcsas stage` committing a session for
            # `keep` while the operator stares at the prompt.
            c = get_connection(db)
            create_session(
                c, media_type="TEST_TINY", staging_dir=str(keep),
                session_id="2025-01-01T00:00:00.000000+00:00-aaaaaaaa",
            )
            c.close()
            return "y"

        monkeypatch.setattr("builtins.input", _confirm)

        result = main(["--config", str(cfg), "staging", "clean"])
        assert result == 0
        assert keep.exists(), "in-flight session dir was wrongly deleted"
        assert not gone.exists(), "stale orphan was not removed"
        out = capsys.readouterr().out
        assert "Removed 1" in out

    def test_staging_clean_reports_but_keeps_unmanaged(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        conn = get_connection(tmp_path / "archive.db")
        create_all(conn)
        conn.close()
        staging = tmp_path / "staging"
        stray = staging / "LCSAS-TINY-0001.iso"
        stray.write_bytes(b"x" * 10)
        orphan = staging / "2025-01-01T00-00-00.000000+00-00-deadbeef"
        orphan.mkdir()

        rc = main(["--config", str(cfg), "staging", "clean", "--force"])
        assert rc == 0
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert stray.exists(), "an unattributed file must never be deleted"
        assert not orphan.exists()
        assert "NOT removed" in text
        assert str(stray) in text
        assert "Removed 1" in text
