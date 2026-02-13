"""Delta analysis: identifies packs needing archival."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Pack
from lcsas.db.packs import get_pack_by_sha256, register_pack
from lcsas.db.queries import get_total_unarchived_bytes, get_unarchived_packs


class DeltaAnalyzer:
    """Compares packs on disk (scanner result) against the catalog DB.

    Identifies new packs to register and unarchived packs to burn.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        scanner_result: dict[str, int],
        repo_id: str | None = None,
    ) -> None:
        self._conn = conn
        self._scanner_result = scanner_result
        self._repo_id = repo_id

    def register_new_packs(self) -> list[Pack]:
        """Register packs found on disk but not yet in the database.

        Returns list of newly registered Pack objects.
        """
        new_packs: list[Pack] = []
        for sha256, size_bytes in self._scanner_result.items():
            existing = get_pack_by_sha256(self._conn, sha256)
            if existing is None:
                pack = register_pack(
                    self._conn, sha256, size_bytes, self._repo_id
                )
                new_packs.append(pack)
        return new_packs

    def get_unarchived(self) -> list[Pack]:
        """Return packs in the DB not yet assigned to any volume."""
        return get_unarchived_packs(self._conn, self._repo_id)

    def get_total_unarchived_bytes(self) -> int:
        """Return total bytes of unarchived packs."""
        return get_total_unarchived_bytes(self._conn, self._repo_id)

    def needs_burn(self, usable_capacity: int) -> bool:
        """Whether unarchived data exceeds the given media capacity."""
        return self.get_total_unarchived_bytes() >= usable_capacity
