"""Catalog-disc cross-validation for LCSAS volumes."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from lcsas.utils.hashing import sha256_file

_logger = logging.getLogger(__name__)


@dataclass
class CatalogValidationResult:
    """Result of a catalog-vs-disc cross-check.

    Attributes:
        disc_path: Path to the mounted disc that was validated.
        volume_label: Volume label from the disc catalog, or '' if unknown.
        catalog_pack_count: Number of packs registered for this volume in the catalog.
        disc_pack_count: Number of pack files found on disc.
        missing_from_disc: Pack IDs listed in the catalog but absent on disc.
        orphaned_on_disc: Pack IDs found on disc but absent from the catalog.
        corrupt_on_disc: Pack IDs whose on-disc content does not
            hash to the filename (content mode only).
        manifest_pack_count: Number of packs in ``volume_info.json``'s
            manifest.
        not_in_catalog: Pack IDs in the manifest that the catalog does
            not place on this volume.
        not_in_manifest: Pack IDs the catalog places on this volume that
            the manifest lacks.
        problems: Other reasons the disc fails, one line each.
        catalog_version: The disc catalog's schema version.
        ok: True if catalog, manifest and data files agree.
    """

    disc_path: Path
    volume_label: str = ""
    catalog_pack_count: int = 0
    disc_pack_count: int = 0
    missing_from_disc: list[str] = field(default_factory=list)
    orphaned_on_disc: list[str] = field(default_factory=list)
    corrupt_on_disc: list[str] = field(default_factory=list)
    manifest_pack_count: int = 0
    not_in_catalog: list[str] = field(default_factory=list)
    not_in_manifest: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    catalog_version: int | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.missing_from_disc
            and not self.orphaned_on_disc
            and not self.corrupt_on_disc
            and not self.not_in_catalog
            and not self.not_in_manifest
            and not self.problems
        )


def _collect_disc_pack_paths(data_dir: Path) -> dict[str, Path]:
    """Walk the data/ directory and map pack IDs to file paths.

    Handles both flat (data/HASH) and two-level (data/ab/abcdef...) layouts.
    Files that do not look like SHA-256 hashes (64 hex chars) are skipped.
    """
    found: dict[str, Path] = {}
    if not data_dir.is_dir():
        return found

    for entry in data_dir.rglob("*"):
        if not entry.is_file():
            continue
        name = entry.name
        # A valid SHA-256 is 64 hex characters (case-insensitive).
        # Normalize to lowercase for consistency.
        if len(name) == 64 and all(c in "0123456789abcdefABCDEF" for c in name):
            found[name.lower()] = entry
        else:
            _logger.debug("Skipping non-pack file on disc: %s", entry)

    return found


def _collect_disc_packs(data_dir: Path) -> set[str]:
    """Walk the data/ directory and collect all pack IDs."""
    return set(_collect_disc_pack_paths(data_dir))


def _read_volume_info(disc_path: Path) -> dict[str, object]:
    """The disc's ``volume_info.json``; ValueError (worded) when missing or
    unreadable: without it the disc cannot say which volume it holds."""
    info_path = disc_path / "volume_info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            f"No volume_info.json at {disc_path}: the disc does not say "
            "which volume it holds, so its catalog cannot be checked."
        ) from None
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cannot read {info_path}: {exc}.  The disc may be damaged."
        ) from exc
    if not isinstance(info, dict) or not isinstance(info.get("uuid"), str) \
            or not info["uuid"]:
        raise ValueError(
            f"{info_path} names no volume uuid: the disc does not say which "
            "volume it holds, so its catalog cannot be checked."
        )
    return info


def validate_disc(disc_path: Path, *, content: bool = False) -> CatalogValidationResult:
    """Cross-check a disc's catalog, its manifest and its data files (#963).

    Opens ``catalog.db`` at the root of *disc_path* read-only with
    :func:`lcsas.db.disc_catalog.open_disc_catalog` (a file that cannot
    be read fails here), finds this disc's volume in it by the uuid in
    ``volume_info.json``, and compares three lists of packs: the
    catalog's ``volume_packs`` for that volume, the manifest in
    ``volume_info.json``, and the pack files under ``data/``.  The disc
    passes only when all three agree.

    Args:
        disc_path: Path to a mounted LCSAS disc (e.g. ``/mnt/disc``).
        content: When True, additionally read every pack file on disc and
            check its SHA-256 against the filename hash, reporting
            mismatches in ``corrupt_on_disc``.  Reads the full data
            payload of the disc (BURN-02 spot-check).

    Returns:
        A :class:`CatalogValidationResult` describing any discrepancies.

    Raises:
        FileNotFoundError: If no ``catalog.db`` is found at *disc_path*.
        ValueError: If the disc has no ``data/`` directory, or no readable
            ``volume_info.json`` naming its volume.
        DiscCatalogError: If the catalog cannot be read or is newer than
            this LCSAS.
    """
    from lcsas.db.disc_catalog import open_disc_catalog

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
            f"The disc layout may be corrupted or this is not a data disc."
        )

    info = _read_volume_info(disc_path)
    uuid_ = str(info["uuid"])
    result = CatalogValidationResult(disc_path=disc_path)
    label = info.get("label")
    result.volume_label = label if isinstance(label, str) else ""

    # --- The catalog first: a disc whose catalog cannot be read fails
    #     before its data is walked. ---
    conn = open_disc_catalog(catalog_db)
    try:
        result.catalog_version = _schema_version(conn)
        row = conn.execute(
            "SELECT volume_id, label FROM volumes WHERE uuid = ?", (uuid_,)
        ).fetchone()
        if row is None:
            result.problems.append(
                f"the disc catalog does not list this disc's volume "
                f"(uuid {uuid_})"
            )
            catalog_hashes: set[str] = set()
        else:
            result.volume_label = str(row["label"])
            catalog_hashes = {
                str(r[0]).lower() for r in conn.execute(
                    "SELECT p.sha256 FROM volume_packs vp "
                    "JOIN packs p ON p.pack_id = vp.pack_id "
                    "WHERE vp.volume_id = ?", (row["volume_id"],),
                )
            }
    except sqlite3.DatabaseError as exc:
        raise ValueError(
            f"Could not read the pack lists in {catalog_db}: {exc}"
        ) from exc
    finally:
        conn.close()

    # --- The manifest.  A volume staged with no packs has none. ---
    manifest = info.get("sha256_manifest", [])
    if not isinstance(manifest, list):
        raise ValueError(
            f"volume_info.json at {disc_path} has a sha256_manifest that is "
            "not a list.  The disc may be damaged."
        )
    manifest_hashes = {str(h).lower() for h in manifest}

    # --- Walk disc data/ directory (ground truth) ---
    _logger.info("Scanning disc data directory: %s", data_dir)
    disc_pack_paths = _collect_disc_pack_paths(data_dir)
    disc_hashes = set(disc_pack_paths)
    result.disc_pack_count = len(disc_hashes)

    if content:
        # Read every pack on disc and check its content hashes to its name.
        _logger.info(
            "Content verification: hashing %d pack file(s) on disc...",
            len(disc_pack_paths),
        )
        for n, (expected, path) in enumerate(sorted(disc_pack_paths.items()), 1):
            actual = sha256_file(path)
            if actual != expected:
                _logger.error(
                    "Pack %s is CORRUPT on disc: content hashes to %s",
                    expected, actual,
                )
                result.corrupt_on_disc.append(expected)
            if n % 100 == 0 or n == len(disc_pack_paths):
                _logger.info(
                    "Content verification: %d/%d pack(s) hashed",
                    n, len(disc_pack_paths),
                )

    result.catalog_pack_count = len(catalog_hashes)
    result.manifest_pack_count = len(manifest_hashes)
    result.missing_from_disc = sorted(
        (catalog_hashes | manifest_hashes) - disc_hashes)
    result.orphaned_on_disc = sorted(
        disc_hashes - (catalog_hashes | manifest_hashes))
    result.not_in_catalog = sorted(manifest_hashes - catalog_hashes)
    result.not_in_manifest = sorted(catalog_hashes - manifest_hashes)

    return result


def _schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else None
