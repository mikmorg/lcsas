"""Restore planning — generates pick lists from snapshot requirements."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lcsas.db.models import Pack
from lcsas.db.queries import get_missing_packs, get_pick_list


@dataclass(frozen=True)
class PickList:
    """A restore plan mapping volumes to needed packs.

    Tells the user which discs to retrieve and what packs to copy.
    """

    volumes: dict[str, list[Pack]]   # {volume_label: [Pack, ...]}
    missing_packs: list[str]         # SHA-256 hashes with no known volume
    total_packs: int = 0
    total_bytes: int = 0


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

        total_packs = sum(len(packs) for packs in volumes.values())
        total_bytes = sum(
            p.size_bytes for packs in volumes.values() for p in packs
        )

        return PickList(
            volumes=volumes,
            missing_packs=missing,
            total_packs=total_packs,
            total_bytes=total_bytes,
        )
