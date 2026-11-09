"""#635: tier 3 must name the disc it needs after at most one disc insert.

The rc6 blind sweep (variant ``tier1-tier2-missing``) cycled discs blindly
for 14 minutes.  Tiers 1 and 2 were missing, the agent skipped the
newest-disc step, and tier 3 (the Python reader) started with only the
META disc in, so no catalog was reachable.  It printed "(no catalog
available)" with no advice.  Then, with a data disc in, it used that
disc's catalog, and the catalog on disc N lists only discs 1..N, so the
packs on later discs printed "(catalog has the pack, but lists no disc
for it)".  After each swap the catalog left with the disc.

What these pin, with real SQLite catalogs shaped like the ones the burn
writes (each disc's catalog lists the discs burned up to and including
itself, and every pack scanned so far):

  * no catalog: the prompt says to insert any data disc, the newest if
    known;
  * an older disc's catalog: the prompt names the newest disc it lists
    and says the pack is on a newer one;
  * after the newest disc has been in once, every pack names its disc,
    even when that disc is swapped out again (the catalog is copied).

This runs the prompt code only: no rustic, xorriso or cdemu.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from lcsas.restore.restic_fallback import PurePythonRestorer

LABELS = [f"LCSAS_CD700_2026_000{n}" for n in (1, 2, 3)]
# One pack per disc.
PACKS = [f"{n:02x}" * 32 for n in (0xA1, 0xB2, 0xC3)]


def _disc_catalog(path: Path, upto: int) -> None:
    """The catalog burned onto disc ``upto`` (1-based): discs 1..upto
    are listed with their packs, and all three packs are known (the scan
    registers packs before they are packed onto a disc)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
                              label TEXT UNIQUE NOT NULL,
                              status TEXT NOT NULL DEFAULT 'STAGING');
        CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sha256 TEXT UNIQUE NOT NULL);
        CREATE TABLE volume_packs (volume_id INTEGER NOT NULL,
                                   pack_id INTEGER NOT NULL);
    """)
    for sha in PACKS:
        conn.execute("INSERT INTO packs (sha256) VALUES (?)", (sha,))
    for i in range(upto):
        conn.execute("INSERT INTO volumes (label) VALUES (?)", (LABELS[i],))
        conn.execute(
            "INSERT INTO volume_packs VALUES (?, ?)", (i + 1, i + 1)
        )
    conn.commit()
    conn.close()


class Drive:
    """One optical drive: a mount point that holds at most one disc."""

    def __init__(self, tmp_path: Path) -> None:
        self.mount = tmp_path / "media" / "cdrom"
        self.mount.mkdir(parents=True)
        self.store = tmp_path / "discs"
        for n in (1, 2, 3):
            _disc_catalog(self.store / LABELS[n - 1] / "catalog.db", n)

    def insert(self, n: int | None) -> None:
        shutil.rmtree(self.mount)
        if n is None:
            self.mount.mkdir()
        else:
            shutil.copytree(self.store / LABELS[n - 1], self.mount)


def _restorer(tmp_path: Path, drive: Drive) -> PurePythonRestorer:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    return PurePythonRestorer(
        repo_path=repo, password=b"unused",
        pack_search_paths=[drive.mount], interactive=True,
    )


def _prompt(r: PurePythonRestorer, pack: str, capsys) -> str:
    capsys.readouterr()
    r._print_swap_prompt(pack)
    return capsys.readouterr().err


def _named(box: str) -> list[str]:
    """Labels under "It is on any disc labelled:"."""
    if "It is on any disc labelled:" not in box:
        return []
    tail = box.split("It is on any disc labelled:", 1)[1]
    tail = tail.split("Currently searching:", 1)[0]
    return [lbl for lbl in LABELS if lbl in tail]


def test_no_catalog_says_to_insert_a_data_disc(tmp_path: Path, capsys) -> None:
    drive = Drive(tmp_path)
    drive.insert(None)          # only the META disc: no catalog anywhere
    box = _prompt(_restorer(tmp_path, drive), PACKS[2], capsys)
    assert "no catalog available" in box
    assert "Insert any data disc, your newest if you know which:" in box


def test_older_disc_catalog_points_past_its_newest_disc(
    tmp_path: Path, capsys,
) -> None:
    drive = Drive(tmp_path)
    drive.insert(1)
    box = _prompt(_restorer(tmp_path, drive), PACKS[2], capsys)
    assert "lists no disc for it" in box
    assert "It is on a disc newer than:" in box
    newer = box.split("It is on a disc newer than:", 1)[1].splitlines()[1]
    assert LABELS[0] in newer, box
    assert "Insert your newest data disc" in box


def test_one_insert_of_the_newest_disc_names_every_pack(
    tmp_path: Path, capsys,
) -> None:
    drive = Drive(tmp_path)
    drive.insert(None)
    r = _restorer(tmp_path, drive)
    assert _named(_prompt(r, PACKS[2], capsys)) == []
    drive.insert(3)             # the one extra insert: the newest disc
    for i, pack in enumerate(PACKS):
        assert _named(_prompt(r, pack, capsys)) == [LABELS[i]]


def test_disc_names_survive_swapping_the_newest_disc_out(
    tmp_path: Path, capsys,
) -> None:
    """The catalog read from disc 3 must still name discs after disc 3
    is swapped for disc 1, whose own catalog lists only disc 1."""
    drive = Drive(tmp_path)
    r = _restorer(tmp_path, drive)
    drive.insert(3)
    assert _named(_prompt(r, PACKS[2], capsys)) == [LABELS[2]]
    drive.insert(1)
    for i, pack in enumerate(PACKS):
        box = _prompt(r, pack, capsys)
        assert _named(box) == [LABELS[i]], box
        assert "lists no disc for it" not in box


def test_every_new_line_fits_the_60_column_box(tmp_path: Path, capsys) -> None:
    drive = Drive(tmp_path)
    for disc in (None, 1):
        drive.insert(disc)
        box = _prompt(_restorer(tmp_path / f"r{disc}", drive), PACKS[2], capsys)
        lines = box[box.index("+---"): box.rindex("+") + 1].split("\n")
        for line in lines:
            assert len(line) == 60, f"{len(line)} columns: {line!r}"
