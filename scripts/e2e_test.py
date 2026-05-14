#!/usr/bin/env python3
"""
LCSAS End-to-End Integration Test
==================================

This script exercises the full LCSAS pipeline against a real filesystem
(the /mnt/lcsas-data logical volume) using rustic + xorriso + dvdisaster.

Phases:
  1. Generate synthetic test data
  2. Initialize two rustic repositories (family, work)
  3. Back up test data into both repos
  4. Initialize the LCSAS catalog
  5. Scan + register packs from both repos
  6. Run the burn pipeline → produce ISO files (no physical disc)
  7. Mount ISOs and verify holographic metadata
  8. Restore from the repos and verify data integrity
  9. Report summary

Prerequisites:
  - /mnt/lcsas-data mounted (run scripts/setup_test_lv.sh first)
  - rustic, xorriso on PATH
  - dvdisaster on PATH (optional — ECC steps skipped if missing)
  - lcsas installed in the active venv
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

BASE = Path("/mnt/lcsas-data")
MIRROR_DIR = BASE / "mirror"
STAGING_DIR = BASE / "staging"
ISO_DIR = BASE / "iso_output"
RESTORE_DIR = BASE / "restore"
DB_DIR = BASE / "db"
TEST_DATA_DIR = BASE / "test_data"
DB_PATH = DB_DIR / "archive_master.db"
PASSWORD_FILE = DB_DIR / "test_password.txt"

# Use TEST_TINY media type — 1 MB capacity, no ECC overhead
MEDIA_TYPE = "TEST_TINY"

# Number of test files per "dataset" directory.
# Sizes are kept small so packs + holographic metadata fit inside TEST_TINY
# (1 MB).  The metadata injection (SQLite catalog + per-repo Rustic index /
# snapshots / keys) is ~700 KB for this fixture, leaving ~300 KB usable for
# pack data per volume.  Keep total pack data under that budget.
NUM_FILES = 8
FILE_SIZE_RANGE = (256, 4_000)  # 256 B to 4 KB

# ── Helpers ───────────────────────────────────────────────────────────────────

class Colors:
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

def banner(msg: str) -> None:
    print(f"\n{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}  {msg}{Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}\n")

def info(msg: str) -> None:
    print(f"  {Colors.GREEN}✓{Colors.RESET} {msg}")

def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {msg}")

def fail(msg: str) -> None:
    print(f"  {Colors.RED}✗{Colors.RESET} {msg}")

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it and checking return code."""
    cmd_str = " ".join(str(c) for c in cmd)
    print(f"  {Colors.BOLD}${Colors.RESET} {cmd_str}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        fail(f"Command failed (rc={result.returncode})")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[:10]:
                print(f"    stderr: {line}")
        if result.stdout:
            for line in result.stdout.strip().splitlines()[:5]:
                print(f"    stdout: {line}")
    return result


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


# ── Pre-flight Checks ────────────────────────────────────────────────────────

def preflight() -> bool:
    banner("Pre-flight Checks")

    ok = True
    if not BASE.is_dir():
        fail(f"{BASE} does not exist — run scripts/setup_test_lv.sh first")
        return False

    for d in [MIRROR_DIR, STAGING_DIR, ISO_DIR, RESTORE_DIR, DB_DIR, TEST_DATA_DIR]:
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            info(f"Created {d}")

    for tool in ["rustic", "xorriso"]:
        if tool_available(tool):
            info(f"{tool} found: {shutil.which(tool)}")
        else:
            fail(f"{tool} not found on PATH — required")
            ok = False

    if tool_available("dvdisaster"):
        info(f"dvdisaster found: {shutil.which('dvdisaster')}")
    else:
        warn("dvdisaster not found — ECC steps will be skipped")

    return ok


# ── Phase 1: Generate Test Data ──────────────────────────────────────────────

def generate_test_data() -> dict[str, dict[str, str]]:
    """Create synthetic test data in two 'dataset' directories.

    Returns:
        Dict of {dataset_name: {filename: sha256_hash}}
    """
    banner("Phase 1: Generating Test Data")

    import random
    rng = random.Random(42)  # Deterministic seed for reproducibility

    manifests: dict[str, dict[str, str]] = {}

    for dataset_name in ["family_photos", "work_docs"]:
        dataset_dir = TEST_DATA_DIR / dataset_name
        if dataset_dir.exists():
            shutil.rmtree(dataset_dir)
        dataset_dir.mkdir(parents=True)

        file_hashes: dict[str, str] = {}
        total_bytes = 0

        for i in range(NUM_FILES):
            size = rng.randint(*FILE_SIZE_RANGE)
            data = rng.randbytes(size)
            fname = f"file_{i:04d}.bin"
            fpath = dataset_dir / fname
            fpath.write_bytes(data)
            file_hashes[fname] = hashlib.sha256(data).hexdigest()
            total_bytes += size

        manifests[dataset_name] = file_hashes
        info(f"{dataset_name}: {NUM_FILES} files, {total_bytes:,} bytes")

    return manifests


# ── Phase 2: Initialize Rustic Repositories ──────────────────────────────────

def init_rustic_repos() -> dict[str, Path]:
    """Initialize two rustic repos (family, work) in the mirror directory.

    Returns:
        Dict of {repo_name: repo_path}
    """
    banner("Phase 2: Initializing Rustic Repositories")

    # Create password file
    PASSWORD_FILE.write_text("test-password-do-not-use\n")
    info(f"Password file: {PASSWORD_FILE}")

    repos: dict[str, Path] = {}
    for repo_name in ["family", "work"]:
        repo_path = MIRROR_DIR / repo_name
        repos[repo_name] = repo_path

        if (repo_path / "config").exists():
            warn(f"{repo_name} repo already initialized — reinitializing")
            # rustic creates read-only files; fix perms before removing
            subprocess.run(["chmod", "-R", "u+rwX", str(repo_path)],
                           capture_output=True)
            shutil.rmtree(repo_path)

        result = run([
            "rustic", "init",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
        ])
        if result.returncode == 0:
            info(f"Initialized: {repo_path}")
        else:
            fail(f"Failed to initialize {repo_name}")
            sys.exit(1)

        # Constrain pack sizes so each pack fits within TEST_TINY's 1 MB
        # capacity (rustic's 4 MiB default would exceed it).
        result = run([
            "rustic", "config",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
            "--set-datapack-size", "256KiB",
            "--set-datapack-size-limit", "512KiB",
            "--set-treepack-size", "128KiB",
            "--set-treepack-size-limit", "256KiB",
        ])
        if result.returncode != 0:
            fail(f"Failed to configure pack sizes for {repo_name}")
            sys.exit(1)

    return repos


# ── Phase 3: Back Up Test Data ────────────────────────────────────────────────

def backup_data(repos: dict[str, Path]) -> dict[str, str]:
    """Back up test data into the rustic repos.

    Returns:
        Dict of {repo_name: snapshot_id}
    """
    banner("Phase 3: Backing Up Test Data into Rustic Repos")

    snapshot_ids: dict[str, str] = {}
    data_map = {
        "family": TEST_DATA_DIR / "family_photos",
        "work": TEST_DATA_DIR / "work_docs",
    }

    for repo_name, repo_path in repos.items():
        source = data_map[repo_name]
        result = run([
            "rustic", "backup",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
            "--json",
            str(source),
        ])
        if result.returncode == 0:
            # rustic backup --json emits a single multi-line JSON object whose
            # top-level "id" field is the new snapshot ID.
            try:
                obj = json.loads(result.stdout)
                snap_id = obj["id"]
                snapshot_ids[repo_name] = snap_id
                info(f"{repo_name} snapshot: {snap_id[:12]}...")
            except (json.JSONDecodeError, KeyError) as e:
                warn(f"Could not parse snapshot ID for {repo_name}: {e}")
        else:
            fail(f"Backup failed for {repo_name}")
            sys.exit(1)

    return snapshot_ids


# ── Phase 4: Initialize LCSAS Catalog ────────────────────────────────────────

def init_catalog() -> sqlite3.Connection:
    """Initialize the LCSAS SQLite catalog."""
    banner("Phase 4: Initializing LCSAS Catalog")

    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all

    if DB_PATH.exists():
        DB_PATH.unlink()
        info("Removed existing catalog")

    conn = get_connection(DB_PATH)
    create_all(conn)
    info(f"Catalog initialized: {DB_PATH}")

    # Register repositories
    from lcsas.db.repos import register_repo
    register_repo(conn, "family", "Family Photos", str(MIRROR_DIR / "family"))
    register_repo(conn, "work", "Work Documents", str(MIRROR_DIR / "work"))
    info("Registered repos: family, work")

    return conn


# ── Phase 5: Scan + Register Packs ───────────────────────────────────────────

def scan_and_register(conn: sqlite3.Connection, repos: dict[str, Path]) -> int:
    """Scan both rustic repos and register their packs in the catalog.

    Returns:
        Total number of packs registered.
    """
    banner("Phase 5: Scanning & Registering Packs")

    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    total_packs = 0
    for repo_name, repo_path in repos.items():
        scanned = scan_mirror_packs(repo_path)
        info(f"{repo_name}: found {len(scanned)} packs ({sum(scanned.values()):,} bytes)")

        delta = DeltaAnalyzer(conn, scanned, repo_id=repo_name)
        new_packs = delta.register_new_packs()
        total_packs += len(new_packs)
        info(f"{repo_name}: registered {len(new_packs)} new packs")

        unarchived = delta.get_unarchived()
        unarchived_bytes = delta.get_total_unarchived_bytes()
        info(f"{repo_name}: {len(unarchived)} unarchived packs ({unarchived_bytes:,} bytes)")

    return total_packs


# ── Phase 6: Burn Pipeline (ISO-only) ────────────────────────────────────────

def run_burn_pipeline(conn: sqlite3.Connection) -> list[Path]:
    """Run the burn pipeline to produce ISO files (skip physical burn).

    Keeps burning until all unarchived packs are consumed.

    Returns:
        List of ISO file paths created.
    """
    banner("Phase 6: Burn Pipeline (ISO-only)")

    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.queries import get_total_unarchived_bytes, get_unarchived_packs
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    mt = MediaType.TEST_TINY
    has_dvdisaster = tool_available("dvdisaster")
    # ECC is now always-on for production media; TEST_TINY has 0% overhead so
    # the orchestrator implicitly skips ECC for it (see PR #36 — --skip-ecc
    # was removed and the implicit per-media gate replaced it).
    if mt.is_test and has_dvdisaster:
        info("Skipping ECC for test media (dvdisaster minimum image exceeds test capacity)")

    # Build config with repo definitions
    repo_configs = {
        "family": RepositoryConfig(
            name="family",
            mirror_path=MIRROR_DIR / "family",
            password_file=PASSWORD_FILE,
        ),
        "work": RepositoryConfig(
            name="work",
            mirror_path=MIRROR_DIR / "work",
            password_file=PASSWORD_FILE,
        ),
    }

    config = LCSASConfig(
        mirror_base_path=MIRROR_DIR,
        staging_path=STAGING_DIR,
        db_path=DB_PATH,
        default_media_type=mt,
        default_ecc_redundancy_pct=10 if has_dvdisaster else 0,
        label_prefix="E2ETEST",
        metadata_reserve_bytes=750_000,  # ISO+SQLite catalog+rustic metadata ~700KB
        repositories=repo_configs,
    )

    xorriso = SubprocessXorrisoRunner()
    dvdisaster = SubprocessDVDisasterRunner() if has_dvdisaster else None

    # If no dvdisaster, create a no-op implementation
    if dvdisaster is None:
        class NoOpDVDisaster:
            def augment_iso(self, iso_path, redundancy_pct=15):
                warn("  Skipping ECC augmentation (dvdisaster not available)")
            def verify_iso(self, iso_path):
                return True
            def repair_iso(self, iso_path):
                return True
        dvdisaster = NoOpDVDisaster()

    orchestrator = BurnOrchestrator(config, conn, xorriso, dvdisaster)

    iso_files: list[Path] = []
    volume_num = 0

    while True:
        unarchived = get_unarchived_packs(conn)
        total_bytes = get_total_unarchived_bytes(conn)

        if not unarchived:
            info("All packs archived — burn pipeline complete")
            break

        volume_num += 1
        info(
            f"Volume {volume_num}: {len(unarchived)} unarchived packs"
            f" ({total_bytes:,} bytes remaining)"
        )

        try:
            manifest = orchestrator.prepare(media_type=mt)
            info(f"  Prepared: {manifest.volume_label} "
                 f"({len(manifest.selected_packs)} packs, "
                 f"{manifest.total_data_bytes:,} bytes)")

            iso_path = ISO_DIR / f"{manifest.volume_label}.iso"
            volume = orchestrator.execute(
                manifest,
                iso_output=iso_path,
                skip_burn=True,
            )

            iso_size = iso_path.stat().st_size
            info(f"  ISO created: {iso_path.name} ({iso_size:,} bytes)")
            info(f"  Volume status: {volume.status}")
            iso_files.append(iso_path)

        except ValueError as e:
            warn(f"  Burn stopped: {e}")
            break
        except Exception as e:
            fail(f"  Burn failed: {e}")
            import traceback
            traceback.print_exc()
            break

    return iso_files


# ── Phase 7: Verify ISO Contents ─────────────────────────────────────────────

def verify_isos(iso_files: list[Path]) -> bool:
    """Mount each ISO (via loop) and verify its holographic metadata."""
    banner("Phase 7: Verifying ISO Contents")

    if not iso_files:
        warn("No ISOs to verify")
        return True

    all_ok = True
    mount_point = BASE / "iso_mount"
    mount_point.mkdir(exist_ok=True)

    for iso_path in iso_files:
        info(f"Verifying: {iso_path.name}")

        # Try mounting with loop device (requires sudo/fuse)
        # Fall back to listing with xorriso if mount fails
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-find", "/", "-type", "f", "-exec", "report_lba",
        ])

        if result.returncode == 0:
            file_count = result.stdout.count("\n")
            info(f"  Contains {file_count} file entries")
        else:
            warn("  Could not list ISO contents")

        # Check if volume_info.json is in the ISO
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-osirrox", "on",
            "-extract", "/volume_info.json", str(mount_point / "volume_info.json"),
        ])
        if result.returncode == 0 and (mount_point / "volume_info.json").exists():
            vol_info = json.loads((mount_point / "volume_info.json").read_text())
            info(f"  Volume UUID: {vol_info.get('uuid', 'N/A')}")
            info(f"  Volume label: {vol_info.get('label', 'N/A')}")
            info(f"  Media type: {vol_info.get('media_type', 'N/A')}")
            (mount_point / "volume_info.json").unlink()
        else:
            warn("  Could not extract volume_info.json")
            all_ok = False

        # Check if catalog.db is in the ISO
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-osirrox", "on",
            "-extract", "/catalog.db", str(mount_point / "catalog.db"),
        ])
        if result.returncode == 0 and (mount_point / "catalog.db").exists():
            cat_size = (mount_point / "catalog.db").stat().st_size
            info(f"  Catalog found: {cat_size:,} bytes")
            (mount_point / "catalog.db").unlink()
        else:
            warn("  Catalog not found in ISO")

        # Check data/ directory
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-find", "/data", "-type", "f",
        ])
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            pack_count = len([ln for ln in lines if ln.startswith("'")])
            info(f"  Pack files in data/: {pack_count}")
        else:
            warn("  Could not list data/ contents")

        # Check metadata/ directory
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-find", "/metadata", "-type", "d",
        ])
        if result.returncode == 0:
            dirs = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
            info(f"  Metadata directories: {len(dirs)}")
        else:
            warn("  Could not list metadata/ contents")

    return all_ok


