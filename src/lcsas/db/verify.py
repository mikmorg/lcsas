"""Catalog-disc cross-validation for LCSAS volumes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)


@dataclass
class CatalogValidationResult:
    """Result of a catalog-vs-disc cross-check.

    Attributes:
        disc_path: Path to the mounted disc that was validated.
        volume_label: Volume label from the disc catalog, or '' if unknown.
        catalog_pack_count: Number of packs registered for this volume in the catalog.
        disc_pack_count: Number of pack files found on disc.
        missing_from_disc: Pack SHA-256 hashes listed in the catalog but absent on disc.
        orphaned_on_disc: Pack SHA-256 hashes found on disc but absent from the catalog.
        ok: True if the disc and catalog are perfectly in sync.
    """

    disc_path: Path
    volume_label: str = ""
    catalog_pack_count: int = 0
    disc_pack_count: int = 0
    missing_from_disc: list[str] = field(default_factory=list)
    orphaned_on_disc: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_from_disc and not self.orphaned_on_disc


def _collect_disc_packs(data_dir: Path) -> set[str]:
    """Walk the data/ directory and collect all pack SHA-256 hashes.

    Handles both flat (data/HASH) and two-level (data/ab/abcdef...) layouts.
    Files that do not look like SHA-256 hashes (64 hex chars) are skipped.
    """
    found: set[str] = set()
    if not data_dir.is_dir():
        return found

    for entry in data_dir.rglob("*"):
        if not entry.is_file():
            continue
        name = entry.name
        # A valid SHA-256 is 64 lowercase hex characters.
        if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
            found.add(name)
        else:
            _logger.debug("Skipping non-pack file on disc: %s", entry)

    return found


def validate_disc(disc_path: Path) -> CatalogValidationResult:
    """Cross-check the catalog embedded on *disc_path* against its data files.

    Opens ``catalog.db`` from the root of *disc_path*, queries the
    ``volume_packs`` and ``packs`` tables to find which pack SHA-256 hashes
    belong to volumes whose data lives on this disc, then walks the ``data/``
    directory to collect the pack files actually present.

    Args:
        disc_path: Path to a mounted LCSAS disc (e.g. ``/mnt/disc``).

    Returns:
        A :class:`CatalogValidationResult` describing any discrepancies.

    Raises:
        FileNotFoundError: If no ``catalog.db`` is found at *disc_path*.
        ValueError: If the disc has no ``data/`` directory.
    """
    catalog_db = disc_path / "catalog.db"
    if not catalog_db.is_file():
        raise FileNotFoundError(
            f"No catalog.db found at {disc_path}. "
            f"Is this a valid LCSAS disc? Is it mounted?"
        )

    data_dir = disc_path / "data"
    if not data_dir.is_dir():
        raise ValueError(
            f"No data/ directory found at {disc_path}. "
            f"The disc layout may be corrupted or this is not a data volume."
        )

    result = CatalogValidationResult(disc_path=disc_path)

    # --- Read catalog ---
    conn = sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row

        # Try to infer the volume label from the catalog.  The disc holds a
        # holographic copy of the full catalog, so there may be multiple
        # volumes; we use the first VERIFIED/BURNED volume as the "owner".
        try:
            row = conn.execute(
                "SELECT label FROM volumes WHERE status IN ('VERIFIED', 'BURNED') "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                result.volume_label = row["label"]
        except sqlite3.OperationalError:
            pass

        # Collect catalog pack hashes: any pack assigned to any volume.
        # (The holographic catalog is complete, so all volumes are present.)
        # We need only the packs that *should* physically be on this disc.
        # Heuristic: packs whose two-level prefix directory exists on disc.
        # Simpler and more correct: use all pack files actually present on
        # disc as the "ground truth" set, and compare against ALL catalog packs.
        # But the real question is: "are all catalogue packs for this disc present?"
        # We use volume_packs to find which packs belong to volumes whose data
        # was meant to be on this disc (any VERIFIED/BURNED/STAGING volume).
        try:
            rows = conn.execute(
                """
                SELECT p.sha256
                FROM packs p
                JOIN volume_packs vp ON vp.pack_id = p.pack_id
                JOIN volumes v ON v.volume_id = vp.volume_id
                WHERE v.status IN ('VERIFIED', 'BURNED', 'STAGING', 'BURNING')
                """
            ).fetchall()
            catalog_hashes: set[str] = {r["sha256"] for r in rows}
        except sqlite3.OperationalError as exc:
            raise ValueError(
                f"Could not read pack catalog from {catalog_db}: {exc}"
            ) from exc
    finally:
        conn.close()

    # --- Walk disc data/ directory ---
    _logger.info("Scanning disc data directory: %s", data_dir)
    disc_hashes = _collect_disc_packs(data_dir)

    result.catalog_pack_count = len(catalog_hashes)
    result.disc_pack_count = len(disc_hashes)

    result.missing_from_disc = sorted(catalog_hashes - disc_hashes)
    result.orphaned_on_disc = sorted(disc_hashes - catalog_hashes)

    return result
