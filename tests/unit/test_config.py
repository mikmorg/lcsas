"""Tests for config and media type modules."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, default_config, load_config


class TestMediaType:
    def test_test_tiny_capacity(self):
        assert MediaType.TEST_TINY.capacity_bytes == 1_048_576

    def test_test_tiny_ecc(self):
        assert MediaType.TEST_TINY.ecc_overhead_pct == 0

    def test_test_tiny_usable_equals_capacity(self):
        """With 0% ECC, usable == capacity."""
        assert MediaType.TEST_TINY.usable_bytes == MediaType.TEST_TINY.capacity_bytes

    def test_bd25_is_optical(self):
        assert MediaType.BD25.is_optical is True

    def test_lto8_is_not_optical(self):
        assert MediaType.LTO8.is_optical is False

    def test_test_tiny_is_test(self):
        assert MediaType.TEST_TINY.is_test is True

    def test_bd25_is_not_test(self):
        assert MediaType.BD25.is_test is False

    def test_all_members_have_positive_capacity(self):
        for mt in MediaType:
            assert mt.capacity_bytes > 0, f"{mt.name} has non-positive capacity"

    def test_usable_never_exceeds_capacity(self):
        for mt in MediaType:
            assert mt.usable_bytes <= mt.capacity_bytes, f"{mt.name} usable > capacity"

    def test_lookup_by_name(self):
        assert MediaType["BD25"] is MediaType.BD25
        assert MediaType["TEST_TINY"] is MediaType.TEST_TINY

    def test_label_name_defaults_to_enum_name(self):
        assert MediaType.BD25.label_name == "BD25"
        assert MediaType.LTO8.label_name == "LTO8"
        assert MediaType.TEST_TINY.label_name == "TEST_TINY"


class TestDefaultConfig:
    def test_creates_config(self, tmp_path):
        cfg = default_config(
            tmp_path / "mirror", tmp_path / "staging",
            tmp_path / "db.db", MediaType.TEST_TINY,
        )
        assert isinstance(cfg, LCSASConfig)
        assert cfg.default_media_type is MediaType.TEST_TINY

    def test_default_metadata_reserve(self, tmp_path):
        cfg = default_config(
            tmp_path / "mirror", tmp_path / "staging", tmp_path / "db.db"
        )
        assert cfg.metadata_reserve_bytes == 104_857_600


class TestLoadConfig:
    def test_load_from_toml(self, tmp_path):
        config_toml = textwrap.dedent("""\
        [paths]
        mirror_base = "/mnt/mirror"
        staging = "/mnt/staging"
        database = "/var/lib/lcsas/archive.db"

        [defaults]
        media_type = "TEST_TINY"
        label_prefix = "TEST"
        metadata_reserve_mb = 1

        [repos.family]
        mirror_path = "/mnt/mirror/family"
        password_file = "/root/keys/family.key"

        [repos.work]
        mirror_path = "/mnt/mirror/work"
        """)
        config_file = tmp_path / "lcsas.toml"
        config_file.write_text(config_toml)

        cfg = load_config(config_file)
        assert cfg.default_media_type is MediaType.TEST_TINY
        assert cfg.label_prefix == "TEST"
        assert cfg.metadata_reserve_bytes == 1_048_576
        assert "family" in cfg.repositories
        assert "work" in cfg.repositories
        assert cfg.repositories["family"].password_file == Path("/root/keys/family.key")
        assert cfg.repositories["work"].password_file is None

    def test_load_survivability_fields(self, tmp_path):
        config_toml = textwrap.dedent("""\
        [paths]
        mirror_base = "/mnt/mirror"
        staging = "/mnt/staging"
        database = "/var/lib/lcsas/archive.db"

        [survivability]
        archive_owner = "Jane Doe"
        archive_description = "Family photos 2000-2025"
        key_storage_hints = "In the home safe"
        technical_contact = "Bob (bob@example.com)"
        """)
        config_file = tmp_path / "lcsas.toml"
        config_file.write_text(config_toml)

        cfg = load_config(config_file)
        assert cfg.archive_owner == "Jane Doe"
        assert cfg.archive_description == "Family photos 2000-2025"
        assert cfg.key_storage_hints == "In the home safe"
        assert cfg.technical_contact == "Bob (bob@example.com)"

    def test_survivability_fields_default_empty(self, tmp_path):
        config_toml = textwrap.dedent("""\
        [paths]
        mirror_base = "/mnt/mirror"
        staging = "/mnt/staging"
        database = "/var/lib/lcsas/archive.db"
        """)
        config_file = tmp_path / "lcsas.toml"
        config_file.write_text(config_toml)

        cfg = load_config(config_file)
        assert cfg.archive_owner == ""
        assert cfg.archive_description == ""
        assert cfg.key_storage_hints == ""
        assert cfg.technical_contact == ""

    def test_invalid_media_type(self, tmp_path):
        config_file = tmp_path / "bad.toml"
        config_file.write_text('[defaults]\nmedia_type = "FLOPPY"\n')
        with pytest.raises(ValueError, match="Unknown media type"):
            load_config(config_file)


class TestTomlKeyValidation:
    """Tests for unknown TOML key warnings."""

    def test_unknown_section_warns(self, tmp_path, caplog):
        import logging
        config_file = tmp_path / "unk.toml"
        config_file.write_text('[paths]\n[zebra_section]\nfoo = 1\n')
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "Unknown config sections" in caplog.text
        assert "zebra_section" in caplog.text

    def test_unknown_defaults_key_warns(self, tmp_path, caplog):
        import logging
        config_file = tmp_path / "unk2.toml"
        config_file.write_text('[defaults]\ntypo_key = "val"\n')
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "Unknown [defaults] keys" in caplog.text
        assert "typo_key" in caplog.text

    def test_valid_config_no_warnings(self, tmp_path, caplog):
        import logging
        config_file = tmp_path / "good.toml"
        config_file.write_text(textwrap.dedent("""\
            [paths]
            mirror_base = "/mnt/mirror"
            staging = "/mnt/staging"
            database = "/tmp/test.db"

            [defaults]
            media_type = "BD25"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "typo" not in caplog.text
        assert "Unknown" not in caplog.text


