"""Mirror damage: a pack file the mirror's index still needs is gone (#947).

``lcsas scan`` notices a pack the catalog knows that is no longer in the
mirror's ``data/``.  Two things look the same from the file list:

- **pruned**: rustic removed it.  The index no longer lists the pack, or
  lists it under ``packs_to_delete`` (rustic's two-step prune marks a
  pack first and deletes the file in a later prune).
- **damaged**: a disk fault, an fsck or a stray delete removed it.  The
  index still lists it as a live pack, so the repository still needs it.

Recording damage as a prune drops the pack from every later decision
(consolidation leaves it behind, and a lost tree pack takes a set's
snapshots off the next disc), so the two are told apart by reading the
index with the tier-3 Python reader, which needs the repository
password.  Index files only; no pack is read.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from lcsas.exceptions import StagingError
from lcsas.restore.restic_fallback import PurePythonRestorer, quiet_log


class MirrorDamageError(StagingError):
    """A mirror lost pack files that its index still needs (#947)."""


def live_pack_ids(mirror_path: Path, password_file: Path) -> set[str]:
    """Pack IDs the mirror's index lists as live packs.

    Read from every index file that no other index file supersedes, under
    ``packs`` only: ``packs_to_delete`` (marked by a prune, deleted by a
    later one) are not live.  Raises whatever the reader raises when the
    repository cannot be opened (wrong password, unreadable index).
    """
    restorer = PurePythonRestorer(
        mirror_path, password_file=password_file, interactive=False,
        strict=True,
    )
    with quiet_log():
        if not restorer.verify_key():
            raise ValueError("the password_file does not open the repository")
        index_dir = mirror_path / "index"
        docs: list[tuple[str, dict[str, object]]] = []
        superseded: set[str] = set()
        for f in sorted(index_dir.iterdir()):
            if not f.is_file():
                continue
            doc = restorer._read_json_file(f)
            docs.append((f.name, doc))
            superseded.update(doc.get("supersedes") or [])
    live: set[str] = set()
    for name, doc in docs:
        if name in superseded:
            continue
        for entry in doc.get("packs") or []:  # type: ignore[attr-defined]
            live.add(str(entry["id"]))
    return live


def discs_holding(
    conn: sqlite3.Connection, shas: Iterable[str],
) -> dict[str, list[str]]:
    """Pack SHA-256 -> labels of the burned volumes that hold it."""
    from lcsas.db.queries import DURABLE_VOLUME_STATUSES

    held: dict[str, list[str]] = {}
    statuses = ",".join("?" * len(DURABLE_VOLUME_STATUSES))
    for sha in shas:
        rows = conn.execute(
            f"""SELECT v.label FROM volumes v
                JOIN volume_packs vp ON vp.volume_id = v.volume_id
                JOIN packs p ON p.pack_id = vp.pack_id
                WHERE p.sha256 = ? AND v.status IN ({statuses})
                ORDER BY v.label""",
            (sha, *DURABLE_VOLUME_STATUSES),
        ).fetchall()
        held[sha] = [str(r[0]) for r in rows]
    return held


def damage_message(
    repo_name: str, discs: dict[str, list[str]], *, unreadable: bool = False,
) -> str:
    """The block ``scan`` and ``stage`` print for a damaged mirror.

    *discs* maps each lost pack to the labels of the discs holding it.
    *unreadable*: the pack file may be present but cannot be read (what
    ``stage`` sees when a tree cannot be read), not only missing.
    """
    n = len(discs)
    what = "missing or unreadable" if unreadable else "missing"
    files = "pack file is" if n == 1 else "pack files are"
    lines = [
        f"Mirror of '{repo_name}' is damaged: {n} {files} {what} but its "
        f"index still needs {'it' if n == 1 else 'them'}:"
    ]
    for sha, labels in sorted(discs.items()):
        where = (f"on disc(s) {', '.join(labels)}" if labels
                 else "on no disc")
        lines.append(f"  {sha}  (data/{sha[:2]}/{sha}, {where})")
    lines.append(
        "Copy each one back from a disc into the mirror's data/ folder "
        "(the path shown), then run 'rustic check', or repair the mirror; "
        f"until then new backups of '{repo_name}' may be incomplete."
    )
    return "\n".join(lines)