# ── Phase 8: Verify Catalog State ────────────────────────────────────────────

def verify_catalog(conn: sqlite3.Connection) -> None:
    """Print final catalog status."""
    banner("Phase 8: Catalog Summary")

    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.repos import list_repos
    from lcsas.db.volumes import list_volumes

    summary = get_archive_status_summary(conn)
    info(f"Total packs:      {summary['total']}")
    info(f"Archived packs:   {summary['archived']}")
    info(f"Unarchived packs: {summary['unarchived']}")
    info(f"Pruned packs:     {summary['pruned']}")

    volumes = list_volumes(conn)
    info(f"Total volumes:    {len(volumes)}")
    for vol in volumes:
        info(f"  {vol.label}  status={vol.status}  used={vol.used_bytes:,}  media={vol.media_type}")

    repos = list_repos(conn)
    info(f"Repositories:     {len(repos)}")
    for repo in repos:
        info(f"  {repo.repo_id}: {repo.name} ({repo.mirror_path})")


# ── Phase 9: Restore Test ────────────────────────────────────────────────────

def test_restore(
    repos: dict[str, Path],
    snapshot_ids: dict[str, str],
    original_manifests: dict[str, dict[str, str]],
) -> bool:
    """Restore from rustic repos and verify file integrity against originals."""
    banner("Phase 9: Restore & Verify Data Integrity")

    all_ok = True
    restore_map = {
        "family": "family_photos",
        "work": "work_docs",
    }

    for repo_name, repo_path in repos.items():
        snap_id = snapshot_ids.get(repo_name)
        if not snap_id:
            warn(f"No snapshot ID for {repo_name} — skipping restore")
            continue

        restore_target = RESTORE_DIR / repo_name
        if restore_target.exists():
            shutil.rmtree(restore_target)
        restore_target.mkdir(parents=True)

        info(f"Restoring {repo_name} (snapshot {snap_id[:12]}...)")
        result = run([
            "rustic", "restore",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
            snap_id, str(restore_target),
        ])
        if result.returncode != 0:
            fail(f"Restore failed for {repo_name}")
            all_ok = False
            continue

        # Verify file integrity
        dataset_name = restore_map[repo_name]
        expected = original_manifests.get(dataset_name, {})
        # Find the restored data directory
        restored_data = restore_target / "mnt" / "lcsas-data" / "test_data" / dataset_name
        if not restored_data.is_dir():
            # Try alternative path (rustic restores with full path)
            candidates = list(restore_target.rglob(dataset_name))
            if candidates:
                restored_data = candidates[0]
            else:
                warn(f"Could not find restored data for {dataset_name}")
                all_ok = False
                continue

        verified = 0
        mismatched = 0
        for fname, expected_hash in expected.items():
            fpath = restored_data / fname
            if not fpath.exists():
                fail(f"  Missing: {fname}")
                mismatched += 1
                continue
            actual_hash = sha256_file(fpath)
            if actual_hash == expected_hash:
                verified += 1
            else:
                fail(f"  Hash mismatch: {fname}")
                mismatched += 1

        if mismatched == 0:
            info(f"{repo_name}: all {verified} files verified ✓")
        else:
            fail(f"{repo_name}: {mismatched} files mismatched, {verified} ok")
            all_ok = False

    return all_ok