class TestXdgDbPath:
    """Test XDG-compliant default database path."""

    def test_xdg_data_home_used(self, tmp_path, monkeypatch):
        from lcsas.config.settings import _xdg_db_path
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
        result = _xdg_db_path()
        assert result == str(tmp_path / "xdg_data" / "lcsas" / "archive.db")

    def test_fallback_to_home(self, monkeypatch):
        from lcsas.config.settings import _xdg_db_path
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        result = _xdg_db_path()
        assert ".local/share/lcsas/archive.db" in result


class TestNegativeConfig:
    """Tests for invalid/incomplete configuration scenarios."""

    def test_invalid_media_type_raises(self, tmp_path):
        """Unknown media_type value should raise ValueError, not silently default."""
        config_file = tmp_path / "bad.toml"
        config_file.write_text(textwrap.dedent("""\
            [paths]
            mirror_base = "/mnt/mirror"
            staging = "/mnt/staging"
            database = "/tmp/test.db"

            [defaults]
            media_type = "BLUERAY_TYPO"
        """))
        with pytest.raises(ValueError, match="Unknown media type"):
            load_config(config_file)

    def test_missing_mirror_path_warns(self, tmp_path, caplog):
        """A repo mirror_path that doesn't exist emits a warning."""
        import logging
        config_file = tmp_path / "warn.toml"
        config_file.write_text(textwrap.dedent(f"""\
            [paths]
            mirror_base = "{tmp_path}"
            staging = "{tmp_path}"
            database = "{tmp_path / 'test.db'}"

            [repos.missing_mirror]
            mirror_path = "/absolute/nonexistent/mirror/path/12345"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "mirror_path does not exist" in caplog.text

    def test_missing_password_file_warns(self, tmp_path, caplog):
        """A password_file that doesn't exist emits a warning."""
        import logging
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        config_file = tmp_path / "warn.toml"
        config_file.write_text(textwrap.dedent(f"""\
            [paths]
            mirror_base = "{tmp_path}"
            staging = "{tmp_path}"
            database = "{tmp_path / 'test.db'}"

            [repos.family]
            mirror_path = "{mirror}"
            password_file = "{tmp_path / 'nonexistent.key'}"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "password_file does not exist" in caplog.text

    def test_malformed_toml_raises(self, tmp_path):
        """Syntactically invalid TOML raises an error on load."""
        import tomllib
        config_file = tmp_path / "bad.toml"
        config_file.write_bytes(b"[unclosed\nkey = value\n")
        with pytest.raises((tomllib.TOMLDecodeError, Exception)):
            load_config(config_file)

    def test_missing_required_mirror_path_key(self, tmp_path):
        """A repo block without mirror_path should raise KeyError."""
        config_file = tmp_path / "bad.toml"
        config_file.write_text(textwrap.dedent("""\
            [repos.no_mirror]
            password_file = "/tmp/key.txt"
        """))
        with pytest.raises(KeyError):
            load_config(config_file)

    def test_missing_mirror_base_warns(self, tmp_path, caplog):
        """Omitting paths.mirror_base emits a warning about defaulting."""
        import logging

        config_file = tmp_path / "no_mirror_base.toml"
        config_file.write_text(textwrap.dedent(f"""\
            [paths]
            staging = "{tmp_path}"
            database = "{tmp_path / 'test.db'}"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "mirror_base" in caplog.text
        assert "not set" in caplog.text

    def test_missing_staging_warns(self, tmp_path, caplog):
        """Omitting paths.staging emits a warning about defaulting."""
        import logging

        config_file = tmp_path / "no_staging.toml"
        config_file.write_text(textwrap.dedent(f"""\
            [paths]
            mirror_base = "{tmp_path}"
            database = "{tmp_path / 'test.db'}"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "staging" in caplog.text
        assert "not set" in caplog.text

    def test_explicit_paths_no_default_warnings(self, tmp_path, caplog):
        """When both mirror_base and staging are set, no default warnings emitted."""
        import logging

        config_file = tmp_path / "full.toml"
        config_file.write_text(textwrap.dedent(f"""\
            [paths]
            mirror_base = "{tmp_path}"
            staging = "{tmp_path}"
            database = "{tmp_path / 'test.db'}"
        """))
        with caplog.at_level(logging.WARNING, logger="lcsas"):
            load_config(config_file)
        assert "not set" not in caplog.text
