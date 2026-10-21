"""Gap tests for config validation and loader warnings (issue #517).

Pins ``validate_config()`` branches and ``_validate_toml_keys()`` warnings
that had no test at any level:

* ``staging_path`` vs ``mirror_base_path`` overlap (identical, nested either
  way, via symlink) -- NB: the validator has no *repo-to-repo* mirror-path
  overlap check, only this staging/mirror one;
* ``label_prefix`` validation (empty, non-ISO-9660 characters, the 32-char
  label ceiling);
* ``metadata_reserve_bytes >= usable_bytes`` of the configured media;
* unknown-key warnings for ``[paths]``, ``[survivability]`` and
  ``[repos.<name>]`` (the loader only warns; it never rejects).
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig, load_config, validate_config
from lcsas.utils.labels import generate_volume_label


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


def _with(cfg: LCSASConfig, **changes) -> LCSASConfig:
    return LCSASConfig(**{**cfg.__dict__, **changes})


# Media whose label token is 5 chars (CD700, BX100, MD100) rather than BD25's 4.
_LONG_MEDIA_TOKEN = {"CD700", "BDXL100", "MDISC100"}
_LONG_MEDIA_TOKEN_BUG = pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: validate_config sizes the label overhead for BD25 "
        "('_BD25_2026_0001' = 15 chars) but CD700 / BDXL100 / MDISC100 have a "
        "5-char media token (16-char overhead), so a 17-char prefix passes "
        "validation and generate_volume_label() then raises at stage time. "
        "See the #517 report."
    ),
)


def _errors_mentioning(errors: list[str], needle: str) -> list[str]:
    return [e for e in errors if needle in e]


# ---------------------------------------------------------------------------
# staging_path / mirror_base_path overlap
# ---------------------------------------------------------------------------

class TestStagingMirrorOverlap:
    def test_identical_dirs_rejected(self, tmp_path):
        cfg = _base_config(tmp_path)
        cfg = _with(cfg, staging_path=cfg.mirror_base_path)
        errors = validate_config(cfg)
        assert _errors_mentioning(errors, "cannot be identical")

    def test_staging_inside_mirror_rejected(self, tmp_path):
        cfg = _base_config(tmp_path)
        nested = cfg.mirror_base_path / "staging"
        nested.mkdir()
        errors = validate_config(_with(cfg, staging_path=nested))
        assert _errors_mentioning(errors, "cannot overlap")

    def test_mirror_inside_staging_rejected(self, tmp_path):
        cfg = _base_config(tmp_path)
        nested = cfg.staging_path / "mirror"
        nested.mkdir()
        errors = validate_config(_with(cfg, mirror_base_path=nested))
        assert _errors_mentioning(errors, "cannot overlap")

    def test_symlinked_alias_of_mirror_detected_as_identical(self, tmp_path):
        """The check compares resolved paths, so a symlink can't dodge it."""
        cfg = _base_config(tmp_path)
        alias = tmp_path / "staging_alias"
        alias.symlink_to(cfg.mirror_base_path, target_is_directory=True)
        errors = validate_config(_with(cfg, staging_path=alias))
        assert _errors_mentioning(errors, "cannot be identical")

    def test_sibling_dirs_sharing_a_name_prefix_are_fine(self, tmp_path):
        """``/x/mirror`` vs ``/x/mirror2`` is a string-prefix match, not nesting."""
        cfg = _base_config(tmp_path)
        sibling = tmp_path / "mirror2"
        sibling.mkdir()
        errors = validate_config(_with(cfg, staging_path=sibling))
        assert errors == []

    def test_distinct_dirs_produce_no_overlap_error(self, tmp_path):
        errors = validate_config(_base_config(tmp_path))
        assert not _errors_mentioning(errors, "cannot overlap")
        assert not _errors_mentioning(errors, "cannot be identical")


# ---------------------------------------------------------------------------
# label_prefix
# ---------------------------------------------------------------------------

