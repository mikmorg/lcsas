"""#968 item 7: a wrong invocation gets a worded refusal, never
"Unexpected error".

``main()`` prints "Unexpected error: <exception>" for an exception that is
not an :class:`~lcsas.exceptions.LcsasError`, which reads as an internal
crash.  The reviewer's sweep of wrong invocations found 11 of 41 that did
so.  This sweep runs each one (and the rest of the reviewer's list as a
guard) against a real install and fails on any "Unexpected error" or
traceback; each row also names the words the refusal must carry.

Real rustic, real xorriso, every command through ``main()``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lcsas.cli.main import main
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

# The shape of a schema-v3 master catalog: below the oldest schema this
# build migrates (v5, #822).  Its volume_copies has no last_verified_at.
_V3_DDL = """
CREATE TABLE schema_version (version INTEGER NOT NULL,
                             applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);
INSERT INTO schema_version (version) VALUES (3);
CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT UNIQUE NOT NULL, uuid TEXT UNIQUE NOT NULL,
    media_type TEXT NOT NULL, capacity_bytes INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL DEFAULT 'Home_Shelf',
    status TEXT NOT NULL DEFAULT 'STAGING',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME, verified_at DATETIME);
CREATE TABLE locations (name TEXT PRIMARY KEY,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, description TEXT DEFAULT '');
CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT NOT NULL,
    mirror_path TEXT NOT NULL, encryption_key_id TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT UNIQUE NOT NULL, size_bytes INTEGER NOT NULL,
    repo_id TEXT NOT NULL, is_pruned INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE volume_packs (volume_id INTEGER NOT NULL,
    pack_id INTEGER NOT NULL, PRIMARY KEY (volume_id, pack_id));
CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
    hostname TEXT, timestamp TEXT, paths TEXT, tags TEXT, description TEXT);
CREATE TABLE volume_copies (id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL, location TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE', burn_date TEXT NOT NULL,
    notes TEXT DEFAULT '', UNIQUE(volume_id, location));
