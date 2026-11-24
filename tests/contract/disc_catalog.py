"""A disc catalog for the disc prompt's cases (#896), and the reading of
the discs a prompt names.

Both readers look up a pack that is on no disc in the drive in the disc
catalog and name the discs of the volumes holding it.  The rule is tier
1's (``recovery/docs/TIERS.txt``, "WHICH DISCS THE PROMPT NAMES"):
every volume that holds the pack and is not DESTROYED, newest volume
(highest volume number) first, at most 8.  The catalogs here are real
(``lcsas.db.schema``, the schema the burn writes), so that a lost
volume is lost by the catalog's own test of it (``get_lost_volume_ids``).

Used by the restore contract table (``restore_contract.py``) and by
``tests/unit/test_disc_search.py``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

HEADING = "| It is on any disc labelled:                              |"
#: What a prompt says when the catalog has the pack on no disc it offers,
#: and when the catalog has no record of the pack.
NONE_LISTED = "none listed"
NO_RECORD = "no record"
NO_PROMPT = "no prompt"
#: A prompt with no catalog in use (none given or found, or none that
#: could be read): "(no disc catalog yet: insert your newest data disc)".
NO_CATALOG = "no catalog"
NEWER_THAN = "| It is on a disc newer than:                              |"


def newer_than(label: str) -> str:
    """What a prompt says when the catalog has the pack on no disc it
    offers and names *label*, the newest disc it lists, as the one the
    pack is newer than (#957)."""
    return f"{NONE_LISTED}; newer than {label}"


@dataclass(frozen=True)
class Vol:
    """A volume (holding the pack unless ``holds=False``), as the
    catalog records it.

    ``disc`` is the status of its one disc record (``volume_copies``),
    ``None`` for a volume with no disc recorded.  ``lost`` makes the
    volume lost the way ``lcsas copy destroy`` of its last disc does
    (ADR 0003): DEPRECATED, the disc DESTROYED and the
    ``auto-deprecated:`` note.
    """

    label: str
    status: str = "VERIFIED"
    disc: str | None = "ACTIVE"
    lost: bool = False
    holds: bool = True


def write_catalog(path: Path, packs: Sequence[str], vols: Sequence[Vol],
                  *, knows_pack: bool = True, repo_id: str = "repo-1",
                  mark: int = 0) -> None:
    """A schema-v10 catalog with *vols* (volume_id 1, 2, ... in order)
    each holding every pack in *packs* (but a ``holds=False`` one).  With ``knows_pack=False`` the
    catalog has the volumes but no record of those packs (they hold an
    unrelated one instead).  The packs belong to backup set *repo_id*
    (a reader takes it from its ``--repo`` folder's name, #950), and
    *mark* is the label mark (``PRAGMA user_version``)."""
    from lcsas.db import schema
    from lcsas.db.queries import AUTO_DEPRECATED_NOTE_PREFIX, get_lost_volume_ids

    conn = sqlite3.connect(path)
    try:
        schema.create_all(conn)
        conn.execute("INSERT INTO repositories (repo_id, name, mirror_path) "
                     "VALUES (?, 'one', '/srv/one')", (repo_id,))
        conn.execute("INSERT OR IGNORE INTO locations (name) "
                     "VALUES ('Home_Shelf')")
        held = list(packs) if knows_pack else ["0f" * 32]
        pack_ids = [
            conn.execute("INSERT INTO packs (sha256, size_bytes, repo_id) "
                         "VALUES (?, 4096, ?)", (sha, repo_id)).lastrowid
            for sha in held
        ]
        conn.execute(f"PRAGMA user_version = {int(mark)}")
        for n, vol in enumerate(vols, start=1):
            status = "DEPRECATED" if vol.lost else vol.status
            disc = "DESTROYED" if vol.lost else vol.disc
            conn.execute(
                "INSERT INTO volumes (volume_id, label, uuid, media_type, "
                "capacity_bytes, status) VALUES (?, ?, ?, 'BD25', 1, ?)",
                (n, vol.label, f"uuid-{n}", status),
            )
            if disc is not None:
                conn.execute(
                    "INSERT INTO volume_copies (volume_id, location, status, "
                    "burn_date) VALUES (?, 'Home_Shelf', ?, '2026-01-01')",
                    (n, disc),
                )
            if vol.lost:
                conn.execute(
                    "INSERT INTO volume_events (volume_id, event_type, detail) "
                    "VALUES (?, 'NOTE', ?)",
                    (n, AUTO_DEPRECATED_NOTE_PREFIX + " last in-service disc"),
                )
            conn.executemany(
                "INSERT INTO volume_packs (volume_id, pack_id) VALUES (?, ?)",
                [(n, pid) for pid in pack_ids] if vol.holds else [],
            )
        conn.commit()
        lost = {n for n, v in enumerate(vols, start=1) if v.lost}
        assert get_lost_volume_ids(conn) == lost, "the fixture's lost volumes"
    finally:
        conn.close()


def named_discs(output: str) -> tuple[str, ...] | str:
    """What the first disc prompt in *output* names: the labels under
    its "It is on any disc labelled:" heading, in order, or
    ``NONE_LISTED`` (``newer_than(label)`` when the prompt names the
    disc the pack is newer than) / ``NO_RECORD`` / ``NO_CATALOG`` /
    ``NO_PROMPT``."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line == HEADING:
            labels = []
            for row in lines[i + 1:]:
                label = row[4:-1].strip()
                if not (row.startswith("|   ") and row.endswith("|")
                        and label):
                    break      # the box's blank line ends the list
                labels.append(label)
            return tuple(labels)
        if "lists no disc for it" in line:
            rest = lines[i + 1:i + 3]
            if len(rest) == 2 and rest[0] == NEWER_THAN:
                return newer_than(rest[1][4:-1].strip())
            return NONE_LISTED
        if "catalog has no record of this pack" in line:
            return NO_RECORD
        if "(no disc catalog yet: insert your newest data disc)" in line:
            return NO_CATALOG
    return NO_PROMPT
