"""Tests for config/settings.py validate_config() — failure paths."""

from __future__ import annotations

from pathlib import Path

from lcsas.config.settings import LCSASConfig, RepositoryConfig, validate_config


def _base_config(tmp_path: Path) -> LCSASConfig:
    """Return a fully valid config rooted in tmp_path."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    return LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=db_dir / "archive.db",
    )


class TestValidConfigPasses:
    def test_valid_config_returns_no_errors(self, tmp_path):
        cfg = _base_config(tmp_path)
        assert validate_config(cfg) == []


class TestMirrorBasePath:
    def test_missing_mirror_base_path(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(
            **{**cfg.__dict__, "mirror_base_path": tmp_path / "no_such_dir"}
        )
        errors = validate_config(cfg)
        assert any("mirror_base_path" in e for e in errors)

    def test_mirror_base_path_is_file(self, tmp_path):
        f = tmp_path / "not_a_dir"
        f.write_text("x")
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "mirror_base_path": f})
        errors = validate_config(cfg)
        assert any("mirror_base_path" in e for e in errors)


class TestStagingPath:
    def test_missing_staging_path(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(
            **{**cfg.__dict__, "staging_path": tmp_path / "no_staging"}
        )
        errors = validate_config(cfg)
        assert any("staging_path" in e for e in errors)

    def test_staging_path_is_file(self, tmp_path):
        f = tmp_path / "staging_file"
        f.write_text("x")
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "staging_path": f})
        errors = validate_config(cfg)
        assert any("staging_path" in e for e in errors)


class TestDbPath:
    def test_missing_db_parent_dir(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(
            **{**cfg.__dict__, "db_path": tmp_path / "no_such_dir" / "archive.db"}
        )
        errors = validate_config(cfg)
        assert any("db_path" in e for e in errors)


class TestEccRedundancy:
    def test_ecc_above_100(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "default_ecc_redundancy_pct": 101})
        errors = validate_config(cfg)
        assert any("out of range" in e for e in errors)

    def test_ecc_negative(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "default_ecc_redundancy_pct": -1})
        errors = validate_config(cfg)
        assert any("out of range" in e for e in errors)

    def test_ecc_zero_ok(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "default_ecc_redundancy_pct": 0})
        assert validate_config(cfg) == []

    def test_ecc_100_ok(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "default_ecc_redundancy_pct": 100})
        assert validate_config(cfg) == []

    def _toml(self, tmp_path, defaults: str) -> Path:
        for sub in ("mirror", "staging"):
            (tmp_path / sub).mkdir(exist_ok=True)
        path = tmp_path / "lcsas.toml"
        path.write_text(
            "[paths]\n"
            f'mirror_base = "{tmp_path}/mirror"\n'
            f'staging = "{tmp_path}/staging"\n'
            f'database = "{tmp_path}/archive.db"\n'
            "[defaults]\n" + defaults
        )
        return path

    def test_ecc_key_warns_ignored(self, tmp_path, caplog):
        """BURN-07 / #699: the key does nothing for RS03 augmented images.

        Any config that sets it, even to the old default 15, is told the
        key is ignored; the config still loads (no error).
        """
        import logging

        from lcsas.config.settings import load_config, unknown_toml_keys

        for value in (15, 30):
            caplog.clear()
            path = self._toml(tmp_path, f"ecc_redundancy_pct = {value}\n")
            with caplog.at_level(logging.WARNING, logger="lcsas"):
                load_config(path)
            hits = [r for r in caplog.records
                    if "ecc_redundancy_pct" in r.getMessage()]
            assert len(hits) == 1, caplog.text
            assert "is ignored" in hits[0].getMessage()

        import tomllib
        raw = tomllib.loads(path.read_text())
        assert unknown_toml_keys(raw) == []  # still a known key

    def test_ecc_key_absent_does_not_warn(self, tmp_path, caplog):
        import logging

        from lcsas.config.settings import load_config

        path = self._toml(tmp_path, 'media_type = "BD25"\n')
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            cfg = load_config(path)
            assert validate_config(cfg) == []
        assert "ecc_redundancy_pct" not in caplog.text


class TestMetadataReserve:
    def test_negative_metadata_reserve_errors(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "metadata_reserve_bytes": -1})
        errors = validate_config(cfg)
        assert any("metadata_reserve_bytes" in e for e in errors)

    def test_zero_metadata_reserve_ok(self, tmp_path):
        """Zero is allowed (risky but not invalid)."""
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(**{**cfg.__dict__, "metadata_reserve_bytes": 0})
        assert validate_config(cfg) == []


class TestRepositoryValidation:
    def test_missing_repo_mirror_path(self, tmp_path):
        cfg = _base_config(tmp_path)
        repo = RepositoryConfig(
            name="test",
            mirror_path=tmp_path / "no_such_repo",
            password_file=None,
        )
        cfg = LCSASConfig(**{**cfg.__dict__, "repositories": {"test": repo}})
        errors = validate_config(cfg)
        assert any("mirror_path" in e for e in errors)

    def test_missing_password_file_errors(self, tmp_path):
        mirror = tmp_path / "repo_mirror"
        mirror.mkdir()
        cfg = _base_config(tmp_path)
        repo = RepositoryConfig(
            name="test",
            mirror_path=mirror,
            password_file=tmp_path / "no_such_key.txt",
        )
        cfg = LCSASConfig(**{**cfg.__dict__, "repositories": {"test": repo}})
        errors = validate_config(cfg)
        assert any("password_file" in e for e in errors)

    def test_none_password_file_ok(self, tmp_path):
        """password_file=None is valid (no encryption key configured)."""
        mirror = tmp_path / "repo_mirror"
        mirror.mkdir()
        cfg = _base_config(tmp_path)
        repo = RepositoryConfig(
            name="test",
            mirror_path=mirror,
            password_file=None,
        )
        cfg = LCSASConfig(**{**cfg.__dict__, "repositories": {"test": repo}})
        assert validate_config(cfg) == []

    def test_existing_password_file_ok(self, tmp_path):
        mirror = tmp_path / "repo_mirror"
        mirror.mkdir()
        pw = tmp_path / "key.txt"
        pw.write_text("secret")
        cfg = _base_config(tmp_path)
        repo = RepositoryConfig(
            name="test",
            mirror_path=mirror,
            password_file=pw,
        )
        cfg = LCSASConfig(**{**cfg.__dict__, "repositories": {"test": repo}})
        assert validate_config(cfg) == []

    def test_multiple_errors_all_reported(self, tmp_path):
        """All validation errors are returned, not just the first."""
        cfg = _base_config(tmp_path)
        cfg = LCSASConfig(
            **{
                **cfg.__dict__,
                "mirror_base_path": tmp_path / "no_mirror",
                "default_ecc_redundancy_pct": 999,
            }
        )
        errors = validate_config(cfg)
        assert len(errors) >= 2
