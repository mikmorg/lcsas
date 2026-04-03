"""Restore planning — generates pick lists from snapshot requirements."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lcsas.db.models import Pack
from lcsas.db.queries import (
    get_deprecated_only_packs,
    get_missing_packs,
    get_pick_list,
    get_pick_list_with_alternates,
)


@dataclass(frozen=True)
class PackSource:
    """A pack plus the volume it's assigned to and alternate volumes."""

    pack: Pack
    volume_label: str
    volume_id: int
    alternates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PickList:
    """A restore plan mapping volumes to needed packs.

    Tells the user which discs to retrieve and what packs to copy.
    """

    volumes: dict[str, list[Pack]]   # {volume_label: [Pack, ...]}
    missing_packs: list[str]         # SHA-256 hashes with no known active volume
    total_packs: int = 0
    total_bytes: int = 0
    # Packs only on DEPRECATED/DESTROYED volumes — may still be physically recoverable.
    # {volume_label: [sha256, ...]}
    deprecated_disc_labels: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PickListV2:
    """A restore plan with alternate source information for resilient restore.

    Like PickList, but each pack carries a list of alternate volumes
    that also hold it, enabling retry on corruption.
    """

    volumes: dict[str, list[PackSource]]  # {volume_label: [PackSource, ...]}
    missing_packs: list[str]
    total_packs: int = 0
    total_bytes: int = 0
    # Packs only on DEPRECATED/DESTROYED volumes — may still be physically recoverable.
    # {volume_label: [sha256, ...]}
    deprecated_disc_labels: dict[str, list[str]] = field(default_factory=dict)


class RestorePlanner:
    """Generate restore pick lists from required pack hashes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def generate_pick_list(
        self,
        required_pack_hashes: list[str],
    ) -> PickList:
        """Given required pack hashes, return a PickList.

        Maps each pack to a volume, identifies any packs that cannot
        be found in the catalog.
        """
        if not required_pack_hashes:
            return PickList(volumes={}, missing_packs=[])

        volumes = get_pick_list(self._conn, required_pack_hashes)
        missing = get_missing_packs(self._conn, required_pack_hashes)
        deprecated = get_deprecated_only_packs(self._conn, required_pack_hashes)

        total_packs = sum(len(packs) for packs in volumes.values())
        total_bytes = sum(
            p.size_bytes for packs in volumes.values() for p in packs
        )

        return PickList(
            volumes=volumes,
            missing_packs=missing,
            total_packs=total_packs,
            total_bytes=total_bytes,
            deprecated_disc_labels=deprecated,
        )

    def generate_pick_list_v2(
        self,
        required_pack_hashes: list[str],
        preferred_location: str = "",
    ) -> PickListV2:
        """Generate a pick list with alternate volume information.

        Like generate_pick_list but each PackSource includes alternate
        volumes for resilient restore (retry on corrupt pack).
        """
        if not required_pack_hashes:
            return PickListV2(volumes={}, missing_packs=[])

        raw = get_pick_list_with_alternates(
            self._conn, required_pack_hashes, preferred_location,
        )
        missing = get_missing_packs(self._conn, required_pack_hashes)
        deprecated = get_deprecated_only_packs(self._conn, required_pack_hashes)

        volumes: dict[str, list[PackSource]] = {}
        total_packs = 0
        total_bytes = 0

        for _sha256, info in raw.items():
            label = info["primary_label"]
            source = PackSource(
                pack=info["pack"],
                volume_label=label,
                volume_id=info["primary_volume_id"],
                alternates=info["alternates"],
            )
            volumes.setdefault(label, []).append(source)
            total_packs += 1
            total_bytes += source.pack.size_bytes

        return PickListV2(
            volumes=volumes,
            missing_packs=missing,
            total_packs=total_packs,
            total_bytes=total_bytes,
            deprecated_disc_labels=deprecated,
        )
