"""LCSAS command-line interface using argparse."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from lcsas import __version__
from lcsas.log import get_logger, setup_logging

logger = get_logger()


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
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="Show full tracebacks on errors.",
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
    scan_p.add_argument(
        "--no-snapshots", action="store_true", default=False,
        help="Skip snapshot listing (faster if rustic is slow).",
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
    burn_p.add_argument("--dry-run", "-n", action="store_true", default=False,
                        help="Show burn plan without making changes.")

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
    stage_p.add_argument("--dry-run", "-n", action="store_true", default=False,
                         help="Show staging plan without creating ISOs or DB rows.")

    # --- burn-iso ---
    burniso_p = subparsers.add_parser("burn-iso",
                                      help="Burn a single ISO file (standalone).")
    burniso_p.add_argument("iso_path", type=Path, help="Path to .iso file.")
    burniso_p.add_argument("--device", type=str, default="/dev/sr0",
                           help="Optical device path.")
    burniso_p.add_argument("--verify", action="store_true", default=True,
                           help="Verify after burning.")

    # --- staging ---
    staging_p = subparsers.add_parser("staging", help="Staging directory management.")
    staging_sub = staging_p.add_subparsers(dest="staging_command")
    staging_clean_p = staging_sub.add_parser(
        "clean", help="Remove orphaned staging directories.",
    )
    staging_clean_p.add_argument(
        "--force", action="store_true", default=False,
        help="Skip confirmation prompt.",
    )

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
    exec_p.add_argument("--skip-verify", action="store_true", default=False,
                        help="Skip SHA-256 verification of ingested packs.")

    # --- consolidate ---
    cons_p = subparsers.add_parser("consolidate", help="Merge volumes into a larger one.")
    cons_p.add_argument("volume_ids", type=int, nargs="+",
                        help="Volume IDs to consolidate.")
    cons_p.add_argument("--target-media", type=str, default="MDISC100",
                        help="Target media type for consolidated volume.")

    # --- verify ---
    verify_p = subparsers.add_parser("verify", help="Verify a volume's ISO or disc.")
    verify_p.add_argument("volume_label", help="Label of the volume to verify.")
    verify_p.add_argument("--iso", type=Path, default=None,
                          help="Path to the ISO file (auto-detected from session if omitted).")
    verify_p.add_argument("--disc", action="store_true", default=False,
                          help="Verify a burned disc instead of an ISO file.")
    verify_p.add_argument("--device", default="/dev/sr0",
                          help="Optical drive device (default: /dev/sr0).")

    # --- db ---
    db_p = subparsers.add_parser("db", help="Database operations.")
    db_sub = db_p.add_subparsers(dest="db_command")
    db_sub.add_parser("export", help="Export catalog summary as JSON.")

    # --- config ---
    config_p = subparsers.add_parser("config", help="Configuration management.")
    config_sub = config_p.add_subparsers(dest="config_command")
    config_sub.add_parser("check", help="Validate TOML config file.")

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
    try:
        create_all(conn)
    finally:
        conn.close()
    logger.info(f"Initialized LCSAS database at {db_path}")
    return 0


def cmd_repo_add(args: argparse.Namespace) -> int:
    """Register a new repository."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.utils.labels import generate_uuid

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    try:
        create_all(conn)

        repo_id = generate_uuid()
        register_repo(
            conn,
            repo_id=repo_id,
            name=args.name,
            mirror_path=str(args.mirror_path.resolve()),
            encryption_key_id="",
        )
    finally:
        conn.close()
    logger.info(f"Registered repository '{args.name}' (id: {repo_id})")
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    """List registered repositories."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import create_all

    db_path = args.db or Path("archive.db")
    conn = get_connection(db_path)
    try:
        create_all(conn)
        repos = list_repos(conn)
    finally:
        conn.close()

    if not repos:
        logger.info("No repositories registered.")
        return 0

    for repo in repos:
        logger.info(f"  {repo.name:<20} {repo.repo_id}  {repo.mirror_path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan mirrors for new packs and register them in the catalog."""
    import json as _json

    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.schema import create_all
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    config = load_config(args.config)
    with locked_connection(config.db_path if args.db is None else args.db) as conn:
        create_all(conn)

        repo_filter = set(args.repo) if args.repo else None
        total_new = 0
        total_scanned = 0

        # Warn about unknown repo names
        if repo_filter:
            unknown = repo_filter - set(config.repositories.keys())
            for name in sorted(unknown):
                logger.warning(f"repository '{name}' not found in config, skipping.")

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

            logger.info(f"  {repo_name}:")
            logger.info(f"    Packs on disk:  {len(packs_on_disk)}")
            logger.info(f"    Newly registered: {len(new_packs)}")
            logger.info(f"    Unarchived:     {len(unarchived)} ({unarchived_bytes:,} bytes)")

        # Persist snapshots (unless --no-snapshots)
        if not getattr(args, "no_snapshots", False):
            from lcsas.db.models import Snapshot
            from lcsas.db.repos import list_repos
            from lcsas.db.snapshots import bulk_upsert_snapshots
            from lcsas.rustic.wrapper import SubprocessRusticRunner

            runner = SubprocessRusticRunner(tmpdir=config.staging_path)
            repos_db = {r.name: r.repo_id for r in list_repos(conn)}
            total_snaps = 0

            for repo_name, repo_cfg in config.repositories.items():
                if repo_filter and repo_name not in repo_filter:
                    continue
                if repo_cfg.password_file is None:
                    continue
                try:
                    snap_infos = runner.snapshots(
                        repo_path=repo_cfg.mirror_path,
                        password_file=repo_cfg.password_file,
                    )
                except Exception as exc:
                    logger.warning(
                        f"  {repo_name}: snapshot listing failed: {exc}"
                    )
                    continue

                repo_id = repos_db.get(repo_name)
                db_snaps = [
                    Snapshot(
                        snapshot_id=si.snapshot_id,
                        repo_id=repo_id,
                        hostname=si.hostname,
                        timestamp=si.timestamp,
                        paths=_json.dumps(si.paths),
                        tags=_json.dumps(si.tags),
                        description="",
                    )
                    for si in snap_infos
                ]
                count = bulk_upsert_snapshots(conn, db_snaps)
                total_snaps += count

            if total_snaps:
                logger.info(f"  Snapshots persisted: {total_snaps}")

        summary = get_archive_status_summary(conn)

    logger.info(f"\nTotal scanned: {total_scanned} packs across "
               f"{len(config.repositories)} repos")
    logger.info(f"New packs registered: {total_new}")
    logger.info(f"Archive: {summary['total']} total, "
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
    try:
        create_all(conn)

        summary = get_archive_status_summary(conn)
        volumes = list_volumes(conn)
    finally:
        conn.close()

    logger.info(f"Packs: {summary['total']} total, "
               f"{summary['archived']} archived, "
               f"{summary['unarchived']} unarchived, "
               f"{summary['pruned']} pruned")
    logger.info(f"Volumes: {len(volumes)} total")
    for v in volumes:
        logger.info(f"  {v.label:<25} {v.media_type:<10} {v.status:<10} {v.location}")
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
    try:
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
    finally:
        conn.close()

    logger.info(json.dumps(export, indent=2))
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    """Validate a TOML configuration file."""
    from lcsas.config.settings import load_config, validate_config

    if args.config is None:
        logger.error("--config is required for config check.")
        return 1

    config = load_config(args.config)
    errors = validate_config(config)

    if not errors:
        logger.info("Configuration is valid.")
        return 0

    for err in errors:
        logger.error(f"  {err}")
    return 1


def cmd_staging_clean(args: argparse.Namespace) -> int:
    """Detect and remove orphaned staging directories."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.staging.cleanup import clean_orphaned_staging, detect_orphaned_staging

    if args.config is None:
        logger.error("--config is required for staging clean.")
        return 1

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    try:
        create_all(conn)
        orphans = detect_orphaned_staging(config, conn)
    finally:
        conn.close()

    if not orphans:
        logger.info("No orphaned staging directories found.")
        return 0

    logger.info(f"Found {len(orphans)} orphaned staging directory(ies):")
    for p in orphans:
        logger.info(f"  {p}")

    if not args.force:
        confirm = input("Remove these directories? [y/N] ").strip().lower()
        if confirm != "y":
            logger.info("Aborted.")
            return 0

    removed = clean_orphaned_staging(orphans)
    logger.info(f"Removed {removed} orphaned staging directory(ies).")
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    """Stage ISOs for deferred burning."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import create_all
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.utils.shutdown import ShutdownManager

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for stage.")
        return 1

    shutdown = ShutdownManager()
    shutdown.install()

    try:
        with locked_connection(args.db or config.db_path) as conn:
            create_all(conn)

            orch = BurnOrchestrator(
                config, conn,
                SubprocessXorrisoRunner(tmpdir=config.staging_path),
                SubprocessDVDisasterRunner(tmpdir=config.staging_path),
            )

            if args.clean:
                session_ref = args.session or "latest"
                orch.clean_session(session_ref)
                logger.info(f"Cleaned session: {session_ref}")
                return 0

            media_type = None
            if args.media:
                try:
                    media_type = MediaType[args.media]
                except KeyError:
                    valid = ", ".join(m.name for m in MediaType)
                    logger.error(f"Unknown media type '{args.media}'. "
                                 f"Valid types: {valid}")
                    return 1

            result = orch.stage(
                media_type=media_type,
                for_location=args.for_location,
                repo_ids=args.repo,
                skip_ecc=args.skip_ecc,
                dry_run=getattr(args, "dry_run", False),
            )

            if getattr(args, "dry_run", False):
                return 0

            logger.info(f"Session: {result.session_id}")
            logger.info(f"Staged {len(result.manifests)} volume(s):")
            for m in result.manifests:
                iso_size = m.iso_path.stat().st_size if m.iso_path and m.iso_path.exists() else 0
                logger.info(f"  {m.iso_path}  ({iso_size / 1e9:.1f} GB, {len(m.selected_packs)} packs)")
            logger.info(f"Manifest: {result.staging_dir / 'session.json'}")
        return 0
    finally:
        shutdown.uninstall()


def cmd_burn_session(args: argparse.Namespace) -> int:
    """Burn a staged session to disc."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import create_all
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for burn.")
        return 1

    with locked_connection(args.db or config.db_path) as conn:
        create_all(conn)

        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        location = args.location or config.default_location

        if getattr(args, "dry_run", False):
            from lcsas.db.sessions import get_session_volumes, resolve_session_id
            sid = resolve_session_id(conn, args.session or "latest")
            vols = get_session_volumes(conn, sid)
            logger.info(f"[DRY RUN] Session {sid}: {len(vols)} volume(s)")
            for v in vols:
                logger.info(f"  {v['volume_label']}  status={v['status']}")
            return 0

        receipts = orch.burn_session(
            session_ref=args.session,
            location=location,
            device=args.device,
        )

    logger.info(f"Burned {len(receipts)} volume(s) to {location}:")
    for r in receipts:
        logger.info(f"  {r.volume_label} → {r.pack_count} packs")
    return 0


def cmd_burn_legacy(args: argparse.Namespace) -> int:
    """Legacy burn: stage + burn in a single command."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import create_all
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = load_config(args.config)
    with locked_connection(config.db_path if args.db is None else args.db) as conn:
        create_all(conn)

        media_type = None
        if args.media:
            try:
                media_type = MediaType[args.media]
            except KeyError:
                valid = ", ".join(m.name for m in MediaType)
                logger.error(f"Unknown media type '{args.media}'. "
                             f"Valid types: {valid}")
                return 1

        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        # Stage first
        result = orch.stage(
            media_type=media_type,
            for_location=args.location,
            repo_ids=args.repo,
            skip_ecc=args.skip_ecc,
            dry_run=getattr(args, "dry_run", False),
        )
        logger.info(f"Session: {result.session_id}")
        logger.info(f"Staged {len(result.manifests)} volume(s)")

        if getattr(args, "dry_run", False):
            return 0

        # Then burn
        location = args.location or "default"
        device = args.device or config.optical_device
        receipts = orch.burn_session(
            session_ref=result.session_id,
            location=location,
            device=device,
        )

    logger.info(f"Burned {len(receipts)} volume(s) to {location}:")
    for r in receipts:
        logger.info(f"  {r.volume_label} → {r.pack_count} packs")
    return 0


def cmd_burn_iso(args: argparse.Namespace) -> int:
    """Burn a single ISO file to optical media (standalone)."""
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    iso_path = args.iso_path
    if not iso_path.exists():
        logger.error(f"ISO file not found: {iso_path}")
        return 1

    runner = SubprocessXorrisoRunner()
    device = args.device

    logger.info(f"Burning {iso_path} to {device} ...")
    runner.burn_iso(iso_path, device=device)
    logger.info("Burn complete.")

    if args.verify:
        logger.info(f"Verifying disc on {device} ...")
        ok = runner.verify_disc(device=device)
        logger.info(f"  Verify: {'PASS' if ok else 'FAIL'}")
        if not ok:
            return 1

    return 0


def cmd_location(args: argparse.Namespace) -> int:
    """Handle location subcommands."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.utils.labels import sanitize_name

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for location.")
        return 1

    conn = get_connection(args.db or config.db_path)
    try:
        create_all(conn)

        if args.location_command == "list":
            from lcsas.db.locations import list_locations
            from lcsas.db.queries import get_location_summary

            locations = list_locations(conn)
            if not locations:
                logger.info("No locations registered.")
                return 0

            summaries = get_location_summary(conn)
            summary_map = {s["location"]: s for s in summaries}

            for loc in locations:
                s = summary_map.get(loc.name, {"volumes": 0, "packs": 0, "missing": 0})
                status = "all current" if s["missing"] == 0 else f"{s['missing']} packs behind"
                logger.info(f"  {loc.name:<20} {s['volumes']} volumes, {s['packs']} packs, {status}")

        elif args.location_command == "add":
            from lcsas.db.locations import create_location
            name = sanitize_name(args.name, "location name")
            create_location(conn, name, args.description)
            logger.info(f"Added location: {name}")

        elif args.location_command == "status":
            from lcsas.db.queries import get_packs_missing_at_location, get_packs_at_location

            at_loc = get_packs_at_location(conn, args.name)
            missing = get_packs_missing_at_location(conn, args.name)

            logger.info(f"Location: {args.name}")
            logger.info(f"  Packs archived here: {len(at_loc)}")
            logger.info(f"  Packs missing: {len(missing)}")
            if missing:
                # Group by repo
                by_repo: dict[str, list] = {}
                for p in missing:
                    repo = p.repo_id or "unknown"
                    by_repo.setdefault(repo, []).append(p)
                for repo, packs in sorted(by_repo.items()):
                    total_size = sum(p.size_bytes for p in packs)
                    logger.info(f"    repo={repo}: {len(packs)} packs ({total_size / 1e9:.1f} GB)")

        elif args.location_command == "move":
            from lcsas.db.volume_copies import move_volume_copy
            from lcsas.db.volumes import get_volume_by_label

            vol = get_volume_by_label(conn, args.volume_label)
            if vol is None:
                logger.error(f"Volume '{args.volume_label}' not found.")
                return 1
            move_volume_copy(conn, vol.volume_id, args.from_location, args.to_location)
            logger.info(f"Moved {args.volume_label}: {args.from_location} → {args.to_location}")

        else:
            logger.error("Usage: lcsas location {list|add|status|move}")
            return 1
    finally:
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
        logger.error("--config is required for catalog.")
        return 1

    conn = get_connection(args.db or config.db_path)
    try:
        create_all(conn)

        imported = 0
        for receipt_file in args.receipt_files:
            with open(receipt_file) as f:
                receipt = json.load(f)

            # Validate required receipt fields
            missing = [k for k in ("volume_label", "location") if k not in receipt]
            if missing:
                logger.warning(f"Receipt '{receipt_file}' missing keys: "
                               f"{', '.join(missing)}, skipping.")
                continue

            vol = get_volume_by_label(conn, receipt["volume_label"])
            if vol is None:
                logger.warning(f"Volume '{receipt['volume_label']}' not found, skipping.")
                continue

            ensure_location(conn, receipt["location"])
            add_volume_copy(
                conn,
                volume_id=vol.volume_id,
                location=receipt["location"],
                burn_date=receipt.get("burn_date", ""),
            )
            imported += 1
    finally:
        conn.close()

    logger.info(f"Imported {imported} receipt(s).")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Plan and display volume consolidation."""
    from lcsas.config.media import MediaType
    from lcsas.config.settings import load_config
    from lcsas.consolidate.merger import VolumeMerger
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all

    config = load_config(args.config) if args.config else None
    db_path = args.db or (config.db_path if config else Path("archive.db"))
    conn = get_connection(db_path)
    try:
        create_all(conn)

        try:
            media_type = MediaType[args.target_media]
        except KeyError:
            valid = ", ".join(m.name for m in MediaType)
            logger.error(f"Unknown media type '{args.target_media}'. "
                         f"Valid types: {valid}")
            return 1

        merger = VolumeMerger(conn)
        plan = merger.plan_consolidation(args.volume_ids, media_type)
    finally:
        conn.close()

    logger.info("Consolidation Plan:")
    logger.info(f"  Source volumes: {', '.join(plan.source_labels)}")
    logger.info(f"  Active packs:  {len(plan.active_packs)}")
    logger.info(f"  Total size:    {plan.total_active_bytes / 1e9:.1f} GB")
    logger.info(f"  Target media:  {plan.target_media_type.name}")
    logger.info(f"  Volumes needed: {plan.volumes_needed}")
    logger.info("")
    logger.info("To execute: stage the active packs onto new volumes,")
    logger.info("then burn and deprecate the source volumes.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a volume's ISO image or burned disc."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.db.volumes import get_volume_by_label

    config = load_config(args.config) if args.config else None
    db_path = args.db or (config.db_path if config else Path("archive.db"))
    conn = get_connection(db_path)
    try:
        create_all(conn)

        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1

        # Find ISO path from session_volumes if not explicitly provided
        iso_path = args.iso
        if iso_path is None and not args.disc:
            row = conn.execute(
                "SELECT iso_path FROM session_volumes WHERE volume_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (vol.volume_id,),
            ).fetchone()
            if row and row["iso_path"]:
                iso_path = Path(row["iso_path"])
            else:
                logger.error("No ISO path found for this volume. "
                             "Use --iso to specify one, or --disc to verify a burned disc.")
                return 1
    finally:
        conn.close()

    passed = True

    if args.disc:
        from lcsas.iso.xorriso import SubprocessXorrisoRunner
        runner = SubprocessXorrisoRunner()
        logger.info(f"Verifying disc on {args.device} ...")
        ok = runner.verify_disc(device=args.device)
        logger.info(f"  Disc verify: {'PASS' if ok else 'FAIL'}")
        if not ok:
            passed = False
    else:
        if not iso_path.exists():
            logger.error(f"ISO file not found: {iso_path}")
            return 1

        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
        dvd_runner = SubprocessDVDisasterRunner()
        logger.info(f"Verifying ISO: {iso_path}")
        ok = dvd_runner.verify_iso(iso_path)
        logger.info(f"  ECC verify: {'PASS' if ok else 'FAIL'}")
        if not ok:
            passed = False

    return 0 if passed else 1


def cmd_meta_build(args: argparse.Namespace) -> int:
    """Build a self-contained meta-volume with all restore tools."""
    from lcsas.meta.builder import MetaVolumeBuilder

    output = args.output.resolve()
    builder = MetaVolumeBuilder(
        output_dir=output,
        project_root=args.project_root,
    )

    logger.info(f"Building meta-volume in {output} ...")
    try:
        builder.build()
    except FileNotFoundError as e:
        logger.error(f"{e}")
        logger.error("Ensure restic, xorriso, and python3 are installed.")
        return 1

    logger.info(f"Meta-volume built successfully at {output}")
    logger.info("Contents:")
    logger.info("  tools/          Portable restic, xorriso, python3 + libraries")
    logger.info("  lcsas/          LCSAS source code")
    logger.info("  restore.sh      Bootstrap restore script")
    logger.info("  README_RESTORE.md  Restore instructions")
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
    try:
        create_all(conn)

        # Resolve repo config
        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            logger.error(f"  Available: {', '.join(config.repositories.keys())}")
            return 1

        repo_cfg = config.repositories[repo_name]

        # Get required pack hashes via rustic dry-run
        runner = SubprocessRusticRunner(tmpdir=config.staging_path)
        plan = runner.restore_dry_run(
            snapshot_id=args.snapshot_id,
            repo_path=repo_cfg.mirror_path,
            password_file=repo_cfg.password_file,
        )

        # Generate pick list
        planner = RestorePlanner(conn)
        pick_list = planner.generate_pick_list(plan.required_pack_hashes)
    finally:
        conn.close()

    # Display results
    logger.info(f"Restore Pick List for snapshot {args.snapshot_id}")
    logger.info(f"  Repository: {repo_name}")
    logger.info(f"  Required packs: {len(plan.required_pack_hashes)}")
    logger.info("")

    if pick_list.volumes:
        for label, packs in sorted(pick_list.volumes.items()):
            total = sum(p.size_bytes for p in packs)
            logger.info(f"  {label:<30} {len(packs):>4} packs  "
                       f"({total / (1024 * 1024):.1f} MB)")
        logger.info("")
        logger.info(f"  Total: {pick_list.total_packs} packs across "
                   f"{len(pick_list.volumes)} volumes "
                   f"({pick_list.total_bytes / (1024 * 1024):.1f} MB)")

    if pick_list.missing_packs:
        logger.warning(f"\n  WARNING: {len(pick_list.missing_packs)} packs not found "
                       f"in any volume!")
        for sha in pick_list.missing_packs[:10]:
            logger.info(f"    {sha}")
        if len(pick_list.missing_packs) > 10:
            logger.info(f"    ... and {len(pick_list.missing_packs) - 10} more")

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
    from lcsas.utils.fs import ensure_dir, safe_remove_tree

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    try:
        create_all(conn)

        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            return 1

        repo_cfg = config.repositories[repo_name]
        runner = SubprocessRusticRunner(tmpdir=config.staging_path)

        # Get required pack hashes
        plan = runner.restore_dry_run(
            snapshot_id=args.snapshot_id,
            repo_path=repo_cfg.mirror_path,
            password_file=args.password_file,
        )

        # Generate pick list
        planner = RestorePlanner(conn)
        pick_list = planner.generate_pick_list(plan.required_pack_hashes)
    finally:
        conn.close()

    if pick_list.missing_packs:
        logger.error(f"{len(pick_list.missing_packs)} required packs not "
                     f"found in any volume.")
        return 1

    # Set up cache directory
    cache_dir = args.cache_dir
    cleanup_cache = False
    if cache_dir is None:
        cache_dir = Path(tempfile.mkdtemp(
            prefix="lcsas-restore-", dir=str(config.staging_path),
        ))
        cleanup_cache = True
    ensure_dir(cache_dir)

    from lcsas.utils.shutdown import ShutdownManager
    shutdown = ShutdownManager()
    if cleanup_cache:
        shutdown.register(lambda: safe_remove_tree(cache_dir))
    shutdown.install()

    try:
        executor = RestoreExecutor(runner)

        # Prepare cache with metadata from the repo mirror
        metadata_source = repo_cfg.mirror_path
        executor.prepare_cache(cache_dir, metadata_source)

        logger.info(f"Restore cache: {cache_dir}")
        logger.info(f"Need packs from {len(pick_list.volumes)} volumes")

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
                ingested = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                    verify=not args.skip_verify,
                )
                logger.info(f"  {label}: ingested {ingested} packs")
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
                        logger.info(f"  Skipping {label}")
                        break
                    vol_path = Path(mount_path)
                    if not vol_path.is_dir():
                        logger.info(f"  '{mount_path}' is not a directory, try again.")
                        continue
                    ingested = executor.ingest_volume(
                        cache_dir, vol_path, pack_hashes,
                        verify=not args.skip_verify,
                    )
                    logger.info(f"  Ingested {ingested} packs from {label}")
                    break

        # Execute restore
        target = args.target_path.resolve()
        logger.info(f"\nRestoring snapshot {args.snapshot_id} → {target}")
        executor.execute_restore(
            cache_dir=cache_dir,
            snapshot_id=args.snapshot_id,
            target_path=target,
            password_file=args.password_file,
        )
        logger.info("Restore complete!")
    finally:
        # Cleanup temporary cache
        if cleanup_cache:
            safe_remove_tree(cache_dir)
        shutdown.uninstall()

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
    elif args.command == "config":
        if args.config_command == "check":
            return cmd_config_check(args)
    elif args.command == "stage":
        return cmd_stage(args)
    elif args.command == "burn":
        if args.session:
            return cmd_burn_session(args)
        # Legacy burn: stage + burn in one shot
        return cmd_burn_legacy(args)
    elif args.command == "burn-iso":
        return cmd_burn_iso(args)
    elif args.command == "staging":
        if args.staging_command == "clean":
            return cmd_staging_clean(args)
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
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command == "consolidate":
        return cmd_consolidate(args)

    logger.error(f"Command '{args.command}' not yet implemented.")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=getattr(args, "verbose", False))

    if not args.command:
        parser.print_help()
        return 0

    try:
        return dispatch(args)
    except Exception as e:
        logger.error(f"{e}")
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
