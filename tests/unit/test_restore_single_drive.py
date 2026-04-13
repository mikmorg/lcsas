"""Unit tests for the bundled meta-volume single-drive restore helper.

The helper is stdlib-only and is invoked as a script from restore.sh on
the meta volume. These tests build a tiny synthetic catalog + on-disc
metadata layout, then drive the three phases (bootstrap, ingest,
finalize) via the in-process ``main()`` entry point.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lcsas.meta import restore_single_drive as helper

REPO_ID = "11111111-1111-1111-1111-111111111111"
REPO_NAME = "alpha"


def _make_catalog(db_path: Path, packs: list[tuple[str, int, str]], *,
                   created_at: str = "2026-01-01T00:00:00") -> None:
    """Create a minimal catalog with one repo and *packs*.

    Each pack tuple is (sha256, size, volume_label). Multiple rows for
    the same sha256 mean the pack lives on multiple volumes.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE volumes (
            volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE,
            status TEXT,
            created_at TEXT
        );
        CREATE TABLE packs (
            pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT UNIQUE,
            size_bytes INTEGER,
            repo_id TEXT,
            is_pruned INTEGER DEFAULT 0
        );
        CREATE TABLE volume_packs (
            volume_id INTEGER,
            pack_id INTEGER,
            PRIMARY KEY (volume_id, pack_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO repositories(repo_id, name) VALUES (?, ?)",
        (REPO_ID, REPO_NAME),
    )

    pack_ids: dict[str, int] = {}
    for sha, size, _label in packs:
        if sha in pack_ids:
            continue
        cur = conn.execute(
            "INSERT INTO packs(sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            (sha, size, REPO_ID),
        )
        pack_ids[sha] = cur.lastrowid

    vol_ids: dict[str, int] = {}
    for _sha, _size, label in packs:
        if label in vol_ids:
            continue
        cur = conn.execute(
            "INSERT INTO volumes(label, status, created_at) VALUES (?, 'BURNED', ?)",
            (label, created_at),
        )
        vol_ids[label] = cur.lastrowid

    for sha, _size, label in packs:
        conn.execute(
            "INSERT INTO volume_packs(volume_id, pack_id) VALUES (?, ?)",
            (vol_ids[label], pack_ids[sha]),
        )
    conn.commit()
    conn.close()


def _seed_disc(mount: Path, packs_on_disc: dict[str, bytes]) -> None:
    """Populate a fake mounted disc with metadata/ and data/ trees."""
    meta_root = mount / "metadata" / REPO_ID
    for sub in ("index", "snapshots", "keys"):
        (meta_root / sub).mkdir(parents=True)
        (meta_root / sub / "marker").write_text(sub)
    (meta_root / "config").write_text("config-blob")

    data_root = mount / "data"
    for sha, blob in packs_on_disc.items():
        prefix_dir = data_root / sha[:2]
        prefix_dir.mkdir(parents=True, exist_ok=True)
        (prefix_dir / sha).write_bytes(blob)


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_emits_pick_list_and_seeds_metadata(tmp_path, capsys):
    blob_a = b"alpha-pack"
    blob_b = b"bravo-pack"
    blob_c = b"charlie-pack"
    sha_a, sha_b, sha_c = _sha(blob_a), _sha(blob_b), _sha(blob_c)

    catalog = tmp_path / "catalog.db"
    _make_catalog(
        catalog,
        [
            (sha_a, len(blob_a), "LCSAS_CD_2026_0001"),
            (sha_b, len(blob_b), "LCSAS_CD_2026_0002"),
            # sha_c lives on both 0002 (primary) and 0003 (alternate)
            (sha_c, len(blob_c), "LCSAS_CD_2026_0002"),
            (sha_c, len(blob_c), "LCSAS_CD_2026_0003"),
        ],
    )

    mount = tmp_path / "disc"
    mount.mkdir()
    _seed_disc(mount, {sha_a: blob_a})

    cache = tmp_path / "cache"

    rc = helper.main(
        [
            "bootstrap",
            "--catalog", str(catalog),
            "--mount", str(mount),
            "--cache", str(cache),
            "--repo", REPO_NAME,
        ]
    )
    assert rc == 0

    pick_list = json.loads((cache / "pick-list.json").read_text())
    assert pick_list["repo"] == REPO_NAME
    assert pick_list["repo_id"] == REPO_ID
    assert pick_list["total_packs"] == 3

    labels = [v["label"] for v in pick_list["volumes"]]
    assert labels == ["LCSAS_CD_2026_0001", "LCSAS_CD_2026_0002"]

    # sha_c's primary is 0002 (lex-smaller), 0003 listed as alternate.
    assert pick_list["alternates"] == {sha_c: ["LCSAS_CD_2026_0003"]}

    # Metadata seeded into the cache
    for sub in ("index", "snapshots", "keys"):
        assert (cache / sub / "marker").read_text() == sub
    assert (cache / "config").read_text() == "config-blob"

    out = capsys.readouterr().out
    assert json.loads(out)["repo"] == REPO_NAME


def test_bootstrap_lists_repos_when_repo_omitted(tmp_path, capsys):
    catalog = tmp_path / "catalog.db"
    _make_catalog(catalog, [(_sha(b"x"), 1, "LCSAS_CD_2026_0001")])

    mount = tmp_path / "disc"
    mount.mkdir()

    cache = tmp_path / "cache"
    rc = helper.main(
        [
            "bootstrap",
            "--catalog", str(catalog),
            "--mount", str(mount),
            "--cache", str(cache),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert REPO_NAME in err


def test_bootstrap_unknown_repo_exits(tmp_path):
    catalog = tmp_path / "catalog.db"
    _make_catalog(catalog, [(_sha(b"x"), 1, "LCSAS_CD_2026_0001")])
    mount = tmp_path / "disc"
    mount.mkdir()

    with pytest.raises(SystemExit) as exc:
        helper.main(
            [
                "bootstrap",
                "--catalog", str(catalog),
                "--mount", str(mount),
                "--cache", str(tmp_path / "cache"),
                "--repo", "no-such-repo",
            ]
        )
    assert "no-such-repo" in str(exc.value)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def _bootstrap(tmp_path: Path, packs: list[tuple[str, int, str]],
               disc_blobs: dict[str, bytes]) -> Path:
    catalog = tmp_path / "catalog.db"
    _make_catalog(catalog, packs)
    mount = tmp_path / "boot_disc"
    mount.mkdir()
    _seed_disc(mount, disc_blobs)
    cache = tmp_path / "cache"
    helper.main(
        [
            "bootstrap",
            "--catalog", str(catalog),
            "--mount", str(mount),
            "--cache", str(cache),
            "--repo", REPO_NAME,
        ]
    )
    return cache


def test_ingest_copies_and_verifies_packs(tmp_path):
    blob_a = b"alpha-data"
    blob_b = b"bravo-data"
    sha_a, sha_b = _sha(blob_a), _sha(blob_b)

    cache = _bootstrap(
        tmp_path,
        [
            (sha_a, len(blob_a), "LCSAS_CD_2026_0001"),
            (sha_b, len(blob_b), "LCSAS_CD_2026_0002"),
        ],
        disc_blobs={sha_a: blob_a},
    )

    disc2 = tmp_path / "disc2"
    disc2.mkdir()
    _seed_disc(disc2, {sha_b: blob_b})

    rc = helper.main(
        [
            "ingest",
            "--mount", str(disc2),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0002",
        ]
    )
    assert rc == 0
    assert (cache / "data" / sha_b[:2] / sha_b).read_bytes() == blob_b


def test_ingest_detects_corruption(tmp_path):
    blob = b"good-pack"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={},
    )

    bad_disc = tmp_path / "bad_disc"
    bad_disc.mkdir()
    # Plant a file under the right SHA name but with wrong contents.
    _seed_disc(bad_disc, {sha: b"corrupted-bytes"})

    rc = helper.main(
        [
            "ingest",
            "--mount", str(bad_disc),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    assert rc == 2
    assert not (cache / "data" / sha[:2] / sha).exists()


def test_ingest_unrelated_disc_is_noop(tmp_path):
    blob = b"x"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={sha: blob},
    )
    # Disc label is not in the pick list — ingest should succeed and do nothing.
    other = tmp_path / "other_disc"
    other.mkdir()
    rc = helper.main(
        [
            "ingest",
            "--mount", str(other),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_9999",
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def test_finalize_succeeds_on_complete_cache(tmp_path):
    blob = b"complete"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={sha: blob},
    )
    # Bootstrap doesn't ingest — do it ourselves so finalize sees it.
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )

    rc = helper.main(["finalize", "--cache", str(cache)])
    assert rc == 0


def test_finalize_reports_missing_packs_by_disc(tmp_path, capsys):
    blob_a = b"a"
    blob_b = b"b"
    sha_a, sha_b = _sha(blob_a), _sha(blob_b)

    cache = _bootstrap(
        tmp_path,
        [
            (sha_a, 1, "LCSAS_CD_2026_0001"),
            (sha_b, 1, "LCSAS_CD_2026_0002"),
        ],
        disc_blobs={sha_a: blob_a},
    )
    # Only ingest disc 0001; leave disc 0002 missing.
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )

    rc = helper.main(["finalize", "--cache", str(cache)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "LCSAS_CD_2026_0002" in err


# ---------------------------------------------------------------------------
# finalize — integrity verification (improvement 1)
# ---------------------------------------------------------------------------


def test_finalize_detects_corrupted_pack_in_cache(tmp_path, capsys):
    """A pack that was truncated/corrupted after ingest should be caught."""
    blob = b"good-data-here"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={sha: blob},
    )
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    # Corrupt the cached pack after ingest.
    pack_path = cache / "data" / sha[:2] / sha
    assert pack_path.is_file()
    pack_path.write_bytes(b"corrupted!")

    rc = helper.main(["finalize", "--cache", str(cache)])
    assert rc == 1  # recoverable — primary disc still available
    err = capsys.readouterr().err
    assert "CORRUPTED" in err
    # The corrupted file should be removed.
    assert not pack_path.is_file()


def test_finalize_skips_integrity_check_when_disabled(tmp_path, capsys):
    """--no-verify-integrity should skip SHA-256 re-check."""
    blob = b"data"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={sha: blob},
    )
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    # Corrupt the pack — finalize should NOT catch it with --no-verify.
    pack_path = cache / "data" / sha[:2] / sha
    pack_path.write_bytes(b"bad")

    rc = helper.main(["finalize", "--cache", str(cache), "--no-verify-integrity"])
    assert rc == 0  # passes because it only checks existence


# ---------------------------------------------------------------------------
# state file (improvement 2)
# ---------------------------------------------------------------------------


def test_ingest_writes_state_file(tmp_path):
    blob = b"state-test"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={sha: blob},
    )
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    state_path = cache / "restore-state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert state["phase"] == "ingest"
    assert "LCSAS_CD_2026_0001" in state["volumes_completed"]
    assert state["packs_ingested"] >= 1
    assert state["packs_total"] == 1
    assert "started_at" in state
    assert "last_updated" in state


def test_state_file_accumulates_across_discs(tmp_path):
    blob_a = b"aa"
    blob_b = b"bb"
    sha_a, sha_b = _sha(blob_a), _sha(blob_b)
    cache = _bootstrap(
        tmp_path,
        [
            (sha_a, len(blob_a), "LCSAS_CD_2026_0001"),
            (sha_b, len(blob_b), "LCSAS_CD_2026_0002"),
        ],
        disc_blobs={sha_a: blob_a},
    )
    # Ingest disc 1
    helper.main(
        [
            "ingest",
            "--mount", str(tmp_path / "boot_disc"),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    state = json.loads((cache / "restore-state.json").read_text())
    assert state["volumes_completed"] == ["LCSAS_CD_2026_0001"]

    # Ingest disc 2
    disc2 = tmp_path / "disc2"
    disc2.mkdir()
    _seed_disc(disc2, {sha_b: blob_b})
    helper.main(
        [
            "ingest",
            "--mount", str(disc2),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0002",
        ]
    )
    state = json.loads((cache / "restore-state.json").read_text())
    assert state["volumes_completed"] == [
        "LCSAS_CD_2026_0001", "LCSAS_CD_2026_0002",
    ]
    assert state["packs_ingested"] == 2


def test_state_records_corrupt_packs(tmp_path):
    blob = b"good"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={},
    )
    bad_disc = tmp_path / "bad_disc"
    bad_disc.mkdir()
    _seed_disc(bad_disc, {sha: b"corrupt"})

    helper.main(
        [
            "ingest",
            "--mount", str(bad_disc),
            "--cache", str(cache),
            "--disc-label", "LCSAS_CD_2026_0001",
        ]
    )
    state = json.loads((cache / "restore-state.json").read_text())
    assert sha in state.get("corrupt_packs", {})
    assert state["corrupt_packs"][sha] == "LCSAS_CD_2026_0001"


# ---------------------------------------------------------------------------
# recoverable vs unrecoverable (improvement 3)
# ---------------------------------------------------------------------------


def test_finalize_recoverable_when_alternates_exist(tmp_path, capsys):
    """Missing pack with an alternate disc → exit 1 (recoverable)."""
    blob = b"recoverable"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [
            (sha, len(blob), "LCSAS_CD_2026_0001"),
            (sha, len(blob), "LCSAS_CD_2026_0002"),  # alternate
        ],
        disc_blobs={},  # don't ingest anything
    )
    rc = helper.main(["finalize", "--cache", str(cache), "--no-verify-integrity"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "RECOVERABLE" in err


def test_finalize_unrecoverable_when_no_alternates(tmp_path, capsys):
    """Missing pack with no alternates and corrupt primary → exit 3."""
    blob = b"doomed"
    sha = _sha(blob)
    cache = _bootstrap(
        tmp_path,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        disc_blobs={},
    )
    # Simulate that the primary disc was already tried and corrupted.
    helper._write_state(cache, {
        "corrupt_packs": {sha: "LCSAS_CD_2026_0001"},
    })

    rc = helper.main(["finalize", "--cache", str(cache), "--no-verify-integrity"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "UNRECOVERABLE" in err
    assert "cannot complete" in err


# ---------------------------------------------------------------------------
# disc verification (improvement 5)
# ---------------------------------------------------------------------------


def test_verify_disc_passes_on_matching_label(tmp_path):
    mount = tmp_path / "disc"
    mount.mkdir()
    (mount / "volume_info.json").write_text(
        json.dumps({"label": "LCSAS_CD_2026_0001"})
    )
    assert helper.verify_disc(mount, "LCSAS_CD_2026_0001") is True


def test_verify_disc_fails_on_mismatched_label(tmp_path, capsys):
    mount = tmp_path / "disc"
    mount.mkdir()
    (mount / "volume_info.json").write_text(
        json.dumps({"label": "LCSAS_CD_2026_0099"})
    )
    assert helper.verify_disc(mount, "LCSAS_CD_2026_0001") is False
    err = capsys.readouterr().err
    assert "mismatch" in err


def test_verify_disc_passes_when_no_volume_info(tmp_path):
    mount = tmp_path / "disc"
    mount.mkdir()
    assert helper.verify_disc(mount, "LCSAS_CD_2026_0001") is True


# ---------------------------------------------------------------------------
# catalog_freshness (organic upgrade support)
# ---------------------------------------------------------------------------


def test_bootstrap_includes_catalog_freshness(tmp_path, capsys):
    """pick-list.json should contain catalog_freshness from MAX(created_at)."""
    blob = b"fresh"
    sha = _sha(blob)
    catalog = tmp_path / "catalog.db"
    _make_catalog(
        catalog,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        created_at="2026-04-10T12:00:00",
    )
    mount = tmp_path / "disc"
    mount.mkdir()
    _seed_disc(mount, {sha: blob})
    cache = tmp_path / "cache"

    rc = helper.main([
        "bootstrap",
        "--catalog", str(catalog),
        "--mount", str(mount),
        "--cache", str(cache),
        "--repo", REPO_NAME,
    ])
    assert rc == 0
    pick_list = json.loads((cache / "pick-list.json").read_text())
    assert pick_list["catalog_freshness"] == "2026-04-10T12:00:00"

    out = json.loads(capsys.readouterr().out)
    assert out["catalog_freshness"] == "2026-04-10T12:00:00"


def test_catalog_freshness_picks_max_across_volumes(tmp_path, capsys):
    """Freshness should be the MAX across all volumes, not the first."""
    blob_a = b"a"
    blob_b = b"b"
    sha_a, sha_b = _sha(blob_a), _sha(blob_b)

    catalog = tmp_path / "catalog.db"
    # Build catalog manually so we can set different created_at per volume.
    conn = sqlite3.connect(catalog)
    conn.executescript("""
        CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE volumes (
            volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE, status TEXT, created_at TEXT
        );
        CREATE TABLE packs (
            pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT UNIQUE, size_bytes INTEGER, repo_id TEXT,
            is_pruned INTEGER DEFAULT 0
        );
        CREATE TABLE volume_packs (
            volume_id INTEGER, pack_id INTEGER,
            PRIMARY KEY (volume_id, pack_id)
        );
    """)
    conn.execute(
        "INSERT INTO repositories(repo_id, name) VALUES (?, ?)",
        (REPO_ID, REPO_NAME),
    )
    conn.execute(
        "INSERT INTO volumes(label, status, created_at) VALUES (?, 'BURNED', ?)",
        ("LCSAS_CD_2026_0001", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO volumes(label, status, created_at) VALUES (?, 'BURNED', ?)",
        ("LCSAS_CD_2026_0002", "2026-04-12T18:30:00"),
    )
    conn.execute(
        "INSERT INTO packs(sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        (sha_a, len(blob_a), REPO_ID),
    )
    conn.execute(
        "INSERT INTO packs(sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        (sha_b, len(blob_b), REPO_ID),
    )
    conn.execute(
        "INSERT INTO volume_packs(volume_id, pack_id) VALUES (1, 1)",
    )
    conn.execute(
        "INSERT INTO volume_packs(volume_id, pack_id) VALUES (2, 2)",
    )
    conn.commit()
    conn.close()

    mount = tmp_path / "disc"
    mount.mkdir()
    _seed_disc(mount, {sha_a: blob_a})
    cache = tmp_path / "cache"

    helper.main([
        "bootstrap",
        "--catalog", str(catalog),
        "--mount", str(mount),
        "--cache", str(cache),
        "--repo", REPO_NAME,
    ])
    pick_list = json.loads((cache / "pick-list.json").read_text())
    assert pick_list["catalog_freshness"] == "2026-04-12T18:30:00"


# ---------------------------------------------------------------------------
# --reseed (re-bootstrap with validate-then-delete)
# ---------------------------------------------------------------------------


def test_reseed_clears_and_reseeds_metadata(tmp_path, capsys):
    """--reseed should clear stale metadata and re-seed from new disc."""
    blob = b"original"
    sha = _sha(blob)

    # Initial bootstrap.
    catalog_v1 = tmp_path / "catalog_v1.db"
    _make_catalog(
        catalog_v1,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
        created_at="2026-01-01T00:00:00",
    )
    mount1 = tmp_path / "disc1"
    mount1.mkdir()
    _seed_disc(mount1, {sha: blob})
    cache = tmp_path / "cache"

    helper.main([
        "bootstrap",
        "--catalog", str(catalog_v1),
        "--mount", str(mount1),
        "--cache", str(cache),
        "--repo", REPO_NAME,
    ])
    assert (cache / "index" / "marker").read_text() == "index"

    # Now reseed with a new catalog from a different disc.
    blob2 = b"newer"
    sha2 = _sha(blob2)
    catalog_v2 = tmp_path / "catalog_v2.db"
    _make_catalog(
        catalog_v2,
        [
            (sha, len(blob), "LCSAS_CD_2026_0001"),
            (sha2, len(blob2), "LCSAS_CD_2026_0002"),
        ],
        created_at="2026-04-12T00:00:00",
    )
    mount2 = tmp_path / "disc2"
    mount2.mkdir()
    # Seed with different metadata content to verify it gets re-copied.
    meta_root = mount2 / "metadata" / REPO_ID
    for sub in ("index", "snapshots", "keys"):
        (meta_root / sub).mkdir(parents=True)
        (meta_root / sub / "marker").write_text(f"v2-{sub}")
    (meta_root / "config").write_text("v2-config")
    data_root = mount2 / "data"
    prefix_dir = data_root / sha2[:2]
    prefix_dir.mkdir(parents=True, exist_ok=True)
    (prefix_dir / sha2).write_bytes(blob2)

    rc = helper.main([
        "bootstrap",
        "--catalog", str(catalog_v2),
        "--mount", str(mount2),
        "--cache", str(cache),
        "--repo", REPO_NAME,
        "--reseed",
    ])
    assert rc == 0

    # Metadata should be from v2.
    assert (cache / "index" / "marker").read_text() == "v2-index"
    assert (cache / "config").read_text() == "v2-config"

    # Pick list should now include both packs.
    pick_list = json.loads((cache / "pick-list.json").read_text())
    assert pick_list["total_packs"] == 2
    assert pick_list["catalog_freshness"] == "2026-04-12T00:00:00"


def test_reseed_fails_gracefully_on_missing_repo(tmp_path, capsys):
    """--reseed with a catalog that doesn't contain the repo should fail
    and leave existing metadata intact."""
    blob = b"keepme"
    sha = _sha(blob)

    # Initial bootstrap.
    catalog_v1 = tmp_path / "catalog_v1.db"
    _make_catalog(
        catalog_v1,
        [(sha, len(blob), "LCSAS_CD_2026_0001")],
    )
    mount = tmp_path / "disc"
    mount.mkdir()
    _seed_disc(mount, {sha: blob})
    cache = tmp_path / "cache"

    helper.main([
        "bootstrap",
        "--catalog", str(catalog_v1),
        "--mount", str(mount),
        "--cache", str(cache),
        "--repo", REPO_NAME,
    ])
    original_index = (cache / "index" / "marker").read_text()

    # Reseed with a catalog that has a different repo (not "alpha").
    bad_catalog = tmp_path / "bad_catalog.db"
    conn = sqlite3.connect(bad_catalog)
    conn.executescript("""
        CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE volumes (
            volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE, status TEXT, created_at TEXT
        );
        CREATE TABLE packs (
            pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT UNIQUE, size_bytes INTEGER, repo_id TEXT,
            is_pruned INTEGER DEFAULT 0
        );
        CREATE TABLE volume_packs (
            volume_id INTEGER, pack_id INTEGER,
            PRIMARY KEY (volume_id, pack_id)
        );
    """)
    conn.execute(
        "INSERT INTO repositories(repo_id, name) VALUES ('other-id', 'other-repo')",
    )
    conn.commit()
    conn.close()

    rc = helper.main([
        "bootstrap",
        "--catalog", str(bad_catalog),
        "--mount", str(mount),
        "--cache", str(cache),
        "--repo", REPO_NAME,
        "--reseed",
    ])
    assert rc == 1  # failed validation

    # Original metadata should be untouched.
    assert (cache / "index" / "marker").read_text() == original_index
    err = capsys.readouterr().err
    assert "repo not found" in err
