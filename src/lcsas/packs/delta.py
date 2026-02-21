"""Delta analysis: identifies packs needing archival."""

from __future__ import annotations

import sqlite3

from lcsas.db.models import Pack
from lcsas.db.packs import bulk_register
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

        Uses bulk_register() for efficient batch insertion (2 queries
        instead of 2N). Returns list of newly registered Pack objects.
        """
        if not self._scanner_result:
            return []

        all_tuples = [
            (sha256, size_bytes, self._repo_id)
            for sha256, size_bytes in self._scanner_result.items()
        ]

        # Filter out already-known packs with a single query
        sha_list = [t[0] for t in all_tuples]
        placeholders = ",".join("?" * len(sha_list))
        existing_rows = self._conn.execute(
            f"SELECT sha256 FROM packs WHERE sha256 IN ({placeholders})",
            sha_list,
        ).fetchall()
        existing_shas = {row["sha256"] for row in existing_rows}

        new_tuples = [t for t in all_tuples if t[0] not in existing_shas]
        if not new_tuples:
            return []

        return bulk_register(self._conn, new_tuples)

    def get_unarchived(self) -> list[Pack]:
        """Return packs in the DB not yet assigned to any volume."""
        return get_unarchived_packs(self._conn, self._repo_id)

    def get_total_unarchived_bytes(self) -> int:
        """Return total bytes of unarchived packs."""
        return get_total_unarchived_bytes(self._conn, self._repo_id)

    def needs_burn(self, usable_capacity: int) -> bool:
        """Whether unarchived data exceeds the given media capacity."""
        return self.get_total_unarchived_bytes() >= usable_capacity
