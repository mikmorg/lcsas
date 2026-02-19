"""LCSAS command-line interface using argparse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lcsas import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="lcsas",
        description="Linux Cold Storage Archival Suite — "
                    "orchestrates Rustic, Xorriso, and DVDisaster for optical archival.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to TOML configuration file.",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Path to SQLite archive catalog (overrides config).",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- init ---
    init_p = subparsers.add_parser("init", help="Initialize LCSAS database and config.")
    init_p.add_argument("--db-path", type=Path, default=Path("archive.db"),
                        help="Path for the SQLite database.")

    # --- repo ---
    repo_p = subparsers.add_parser("repo", help="Manage backup repositories.")
    repo_sub = repo_p.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Register a new repository.")
    repo_add.add_argument("name", help="Repository name (e.g., 'family').")
    repo_add.add_argument("mirror_path", type=Path, help="Path to the local mirror.")
    repo_add.add_argument("--key-file", type=Path, default=None,
                          help="Path to the encryption key file.")

    repo_sub.add_parser("list", help="List registered repositories.")

    # --- scan ---
    scan_p = subparsers.add_parser(
        "scan",
        help="Scan mirrors for new packs and register them in the catalog.",
    )
    scan_p.add_argument(
        "--repo", type=str, default=None, nargs="*",
        help="Specific repository names to scan (default: all).",
    )

    # --- status ---
    subparsers.add_parser("status", help="Show archive status summary.")

    # --- burn ---
    burn_p = subparsers.add_parser("burn", help="Burn staged ISOs to disc.")
    burn_p.add_argument("--media", type=str, default=None,
                        help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    burn_p.add_argument("--repo", type=str, default=None, nargs="*",
                        help="Specific repository IDs to burn.")
    burn_p.add_argument("--iso-only", type=Path, default=None,
                        help="Create ISO file at this path without burning to disc.")
    burn_p.add_argument("--skip-ecc", action="store_true",
                        help="Skip DVDisaster ECC augmentation.")
    burn_p.add_argument("--session", type=str, default=None,
                        help="Burn a previously staged session (ID or 'latest').")
    burn_p.add_argument("--location", type=str, default=None,
                        help="Physical location tag for this copy.")
    burn_p.add_argument("--device", type=str, default=None,
                        help="Optical device path (overrides config).")

    # --- stage ---
    stage_p = subparsers.add_parser("stage", help="Stage ISOs for deferred burning.")
    stage_p.add_argument("--media", type=str, default=None,
                         help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    stage_p.add_argument("--for-location", type=str, default=None,
                         help="Stage only packs missing at this location.")
    stage_p.add_argument("--repo", type=str, default=None, nargs="*",
                         help="Specific repository IDs to stage.")
    stage_p.add_argument("--skip-ecc", action="store_true",
                         help="Skip DVDisaster ECC augmentation.")
    stage_p.add_argument("--clean", action="store_true",
                         help="Clean up staged ISOs for a session.")
    stage_p.add_argument("--session", type=str, default=None,
                         help="Session ID (for --clean).")

    # --- burn-iso ---
    burniso_p = subparsers.add_parser("burn-iso",
                                      help="Burn a single ISO file (standalone).")
    burniso_p.add_argument("iso_path", type=Path, help="Path to .iso file.")
    burniso_p.add_argument("--device", type=str, default="/dev/sr0",
                           help="Optical device path.")
    burniso_p.add_argument("--verify", action="store_true", default=True,
                           help="Verify after burning.")

    # --- location ---
    loc_p = subparsers.add_parser("location", help="Manage physical storage locations.")
    loc_sub = loc_p.add_subparsers(dest="location_command")

    loc_sub.add_parser("list", help="List all locations and their status.")

    loc_add_p = loc_sub.add_parser("add", help="Register a new storage location.")
    loc_add_p.add_argument("name", help="Location name (e.g. Offsite_Safe).")
    loc_add_p.add_argument("--description", type=str, default="",
                           help="Optional description.")

    loc_status_p = loc_sub.add_parser("status",
                                      help="Show packs present/missing at a location.")
    loc_status_p.add_argument("name", help="Location name.")

    loc_move_p = loc_sub.add_parser("move",
                                    help="Record a disc moving between locations.")
    loc_move_p.add_argument("volume_label",
                            help="Volume label (e.g. ARCHIVE_MDISC100_0001).")
    loc_move_p.add_argument("--from", dest="from_location", required=True,
                            help="Source location.")
    loc_move_p.add_argument("--to", dest="to_location", required=True,
                            help="Destination location.")

    # --- catalog ---
    cat_p = subparsers.add_parser("catalog", help="Catalog management.")
    cat_sub = cat_p.add_subparsers(dest="catalog_command")
    cat_import_p = cat_sub.add_parser("import-receipts",
                                      help="Import burn receipts from remote burns.")
    cat_import_p.add_argument("receipt_files", nargs="+",
                              help="Receipt JSON files.")

    # --- restore ---
    restore_p = subparsers.add_parser("restore", help="Plan or execute a restore.")
    restore_sub = restore_p.add_subparsers(dest="restore_command")

    plan_p = restore_sub.add_parser("plan", help="Generate a restore pick list.")
    plan_p.add_argument("snapshot_id", help="Rustic snapshot ID to restore.")
    plan_p.add_argument("--repo", type=str, required=True,
                        help="Repository name containing the snapshot.")

    exec_p = restore_sub.add_parser("exec", help="Execute a restore.")
    exec_p.add_argument("snapshot_id", help="Rustic snapshot ID to restore.")
    exec_p.add_argument("target_path", type=Path, help="Target directory for restored files.")
    exec_p.add_argument("--repo", type=str, required=True,
                        help="Repository name containing the snapshot.")
    exec_p.add_argument("--password-file", type=Path, required=True,
                        help="Path to the repository password file.")
    exec_p.add_argument("--cache-dir", type=Path, default=None,
                        help="Directory for the restore cache.")
    exec_p.add_argument("--volume-dir", type=Path, default=None,
                        help="Directory containing extracted volume data "
                             "(skips interactive disc prompts).")

    # --- consolidate ---
    cons_p = subparsers.add_parser("consolidate", help="Merge volumes into a larger one.")
    cons_p.add_argument("volume_ids", type=int, nargs="+",
                        help="Volume IDs to consolidate.")
    cons_p.add_argument("--target-media", type=str, default="MDISC100",
                        help="Target media type for consolidated volume.")

    # --- verify ---
    verify_p = subparsers.add_parser("verify", help="Verify a volume.")
    verify_p.add_argument("volume_label", help="Label of the volume to verify.")

    # --- db ---
    db_p = subparsers.add_parser("db", help="Database operations.")
    db_sub = db_p.add_subparsers(dest="db_command")
    db_sub.add_parser("export", help="Export catalog summary as JSON.")

    # --- meta ---
    meta_p = subparsers.add_parser(
        "meta",
        help="Build a self-contained rescue volume (tools + source).",
    )
    meta_sub = meta_p.add_subparsers(dest="meta_command")

    meta_build = meta_sub.add_parser(
        "build",
        help="Build a meta-volume directory with all restore tools.",
    )
    meta_build.add_argument(
        "--output", "-o", type=Path, required=True,
        help="Output directory for the meta-volume.",
    )
    meta_build.add_argument(
        "--project-root", type=Path, default=None,
        help="LCSAS project root (default: auto-detect).",
    )

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize the LCSAS database."""
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all

    db_path = args.db_path
    conn = get_connection(db_path)
    create_all(conn)
    conn.close()
    print(f"Initialized LCSAS database at {db_path}")
    return 0


