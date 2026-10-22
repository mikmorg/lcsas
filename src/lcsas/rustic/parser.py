"""Parsers for Rustic CLI JSON output.

These functions are pure: they take strings and return dataclass instances.
They can be tested independently with fixture JSON strings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from lcsas.rustic.types import BackupResult, PruneResult, RestorePlan, SnapshotInfo

_logger = logging.getLogger(__name__)


def parse_backup_output(output: str) -> BackupResult:
    """Parse the JSON output of ``rustic backup --json``.

    Handles both single-object and line-delimited JSON formats.
    Extracts the summary message from the output.
    """
    # Rustic may emit multiple JSON lines; find the summary
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Look for summary-like fields
        if isinstance(data, dict):
            snap_id = (
                data.get("snapshot_id", "")
                or data.get("id", "")
                or data.get("short_id", "")
            )
            if snap_id:
                return BackupResult(
                    snapshot_id=snap_id,
                    files_new=data.get("files_new", 0),
                    files_changed=data.get("files_changed", 0),
                    files_unmodified=data.get("files_unmodified", 0),
                    data_added_bytes=data.get("data_added", 0),
                    total_duration_secs=data.get("total_duration", 0.0),
                )

    # Fallback: couldn't parse structured output
    raise ValueError(
        f"Could not extract snapshot_id from {len(output.strip().splitlines())} "
        "line(s) of rustic output — output format may have changed or rustic failed"
    )


def parse_snapshots_output(output: str) -> list[SnapshotInfo]:
    """Parse the JSON output of ``rustic snapshots --json``."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        _logger.warning(
            "parse_snapshots_output: failed to parse JSON — "
            "rustic output format may have changed"
        )
        return []

    if not isinstance(data, list):
        data = [data]

    snapshots: list[SnapshotInfo] = []
    skipped = 0
    for item, group in _iter_snapshot_dicts(data):
        snapshot_id = item.get("id", item.get("short_id", "")) or ""
        if not snapshot_id:
            skipped += 1
            continue
        snapshots.append(SnapshotInfo(
            snapshot_id=snapshot_id,
            timestamp=item.get("time", ""),
            hostname=item.get("hostname") or group.get("hostname", ""),
            paths=item.get("paths") or group.get("paths", []),
            tags=item.get("tags", []),
        ))
    if skipped:
        _logger.warning(
            "parse_snapshots_output: skipped %d entr%s with no snapshot id — "
            "rustic output format may have changed",
            skipped, "y" if skipped == 1 else "ies",
        )

    return snapshots


def _iter_snapshot_dicts(
    data: list[object],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(snapshot, group_key)`` pairs from ``snapshots --json`` output.

    restic (and rustic with grouping off) prints a flat list of snapshots.
    rustic 0.11 groups by default: ``[{"group_key": {...}, "snapshots":
    [...]}]``, and some older rustic releases printed ``[[group_key,
    [snapshots]]]``.  A group object is not a snapshot (#536).
    """
    for item in data:
        group: object
        members: list[object]
        if isinstance(item, dict) and isinstance(item.get("snapshots"), list):
            group, members = item.get("group_key"), item["snapshots"]
        elif (isinstance(item, list) and len(item) == 2
              and isinstance(item[1], list)):
            group, members = item[0], item[1]
        elif isinstance(item, dict):
            yield item, {}
            continue
        else:
            continue
        group_key = group if isinstance(group, dict) else {}
        for snap in members:
            if isinstance(snap, dict):
                yield snap, group_key


def parse_restore_plan_output(
    snapshot_id: str,
    output: str,
) -> RestorePlan:
    """Parse the JSON output of ``rustic restore --dry-run --json``.

    Extracts the list of pack hashes required for the restore.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"parse_restore_plan_output: failed to parse JSON for snapshot {snapshot_id} — "
            f"rustic output format may have changed: {exc}"
        ) from exc

    if isinstance(data, dict):
        packs = data.get("packs", data.get("pack_ids", []))
        return RestorePlan(
            snapshot_id=snapshot_id,
            required_pack_hashes=packs if isinstance(packs, list) else [],
            total_size_bytes=data.get("total_size", 0),
            file_count=data.get("file_count", 0),
        )

    return RestorePlan(snapshot_id=snapshot_id)


def parse_prune_output(output: str) -> PruneResult:
    """Parse the JSON output of ``rustic prune --dry-run --json``."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        _logger.warning(
            "parse_prune_output: failed to parse JSON — "
            "rustic output format may have changed"
        )
        return PruneResult()

    if isinstance(data, dict):
        return PruneResult(
            packs_to_delete=data.get("packs_to_delete", []),
            packs_to_repack=data.get("packs_to_repack", []),
            space_freed_bytes=data.get("space_freed", 0),
        )

    return PruneResult()
