"""Media type definitions for LCSAS volumes."""

from __future__ import annotations

from enum import Enum


class MediaType(Enum):
    """Supported physical media types with capacity and ECC overhead.

    Each member is a tuple of (capacity_bytes, ecc_overhead_pct).
    capacity_bytes: usable raw capacity of the media.
    ecc_overhead_pct: percentage of capacity reserved for DVDisaster ECC data.
    """

    # Production media
    BD25 = (25_025_314_816, 15)        # 25 GB BD-R (single layer)
    BD50 = (50_050_629_632, 15)        # 50 GB BD-R (dual layer)
    BDXL100 = (100_103_356_416, 15)    # 100 GB BDXL
    MDISC25 = (25_025_314_816, 15)     # 25 GB M-Disc BD-R
    MDISC100 = (100_103_356_416, 15)   # 100 GB M-Disc BDXL

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
    TEST_TINY = (2_097_152, 0)         # 2 MB — fast unit tests

    def __init__(self, capacity_bytes: int, ecc_overhead_pct: int) -> None:
        self._capacity_bytes = capacity_bytes
        self._ecc_overhead_pct = ecc_overhead_pct

    @property
    def capacity_bytes(self) -> int:
        """Total raw capacity in bytes."""
        return self._capacity_bytes

    @property
    def ecc_overhead_pct(self) -> int:
        """Percentage of capacity reserved for ECC data (0–100)."""
        return self._ecc_overhead_pct

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
