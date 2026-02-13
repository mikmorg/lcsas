#!/usr/bin/env python3
"""
LCSAS End-to-End Integration Test
==================================

This script exercises the full LCSAS pipeline against a real filesystem
(the /mnt/lcsas-test logical volume) using restic + xorriso + dvdisaster.

Phases:
  1. Generate synthetic test data
  2. Initialize two restic repositories (family, work)
  3. Back up test data into both repos
  4. Initialize the LCSAS catalog
  5. Scan + register packs from both repos
  6. Run the burn pipeline → produce ISO files (no physical disc)
  7. Mount ISOs and verify holographic metadata
  8. Restore from the repos and verify data integrity
  9. Report summary

Prerequisites:
  - /mnt/lcsas-test mounted (run scripts/setup_test_lv.sh first)
  - restic, xorriso on PATH
  - dvdisaster on PATH (optional — ECC steps skipped if missing)
  - lcsas installed in the active venv
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

BASE = Path("/mnt/lcsas-test")
MIRROR_DIR = BASE / "mirror"
STAGING_DIR = BASE / "staging"
ISO_DIR = BASE / "iso_output"
RESTORE_DIR = BASE / "restore"
DB_DIR = BASE / "db"
TEST_DATA_DIR = BASE / "test_data"
DB_PATH = DB_DIR / "archive_master.db"
PASSWORD_FILE = DB_DIR / "test_password.txt"

# Use TEST_SMALL media type — 10 MB capacity, 10% ECC overhead
MEDIA_TYPE = "TEST_SMALL"

# Number of test files per "dataset" directory
NUM_FILES = 30
FILE_SIZE_RANGE = (1024, 50_000)  # 1 KB to 50 KB

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

    for tool in ["restic", "xorriso"]:
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


# ── Phase 2: Initialize Restic Repositories ──────────────────────────────────

def init_restic_repos() -> dict[str, Path]:
    """Initialize two restic repos (family, work) in the mirror directory.

    Returns:
        Dict of {repo_name: repo_path}
    """
    banner("Phase 2: Initializing Restic Repositories")

    # Create password file
    PASSWORD_FILE.write_text("test-password-do-not-use\n")
    info(f"Password file: {PASSWORD_FILE}")

    repos: dict[str, Path] = {}
    for repo_name in ["family", "work"]:
        repo_path = MIRROR_DIR / repo_name
        repos[repo_name] = repo_path

        if (repo_path / "config").exists():
            warn(f"{repo_name} repo already initialized — reinitializing")
            shutil.rmtree(repo_path)

        result = run([
            "restic", "init",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
        ])
        if result.returncode == 0:
            info(f"Initialized: {repo_path}")
        else:
            fail(f"Failed to initialize {repo_name}")
            sys.exit(1)

    return repos


# ── Phase 3: Back Up Test Data ────────────────────────────────────────────────

def backup_data(repos: dict[str, Path]) -> dict[str, str]:
    """Back up test data into the restic repos.

    Returns:
        Dict of {repo_name: snapshot_id}
    """
    banner("Phase 3: Backing Up Test Data into Restic Repos")

    snapshot_ids: dict[str, str] = {}
    data_map = {
        "family": TEST_DATA_DIR / "family_photos",
        "work": TEST_DATA_DIR / "work_docs",
    }

    for repo_name, repo_path in repos.items():
        source = data_map[repo_name]
        result = run([
            "restic", "backup",
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
            "--json",
            str(source),
        ])
        if result.returncode == 0:
            # Parse snapshot ID from JSON output
            for line in result.stdout.strip().splitlines():
                try:
                    obj = json.loads(line)
                    if "snapshot_id" in obj:
                        snapshot_ids[repo_name] = obj["snapshot_id"]
                        info(f"{repo_name} snapshot: {obj['snapshot_id'][:12]}...")
                        break
                except json.JSONDecodeError:
                    continue
            else:
                warn(f"Could not parse snapshot ID for {repo_name}")
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
    """Scan both restic repos and register their packs in the catalog.

    Returns:
        Total number of packs registered.
    """
    banner("Phase 5: Scanning & Registering Packs")

    from lcsas.packs.scanner import scan_mirror_packs
    from lcsas.packs.delta import DeltaAnalyzer

    total_packs = 0
    for repo_name, repo_path in repos.items():
        scanned = scan_mirror_packs(repo_path)
        info(f"{repo_name}: found {len(scanned)} packs ({sum(scanned.values()):,} bytes)")

        delta = DeltaAnalyzer(conn, scanned, repo_id=repo_name)
        new_packs = delta.register_new_packs()
        total_packs += len(new_packs)
        info(f"{repo_name}: registered {len(new_packs)} new packs")

        unarchived = delta.get_unarchived()
        info(f"{repo_name}: {len(unarchived)} unarchived packs ({delta.get_total_unarchived_bytes():,} bytes)")

    return total_packs


# ── Phase 6: Burn Pipeline (ISO-only) ────────────────────────────────────────

def run_burn_pipeline(conn: sqlite3.Connection) -> list[Path]:
    """Run the burn pipeline to produce ISO files (skip physical burn).

    Keeps burning until all unarchived packs are consumed.

    Returns:
        List of ISO file paths created.
    """
    banner("Phase 6: Burn Pipeline (ISO-only)")

    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.db.queries import get_total_unarchived_bytes, get_unarchived_packs

    mt = MediaType.TEST_SMALL
    has_dvdisaster = tool_available("dvdisaster")
    skip_ecc = not has_dvdisaster

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
        metadata_reserve_bytes=50_000,  # Small reserve for test media
        repositories=repo_configs,
    )

    xorriso = SubprocessXorrisoRunner()
    dvdisaster = SubprocessDVDisasterRunner() if has_dvdisaster else None

    # If no dvdisaster, create a no-op implementation
    if dvdisaster is None:
        class NoOpDVDisaster:
            def augment_iso(self, iso_path, redundancy_pct=15):
                warn(f"  Skipping ECC augmentation (dvdisaster not available)")
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
        info(f"Volume {volume_num}: {len(unarchived)} unarchived packs ({total_bytes:,} bytes remaining)")

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
                skip_ecc=skip_ecc,
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
            warn(f"  Could not list ISO contents")

        # Check if volume_info.json is in the ISO
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-extract", "/volume_info.json", str(mount_point / "volume_info.json"),
        ])
        if result.returncode == 0 and (mount_point / "volume_info.json").exists():
            vol_info = json.loads((mount_point / "volume_info.json").read_text())
            info(f"  Volume UUID: {vol_info.get('uuid', 'N/A')}")
            info(f"  Volume label: {vol_info.get('label', 'N/A')}")
            info(f"  Media type: {vol_info.get('media_type', 'N/A')}")
            (mount_point / "volume_info.json").unlink()
        else:
            warn(f"  Could not extract volume_info.json")
            all_ok = False

        # Check if catalog.db is in the ISO
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-extract", "/catalog.db", str(mount_point / "catalog.db"),
        ])
        if result.returncode == 0 and (mount_point / "catalog.db").exists():
            cat_size = (mount_point / "catalog.db").stat().st_size
            info(f"  Catalog found: {cat_size:,} bytes")
            (mount_point / "catalog.db").unlink()
        else:
            warn(f"  Catalog not found in ISO")

        # Check data/ directory
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-find", "/data", "-type", "f",
        ])
        if result.returncode == 0:
            pack_count = len([l for l in result.stdout.strip().splitlines() if l.startswith("'")])
            info(f"  Pack files in data/: {pack_count}")
        else:
            warn(f"  Could not list data/ contents")

        # Check metadata/ directory
        result = run([
            "xorriso", "-indev", str(iso_path),
            "-find", "/metadata", "-type", "d",
        ])
        if result.returncode == 0:
            dirs = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            info(f"  Metadata directories: {len(dirs)}")
        else:
            warn(f"  Could not list metadata/ contents")

    return all_ok


# ── Phase 8: Verify Catalog State ────────────────────────────────────────────

def verify_catalog(conn: sqlite3.Connection) -> None:
    """Print final catalog status."""
    banner("Phase 8: Catalog Summary")

    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.volumes import list_volumes
    from lcsas.db.repos import list_repos

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
        info(f"  {repo.repo_id}: {repo.display_name} ({repo.mirror_path})")


# ── Phase 9: Restore Test ────────────────────────────────────────────────────

def test_restore(
    repos: dict[str, Path],
    snapshot_ids: dict[str, str],
    original_manifests: dict[str, dict[str, str]],
) -> bool:
    """Restore from restic repos and verify file integrity against originals."""
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
            "restic", "restore", snap_id,
            "--repo", str(repo_path),
            "--password-file", str(PASSWORD_FILE),
            "--target", str(restore_target),
        ])
        if result.returncode != 0:
            fail(f"Restore failed for {repo_name}")
            all_ok = False
            continue

        # Verify file integrity
        dataset_name = restore_map[repo_name]
        expected = original_manifests.get(dataset_name, {})
        # Find the restored data directory
        restored_data = restore_target / "mnt" / "lcsas-test" / "test_data" / dataset_name
        if not restored_data.is_dir():
            # Try alternative path (restic restores with full path)
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
    repos = init_restic_repos()

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

    # ── Summary ───────────────────────────────────────────────────────────────
    banner("Test Summary")

    df_result = subprocess.run(["df", "-h", str(BASE)], capture_output=True, text=True)
    if df_result.returncode == 0:
        for line in df_result.stdout.strip().splitlines():
            print(f"  {line}")
        print()

    results = {
        "Test data generation": True,
        "Restic repos initialized": bool(repos),
        "Backups completed": bool(snapshot_ids),
        "Packs registered": total_packs > 0,
        "ISOs created": len(iso_files) > 0,
        "ISO verification": isos_ok,
        "Restore verification": restore_ok,
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