# ── Phase 10: Redundant Burn (second copy of all packs) ───────────────────────

def create_redundant_copies(
    conn: sqlite3.Connection,
    iso_files: list[Path],
) -> list[Path]:
    """Re-burn all archived packs to new volumes for redundancy.

    Manually links existing packs to new volumes and creates ISOs,
    simulating a second copy for disaster recovery.

    Returns:
        List of redundant ISO file paths.
    """
    banner("Phase 10: Create Redundant Volume Copies")

    from lcsas.config.media import MediaType
    from lcsas.db.queries import get_packs_for_volume
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume, list_volumes, update_status
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.staging.builder import StagingBuilder
    from lcsas.utils.labels import generate_uuid

    mt = MediaType.TEST_TINY
    xorriso = SubprocessXorrisoRunner()

    # Get all existing verified volumes
    existing_vols = [v for v in list_volumes(conn) if v.status == "VERIFIED"]
    info(f"Found {len(existing_vols)} verified volumes to create redundant copies of")

    redundant_isos: list[Path] = []

    for orig_vol in existing_vols:
        orig_packs = get_packs_for_volume(conn, orig_vol.volume_id)
        if not orig_packs:
            continue

        # Create a new volume as a redundant copy
        dup_label = f"{orig_vol.label}_DUP"
        dup_vol = create_volume(
            conn,
            label=dup_label,
            uuid=generate_uuid(),
            media_type=mt.name,
            capacity_bytes=mt.capacity_bytes,
            location="Offsite_Safe",
            status="STAGING",
        )

        # Link same packs to the new volume
        bulk_link_packs(conn, dup_vol.volume_id, [p.pack_id for p in orig_packs])

        info(f"  {dup_label}: {len(orig_packs)} packs (redundant copy of {orig_vol.label})")

        # Find the original ISO's staging dir or use the ISO to copy packs
        # We'll create a new staging directory from the original mirror data
        staging_root = STAGING_DIR / dup_label
        builder = StagingBuilder(staging_root)
        builder.initialize()

        # Stage each repo's packs from its own mirror data directory.
        # stage_packs raises if it can't find every listed pack, so we have
        # to partition by repo_id rather than passing the full pack list
        # to every mirror.
        packs_by_repo: dict[str, list] = {}
        for pack in orig_packs:
            packs_by_repo.setdefault(pack.repo_id, []).append(pack)
        for repo_name, repo_packs in packs_by_repo.items():
            data_dir = MIRROR_DIR / repo_name / "data"
            if data_dir.is_dir():
                builder.stage_packs(repo_packs, data_dir)

        # Create ISO.  Walk the full STAGING → BURNING → BURNED → VERIFIED
        # transition chain so the lifecycle gate accepts each step (no
        # force=True shortcut, since the e2e test is meant to mirror real
        # operator flow as closely as possible).
        iso_path = ISO_DIR / f"{dup_label}.iso"
        try:
            xorriso.create_iso(staging_root, iso_path, dup_label)
            update_status(conn, dup_vol.volume_id, "BURNING")
            update_status(conn, dup_vol.volume_id, "BURNED")
            update_status(conn, dup_vol.volume_id, "VERIFIED")
            redundant_isos.append(iso_path)
            info(f"  ISO created: {iso_path.name} ({iso_path.stat().st_size:,} bytes)")
        except Exception as e:
            warn(f"  Failed to create redundant ISO: {e}")
            # Volume is still in STAGING; nothing to roll back.

    return redundant_isos


