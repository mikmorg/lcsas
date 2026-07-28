"""Media type definitions for LCSAS volumes."""

from __future__ import annotations

from enum import Enum


class MediaType(Enum):
    """Supported physical media types with capacity and ECC overhead.

    Each member is a tuple of ``(capacity_bytes, ecc_overhead_pct, family)``.

    capacity_bytes: usable raw capacity of the media.
    ecc_overhead_pct: percentage of capacity reserved for DVDisaster ECC data.
    family: operator-facing media family ("BD-R", "M-DISC", ...).

    The ``family`` field is not decoration.  M-Disc tiers share exact byte
    capacities with their BD-R counterparts, and a plain ``Enum`` collapses
    members with identical values into aliases -- which silently made
    ``MediaType.MDISC25 is MediaType.BD25`` true, so M-Disc volumes were
    labelled ``BD25``/``BDXL100`` and the MDISC tiers never appeared when the
    CLI listed valid media types.  Keeping ``family`` distinct keeps every
    tier a real member.  Any future same-capacity tier must differ here too.

    Every production capacity below is a rung on dvdisaster's RS03 medium
    ladder (``RS03_MEDIUM_LADDER_BYTES`` in lcsas/ecc/dvdisaster.py).  That
    is a hard constraint: RS03 augmented images cannot take a redundancy
    setting -- dvdisaster pads the image up to the smallest fitting medium
    and the slack becomes the parity.  A capacity that is not a ladder rung
    would pad up to the next one and burn a disc mostly full of padding, so
    do not add media types off the ladder.
    """

    # Production media
    CD700 = (737_280_000, 15, "CD-R")               # 700 MB CD-R (80 min)
    BD25 = (25_025_314_816, 15, "BD-R")             # 25 GB BD-R single layer
    BD50 = (50_050_629_632, 15, "BD-R DL")          # 50 GB BD-R dual layer
    BDXL100 = (100_103_356_416, 15, "BD-R XL")      # 100 GB BDXL
    MDISC25 = (25_025_314_816, 15, "M-DISC")        # 25 GB M-Disc BD-R
    MDISC50 = (50_050_629_632, 15, "M-DISC DL")     # 50 GB M-Disc BD-R DL
    MDISC100 = (100_103_356_416, 15, "M-DISC XL")   # 100 GB M-Disc BDXL

    # Testing media (tiny volumes for automated tests).
    #
    # Sized so a freshly-staged TEST_TINY ISO (with the full holographic
    # injection — SQLite catalog + per-repo Rustic metadata + ISO 9660
    # padding) still has a few hundred KB of pack-data headroom.  The 1 MB
    # cap was raised to 2 MB in #142: an empty catalog alone is ~144 KB,
    # the standalone restorer is ~44 KB, and xorriso's ISO 9660 overhead
    # adds ~600 KB on small staging trees with many small files.  That put
    # the bare-minimum ISO right at the 1 MB ceiling with zero pack budget,
    # which broke e2e on dev hosts every time the catalog or restorer grew.
    TEST_TINY = (2_097_152, 0, "TEST")              # 2 MB — fast unit tests

    # CD700 note — two things to know before choosing it.
    #
    # Sizing: the holographic catalog is copied onto EVERY disc and grows
    # with the whole archive, while ``metadata_reserve_mb`` (default 100 MB)
    # is subtracted from every volume's pack budget.  Against CD700's
    # 626 MB usable that default eats ~17%, leaving ~522 MB of packs per
    # disc.  Small collections should lower metadata_reserve_mb to match
    # their actual catalog; large archives should not use CD700 at all,
    # because the growing catalog eventually will not fit at any reserve.
    #
    # Durability: CD-R is organic dye on a thin reflective layer and rots
    # far sooner than BD-R HTL or M-Disc's inorganic layer.  Reasonable for
    # bounded collections, transfer discs, and realistic-size testing —
    # a poor choice for the decades-scale archival this project targets.

    def __init__(
        self,
        capacity_bytes: int,
        ecc_overhead_pct: int,
        family: str,
    ) -> None:
        self._capacity_bytes = capacity_bytes
        self._ecc_overhead_pct = ecc_overhead_pct
        self._family = family

    @property
    def capacity_bytes(self) -> int:
        """Total raw capacity in bytes."""
        return self._capacity_bytes

    @property
    def ecc_overhead_pct(self) -> int:
        """Percentage of capacity reserved for ECC data (0–100)."""
        return self._ecc_overhead_pct

    @property
    def family(self) -> str:
        """Operator-facing media family, e.g. ``"M-DISC DL"``."""
        return self._family

    @property
    def usable_bytes(self) -> int:
        """Capacity available for data after subtracting ECC overhead."""
        return int(self._capacity_bytes * (100 - self._ecc_overhead_pct) / 100)

    @property
    def is_test(self) -> bool:
        """Whether this is a testing-only media type."""
        return self.name.startswith("TEST")

    @property
    def label_name(self) -> str:
        """Short media token used in volume labels.

        Defaults to the enum member name.
        """
        return self.name
