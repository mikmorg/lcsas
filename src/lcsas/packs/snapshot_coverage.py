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

The same walk answers "which packs does snapshot S need" for ``restore
plan``, ``restore exec`` and ``volume impact --snapshots``
(:func:`packs_for_snapshot`, #948).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lcsas.exceptions import RestoreError
from lcsas.restore.restic_fallback import (
    IntegrityError,
    NoKeyUnlockedError,
    PurePythonRestorer,
    quiet_log,
)
from lcsas.rustic.types import RestorePlan


@dataclass(frozen=True)
class LeftOutSnapshot:
    """A snapshot whose packs are not all held."""

    snapshot_id: str
    time: str
    missing_packs: int


@dataclass
class SnapshotCoverage:
    """The result for one repository.

    ``missing`` maps every snapshot ID to the pack IDs it needs that are
    not held (empty when it is complete).  ``stage`` uses it to decide,
    volume by volume, which snapshots the packs staged so far complete
    (#813).
    """

    complete: set[str] = field(default_factory=set)
    left_out: list[LeftOutSnapshot] = field(default_factory=list)
    missing: dict[str, frozenset[str]] = field(default_factory=dict)
    times: dict[str, str] = field(default_factory=dict)
    # #947: the packs (by the index) of the trees that could not be read.
    # When one of them is on a burned disc, the mirror is damaged.
    unreadable_tree_packs: set[str] = field(default_factory=set)


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
    index = restorer._blob_index or {}
    for snap, missing in per_snapshot:
        for m in missing:
            if m.startswith("?tree:"):
                loc = index.get(m[len("?tree:"):])
                if loc is not None:
                    result.unreadable_tree_packs.add(loc.pack_id)
        result.missing[snap.snapshot_id] = missing
        result.times[snap.snapshot_id] = snap.time
        if missing:
            result.left_out.append(LeftOutSnapshot(
                snapshot_id=snap.snapshot_id, time=snap.time,
                missing_packs=len(missing),
            ))
        else:
            result.complete.add(snap.snapshot_id)
    return result


# ── Which packs does one snapshot need (#948) ────────────────────


class SnapshotPacksError(RestoreError):
    """The packs of a snapshot cannot be worked out.  The message says
    why in words; ``wrong_password`` is True when a key file rejected
    the password."""

    def __init__(self, message: str, *, wrong_password: bool = False) -> None:
        super().__init__(message)
        self.wrong_password = wrong_password


@dataclass(frozen=True)
class SnapshotPacks:
    """The packs one snapshot needs.

    ``packs`` are real pack IDs.  ``unreadable`` are the trees that
    could not be read and the blobs the index does not know (the
    reader's ``?tree:``/``?blob:`` pseudo-packs): when it is not empty,
    ``packs`` may be short.
    """

    snapshot_id: str
    time: str
    packs: frozenset[str]
    unreadable: frozenset[str]


def snapshot_packs(mirror_path: Path, password_file: Path) -> list[SnapshotPacks]:
    """Every snapshot of the repository and the packs it needs, oldest
    first (by time, as "latest" is chosen).

    One walk of the snapshot trees with the tier-3 Python reader (only
    tree blobs are read); trees shared between snapshots are read once.
    ``volume impact --snapshots`` asks for every snapshot of a
    repository in one call.

    Raises:
        SnapshotPacksError: the repository cannot be read (wrong
            password, damaged key files, no index or snapshots).
    """
    try:
        restorer = PurePythonRestorer(
            mirror_path, password_file=password_file, interactive=False,
            strict=True,
        )
        with quiet_log():
            walked = restorer.missing_packs_by_snapshot(set())
    except NoKeyUnlockedError as exc:
        if exc.rejected:
            raise SnapshotPacksError(
                f"The password file {password_file} does not open the "
                f"repository at {mirror_path}: {exc}",
                wrong_password=True,
            ) from exc
        raise SnapshotPacksError(
            f"No key file of the repository at {mirror_path} can be "
            f"read: {exc}"
        ) from exc
    except (IntegrityError, OSError, ValueError, KeyError) as exc:
        raise SnapshotPacksError(
            f"Cannot read the repository at {mirror_path}: {exc}"
        ) from exc
    result = []
    for snap, needed in walked:
        pseudo = frozenset(p for p in needed if p.startswith("?"))
        result.append(SnapshotPacks(
            snapshot_id=snap.snapshot_id, time=snap.time,
            packs=frozenset(needed - pseudo), unreadable=pseudo,
        ))
    return result


def select_snapshot(
    snapshots: list[SnapshotPacks], wanted: str, where: Path,
) -> SnapshotPacks:
    """The snapshot *wanted* names: ``latest`` (the newest by time), a
    full ID, or an ID prefix that only one snapshot has, as rustic
    accepts.  *snapshots* are oldest first; *where* names the
    repository in the error.

    Raises:
        SnapshotPacksError: no snapshot, or none or several match.
    """
    if not snapshots:
        raise SnapshotPacksError(f"The repository at {where} has no snapshots.")
    if wanted == "latest":
        return snapshots[-1]
    for snap in snapshots:
        if snap.snapshot_id == wanted:
            return snap
    matches = [s for s in snapshots if s.snapshot_id.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise SnapshotPacksError(
            f"Snapshot '{wanted}' is ambiguous: {len(matches)} snapshots in "
            f"{where} start with it.  Give more of the ID."
        )
    raise SnapshotPacksError(
        f"No snapshot '{wanted}' in the repository at {where}.  "
        "'rustic snapshots' lists them."
    )


def packs_for_snapshot(
    mirror_path: Path, password_file: Path, snapshot: str,
) -> RestorePlan:
    """The packs snapshot *snapshot* of the mirror needs.

    *snapshot* is ``latest``, a full ID or a unique prefix; the plan
    carries the full ID it resolved to, which is what ``rustic restore``
    must then be given.  The walk reads the mirror's tree packs, which
    the mirror always holds.

    Raises:
        SnapshotPacksError: the repository cannot be read, the snapshot
            is not there, or some of its trees cannot be read (the plan
            would be short).
    """
    snap = select_snapshot(
        snapshot_packs(mirror_path, password_file), snapshot, mirror_path,
    )
    if snap.unreadable:
        trees = sorted(p[len("?tree:"):] for p in snap.unreadable
                       if p.startswith("?tree:"))
        blobs = sorted(p[len("?blob:"):] for p in snap.unreadable
                       if p.startswith("?blob:"))
        parts = []
        if trees:
            parts.append(f"{len(trees)} tree(s) cannot be read "
                         f"(first: {trees[0][:12]})")
        if blobs:
            parts.append(f"{len(blobs)} blob(s) are in no index file "
                         f"(first: {blobs[0][:12]})")
        raise SnapshotPacksError(
            f"Cannot tell which packs snapshot {snap.snapshot_id[:12]} "
            f"needs: {'; '.join(parts)}.  The mirror at {mirror_path} may be "
            "damaged: check it with 'rustic check'."
        )
    return RestorePlan(
        snapshot_id=snap.snapshot_id,
        required_pack_hashes=sorted(snap.packs),
    )