class TestLabelPrefix:
    def test_empty_prefix_rejected(self, tmp_path):
        cfg = _with(_base_config(tmp_path), label_prefix="")
        errors = validate_config(cfg)
        assert _errors_mentioning(errors, "label_prefix must not be empty")

    @pytest.mark.parametrize(
        "prefix", ["lcsas", "MY-ARCHIVE", "MY ARCHIVE", "ARCH.IVE", "ARCHÍVE", "A/B"]
    )
    def test_non_iso9660_characters_rejected(self, tmp_path, prefix):
        cfg = _with(_base_config(tmp_path), label_prefix=prefix)
        errors = validate_config(cfg)
        bad = _errors_mentioning(errors, "ISO 9660 volume identifiers")
        assert len(bad) == 1, errors
        assert prefix in bad[0]

    @pytest.mark.parametrize("prefix", ["LCSAS", "FAMILY_2026", "A", "0123456789", "_"])
    def test_valid_prefixes_accepted(self, tmp_path, prefix):
        cfg = _with(_base_config(tmp_path), label_prefix=prefix)
        assert validate_config(cfg) == []

    def test_longest_accepted_prefix_is_17_chars_for_bd25(self, tmp_path):
        """17 + len('_BD25_2026_0001') == 32, the ISO 9660 limit: accepted."""
        cfg = _with(_base_config(tmp_path), label_prefix="A" * 17)
        assert validate_config(cfg) == []

    def test_18_char_prefix_rejected_as_too_long(self, tmp_path):
        cfg = _with(_base_config(tmp_path), label_prefix="A" * 18)
        errors = validate_config(cfg)
        too_long = _errors_mentioning(errors, "too long")
        assert len(too_long) == 1, errors
        assert "32-character" in too_long[0]

    def test_character_error_suppresses_length_error(self, tmp_path):
        """A bad-charset prefix reports the charset problem only (elif chain)."""
        cfg = _with(_base_config(tmp_path), label_prefix="a" * 30)
        errors = validate_config(cfg)
        assert _errors_mentioning(errors, "ISO 9660 volume identifiers")
        assert not _errors_mentioning(errors, "too long")

    def test_accepted_prefix_really_yields_a_legal_bd25_label(self, tmp_path):
        """The validator's 17-char ceiling matches the real label generator."""
        cfg = _with(_base_config(tmp_path), label_prefix="A" * 17)
        assert validate_config(cfg) == []
        label = generate_volume_label(cfg.label_prefix, MediaType.BD25.label_name, 1)
        assert len(label) == 32
        with pytest.raises(ValueError, match="exceeds ISO 9660"):
            generate_volume_label("A" * 18, MediaType.BD25.label_name, 1)

    @pytest.mark.parametrize(
        "media",
        [
            pytest.param(m, marks=_LONG_MEDIA_TOKEN_BUG) if m.name in _LONG_MEDIA_TOKEN else m
            for m in MediaType
            if not m.is_test
        ],
        ids=lambda m: m.name,
    )
    def test_any_accepted_prefix_yields_legal_labels_for_configured_media(
        self, tmp_path, media
    ):
        """validate_config() promises the prefix 'won't produce labels
        exceeding 32 chars'.  Take the longest prefix it accepts for this
        media type and check the real generator agrees."""
        base = _with(_base_config(tmp_path), default_media_type=media)
        longest = 0
        for n in range(1, 40):
            probe = _with(base, label_prefix="A" * n)
            if not _errors_mentioning(validate_config(probe), "label_prefix"):
                longest = n
        assert longest > 0
        # Must not raise ValueError("... exceeds ISO 9660 maximum ...").
        generate_volume_label("A" * longest, media.label_name, 1)


# ---------------------------------------------------------------------------
# metadata_reserve_bytes >= usable media capacity
# ---------------------------------------------------------------------------

