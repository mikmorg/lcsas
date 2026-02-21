"""Pure data models for LCSAS database entities.

These are plain dataclasses with no database logic — they serve as
value objects passed between modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Volume:
    """A physical media volume (disc, tape, etc.)."""

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
    """An encrypted data pack (chunk container)."""

    pack_id: int
    sha256: str
    size_bytes: int
    repo_id: str
    is_pruned: bool
    created_at: str


@dataclass(frozen=True)
class Repository:
    """A logical backup repository (tenant)."""

    repo_id: str
    name: str
    mirror_path: str
    encryption_key_id: str
    created_at: str


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
    """A physical storage location (e.g. Home_Shelf, Offsite_Safe)."""

    name: str
    created_at: str
    description: str


@dataclass(frozen=True)
class VolumeCopy:
    """A physical copy of a volume at a specific location."""

    id: int
    volume_id: int
    location: str
    status: str
    burn_date: str
    notes: str
    iso_sha256: str | None
    last_verified_at: str | None
    media_serial: str


@dataclass(frozen=True)
class BurnSession:
    """A staging session grouping volumes for burning."""

    session_id: str
    created_at: str
    media_type: str
    status: str
    staging_dir: str


@dataclass(frozen=True)
class SessionVolume:
    """Association between a session and a volume with its ISO path."""

    session_id: str
    volume_id: int
    iso_path: str
    iso_sha256: str | None


@dataclass(frozen=True)
class VolumeEvent:
    """A lifecycle event for a volume (verification, ECC repair, etc.)."""

    event_id: int
    volume_id: int
    event_type: str
    event_date: str
    location: str | None
    detail: str
