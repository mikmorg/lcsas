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

    # --- status ---
    subparsers.add_parser("status", help="Show archive status summary.")

    # --- burn ---
    burn_p = subparsers.add_parser("burn", help="Prepare and execute a burn cycle.")
    burn_p.add_argument("--media", type=str, default=None,
                        help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    burn_p.add_argument("--repo", type=str, default=None, nargs="*",
                        help="Specific repository IDs to burn.")
    burn_p.add_argument("--iso-only", type=Path, default=None,
                        help="Create ISO file at this path without burning to disc.")
    burn_p.add_argument("--skip-ecc", action="store_true",
                        help="Skip DVDisaster ECC augmentation.")

    # --- restore ---
    restore_p = subparsers.add_parser("restore", help="Plan or execute a restore.")
    restore_sub = restore_p.add_subparsers(dest="restore_command")

    plan_p = restore_sub.add_parser("plan", help="Generate a restore pick list.")
    plan_p.add_argument("snapshot_id", help="Rustic snapshot ID to restore.")

    exec_p = restore_sub.add_parser("exec", help="Execute a restore.")
    exec_p.add_argument("snapshot_id", help="Rustic snapshot ID to restore.")
    exec_p.add_argument("target_path", type=Path, help="Target directory for restored files.")
    exec_p.add_argument("--password-file", type=Path, required=True,
                        help="Path to the repository password file.")
    exec_p.add_argument("--cache-dir", type=Path, default=None,
                        help="Directory for the restore cache.")

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


def dispatch(args: argparse.Namespace) -> int:
    """Route parsed args to the appropriate command handler."""
    if args.command == "init":
        return cmd_init(args)
    elif args.command == "repo":
        if args.repo_command == "add":
            return cmd_repo_add(args)
        elif args.repo_command == "list":
            return cmd_repo_list(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "db" and args.db_command == "export":
        return cmd_db_export(args)

    # Commands requiring more infrastructure (burn, restore, consolidate, verify)
    # will be wired up once all dependencies exist
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