class TestMetadataReserveVsUsable:
    def test_reserve_equal_to_usable_rejected(self, tmp_path):
        usable = MediaType.TEST_TINY.usable_bytes
        cfg = _with(
            _base_config(tmp_path),
            default_media_type=MediaType.TEST_TINY,
            metadata_reserve_bytes=usable,
        )
        errors = validate_config(cfg)
        bad = _errors_mentioning(errors, "must be less than the usable capacity")
        assert len(bad) == 1, errors
        assert "TEST_TINY" in bad[0]
        assert f"{usable:,}" in bad[0]

    def test_reserve_above_usable_rejected(self, tmp_path):
        cfg = _with(
            _base_config(tmp_path),
            default_media_type=MediaType.TEST_TINY,
            metadata_reserve_bytes=MediaType.TEST_TINY.usable_bytes * 10,
        )
        errors = validate_config(cfg)
        assert _errors_mentioning(errors, "must be less than the usable capacity")

    def test_reserve_one_byte_below_usable_accepted(self, tmp_path):
        cfg = _with(
            _base_config(tmp_path),
            default_media_type=MediaType.TEST_TINY,
            metadata_reserve_bytes=MediaType.TEST_TINY.usable_bytes - 1,
        )
        assert validate_config(cfg) == []

    def test_default_100mb_reserve_does_not_fit_test_tiny(self, tmp_path):
        """The 100 MB default only makes sense on real media."""
        cfg = _with(_base_config(tmp_path), default_media_type=MediaType.TEST_TINY)
        assert cfg.metadata_reserve_bytes == 104_857_600
        assert _errors_mentioning(validate_config(cfg), "usable capacity")

    def test_default_reserve_fits_bd25(self, tmp_path):
        cfg = _base_config(tmp_path)
        assert cfg.default_media_type is MediaType.BD25
        assert validate_config(cfg) == []

    def test_limit_tracks_the_configured_media_type(self, tmp_path):
        """A reserve that is fine for BD25 is fatal for CD700-sized media."""
        reserve = MediaType.CD700.usable_bytes  # >= CD700 usable, << BD25 usable
        base = _with(_base_config(tmp_path), metadata_reserve_bytes=reserve)
        assert validate_config(_with(base, default_media_type=MediaType.BD25)) == []
        errors = validate_config(_with(base, default_media_type=MediaType.CD700))
        assert _errors_mentioning(errors, "CD700")

    def test_negative_reserve_takes_the_other_branch(self, tmp_path):
        """Negative is reported as 'negative', not as 'exceeds usable'."""
        cfg = _with(_base_config(tmp_path), metadata_reserve_bytes=-5)
        errors = validate_config(cfg)
        assert _errors_mentioning(errors, "must not be negative")
        assert not _errors_mentioning(errors, "usable capacity")


# ---------------------------------------------------------------------------
# Loader: unknown [paths] / [survivability] / [repos.<name>] keys warn
# ---------------------------------------------------------------------------

def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _load(tmp_path: Path, body: str, caplog) -> tuple[LCSASConfig, list[str]]:
    config_file = tmp_path / "lcsas.toml"
    config_file.write_text(textwrap.dedent(body))
    with caplog.at_level(logging.WARNING, logger="lcsas"):
        cfg = load_config(config_file)
    return cfg, _warnings(caplog)


_GOOD_PATHS = """\
    [paths]
    mirror_base = "/mnt/mirror"
    staging = "/mnt/staging"
    database = "/tmp/test.db"
"""


