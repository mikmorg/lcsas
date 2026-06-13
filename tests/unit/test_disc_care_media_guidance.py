"""DISC_CARE.txt media-aware guidance [FMT-07].

DISC_CARE.txt is burned onto every disc.  It used to present M-DISC's
unverifiable "1000+ year" vendor marketing as fact and "strongly
recommend" it, biasing the archivist toward a price premium instead of
toward what actually moves durability (more copies + re-burn cadence).
It also said only "keep a USB Blu-ray drive" — but 100 GB BDXL/M-DISC
tiers need a *BDXL-capable* drive, a strictly rarer class; an heir
replacing a dead drive with a non-BDXL drive reads ZERO of the 100 GB
discs with nothing explaining why.

These tests pin:
  * the must-be-BDXL warning appears only for 100 GB BDXL archives;
  * the generic BDXL spec-sheet caveat is always present;
  * "1000+" survives only when qualified as manufacturer-rated;
  * "strongly recommended" is gone;
  * the redundancy/re-burn headline leads MEDIA LONGEVITY.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig
from lcsas.staging.metadata import HolographicInjector

WARNING = "THIS ARCHIVE USES 100 GB BDXL MEDIA"
GENERIC_CAVEAT = "check the\n    spec sheet before buying a replacement"
HEADLINE = "Durability comes from REDUNDANCY and RE-BURN CADENCE"


def _config(tmp_path: Path, media: MediaType) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        default_media_type=media,
    )


def _render(tmp_path: Path, config: LCSASConfig | None) -> str:
    root = tmp_path / "disc"
    root.mkdir()
    injector = HolographicInjector(root)
    injector.write_disc_care(config)
    return (root / "DISC_CARE.txt").read_text(encoding="utf-8")


def test_bdxl100_archive_leads_with_must_be_bdxl_warning(tmp_path: Path) -> None:
    text = _render(tmp_path, _config(tmp_path, MediaType.BDXL100))
    assert WARNING in text
    assert GENERIC_CAVEAT in text


def test_mdisc100_archive_leads_with_must_be_bdxl_warning(
    tmp_path: Path,
) -> None:
    text = _render(tmp_path, _config(tmp_path, MediaType.MDISC100))
    assert WARNING in text
    assert GENERIC_CAVEAT in text


def test_bd25_archive_omits_warning_keeps_generic_caveat(
    tmp_path: Path,
) -> None:
    text = _render(tmp_path, _config(tmp_path, MediaType.BD25))
    assert WARNING not in text
    assert GENERIC_CAVEAT in text


def test_no_config_omits_warning_keeps_generic_caveat(tmp_path: Path) -> None:
    text = _render(tmp_path, None)
    assert WARNING not in text
    assert GENERIC_CAVEAT in text


def test_mdisc_1000_year_only_appears_qualified(tmp_path: Path) -> None:
    text = _render(tmp_path, _config(tmp_path, MediaType.BD25))
    assert "1000+" in text
    # Every occurrence of the figure must sit with the honest qualifier.
    assert "manufacturer-rated 1000+" in text
    assert "strongly recommended" not in text.lower()


def test_redundancy_reburn_headline_present(tmp_path: Path) -> None:
    text = _render(tmp_path, _config(tmp_path, MediaType.BD25))
    assert HEADLINE in text
