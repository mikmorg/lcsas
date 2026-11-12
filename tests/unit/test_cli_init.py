"""Tests for the ``lcsas init`` command (``lcsas.cli.commands.init``)."""

from __future__ import annotations

from lcsas.cli.main import main


class TestCmdInit:
    def test_init_creates_db(self, tmp_path, capsys):
        db = tmp_path / "new.db"
        result = main(["init", "--db-path", str(db)])
        assert result == 0
        assert db.exists()

    def test_reinit_on_existing_db(self, tmp_path, capsys):
        db = tmp_path / "existing.db"
        main(["init", "--db-path", str(db)])
        result = main(["init", "--db-path", str(db)])
        assert result == 0

    def test_init_creates_nested_parent_dirs(self, tmp_path, capsys):
        """`lcsas init --db-path` creates missing intermediate directories.

        Issue #40: ``cmd_init`` calls ``mkdir(parents=True, exist_ok=True)``
        on the DB path's parent so XDG-style or fresh-deployment paths
        with several missing intermediate directories work without the
        operator having to ``mkdir -p`` by hand. This test exercises the
        ``parents=True`` branch.
        """
        db_path = tmp_path / "new" / "nested" / "dir" / "archive.db"
        # Sanity: the parent chain is genuinely missing up front.
        assert not db_path.parent.exists()

        result = main(["init", "--db-path", str(db_path)])
        assert result == 0

        # Every intermediate directory must now exist.
        assert (tmp_path / "new").is_dir()
        assert (tmp_path / "new" / "nested").is_dir()
        assert (tmp_path / "new" / "nested" / "dir").is_dir()
        # And the DB file must exist at the requested path.
        assert db_path.is_file()


class TestCLIInit:
    def test_init_creates_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        result = main(["init", "--db-path", str(db_path)])
        assert result == 0
        assert db_path.exists()

    def test_init_honors_config_flag(self, tmp_path, monkeypatch):
        """`lcsas --config <path> init` must create the catalog DB at the
        path declared in the TOML config, not the default ``archive.db``.

        Regression test for issue #17.
        """
        # Run in an isolated cwd so any default `./archive.db` would be
        # easy to detect (and must NOT appear).
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        # Custom DB location declared inside the TOML config.
        custom_db = tmp_path / "custom" / "catalog.db"
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()

        cfg_path = tmp_path / "lcsas.toml"
        cfg_path.write_text(
            f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{custom_db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
"""
        )

        result = main(["--config", str(cfg_path), "init"])
        assert result == 0

        # The catalog DB must land at the path the TOML config specified.
        assert custom_db.exists(), (
            f"expected catalog DB at {custom_db}, not found"
        )

        # And the default location must NOT have been written.
        assert not (workdir / "archive.db").exists(), (
            "init silently fell back to default archive.db in cwd; "
            "--config was ignored"
        )