def cmd_repo_add(args: argparse.Namespace) -> int:
    """Register a new repository."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.utils.labels import generate_uuid

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    create_all(conn)

    repo_id = generate_uuid()
    register_repo(
        conn,
        repo_id=repo_id,
        name=args.name,
        mirror_path=str(args.mirror_path.resolve()),
        encryption_key_id="",
    )
    conn.close()
    print(f"Registered repository '{args.name}' (id: {repo_id})")
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    """List registered repositories."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import create_all

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    create_all(conn)

    repos = list_repos(conn)
    conn.close()

    if not repos:
        print("No repositories registered.")
        return 0

    for repo in repos:
        print(f"  {repo.name:<20} {repo.repo_id}  {repo.mirror_path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan mirrors for new packs and register them in the catalog."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.schema import create_all
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    create_all(conn)

    repo_filter = set(args.repo) if args.repo else None
    total_new = 0
    total_scanned = 0

    for repo_name, repo_cfg in config.repositories.items():
        if repo_filter and repo_name not in repo_filter:
            continue

        mirror_path = repo_cfg.mirror_path
        packs_on_disk = scan_mirror_packs(mirror_path)
        total_scanned += len(packs_on_disk)

        analyzer = DeltaAnalyzer(conn, packs_on_disk, repo_name)
        new_packs = analyzer.register_new_packs()
        unarchived = analyzer.get_unarchived()
        unarchived_bytes = analyzer.get_total_unarchived_bytes()

        total_new += len(new_packs)

        print(f"  {repo_name}:")
        print(f"    Packs on disk:  {len(packs_on_disk)}")
        print(f"    Newly registered: {len(new_packs)}")
        print(f"    Unarchived:     {len(unarchived)} ({unarchived_bytes:,} bytes)")

    summary = get_archive_status_summary(conn)
    conn.close()

    print(f"\nTotal scanned: {total_scanned} packs across "
          f"{len(config.repositories)} repos")
    print(f"New packs registered: {total_new}")
    print(f"Archive: {summary['total']} total, "
          f"{summary['archived']} archived, "
          f"{summary['unarchived']} unarchived")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show archive status summary."""
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.schema import create_all
    from lcsas.db.volumes import list_volumes

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    create_all(conn)

    summary = get_archive_status_summary(conn)
    volumes = list_volumes(conn)
    conn.close()

    print(f"Packs: {summary['total']} total, "
          f"{summary['archived']} archived, "
          f"{summary['unarchived']} unarchived, "
          f"{summary['pruned']} pruned")
    print(f"Volumes: {len(volumes)} total")
    for v in volumes:
        print(f"  {v.label:<25} {v.media_type:<10} {v.status:<10} {v.location}")
    return 0


def cmd_db_export(args: argparse.Namespace) -> int:
    """Export catalog summary as JSON."""
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import create_all
    from lcsas.db.volumes import list_volumes

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    create_all(conn)

    export = {
        "status": get_archive_status_summary(conn),
        "volumes": [
            {"label": v.label, "media_type": v.media_type,
             "status": v.status, "location": v.location}
            for v in list_volumes(conn)
        ],
        "repositories": [
            {"repo_id": r.repo_id, "name": r.name, "mirror_path": r.mirror_path}
            for r in list_repos(conn)
        ],
    }
    conn.close()

    print(json.dumps(export, indent=2))
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    """Stage ISOs for deferred burning."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = load_config(args.config) if args.config else None
    if config is None:
        print("Error: --config is required for stage.", file=sys.stderr)
        return 1

    conn = get_connection(args.db or config.db_path)
    create_all(conn)

    orch = BurnOrchestrator(
        config, conn, SubprocessXorrisoRunner(), SubprocessDVDisasterRunner(),
    )

    if args.clean:
        session_ref = args.session or "latest"
        orch.clean_session(session_ref)
        print(f"Cleaned session: {session_ref}")
        conn.close()
        return 0

    media_type = None
    if args.media:
        media_type = MediaType[args.media]

    result = orch.stage(
        media_type=media_type,
        for_location=args.for_location,
        repo_ids=args.repo,
        skip_ecc=args.skip_ecc,
    )

    print(f"Session: {result.session_id}")
    print(f"Staged {len(result.manifests)} volume(s):")
    for m in result.manifests:
        iso_size = m.iso_path.stat().st_size if m.iso_path and m.iso_path.exists() else 0
        print(f"  {m.iso_path}  ({iso_size / 1e9:.1f} GB, {len(m.selected_packs)} packs)")
    print(f"Manifest: {result.staging_dir / 'session.json'}")
    conn.close()
    return 0


def cmd_burn_session(args: argparse.Namespace) -> int:
    """Burn a staged session to disc."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = load_config(args.config) if args.config else None
    if config is None:
        print("Error: --config is required for burn.", file=sys.stderr)
        return 1

    conn = get_connection(args.db or config.db_path)
    create_all(conn)

    orch = BurnOrchestrator(
        config, conn, SubprocessXorrisoRunner(), SubprocessDVDisasterRunner(),
    )

    location = args.location or config.default_location
    receipts = orch.burn_session(
        session_ref=args.session,
        location=location,
        device=args.device,
    )

    print(f"Burned {len(receipts)} volume(s) to {location}:")
    for r in receipts:
        print(f"  {r.volume_label} → {r.pack_count} packs")
    conn.close()
    return 0


def cmd_location(args: argparse.Namespace) -> int:
    """Handle location subcommands."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all

    config = load_config(args.config) if args.config else None
    if config is None:
        print("Error: --config is required for location.", file=sys.stderr)
        return 1

    conn = get_connection(args.db or config.db_path)
    create_all(conn)

    if args.location_command == "list":
        from lcsas.db.locations import list_locations
        from lcsas.db.queries import get_location_summary

        locations = list_locations(conn)
        if not locations:
            print("No locations registered.")
            conn.close()
            return 0

        summaries = get_location_summary(conn)
        summary_map = {s["location"]: s for s in summaries}

        for loc in locations:
            s = summary_map.get(loc.name, {"volumes": 0, "packs": 0, "missing": 0})
            status = "all current" if s["missing"] == 0 else f"{s['missing']} packs behind"
            print(f"  {loc.name:<20} {s['volumes']} volumes, {s['packs']} packs, {status}")

    elif args.location_command == "add":
        from lcsas.db.locations import create_location
        create_location(conn, args.name, args.description)
        print(f"Added location: {args.name}")

    elif args.location_command == "status":
        from lcsas.db.queries import get_packs_missing_at_location, get_packs_at_location

        at_loc = get_packs_at_location(conn, args.name)
        missing = get_packs_missing_at_location(conn, args.name)

        print(f"Location: {args.name}")
        print(f"  Packs archived here: {len(at_loc)}")
        print(f"  Packs missing: {len(missing)}")
        if missing:
            # Group by repo
            by_repo: dict[str, list] = {}
            for p in missing:
                repo = p.repo_id or "unknown"
                by_repo.setdefault(repo, []).append(p)
            for repo, packs in sorted(by_repo.items()):
                total_size = sum(p.size_bytes for p in packs)
                print(f"    repo={repo}: {len(packs)} packs ({total_size / 1e9:.1f} GB)")

    elif args.location_command == "move":
        from lcsas.db.volume_copies import move_volume_copy
        from lcsas.db.volumes import get_volume_by_label

        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            print(f"Error: Volume '{args.volume_label}' not found.", file=sys.stderr)
            conn.close()
            return 1
        move_volume_copy(conn, vol.volume_id, args.from_location, args.to_location)
        print(f"Moved {args.volume_label}: {args.from_location} → {args.to_location}")

    else:
        print("Usage: lcsas location {list|add|status|move}", file=sys.stderr)
        conn.close()
        return 1

    conn.close()
    return 0


def cmd_catalog_import(args: argparse.Namespace) -> int:
    """Import burn receipts from remote burns."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.schema import create_all
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import get_volume_by_label

    config = load_config(args.config) if args.config else None
    if config is None:
        print("Error: --config is required for catalog.", file=sys.stderr)
        return 1

    conn = get_connection(args.db or config.db_path)
    create_all(conn)

    imported = 0
    for receipt_file in args.receipt_files:
        with open(receipt_file) as f:
            receipt = json.load(f)

        vol = get_volume_by_label(conn, receipt["volume_label"])
        if vol is None:
            print(f"Warning: Volume '{receipt['volume_label']}' not found, skipping.",
                  file=sys.stderr)
            continue

        ensure_location(conn, receipt["location"])
        add_volume_copy(
            conn,
            volume_id=vol.volume_id,
            location=receipt["location"],
            burn_date=receipt.get("burn_date", ""),
        )
        imported += 1

    print(f"Imported {imported} receipt(s).")
    conn.close()
    return 0


def cmd_meta_build(args: argparse.Namespace) -> int:
    """Build a self-contained meta-volume with all restore tools."""
    from lcsas.meta.builder import MetaVolumeBuilder

    output = args.output.resolve()
    builder = MetaVolumeBuilder(
        output_dir=output,
        project_root=args.project_root,
    )

    print(f"Building meta-volume in {output} ...")
    try:
        builder.build()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Ensure restic, xorriso, and python3 are installed.", file=sys.stderr)
        return 1

    print(f"Meta-volume built successfully at {output}")
    print("Contents:")
    print("  tools/          Portable restic, xorriso, python3 + libraries")
    print("  lcsas/          LCSAS source code")
    print("  restore.sh      Bootstrap restore script")
    print("  README_RESTORE.md  Restore instructions")
    return 0


def cmd_restore_plan(args: argparse.Namespace) -> int:
    """Generate a restore pick list for a snapshot."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    create_all(conn)

    # Resolve repo config
    repo_name = args.repo
    if repo_name not in config.repositories:
        print(f"Error: repository '{repo_name}' not found in config.",
              file=sys.stderr)
        print(f"  Available: {', '.join(config.repositories.keys())}",
              file=sys.stderr)
        conn.close()
        return 1

    repo_cfg = config.repositories[repo_name]

    # Get required pack hashes via rustic dry-run
    runner = SubprocessRusticRunner()
    plan = runner.restore_dry_run(
        snapshot_id=args.snapshot_id,
        repo_path=repo_cfg.mirror_path,
        password_file=repo_cfg.password_file,
    )

    # Generate pick list
    planner = RestorePlanner(conn)
    pick_list = planner.generate_pick_list(plan.required_pack_hashes)
    conn.close()

    # Display results
    print(f"Restore Pick List for snapshot {args.snapshot_id}")
    print(f"  Repository: {repo_name}")
    print(f"  Required packs: {len(plan.required_pack_hashes)}")
    print()

    if pick_list.volumes:
        for label, packs in sorted(pick_list.volumes.items()):
            total = sum(p.size_bytes for p in packs)
            print(f"  {label:<30} {len(packs):>4} packs  "
                  f"({total / (1024 * 1024):.1f} MB)")
        print()
        print(f"  Total: {pick_list.total_packs} packs across "
              f"{len(pick_list.volumes)} volumes "
              f"({pick_list.total_bytes / (1024 * 1024):.1f} MB)")

    if pick_list.missing_packs:
        print(f"\n  WARNING: {len(pick_list.missing_packs)} packs not found "
              f"in any volume!")
        for sha in pick_list.missing_packs[:10]:
            print(f"    {sha}")
        if len(pick_list.missing_packs) > 10:
            print(f"    ... and {len(pick_list.missing_packs) - 10} more")

    return 0


def cmd_restore_exec(args: argparse.Namespace) -> int:
    """Execute a restore operation."""
    import tempfile

    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.restore.executor import RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    create_all(conn)

    repo_name = args.repo
    if repo_name not in config.repositories:
        print(f"Error: repository '{repo_name}' not found in config.",
              file=sys.stderr)
        conn.close()
        return 1

    repo_cfg = config.repositories[repo_name]
    runner = SubprocessRusticRunner()

    # Get required pack hashes
    plan = runner.restore_dry_run(
        snapshot_id=args.snapshot_id,
        repo_path=repo_cfg.mirror_path,
        password_file=args.password_file,
    )

    # Generate pick list
    planner = RestorePlanner(conn)
    pick_list = planner.generate_pick_list(plan.required_pack_hashes)
    conn.close()

    if pick_list.missing_packs:
        print(f"Error: {len(pick_list.missing_packs)} required packs not "
              f"found in any volume.", file=sys.stderr)
        return 1

    # Set up cache directory
    cache_dir = args.cache_dir
    cleanup_cache = False
    if cache_dir is None:
        cache_dir = Path(tempfile.mkdtemp(prefix="lcsas-restore-"))
        cleanup_cache = True
    ensure_dir(cache_dir)

    executor = RestoreExecutor(runner)

    # Prepare cache with metadata from the repo mirror
    metadata_source = repo_cfg.mirror_path
    executor.prepare_cache(cache_dir, metadata_source)

    print(f"Restore cache: {cache_dir}")
    print(f"Need packs from {len(pick_list.volumes)} volumes")

    # Ingest packs from volumes
    if args.volume_dir:
        # Non-interactive: all volume data is pre-extracted in one directory
        vol_dir = args.volume_dir
        for label, packs in pick_list.volumes.items():
            pack_hashes = [p.sha256 for p in packs]
            # Try label-named subdirectory first, then the dir itself
            vol_path = vol_dir / label
            if not vol_path.is_dir():
                vol_path = vol_dir
            ingested = executor.ingest_volume(cache_dir, vol_path, pack_hashes)
            print(f"  {label}: ingested {ingested} packs")
    else:
        # Interactive: prompt user to mount each volume
        for label, packs in sorted(pick_list.volumes.items()):
            pack_hashes = [p.sha256 for p in packs]
            while True:
                mount_path = input(
                    f"\nMount volume '{label}' and enter mount path "
                    f"(or 'skip' to skip): "
                ).strip()
                if mount_path.lower() == "skip":
                    print(f"  Skipping {label}")
                    break
                vol_path = Path(mount_path)
                if not vol_path.is_dir():
                    print(f"  '{mount_path}' is not a directory, try again.")
                    continue
                ingested = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                )
                print(f"  Ingested {ingested} packs from {label}")
                break

    # Execute restore
    target = args.target_path.resolve()
    print(f"\nRestoring snapshot {args.snapshot_id} → {target}")
    executor.execute_restore(
        cache_dir=cache_dir,
        snapshot_id=args.snapshot_id,
        target_path=target,
        password_file=args.password_file,
    )
    print("Restore complete!")

    # Cleanup temporary cache
    if cleanup_cache:
        from lcsas.utils.fs import safe_remove_tree
        safe_remove_tree(cache_dir)

    return 0


