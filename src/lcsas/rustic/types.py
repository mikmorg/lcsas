"""Data types for Rustic wrapper results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BackupResult:
    """Result of a rustic backup operation."""

    snapshot_id: str
    files_new: int = 0
    files_changed: int = 0
    files_unmodified: int = 0
    data_added_bytes: int = 0
    total_duration_secs: float = 0.0


@dataclass(frozen=True)
class SnapshotInfo:
    """Metadata about a single Rustic snapshot."""

    snapshot_id: str
    timestamp: str
    hostname: str
    paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RestorePlan:
    """Result of a restore dry-run: the list of required pack hashes."""

    snapshot_id: str
    required_pack_hashes: list[str] = field(default_factory=list)
    total_size_bytes: int = 0
    file_count: int = 0


@dataclass(frozen=True)
class PruneResult:
    """Result of a prune dry-run."""

    packs_to_delete: list[str] = field(default_factory=list)
    packs_to_repack: list[str] = field(default_factory=list)
    space_freed_bytes: int = 0