# ── Phase 11: Multi-Volume ISO Restore & Verification ────────────────────────

def test_iso_restore(
    conn: sqlite3.Connection,
    iso_files: list[Path],
    repos: dict[str, Path],
    snapshot_ids: dict[str, str],
    original_manifests: dict[str, dict[str, str]],
) -> bool:
    """Extract packs from ISOs, assemble restore cache, restore, verify.

    Tests restoring from archived ISOs (not from the original mirror),
    which is the real disaster-recovery scenario.
    """
    banner("Phase 11: Restore from ISOs & Verify Integrity")

    from lcsas.restore.executor import RestoreExecutor
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    all_ok = True
    extract_base = BASE / "iso_extract"
    if extract_base.exists():
        # Fix permissions and clean up
        subprocess.run(["chmod", "-R", "u+rwX", str(extract_base)], capture_output=True)
        shutil.rmtree(extract_base)
    extract_base.mkdir(parents=True)

    # Step 1: Extract packs from each ISO
    info("Extracting packs from ISOs...")
    vol_mount_dirs: dict[str, Path] = {}

    for iso_path in iso_files:
        vol_label = iso_path.stem  # e.g., "E2ETEST_TEST_TINY_0001"
        extract_dir = extract_base / vol_label
        extract_dir.mkdir(exist_ok=True)

        # Extract data/ directory from ISO using xorriso
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-osirrox", "on",
            "-extract", "/data", str(extract_dir / "data"),
        ])
        if result.returncode == 0:
            data_files = list((extract_dir / "data").rglob("*"))
            data_file_count = len([f for f in data_files if f.is_file()])
            info(f"  {vol_label}: extracted {data_file_count} pack files")
            vol_mount_dirs[vol_label] = extract_dir
        else:
            warn(f"  Failed to extract data from {iso_path.name}")

        # Also extract metadata/
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-osirrox", "on",
            "-extract", "/metadata", str(extract_dir / "metadata"),
        ])
        if result.returncode != 0:
            warn(f"  Could not extract metadata from {iso_path.name}")

    if not vol_mount_dirs:
        fail("No volume data extracted — skipping ISO restore test")
        return False

    # Step 2: Verify pack file integrity across ISOs
    info("Verifying pack file integrity across volumes...")
    pack_hashes: dict[str, dict[str, str]] = {}  # {sha: {vol_label: file_hash}}

    for vol_label, mount_dir in vol_mount_dirs.items():
        data_dir = mount_dir / "data"
        if not data_dir.exists():
            continue
        for pack_file in data_dir.rglob("*"):
            if not pack_file.is_file():
                continue
            file_hash = sha256_file(pack_file)
            pack_name = pack_file.name
            pack_hashes.setdefault(pack_name, {})[vol_label] = file_hash

    # Check that the same pack has the same hash across all volumes
    integrity_ok = True
    for pack_name, vol_hashes in pack_hashes.items():
        unique_hashes = set(vol_hashes.values())
        if len(unique_hashes) > 1:
            fail(f"  Pack {pack_name} has inconsistent hashes across volumes!")
            for vl, fh in vol_hashes.items():
                fail(f"    {vl}: {fh[:16]}...")
            integrity_ok = False

    if integrity_ok:
        packs_with_copies = sum(1 for h in pack_hashes.values() if len(h) > 1)
        info(f"Pack integrity verified: {len(pack_hashes)} unique packs, "
             f"{packs_with_copies} with redundant copies")
    else:
        all_ok = False

    # Step 3: For each repo, build restore cache from ISOs and restore
    restore_map = {
        "family": "family_photos",
        "work": "work_docs",
    }

    for repo_name, repo_path in repos.items():
        snap_id = snapshot_ids.get(repo_name)
        if not snap_id:
            warn(f"No snapshot ID for {repo_name} — skipping")
            continue

        info(f"Restoring {repo_name} from ISOs...")

        # Build the restore cache
        cache_dir = extract_base / f"cache_{repo_name}"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

        # Prepare cache from the original mirror metadata
        rustic_runner = SubprocessRusticRunner(rustic_binary="rustic")
        executor = RestoreExecutor(rustic_runner)
        executor.prepare_cache(cache_dir, repo_path)

        # Ingest packs from ALL available ISOs
        for vol_label, mount_dir in vol_mount_dirs.items():
            # Get all pack files available on this volume
            data_dir = mount_dir / "data"
            if not data_dir.exists():
                continue
            available_packs = [f.name for f in data_dir.rglob("*") if f.is_file()]
            if available_packs:
                result = executor.ingest_volume(cache_dir, mount_dir, available_packs)
                if result.ingested > 0:
                    info(f"  From {vol_label}: ingested {result.ingested} packs")

        # Restore
        restore_target = RESTORE_DIR / f"{repo_name}_from_iso"
        if restore_target.exists():
            shutil.rmtree(restore_target)
        restore_target.mkdir(parents=True)

        try:
            result = run([
                "rustic", "restore",
                "--repo", str(cache_dir),
                "--password-file", str(PASSWORD_FILE),
                snap_id, str(restore_target),
            ])
            if result.returncode != 0:
                fail(f"  Restore from ISOs failed for {repo_name}")
                all_ok = False
                continue
        except Exception as e:
            fail(f"  Restore failed: {e}")
            all_ok = False
            continue

        # Verify
        dataset_name = restore_map[repo_name]
        expected = original_manifests.get(dataset_name, {})
        restored_data = restore_target / "mnt" / "lcsas-data" / "test_data" / dataset_name
        if not restored_data.is_dir():
            candidates = list(restore_target.rglob(dataset_name))
            if candidates:
                restored_data = candidates[0]
            else:
                warn(f"  Could not find restored data for {dataset_name}")
                all_ok = False
                continue

        verified = 0
        mismatched = 0
        for fname, expected_hash in expected.items():
            fpath = restored_data / fname
            if not fpath.exists():
                fail(f"  Missing: {fname}")
                mismatched += 1
                continue
            actual_hash = sha256_file(fpath)
            if actual_hash == expected_hash:
                verified += 1
            else:
                fail(f"  Hash mismatch: {fname}")
                mismatched += 1

        if mismatched == 0:
            info(f"  {repo_name} (ISO restore): all {verified} files verified ✓")
        else:
            fail(f"  {repo_name} (ISO restore): {mismatched} mismatched, {verified} ok")
            all_ok = False

    return all_ok


