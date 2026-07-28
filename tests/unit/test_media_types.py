"""Media type definitions: RS03 ladder conformance and member distinctness.

The distinctness tests exist because M-Disc tiers share exact byte capacities
with their BD-R counterparts.  Under a plain ``Enum`` that made them aliases:
``MediaType.MDISC25 is MediaType.BD25`` was true, so M-Disc archives were
labelled ``BD25``/``BDXL100``, the MDISC tiers never appeared in the CLI's
"valid media types" message, and the ``MDISC`` -> ``MD`` abbreviation in
``generate_volume_label`` was unreachable code.
"""

from __future__ import annotations

from lcsas.config.media import MediaType
from lcsas.ecc.dvdisaster import RS03_MEDIUM_LADDER_BYTES
from lcsas.utils.labels import generate_volume_label


def test_production_capacities_are_rs03_ladder_rungs() -> None:
    """Every production medium must sit exactly on a dvdisaster RS03 rung.

    RS03 augmented images cannot take a redundancy setting: dvdisaster pads
    up to the smallest fitting medium and the slack becomes the parity.  An
    off-ladder capacity would pad up to the next rung and burn a disc mostly
    full of padding.
    """
    for media in MediaType:
        if media.is_test:
            continue
        assert media.capacity_bytes in RS03_MEDIUM_LADDER_BYTES, (
            f"{media.name} capacity {media.capacity_bytes:,} is not an RS03 "
            f"medium rung -- it would pad up to the next one, wasting the disc"
        )


def test_every_member_is_distinct_not_an_alias() -> None:
    """Same-capacity tiers must remain separate members, never aliases."""
    names = [m.name for m in MediaType]
    assert len(names) == len(set(names))
    # The pairs that collide on capacity are the ones at risk.
    assert MediaType.MDISC25 is not MediaType.BD25
    assert MediaType.MDISC50 is not MediaType.BD50
    assert MediaType.MDISC100 is not MediaType.BDXL100


def test_all_declared_tiers_are_iterable() -> None:
    """Aliased members vanish from iteration -- that is how the bug hid."""
    names = {m.name for m in MediaType}
    assert {
        "CD700", "BD25", "BD50", "BDXL100",
        "MDISC25", "MDISC50", "MDISC100", "TEST_TINY",
    } <= names


def test_mdisc_tiers_mirror_their_bd_counterpart_capacity() -> None:
    assert MediaType.MDISC25.capacity_bytes == MediaType.BD25.capacity_bytes
    assert MediaType.MDISC50.capacity_bytes == MediaType.BD50.capacity_bytes
    assert MediaType.MDISC100.capacity_bytes == MediaType.BDXL100.capacity_bytes


def test_mdisc_tiers_label_as_mdisc_not_bd() -> None:
    """Regression: M-Disc volumes were labelled with their BD-R token."""
    assert "MD50" in generate_volume_label("ARCHIVE", MediaType.MDISC50.label_name, 1)
    assert "MD100" in generate_volume_label("ARCHIVE", MediaType.MDISC100.label_name, 1)


def test_cd700_matches_80_minute_cd_geometry() -> None:
    # 80 min x 60 s x 75 sectors/s = 360,000 sectors of 2048 bytes.
    assert MediaType.CD700.capacity_bytes == 360_000 * 2048


def test_usable_bytes_subtracts_ecc_overhead() -> None:
    assert MediaType.CD700.usable_bytes == int(737_280_000 * 0.85)
    assert MediaType.TEST_TINY.usable_bytes == MediaType.TEST_TINY.capacity_bytes


def test_only_test_tiers_are_flagged_is_test() -> None:
    assert MediaType.TEST_TINY.is_test
    for media in (MediaType.CD700, MediaType.MDISC50, MediaType.BD50):
        assert not media.is_test


def test_new_tiers_resolve_by_name() -> None:
    """Config and CLI both resolve media via ``MediaType[name.upper()]``."""
    assert MediaType["CD700"] is MediaType.CD700
    assert MediaType["MDISC50"] is MediaType.MDISC50
