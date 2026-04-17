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

    # --- Walk disc data/ directory first (ground truth) ---
    _logger.info("Scanning disc data directory: %s", data_dir)
    disc_hashes = _collect_disc_packs(data_dir)
    result.disc_pack_count = len(disc_hashes)

    # --- Read catalog ---
    conn = sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row

        # Try to read volume_info.json to get the label; fallback to catalog query
        volume_info_path = disc_path / "volume_info.json"
        if volume_info_path.is_file():
            try:
                import json
                with open(volume_info_path, encoding="utf-8") as f:
                    info = json.load(f)
                    result.volume_label = info.get("label", "")
                    # If volume_info has sha256_manifest, use it as the expected set
                    if "sha256_manifest" in info:
                        catalog_hashes: set[str] = set(info["sha256_manifest"])
                    else:
                        catalog_hashes = set()
            except (IOError, ValueError) as e:
                _logger.warning("Could not read volume_info.json: %s; falling back to catalog", e)
                catalog_hashes = set()
        else:
            catalog_hashes = set()

        # If volume_info didn't provide packs, query the catalog using disc packs as filter
        if not catalog_hashes:
            # Use disc packs as ground truth: find volumes that contain any of these packs
            if disc_hashes:
                placeholders = ",".join("?" * len(disc_hashes))
                try:
                    rows = conn.execute(
                        f"""
                        SELECT DISTINCT v.volume_id, v.label, v.status
                        FROM volumes v
                        JOIN volume_packs vp ON vp.volume_id = v.volume_id
                        JOIN packs p ON p.pack_id = vp.pack_id
                        WHERE p.sha256 IN ({placeholders})
                        AND v.status IN ('VERIFIED', 'BURNED', 'STAGING', 'BURNING')
                        """,
                        sorted(disc_hashes),
                    ).fetchall()
                    if rows:
                        # Get the first volume's label
                        result.volume_label = rows[0]["label"]
                        # Collect all packs from all volumes found on this disc
                        volume_ids = [r["volume_id"] for r in rows]
                        vol_placeholders = ",".join("?" * len(volume_ids))
                        pack_rows = conn.execute(
                            f"""
                            SELECT p.sha256
                            FROM packs p
                            JOIN volume_packs vp ON vp.pack_id = p.pack_id
                            WHERE vp.volume_id IN ({vol_placeholders})
                            """,
                            volume_ids,
                        ).fetchall()
                        catalog_hashes = {r["sha256"] for r in pack_rows}
                except sqlite3.OperationalError as exc:
                    raise ValueError(
                        f"Could not read pack catalog from {catalog_db}: {exc}"
                    ) from exc

        # If still no packs found in catalog, check if disc is truly empty
        if not catalog_hashes:
            if not disc_hashes:
                # Empty disc with no packs in catalog is consistent
                catalog_hashes = set()
            # Otherwise disc has orphaned packs (handled below as orphaned_on_disc)

    except sqlite3.OperationalError as exc:
        raise ValueError(
            f"Could not read catalog from {catalog_db}: {exc}"
        ) from exc
    finally:
        conn.close()

    result.catalog_pack_count = len(catalog_hashes)
    result.missing_from_disc = sorted(catalog_hashes - disc_hashes)
    result.orphaned_on_disc = sorted(disc_hashes - catalog_hashes)

    return result
