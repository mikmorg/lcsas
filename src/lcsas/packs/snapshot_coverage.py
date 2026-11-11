"""Which snapshots are fully on discs (#657).

Every disc carries each repository's snapshot files.  A snapshot whose
packs are not all on discs is a trap for the person restoring: "latest"
picks it, and the restore then asks for a disc that does not exist.
``lcsas stage`` therefore puts on a disc only the snapshots whose packs
are all *held*: on a durable (burned) volume, or in the session being
staged.  The rest are left off and listed, and a later disc carries them
once their packs are burned.

Working out a snapshot's packs means walking its trees, which needs the
repository password; the walk uses the tier-3 Python reader on the
mirror.  Only tree blobs are read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lcsas.restore.restic_fallback import PurePythonRestorer, quiet_log


@dataclass(frozen=True)
class LeftOutSnapshot:
    """A snapshot whose packs are not all held."""

    snapshot_id: str
    time: str
    missing_packs: int


@dataclass
class SnapshotCoverage:
    """The result for one repository."""

    complete: set[str] = field(default_factory=set)
    left_out: list[LeftOutSnapshot] = field(default_factory=list)


def snapshot_coverage(
    mirror_path: Path, password_file: Path, held_packs: set[str],
) -> SnapshotCoverage:
    """Split the mirror's snapshots into fully held and left out.

    Args:
        mirror_path: The repository (index/, snapshots/, keys/, data/).
        password_file: The repository's password file.
        held_packs: Pack IDs that are on durable volumes or in the
            session being staged.

    Raises whatever the reader raises when the repository cannot be
    opened (wrong password, unreadable index); the caller decides.
    """
    restorer = PurePythonRestorer(
        mirror_path, password_file=password_file, interactive=False,
        strict=True,
    )
    result = SnapshotCoverage()
    with quiet_log():
        per_snapshot = restorer.missing_packs_by_snapshot(held_packs)
    for snap, missing in per_snapshot:
        if missing:
            result.left_out.append(LeftOutSnapshot(
                snapshot_id=snap.snapshot_id, time=snap.time,
                missing_packs=len(missing),
            ))
        else:
            result.complete.add(snap.snapshot_id)
    return result
