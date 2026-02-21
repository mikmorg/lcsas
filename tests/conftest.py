"""Shared pytest fixtures for LCSAS tests."""

from __future__ import annotations

import shutil

import pytest

from lcsas.config.media import MediaType
from lcsas.config.settings import default_config
from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid

# ---------------------------------------------------------------------------
# Markers for conditional test skipping
# ---------------------------------------------------------------------------

requires_rustic = pytest.mark.skipif(
    not shutil.which("rustic"),
    reason="rustic not installed",
)

requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"),
    reason="xorriso not installed",
)

requires_dvdisaster = pytest.mark.skipif(
    not shutil.which("dvdisaster"),
    reason="dvdisaster not installed",
)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_db():
    """In-memory SQLite connection with schema initialized.

    Registers a default '_test' repo so that ``register_pack()`` calls
    in tests always have a valid repo_id foreign key.
    """
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "_test", "Test", "/test")
    yield conn
    conn.close()


@pytest.fixture
def populated_db(memory_db):
    """In-memory DB populated with 3 repos, 5 volumes, 20 packs.

    Layout:
    - repo_family, repo_work, repo_friend
    - vol_1 (BD25, VERIFIED): packs 1-5
    - vol_2 (BD25, VERIFIED): packs 6-10
    - vol_3 (BD25, VERIFIED): packs 11-14
    - vol_4 (MDISC100, VERIFIED): packs 1-3 (redundant copies)
    - vol_5 (BD25, STAGING): empty (open)
    - Packs 15-20: unarchived (not on any volume)
    """
    conn = memory_db

    # Repos
    register_repo(conn, "repo_family", "Family", "/mnt/mirror/family")
    register_repo(conn, "repo_work", "Work", "/mnt/mirror/work")
    register_repo(conn, "repo_friend", "Friend", "/mnt/mirror/friend")

    # Create volumes
    vols = []
    for _i, (label, media, status) in enumerate([
        ("LCSAS_BD_2026_001", "BD25", "VERIFIED"),
        ("LCSAS_BD_2026_002", "BD25", "VERIFIED"),
        ("LCSAS_BD_2026_003", "BD25", "VERIFIED"),
        ("LCSAS_MD_2026_001", "MDISC100", "VERIFIED"),
        ("LCSAS_BD_2026_004", "BD25", "STAGING"),
    ], start=1):
        vol = create_volume(
            conn, label=label, uuid=generate_uuid(),
            media_type=media, capacity_bytes=25_000_000_000,
            status=status,
        )
        vols.append(vol)

    # Create 20 packs across repos
    packs = []
    for i in range(1, 21):
        repo = ["repo_family", "repo_work", "repo_friend"][(i - 1) % 3]
        p = register_pack(conn, sha256=f"pack_{i:04d}_hash", size_bytes=1000 * i, repo_id=repo)
        packs.append(p)

    # Link packs to volumes
    # Vol 1: packs 1-5
    bulk_link_packs(conn, vols[0].volume_id, [p.pack_id for p in packs[0:5]])
    # Vol 2: packs 6-10
    bulk_link_packs(conn, vols[1].volume_id, [p.pack_id for p in packs[5:10]])
    # Vol 3: packs 11-14
    bulk_link_packs(conn, vols[2].volume_id, [p.pack_id for p in packs[10:14]])
    # Vol 4: packs 1-3 (redundant)
    bulk_link_packs(conn, vols[3].volume_id, [p.pack_id for p in packs[0:3]])
    # Vol 5: empty
    # Packs 15-20: unarchived

    yield conn


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_config(tmp_path):
    """LCSASConfig using TEST_TINY media and tmp directories."""
    mirror = tmp_path / "mirror"
    staging = tmp_path / "staging"
    db_path = tmp_path / "archive.db"
    mirror.mkdir()
    staging.mkdir()
    return default_config(mirror, staging, db_path, MediaType.TEST_TINY)


# ---------------------------------------------------------------------------
# Filesystem fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_mirror(tmp_path):
    """Create a tmp directory mimicking a Rustic repository layout.

    Creates data/ with a two-level hash-prefix structure containing
    small fake pack files, plus index/, snapshots/, keys/, config.
    """
    repo = tmp_path / "mirror_repo"
    data_dir = repo / "data"

    # Create fake packs in two-level layout
    for i in range(1, 11):
        sha = f"{i:064x}"
        prefix = sha[:2]
        pack_dir = data_dir / prefix
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file = pack_dir / sha
        pack_file.write_bytes(b"x" * (100 * i))

    # Create metadata dirs
    for subdir in ["index", "snapshots", "keys"]:
        (repo / subdir).mkdir(parents=True)
        (repo / subdir / "dummy.json").write_text("{}")

    (repo / "config").write_text('{"version":2}')

    return repo