class TestUnknownKeyWarnings:
    def test_unknown_paths_key_warns_but_still_loads(self, tmp_path, caplog):
        cfg, warns = _load(
            tmp_path,
            """\
            [paths]
            mirror_base = "/mnt/mirror"
            staging = "/mnt/staging"
            database = "/tmp/test.db"
            stagging = "/mnt/oops"
            """,
            caplog,
        )
        hits = [w for w in warns if "Unknown [paths] keys" in w]
        assert len(hits) == 1, warns
        assert "stagging" in hits[0]
        # A warning, never a rejection: the known keys still took effect.
        assert cfg.staging_path == Path("/mnt/staging")
        assert cfg.mirror_base_path == Path("/mnt/mirror")

    def test_unknown_survivability_key_warns_but_still_loads(self, tmp_path, caplog):
        cfg, warns = _load(
            tmp_path,
            _GOOD_PATHS
            + """\

    [survivability]
    archive_owner = "Jane"
    tecnical_contact = "typo@example.com"
            """,
            caplog,
        )
        hits = [w for w in warns if "Unknown [survivability] keys" in w]
        assert len(hits) == 1, warns
        assert "tecnical_contact" in hits[0]
        assert cfg.archive_owner == "Jane"
        # The typo'd key silently did NOT populate the real field.
        assert cfg.technical_contact == ""

    def test_unknown_repo_key_warns_and_names_the_repo(self, tmp_path, caplog):
        cfg, warns = _load(
            tmp_path,
            _GOOD_PATHS
            + """\

    [repos.family]
    mirror_path = "/mnt/mirror/family"
    passwrd_file = "/root/keys/family.key"
            """,
            caplog,
        )
        hits = [w for w in warns if "Unknown [repos.family] keys" in w]
        assert len(hits) == 1, warns
        assert "passwrd_file" in hits[0]
        # The repo itself loads, but the typo'd password_file was dropped.
        assert cfg.repositories["family"].mirror_path == Path("/mnt/mirror/family")
        assert cfg.repositories["family"].password_file is None

    def test_each_repo_is_checked_independently(self, tmp_path, caplog):
        _cfg, warns = _load(
            tmp_path,
            _GOOD_PATHS
            + """\

    [repos.family]
    mirror_path = "/mnt/mirror/family"
    password_file = "/root/keys/family.key"

    [repos.work]
    mirror_path = "/mnt/mirror/work"
    bogus = 1
            """,
            caplog,
        )
        assert any("Unknown [repos.work] keys" in w and "bogus" in w for w in warns)
        assert not any("[repos.family]" in w for w in warns)

    def test_multiple_unknown_keys_are_all_listed(self, tmp_path, caplog):
        _cfg, warns = _load(
            tmp_path,
            """\
            [paths]
            mirror_base = "/mnt/mirror"
            staging = "/mnt/staging"
            database = "/tmp/test.db"
            zzz = 1
            aaa = 2
            """,
            caplog,
        )
        hit = next(w for w in warns if "Unknown [paths] keys" in w)
        assert "aaa" in hit and "zzz" in hit

    def test_known_keys_in_all_three_tables_do_not_warn(self, tmp_path, caplog):
        cfg, warns = _load(
            tmp_path,
            _GOOD_PATHS
            + """\

    [survivability]
    archive_owner = "Jane"
    archive_description = "Photos"
    key_storage_hints = "Attorney"
    technical_contact = "Joe"

    [repos.family]
    mirror_path = "/mnt/mirror/family"
    password_file = "/root/keys/family.key"
    encryption_key_id = "k1"
            """,
            caplog,
        )
        assert not [w for w in warns if "Unknown" in w], warns
        assert cfg.repositories["family"].encryption_key_id == "k1"

    def test_warnings_do_not_make_validate_config_fail(self, tmp_path, caplog):
        """Unknown keys are advisory: a config that loads with warnings and has
        valid paths still validates clean (the #515 'typos load as valid' shape)."""
        mirror = tmp_path / "mirror"
        staging = tmp_path / "staging"
        mirror.mkdir()
        staging.mkdir()
        cfg, warns = _load(
            tmp_path,
            f"""\
            [paths]
            mirror_base = "{mirror}"
            staging = "{staging}"
            database = "{tmp_path / "archive.db"}"
            mirrer_base = "typo"
            """,
            caplog,
        )
        assert any("Unknown [paths] keys" in w for w in warns)
        assert validate_config(cfg) == []

    def test_repository_config_default_password_is_none(self):
        """Guard for the 'typo'd passwrd_file is silently dropped' assertions."""
        assert RepositoryConfig(name="x", mirror_path=Path("/m")).password_file is None
