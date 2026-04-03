"""Bin packing algorithm for fitting data packs onto fixed-capacity media."""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def first_fit_decreasing(
    items: list[tuple[str, int]],
    capacity: int,
    reserved: int = 0,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Select items to fill a bin using First-Fit Decreasing.

    Args:
        items: List of (identifier, size_bytes) tuples.
        capacity: Total bin capacity in bytes.
        reserved: Bytes reserved for metadata/overhead (subtracted from capacity).

    Returns:
        (selected, remaining): Two lists of (identifier, size_bytes) tuples.
        ``selected`` fits within (capacity - reserved).
        ``remaining`` is everything that didn't fit.
    """
    usable = capacity - reserved
    if usable <= 0:
        return [], list(items)

    # Sort by size descending, then by identifier ascending as tiebreaker
    # to ensure deterministic output across runs.
    sorted_items = sorted(items, key=lambda x: (-x[1], x[0]))

    # Log an error if any item exceeds usable capacity — it will never fit on
    # any single volume and will always land in `remaining`.  Callers should
    # treat oversized items in `remaining` as a fatal configuration error.
    oversized_count = sum(1 for _, sz in sorted_items if sz > usable)
    if oversized_count:
        item_id, item_size = sorted_items[0]
        _logger.error(
            "%d item(s) exceed usable capacity (%d bytes); largest: '%s' (%d bytes). "
            "These items will always land in `remaining` and can never be archived "
            "on this media type.",
            oversized_count, usable, item_id, item_size,
        )

    selected: list[tuple[str, int]] = []
    remaining: list[tuple[str, int]] = []
    current_fill = 0

    for item_id, size in sorted_items:
        if current_fill + size <= usable:
            selected.append((item_id, size))
            current_fill += size
        else:
            remaining.append((item_id, size))

    return selected, remaining


def estimate_volumes_needed(
    total_bytes: int,
    capacity: int,
    reserved: int = 0,
    ecc_overhead_pct: int = 0,
) -> int:
    """Estimate how many volumes are needed for a given data size.

    Args:
        total_bytes: Total data to store.
        capacity: Raw media capacity.
        reserved: Per-volume bytes reserved for metadata.
        ecc_overhead_pct: Percentage of capacity lost to error correction.

    Returns:
        Number of volumes needed (minimum 1 if total_bytes > 0).
    """
    if total_bytes <= 0:
        return 0

    usable = int(capacity * (100 - ecc_overhead_pct) / 100) - reserved
    if usable <= 0:
        raise ValueError(
            f"No usable capacity: capacity={capacity}, "
            f"ecc={ecc_overhead_pct}%, reserved={reserved}"
        )

    count = (total_bytes + usable - 1) // usable  # ceil division
    return max(1, count)