"""


@pytest.fixture(scope="module")
def inst(tmp_path_factory: pytest.TempPathFactory) -> ScratchInstall:
    """alpha backed up, registered and scanned; Home_Shelf registered."""
    root = tmp_path_factory.mktemp("refusals")
    inst = ScratchInstall(root, repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    # Not asserted: `init` may already register the default location.
    inst.lcsas("location", "add", "Home_Shelf")
    # A regular file: any path under it is one no command can create.
    (root / "a-file").write_text("not a folder\n")
    (root / "a-folder").mkdir()
    # A damaged master: not an SQLite file at all.
    (root / "damaged.db").write_bytes(b"\x13\x37" * 4096)
    conn = sqlite3.connect(root / "v3.db")
    conn.executescript(_V3_DDL)
    conn.close()
    return inst


# (argv, words the refusal must carry).  ROOT is the install's folder.
# The first rows are the reviewer's "Unexpected error" cases; the rest
# already refused in words and stay in the sweep as a guard.
_SWEEP: list[tuple[list[str], str]] = [
    # A damaged master catalog points at `catalog rebuild`.
    (["--catalog", "ROOT/damaged.db", "status"], "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "scan"], "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "stage"], "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "repo", "list"], "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "session", "list"],
     "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "location", "list"],
     "lcsas catalog rebuild"),
    (["--catalog", "ROOT/damaged.db", "volume", "impact", "X"],
     "lcsas catalog rebuild"),
    # A master below the oldest schema this build migrates.
    (["--catalog", "ROOT/v3.db", "status"], "schema is v3"),
    (["--catalog", "ROOT/v3.db", "repo", "list"], "schema is v3"),
    (["--catalog", "ROOT/v3.db", "session", "list"], "schema is v3"),
    # `session abort nosuch` already words it; burn now does too.
    (["burn", "--session", "nosuch", "--dry-run"],
     "Session 'nosuch' not found"),
    # Location names.
    (["location", "add", "a/b"], "unsafe characters"),
    (["location", "add", "x" * 129], "maximum length of 128"),
    (["location", "add", "   "], "must not be empty"),
    (["location", "rename", "Home_Shelf", "a/b"], "unsafe characters"),
    (["location", "rename", "Home_Shelf", "y" * 129], "maximum length of 128"),
    # Files and folders that cannot be read or written.
    (["password", "combine", "--share-file", "ROOT/a-folder"],
     "Is a directory"),
    (["password", "split", "--repo", "alpha", "--out", "ROOT/shares",
      "--password-file", "ROOT/a-folder"], "Is a directory"),
    (["estate", "card", "--output", "ROOT/a-file/card.txt"],
     "Not a directory"),
    (["recovery", "manifest", "--output", "ROOT/a-file/MANIFEST"],
     "Cannot use ROOT/a-file"),
    (["catalog", "rebuild", "--output", "ROOT/a-file/new.db",
      "ROOT/a-folder"], "ROOT/a-file"),
    # Already worded before #968.
    (["session", "abort", "nosuch"], "Session 'nosuch' not found"),
    (["location", "rename", "nosuch", "other"], "Location 'nosuch' not found"),
    (["location", "remove", "nosuch"], "Location 'nosuch' not found"),
    (["location", "status", "nosuch"], "Location 'nosuch' not found"),
    (["volume", "impact", "999"], "Volume '999' not found"),
    (["volume", "impact", "--snapshots", "LCSAS_9999"],
     "Volume 'LCSAS_9999' not found"),
    (["copy", "destroy", "LCSAS_9999", "Home_Shelf"],
     "Volume 'LCSAS_9999' not found"),
    (["copy", "deprecate", "LCSAS_9999", "Home_Shelf"],
     "Volume 'LCSAS_9999' not found"),
    (["pack", "unprune", "deadbeef"], "No pack matches prefix 'deadbeef'"),
    (["repo", "remove", "nosuch"], "Repository 'nosuch' not found"),
    (["repo", "retire", "nosuch"], "Repository 'nosuch' not found"),
    (["repo", "activate", "nosuch"], "Repository 'nosuch' not found"),
    (["repo", "add", "alpha", "ROOT/repos/alpha"], "already registered"),
    (["scan", "--repo", "nosuch"], "repository 'nosuch' not found in config"),
    (["stage", "--repo", "nosuch"], "None of the specified repositories"),
    (["stage", "--collection", "nosuch"], "Unknown collection 'nosuch'"),
    (["stage", "--for-location", "nosuch"], "is not registered"),
    (["stage", "--media", "NOSUCH"], "Unknown media type 'NOSUCH'"),
    (["burn-iso", "ROOT/missing.iso", "--verify-only"], "ISO file not found"),
    (["restore", "plan", "--repo", "nosuch", "abc"],
     "repository 'nosuch' not found in config"),
    (["restore", "plan", "--repo", "alpha", "nosuch"], "No snapshot 'nosuch'"),
    (["password", "card", "--repo", "nosuch"], "not defined in the config"),
    (["password", "split", "--repo", "nosuch"], "not defined in the config"),
    (["password", "unsplit", "--repo", "nosuch", "--yes"],
     "not registered in the catalog"),
    (["catalog", "validate", "ROOT/missing"], "Disc path does not exist"),
    (["meta", "verify", "ROOT/missing"], "is this really a meta disc"),
    (["session", "abort", "--volume", "999"], "Volume '999' not found"),
]


def _ids() -> list[str]:
    return [" ".join(a)[:60] for a, _w in _SWEEP]


@pytest.mark.parametrize(("argv", "words"), _SWEEP, ids=_ids())
def test_wrong_invocation_is_refused_in_words(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
    argv: list[str], words: str,
) -> None:
    root = str(inst.root)
    argv = [a.replace("ROOT", root) for a in argv]
    words = words.replace("ROOT", root)
    rc = main(["--config", str(inst.config), *argv])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Unexpected error" not in out, out
    assert "Traceback" not in out, out
    assert rc != 0, out
    assert words in out, out


def test_damaged_master_names_the_file_and_the_rebuild(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal says which file is damaged and how to get a catalog
    back, and leaves the file as it was."""
    damaged = inst.root / "damaged.db"
    before = damaged.read_bytes()
    assert main(["--catalog", str(damaged), "status"]) == 1
    out = capsys.readouterr().out
    assert f"The catalog at {damaged} is damaged or is not a catalog" in out
    assert "lcsas catalog rebuild --output" in out
    assert damaged.read_bytes() == before


def test_v3_master_is_refused_by_a_read_only_command(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    """`status` reads without migrating; it now meets the same refusal as
    `init` (#822) instead of "no such column: vc.last_verified_at"."""
    assert main(["--catalog", str(inst.root / "v3.db"), "status"]) == 1
    out = capsys.readouterr().out
    assert "no such column" not in out
    assert "This LCSAS build migrates catalogs from schema v5 on" in out


def test_location_refusals_change_nothing(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    before = inst.query("SELECT name, description FROM locations ORDER BY name")
    for name in ("a/b", "x" * 129):
        assert inst.lcsas("location", "add", "--description", "d", name) == 1
    # An existing name is not an error since #985 (init registers the
    # default location), and leaves the row as it was.
    assert inst.lcsas("location", "add", "--description", "d", "Home_Shelf") == 0
    after = inst.query("SELECT name, description FROM locations ORDER BY name")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    capsys.readouterr()


def test_unexpected_error_is_kept_for_real_bugs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fallback still exists: an exception LCSAS did not foresee is
    reported as one, not dressed up as an owner's mistake."""
    import lcsas.cli.commands.status as status_mod

    def _boom(_args: object) -> int:
        raise KeyError("internal")

    monkeypatch.setattr(status_mod, "_cmd_status", _boom)
    assert main(["--catalog", str(tmp_path / "c.db"), "status"]) == 1
    assert "Unexpected error" in capsys.readouterr().out
