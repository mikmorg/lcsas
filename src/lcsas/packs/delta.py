"""Delta analysis: identifies packs needing archival."""

from __future__ import annotations

import logging
import sqlite3

from lcsas.db.models import Pack
from lcsas.db.packs import bulk_register
from lcsas.db.queries import get_total_unarchived_bytes, get_unarchived_packs

_logger = logging.getLogger(__name__)


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

        Raises ValueError if no repo_id was provided at construction
        time, since packs require a repository association.
        """
        if not self._scanner_result:
            return []

        if self._repo_id is None:
            raise ValueError(
                "DeltaAnalyzer.register_new_packs() requires a repo_id; "
                "pass repo_id to the constructor"
            )

        all_tuples = [
            (sha256, size_bytes, self._repo_id)
            for sha256, size_bytes in self._scanner_result.items()
        ]

        # Filter out already-known packs in batches to avoid SQLite variable limit
        _batch = 900
        sha_list = [t[0] for t in all_tuples]
        existing_shas: set[str] = set()
        for i in range(0, len(sha_list), _batch):
            batch = sha_list[i : i + _batch]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT sha256 FROM packs WHERE sha256 IN ({placeholders})",
                batch,
            ).fetchall()
            existing_shas.update(row["sha256"] for row in rows)

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

    def detect_pruned(self) -> list[Pack]:
        """Find packs in the DB that are no longer present on the mirror.

        Returns active (non-pruned) packs whose SHA-256 is absent from
        the scanner_result.  These are packs that rustic has pruned from
        the local mirror but are still tracked as active in the catalog.
        """
        from lcsas.db.packs import list_packs

        all_active = list_packs(self._conn, repo_id=self._repo_id, include_pruned=False)

        if not self._scanner_result:
            # No scanner data — cannot detect pruned packs
            if all_active:
                _logger.warning(
                    "Mirror scan returned no packs but DB has %d active pack(s) for this repo — "
                    "skipping prune detection (is the mirror path correct? permission error?)",
                    len(all_active),
                )
            return []

        mirror_hashes = set(self._scanner_result.keys())
        return [p for p in all_active if p.sha256 not in mirror_hashes]
