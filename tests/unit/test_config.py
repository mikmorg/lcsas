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

    def test_test_small_usable_with_ecc(self):
        """10% ECC overhead reduces usable bytes."""
        expected = int(10_485_760 * 90 / 100)
        assert MediaType.TEST_SMALL.usable_bytes == expected

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
