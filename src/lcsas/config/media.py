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
    LTO8 = (12_000_000_000_000, 0)     # 12 TB LTO-8 (no ECC overhead — tape has its own)
    LTO9 = (18_000_000_000_000, 0)     # 18 TB LTO-9

    # Testing media (tiny volumes for automated tests)
    TEST_TINY = (1_048_576, 0)         # 1 MB — fast unit tests
    TEST_SMALL = (10_485_760, 10)      # 10 MB — pipeline smoke tests
    TEST_CD = (104_857_600, 10)        # 100 MB — simulates a CD-ROM in
                                       # blind-restore acceptance tests.
                                       # Renders as "CD" in disc labels.

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
    def is_optical(self) -> bool:
        """Whether this media type is optical (BD-R / M-Disc)."""
        return self.name.startswith(("BD", "MDISC"))

    @property
    def is_tape(self) -> bool:
        """Whether this media type is tape (LTO).

        Tape has built-in ECC; DVDisaster augmentation must be skipped.
        """
        return self.name.startswith("LTO")

    @property
    def is_test(self) -> bool:
        """Whether this is a testing-only media type."""
        return self.name.startswith("TEST")

    @property
    def label_name(self) -> str:
        """Short media token used in volume labels.

        Defaults to the enum member name. Test-only media types
        override this so labels look like real production discs to
        any operator (or blind agent) reading them.
        """
        if self.name == "TEST_CD":
            return "CD"
        return self.name