def dispatch(args: argparse.Namespace) -> int:
    """Route parsed args to the appropriate command handler."""
    if args.command == "init":
        return cmd_init(args)
    elif args.command == "repo":
        if args.repo_command == "add":
            return cmd_repo_add(args)
        elif args.repo_command == "list":
            return cmd_repo_list(args)
    elif args.command == "scan":
        return cmd_scan(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "db" and args.db_command == "export":
        return cmd_db_export(args)
    elif args.command == "stage":
        return cmd_stage(args)
    elif args.command == "burn":
        if args.session:
            return cmd_burn_session(args)
        # Legacy burn (prepare + execute single volume)
        print(f"Command 'burn' without --session not yet implemented.", file=sys.stderr)
        return 1
    elif args.command == "location":
        return cmd_location(args)
    elif args.command == "catalog":
        if args.catalog_command == "import-receipts":
            return cmd_catalog_import(args)
    elif args.command == "restore":
        if args.restore_command == "plan":
            return cmd_restore_plan(args)
        elif args.restore_command == "exec":
            return cmd_restore_exec(args)
    elif args.command == "meta":
        if args.meta_command == "build":
            return cmd_meta_build(args)

    # Commands requiring more infrastructure (consolidate, verify)
    # will be wired up in later phases
    print(f"Command '{args.command}' not yet implemented.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        return dispatch(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
