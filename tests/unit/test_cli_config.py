"""Tests for the ``lcsas config`` command (``lcsas.cli.commands.config``)."""

from __future__ import annotations

from pathlib import Path

from lcsas.cli.main import main


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


class TestCmdConfigCheck:
    def test_valid_config(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 0
        out = capsys.readouterr().out
        assert "valid" in out.lower()

    def test_missing_paths_errors(self, tmp_path, capsys):
        cfg = tmp_path / "bad.toml"
        cfg.write_text("""
[paths]
mirror_base = "/nonexistent/mirror"
staging = "/nonexistent/staging"
database = "/nonexistent/db/archive.db"
""")
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "does not exist" in out

    def test_config_required(self, capsys):
        result = main(["config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    # #515: a typo must not read as "valid".
    def test_section_typo_fails_the_check(self, tmp_path, capsys):
        cfg = _write_config(tmp_path, f"""
[repo.family]
mirror_path = "{tmp_path / 'mirror'}"
""")
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "Unknown config sections" in out
        assert "valid" not in out.lower().replace("invalid", "")

    def test_repo_key_typo_fails_the_check(self, tmp_path, capsys):
        cfg = _write_config(tmp_path, f"""
[repos.family]
mirror_path = "{tmp_path / 'mirror'}"
passwrd_file = "/x"
""")
        assert main(["--config", str(cfg), "config", "check"]) == 1
        out = capsys.readouterr().out
        assert "Unknown [repos.family] keys" in out
        assert out.count("Unknown [repos.family] keys") == 1

    def test_missing_mirror_path_is_named(self, tmp_path, capsys):
        """It used to be a bare KeyError: "Unexpected error: 'mirror_path'"."""
        cfg = _write_config(tmp_path, """
[repos.family]
mirrorpath = "/x"
""")
        assert main(["--config", str(cfg), "config", "check"]) != 0
        out = capsys.readouterr().out
        assert "[repos.family] has no mirror_path" in out
        assert "Unexpected error" not in out

    def test_repo_that_is_not_a_table_is_named(self, tmp_path, capsys):
        cfg = _write_config(tmp_path, """
[repos]
family = "/x"
""")
        assert main(["--config", str(cfg), "config", "check"]) != 0
        assert "[repos.family] must be a table" in capsys.readouterr().out

    def test_no_repos_is_valid_but_noted(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        assert main(["--config", str(cfg), "config", "check"]) == 0
        assert "No repositories configured yet" in capsys.readouterr().out

    def test_bad_ecc_redundancy(self, tmp_path, capsys):
        cfg = _write_config(tmp_path, "ecc_redundancy_pct = 200")
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "out of range" in out
