"""Pure data models for LCSAS database entities.

These are plain dataclasses with no database logic — they serve as
value objects passed between modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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


@dataclass(frozen=True)
class Pack:
    """An encrypted data pack (chunk container)."""

    pack_id: int
    sha256: str
    size_bytes: int
    repo_id: str | None
    is_pruned: bool
    created_at: str


@dataclass(frozen=True)
class Repository:
    """A logical backup repository (tenant)."""

    repo_id: str
    name: str
    mirror_path: str
    encryption_key_id: str


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time backup snapshot."""

    snapshot_id: str
    repo_id: str | None
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
