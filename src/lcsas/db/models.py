"""Pure data models for LCSAS catalog entities.

These are plain dataclasses with no catalog logic — they serve as
value objects passed between modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Volume:
    """A volume: a set of packs mastered together under one label.

    A volume exists as one ISO image and as zero or more discs.
    """

    volume_id: int
    label: str
    uuid: str
    media_type: str
    capacity_bytes: int
    used_bytes: int
    location: str
    status: str
    created_at: str
    closed_at: str | None
    verified_at: str | None


@dataclass(frozen=True)
class Pack:
    """One encrypted pack file from a repository."""

    pack_id: int
    sha256: str
    size_bytes: int
    repo_id: str
    is_pruned: bool
    created_at: str


@dataclass(frozen=True)
class Repository:
    """One backup repository, with its own password."""

    repo_id: str
    name: str
    mirror_path: str
    encryption_key_id: str
    created_at: str
    # 'active' | 'retired' (schema v10).  'retired' means the mirror is
    # gone for good, not merely unmounted — see lcsas.db.repos.
    # Catalogs older than v10 have no column and read back as 'active'.
    status: str = "active"


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time backup snapshot."""

    snapshot_id: str
    repo_id: str
    hostname: str
    timestamp: str
    paths: str      # JSON array
    tags: str        # JSON array
    description: str


@dataclass(frozen=True)
class VolumePack:
    """Association between a volume and a pack (many-to-many)."""

    volume_id: int
    pack_id: int


@dataclass(frozen=True)
class Location:
    """A named place where discs are kept (e.g. Home_Shelf, Offsite_Safe)."""

    name: str
    created_at: str
    description: str


@dataclass(frozen=True)
class VolumeCopy:
    """One burned disc of a volume, kept at a specific location."""

    id: int
    volume_id: int
    location: str
    status: str
    burn_date: str
    notes: str
    iso_sha256: str | None
    last_verified_at: str | None
    media_serial: str
    # Post-ECC ISO byte length, mirrored from session_volumes so device
    # verification survives receipt import / catalog rebuild (FMA-03).
    # None on rows written before schema v8.
    iso_size_bytes: int | None = None


@dataclass(frozen=True)
class BurnSession:
    """A burn session: the volumes staged and burned together in one run."""

    session_id: str
    created_at: str
    media_type: str
    status: str
    staging_dir: str


@dataclass(frozen=True)
class SessionVolume:
    """Association between a burn session and a volume, with its ISO image path."""

    session_id: str
    volume_id: int
    iso_path: str
    iso_sha256: str | None
    # Post-ECC ISO byte length — what device read-back verification
    # hashes once the ISO file is gone (BURN-04).  None on rows written
    # before schema v7.
    iso_size_bytes: int | None = None


@dataclass(frozen=True)
class VolumeEvent:
    """A lifecycle event for a volume (a verify, an ECC repair, etc.)."""

    event_id: int
    volume_id: int
    event_type: str
    event_date: str
    location: str | None
    detail: str


@dataclass(frozen=True)
class KeyEscrow:
    """The durable record of a `lcsas key split` for one repository (KEY-08)."""

    repo_id: str
    threshold: int       # K: shares needed to reconstruct
    shares: int          # N: total shares produced
    slip39_id: int       # SLIP-0039 identifier of the split
    split_at: str        # ISO datetime the split was recorded