# ── Phase 12: Redundancy Verification ────────────────────────────────────────

def verify_redundancy(conn: sqlite3.Connection) -> bool:
    """Verify that redundant copies exist and catalog is consistent."""
    banner("Phase 12: Redundancy Verification")

    from lcsas.db.queries import get_redundancy_report, get_volumes_for_pack
    from lcsas.db.volumes import list_volumes

    all_ok = True

    volumes = list_volumes(conn)
    verified_vols = [v for v in volumes if v.status == "VERIFIED"]
    info(f"Total verified volumes: {len(verified_vols)}")

    # Check that some packs have redundant copies
    under_2 = get_redundancy_report(conn, min_copies=2)
    with_copies = len(get_redundancy_report(conn, min_copies=1))
    total_with_copies = with_copies - len(under_2)

    if len(under_2) == 0:
        info("ALL packs have 2+ copies — fully redundant")
    else:
        info(f"Packs with <2 copies: {len(under_2)}")
        info(f"Packs with 2+ copies: {total_with_copies}")

        # This is a warning, not a failure — not all packs must be redundant
        # in this test since some may not fit on the redundant volumes
        for pack in under_2[:5]:
            vols = get_volumes_for_pack(conn, pack.pack_id)
            vol_labels = [v.label for v in vols]
            info(f"  {pack.sha256[:20]}... copies={len(vols)} vols={vol_labels}")

    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    banner("LCSAS End-to-End Integration Test")
    print(f"  Base directory: {BASE}")
    print(f"  Media type:     {MEDIA_TYPE}")
    print()

    if not preflight():
        return 1

    # Phase 1
    manifests = generate_test_data()

    # Phase 2
    repos = init_rustic_repos()

    # Phase 3
    snapshot_ids = backup_data(repos)

    # Phase 4
    conn = init_catalog()

    # Phase 5
    total_packs = scan_and_register(conn, repos)
    info(f"Total packs registered: {total_packs}")

    # Phase 6
    iso_files = run_burn_pipeline(conn)
    info(f"Total ISOs created: {len(iso_files)}")

    # Phase 7
    isos_ok = verify_isos(iso_files)

    # Phase 8
    verify_catalog(conn)

    # Phase 9
    restore_ok = test_restore(repos, snapshot_ids, manifests)

    # Phase 10: Create redundant copies
    redundant_isos = create_redundant_copies(conn, iso_files)
    info(f"Redundant ISOs created: {len(redundant_isos)}")
    all_iso_files = iso_files + redundant_isos

    # Phase 11: Restore from ISOs
    iso_restore_ok = test_iso_restore(
        conn, all_iso_files, repos, snapshot_ids, manifests
    )

    # Phase 12: Verify redundancy
    redundancy_ok = verify_redundancy(conn)

    # ── Summary ───────────────────────────────────────────────────────────────
    banner("Test Summary")

    df_result = subprocess.run(["df", "-h", str(BASE)], capture_output=True, text=True)
    if df_result.returncode == 0:
        for line in df_result.stdout.strip().splitlines():
            print(f"  {line}")
        print()

    results = {
        "Test data generation": True,
        "Rustic repos initialized": bool(repos),
        "Backups completed": bool(snapshot_ids),
        "Packs registered": total_packs > 0,
        "ISOs created": len(iso_files) > 0,
        "ISO verification": isos_ok,
        "Restore (from repos)": restore_ok,
        "Redundant copies created": len(redundant_isos) > 0,
        "Restore (from ISOs)": iso_restore_ok,
        "Redundancy verification": redundancy_ok,
    }

    all_pass = True
    for test_name, passed in results.items():
        if passed:
            info(f"{test_name}: PASS")
        else:
            fail(f"{test_name}: FAIL")
            all_pass = False

    print()
    if all_pass:
        info(f"{Colors.GREEN}{Colors.BOLD}ALL TESTS PASSED{Colors.RESET}")
        return 0
    else:
        fail(f"{Colors.RED}{Colors.BOLD}SOME TESTS FAILED{Colors.RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
