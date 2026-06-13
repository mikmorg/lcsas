"""LCSAS command-line interface using argparse."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from lcsas import __version__
from lcsas.log import get_logger, setup_logging

logger = get_logger()


def _validate_config_or_exit(config: object, skip_staging: bool = False) -> bool:
    """Run validate_config() and log all errors.  Returns False if invalid.

    Pass ``skip_staging=True`` for commands (scan, restore) that do not
    write to the staging area and therefore do not need it to exist.
    """
    from lcsas.config.settings import LCSASConfig, validate_config

    if not isinstance(config, LCSASConfig):
        return True  # callers that pass non-config objects skip validation

    errors = validate_config(config)
    if skip_staging:
        errors = [e for e in errors if "staging_path" not in e]
    if errors:
        logger.error(
            "Configuration has %d error(s) — fix before running this command:",
            len(errors),
        )
        for err in errors:
            logger.error("  %s", err)
        return False
    return True


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
    init_p.add_argument("--db-path", type=Path, default=None,
                        help="Path for the SQLite database. "
                             "Overrides --config's [paths].database when set.")

    # --- repo ---
    repo_p = subparsers.add_parser("repo", help="Manage backup repositories.")
    repo_sub = repo_p.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Register a new repository.")
    repo_add.add_argument("name", help="Repository name (e.g., 'family').")
    repo_add.add_argument("mirror_path", type=Path, help="Path to the local mirror.")

    repo_sub.add_parser("list", help="List registered repositories.")

    repo_rm = repo_sub.add_parser("remove", help="Remove a repository.")
    repo_rm.add_argument("repo_id", help="Repository ID to remove.")
    repo_rm.add_argument("--force", action="store_true",
                         help="Force removal even if packs exist (marks them pruned).")

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
    scan_p.add_argument(
        "--no-prune-sync", action="store_true", default=False,
        help="Don't mark packs as pruned when absent from mirror.",
    )
    scan_p.add_argument(
        "--yes-prune", action="store_true", default=False,
        help="Confirm a mass-prune: allow one scan to mark more than "
             "max(10, 20%% of a repo's active packs) as pruned.",
    )

    # --- pack ---
    pack_p = subparsers.add_parser("pack", help="Manage individual packs in the catalog.")
    pack_sub = pack_p.add_subparsers(dest="pack_command")

    pack_unprune_p = pack_sub.add_parser(
        "unprune",
        help="Restore a wrongly-pruned pack to the active pool.",
    )
    pack_unprune_p.add_argument(
        "sha256",
        help="SHA-256 of the pack (a unique prefix is accepted).",
    )

    # --- status ---
    status_p = subparsers.add_parser("status", help="Show archive status summary.")
    status_p.add_argument("--stale-copies", action="store_true", default=False,
                          help="List physical copies never verified or not "
                               "verified within the threshold (FMA-05).")
    status_p.add_argument("--older-than-days", type=int, default=365,
                          help="Staleness threshold in days for --stale-copies "
                               "and the summary warning (default: 365).")
    status_p.add_argument("--redundancy", action="store_true", default=False,
                          help="Blast-radius report: under-replicated packs "
                               "grouped by the disc holding them (FMA-08).")
    status_p.add_argument("--min-copies", type=int, default=2,
                          help="Copy threshold for --redundancy (default: 2).")

    # --- burn ---
    burn_p = subparsers.add_parser("burn", help="Burn staged ISOs to disc.")
    burn_p.add_argument("--media", type=str, default=None,
                        help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    burn_p.add_argument("--repo", type=str, default=None, nargs="*",
                        help="Specific repository IDs to burn.")
    burn_p.add_argument("--session", type=str, required=True,
                        help="Burn a previously staged session (ID or 'latest'). "
                             "Required; use `lcsas stage` first to create a session.")
    burn_p.add_argument("--location", type=str, default=None,
                        help="Physical location tag for this copy. Must "
                             "already be registered (use `lcsas location "
                             "add` first or pass --create-location).")
    burn_p.add_argument("--create-location", action="store_true", default=False,
                        help="Create the --location row if it does not "
                             "already exist (otherwise unknown names are "
                             "rejected to guard against typos).")
    burn_p.add_argument("--device", type=str, default=None,
                        help="Optical device path (overrides config).")
    burn_p.add_argument("--dry-run", "-n", action="store_true", default=False,
                        help="Show burn plan without making changes.")

    # --- stage ---
    stage_p = subparsers.add_parser(
        "stage",
        help="Stage ISOs for deferred burning.",
        epilog=(
            "Staging reads every selected pack in full and verifies its "
            "SHA-256 against the catalog before mastering (corrupt mirror "
            "packs abort the stage). Expect one extra full read of all "
            "staged data, e.g. ~14 minutes for 25 GB at 30 MB/s."
        ),
    )
    stage_p.add_argument("--media", type=str, default=None,
                         help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    stage_p.add_argument("--for-location", type=str, default=None,
                         help="Stage only packs missing at this location.")
    stage_p.add_argument("--repo", type=str, default=None, nargs="*",
                         help="Specific repository IDs to stage.")
    stage_p.add_argument("--clean", action="store_true",
                         help="Clean up staged ISOs for a session.")
    stage_p.add_argument("--force", action="store_true",
                         help="With --clean: also abort a never-burned "
                              "session, deleting its volumes and returning "
                              "their packs to the unarchived pool.")
    stage_p.add_argument("--session", type=str, default=None,
                         help="Session ID (for --clean).")
    stage_p.add_argument("--dry-run", "-n", action="store_true", default=False,
                         help="Show staging plan without creating ISOs or DB rows.")
    stage_p.add_argument("--allow-escrow-drift", action="store_true", default=False,
                         help="Proceed even if lcsas.toml's key_split/K/N "
                              "disagrees with the recorded split (KEY-08). "
                              "Logged as a volume event. For multi-config "
                              "edge cases only — discs may print share "
                              "instructions that do not match the real split.")

    # --- burn-iso ---
    burniso_p = subparsers.add_parser(
        "burn-iso",
        help="Burn a single ISO file (standalone, no DB access required).",
        description=(
            "Burn a single ISO to disc. Useful for split-machine workflows: "
            "stage on one machine, transfer ISOs to a faster burner, then "
            "import the emitted receipts back with 'catalog import-receipts'."
        ),
    )
    burniso_p.add_argument("iso_path", type=Path, help="Path to .iso file.")
    burniso_p.add_argument("--device", type=str, default="/dev/sr0",
                           help="Optical device path.")
    burniso_p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                           help="Verify after burning (use --no-verify to skip).")
    burniso_p.add_argument("--emit-receipt", type=Path, default=None,
                           help="Write a burn receipt JSON to this path "
                                "(file or directory) for later catalog import.")
    burniso_p.add_argument("--label", type=str, default=None,
                           help="Volume label for the receipt. Defaults to the "
                                "ISO's parent directory name.")
    burniso_p.add_argument("--location", type=str, default=None,
                           help="Location tag for the receipt "
                                "(required with --emit-receipt).")
    burniso_p.add_argument("--session", type=str, default="",
                           help="Optional session ID to record in the receipt.")

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

    # --- copy ---
    copy_p = subparsers.add_parser(
        "copy",
        help="Record the fate of physical disc copies (deprecate/destroy).",
    )
    copy_sub = copy_p.add_subparsers(dest="copy_command")

    copy_dep_p = copy_sub.add_parser(
        "deprecate",
        help="Record a disc copy as deprecated (e.g. damaged, retired).",
    )
    copy_dep_p.add_argument("volume_label",
                            help="Volume label (e.g. ARCHIVE_MDISC100_0001).")
    copy_dep_p.add_argument("location",
                            help="Location holding the copy (e.g. Home_Shelf).")

    copy_des_p = copy_sub.add_parser(
        "destroy",
        help="Record a disc copy as destroyed or lost.",
    )
    copy_des_p.add_argument("volume_label",
                            help="Volume label (e.g. ARCHIVE_MDISC100_0001).")
    copy_des_p.add_argument("location",
                            help="Location that held the copy (e.g. Home_Shelf).")

    # --- volume ---
    volume_p = subparsers.add_parser(
        "volume", help="Per-volume catalog queries.",
    )
    volume_sub = volume_p.add_subparsers(dest="volume_command")

    vol_impact_p = volume_sub.add_parser(
        "impact",
        help="Blast radius: what becomes unrestorable if every copy of "
             "this volume fails? (FMA-08)",
    )
    vol_impact_p.add_argument("volume_label",
                              help="Volume label (e.g. ARCHIVE_MDISC100_0001).")
    vol_impact_p.add_argument(
        "--snapshots", action="store_true", default=False,
        help="Also list snapshots referencing at-risk packs (needs the "
             "live mirror, the rustic binary, and --config with the "
             "repo's password_file).",
    )

    # --- catalog ---
    cat_p = subparsers.add_parser("catalog", help="Catalog management.")
    cat_sub = cat_p.add_subparsers(dest="catalog_command")
    cat_import_p = cat_sub.add_parser("import-receipts",
                                      help="Import burn receipts from remote burns.")
    cat_import_p.add_argument("receipt_files", nargs="+",
                              help="Receipt JSON files.")

    cat_validate_p = cat_sub.add_parser(
        "validate",
        help="Cross-check a mounted disc's data files against its embedded catalog.",
    )
    cat_validate_p.add_argument(
        "disc",
        type=Path,
        help="Path to a mounted LCSAS disc (must contain catalog.db and data/).",
    )
    cat_validate_p.add_argument(
        "--content",
        action="store_true",
        default=False,
        help="Also read every pack file on the disc and verify its SHA-256 "
             "against the filename hash (detects bit-rot; reads the full "
             "data payload of the disc).",
    )

    cat_reconcile_p = cat_sub.add_parser(
        "reconcile",
        help="Report (and optionally fix) catalog/physical disagreements: "
             "ghost volumes that were never burned, and durable volumes "
             "with no ACTIVE copy record.",
    )
    cat_reconcile_p.add_argument(
        "--fix", action="store_true", default=False,
        help="Delete ghost volumes (never-burned STAGING/BURNING volumes "
             "with zero copies), returning their packs to the unarchived "
             "pool. Asks for confirmation unless --yes is given.",
    )
    cat_reconcile_p.add_argument(
        "--yes", action="store_true", default=False,
        help="With --fix: skip the interactive confirmation.",
    )
    cat_reconcile_p.add_argument(
        "--older-than-hours", type=int, default=24,
        help="Treat STAGING/BURNING volumes as ghosts only when older than "
             "this many hours (default: 24; protects in-flight sessions).",
    )

    cat_rebuild_p = cat_sub.add_parser(
        "rebuild",
        help="Rebuild master catalog by merging disc-embedded holographic catalogs.",
    )
    cat_rebuild_p.add_argument(
        "disc_dirs",
        nargs="+",
        type=Path,
        help="Paths to mounted LCSAS discs (each must contain catalog.db).",
    )
    cat_rebuild_p.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path for the rebuilt master catalog (created or merged into).",
    )

    # --- restore ---
    restore_p = subparsers.add_parser("restore", help="Plan or execute a restore.")
    restore_sub = restore_p.add_subparsers(dest="restore_command")

    plan_p = restore_sub.add_parser("plan", help="Generate a restore pick list.")
    plan_p.add_argument("snapshot_id", help="Rustic snapshot ID to restore.")
    plan_p.add_argument("--repo", type=str, required=True,
                        help="Repository name containing the snapshot.")

    exec_p = restore_sub.add_parser(
        "exec",
        help="Execute a restore (requires live mirrors and config file).",
        description=(
            "Restore a snapshot using the live rustic mirror and LCSAS config. "
            "Requires --repo and --password-file. "
            "For offline disc-only restore (no mirrors or config needed), "
            "use: lcsas restore standalone"
        ),
    )
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
    exec_p.add_argument("--iso-dir", type=Path, default=None,
                        help="Directory holding original <label>.iso files alongside the "
                             "mounted discs.  When supplied, each volume's ISO is "
                             "SHA-256-verified against the catalog before its packs are "
                             "read.  Phase 21.9 — applies to interactive restore where "
                             "--volume-dir is not used.")
    exec_p.add_argument("--skip-verify", action="store_true", default=False,
                        help="Skip SHA-256 verification of ingested packs.")

    # restore standalone — disc-only restore without config or mirror
    fromdisc_p = restore_sub.add_parser(
        "standalone",
        help="Restore directly from disc — no config file or mirror needed.",
        description=(
            "Restore from a mounted or extracted LCSAS disc without any config "
            "file or live mirror. Works offline from any disc that contains "
            "catalog.db and the repository metadata. "
            "Falls back to pure-Python restore if rustic is not available."
        ),
    )
    fromdisc_p.add_argument(
        "disc", type=Path,
        help="Path to a mounted or extracted LCSAS disc (must contain catalog.db).",
    )
    fromdisc_p.add_argument(
        "target_path", type=Path,
        help="Directory to restore files into.",
    )
    fromdisc_p.add_argument(
        "--password-file", type=Path, required=True,
        help="Path to the repository encryption key / password file.",
    )
    fromdisc_p.add_argument(
        "--repo", type=str, default=None,
        help="Repository name to restore (auto-selected if only one repo on disc).",
    )
    fromdisc_p.add_argument(
        "--snapshot", type=str, default="latest",
        help="Snapshot ID to restore (default: latest).",
    )
    fromdisc_p.add_argument(
        "--volume-dir", type=Path, default=None,
        help="Directory containing all pre-extracted disc volumes for batch restore.",
    )
    fromdisc_p.add_argument(
        "--catalog", type=Path, default=None,
        help="Explicit path to catalog.db (default: <disc>/catalog.db).",
    )
    fromdisc_p.add_argument(
        "--cache-dir", type=Path, default=None,
        help="Reuse an existing restore cache directory instead of a temp dir.",
    )
    fromdisc_p.add_argument(
        "--skip-verify", action="store_true", default=False,
        help="Skip SHA-256 verification of ingested packs.",
    )

    # --- consolidate ---
    cons_p = subparsers.add_parser("consolidate", help="Merge volumes into a larger one.")
    cons_p.add_argument("volume_ids", type=int, nargs="+",
                        help="Volume IDs to consolidate.")
    cons_p.add_argument("--target-media", type=str, default="MDISC100",
                        help="Target media type for consolidated volume.")
    cons_p.add_argument("--execute", action="store_true", default=False,
                        help="Stage active packs (without deprecating source volumes).")
    cons_p.add_argument("--deprecate", action="store_true", default=False,
                        help="Mark source volumes as DEPRECATED (run after --execute succeeds).")

    # --- verify ---
    verify_p = subparsers.add_parser("verify", help="Verify a volume's ISO or disc.")
    verify_p.add_argument("volume_label", nargs="?", default=None,
                          help="Label of the volume to verify (omit with --all).")
    verify_p.add_argument("--iso", type=Path, default=None,
                          help="Path to the ISO file (auto-detected from session if omitted).")
    verify_p.add_argument("--disc", action="store_true", default=False,
                          help="Verify a burned disc instead of an ISO file.")
    verify_p.add_argument("--device", default="/dev/sr0",
                          help="Optical drive device (default: /dev/sr0).")
    verify_p.add_argument("--mark-verified", action="store_true", default=False,
                          help="Manually mark the volume as verified (remote workflow).")
    verify_p.add_argument("--mark-failed", action="store_true", default=False,
                          help="Record a verification failure without checking media.")
    verify_p.add_argument("--detail", default="",
                          help="Detail text for --mark-verified/--mark-failed event.")
    verify_p.add_argument("--all", action="store_true", default=False, dest="verify_all",
                          help="Verify all BURNED/VERIFIED volumes (batch mode). "
                               "Combine with --disc to re-verify physical discs "
                               "copy-by-copy, stamping last_verified_at.")
    verify_p.add_argument("--location", default=None,
                          help="Filter --all to copies/volumes at this location; "
                               "with single-volume --disc, stamp the copy at "
                               "this location (defaults to the volume's only "
                               "ACTIVE copy when unambiguous).")

    # --- session ---
    session_p = subparsers.add_parser("session", help="Manage burn sessions.")
    session_sub = session_p.add_subparsers(dest="session_command")
    sess_list_p = session_sub.add_parser("list", help="List all burn sessions.")
    sess_list_p.add_argument(
        "--status", type=str, default=None,
        help="Filter by status (STAGED, COMPLETE, PARTIAL, ABORTED).",
    )
    sess_abort_p = session_sub.add_parser(
        "abort",
        help="Abort a never-burned session: delete its volumes and return "
             "their packs to the unarchived pool.",
    )
    sess_abort_p.add_argument(
        "ref", nargs="?", default="latest",
        help="Session ID or 'latest' (default).",
    )
    sess_abort_p.add_argument(
        "--volume", type=str, default=None,
        help="Abort a single stranded STAGING volume by label instead of "
             "a whole session (for volumes with no session).",
    )

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
    meta_build.add_argument(
        "--allow-no-zstd", action="store_true",
        help=(
            "Build even if the native 'zstandard' package is missing on "
            "this host. The bundled pure-Python zstd decoder still enables "
            "tier-3 restore of compressed repos (slower); without this flag "
            "the build fails loud so the missing fast path is not a "
            "surprise."
        ),
    )

    meta_verify = meta_sub.add_parser(
        "verify",
        help="Audit a built meta-volume against its recovery/MANIFEST.sha256.",
        description=(
            "Reads every file listed in <output>/recovery/MANIFEST.sha256 "
            "and confirms each one's SHA-256 matches.  Reports missing "
            "files, mismatched hashes, and (when --strict) any files "
            "present under recovery/ that aren't listed in the manifest. "
            "Exits 0 when the meta-volume is intact, 1 on any issue."
        ),
    )
    meta_verify.add_argument(
        "output", type=Path,
        help="The meta-volume directory built by `lcsas meta build`.",
    )
    meta_verify.add_argument(
        "--strict", action="store_true",
        help="Also flag files present under recovery/ but absent from the manifest.",
    )

    # ── recovery ────────────────────────────────────────────────
    recovery_p = subparsers.add_parser(
        "recovery",
        help="Build / verify the C89 + POSIX-sh recovery toolchain.",
    )
    recovery_sub = recovery_p.add_subparsers(dest="recovery_command")

    rb = recovery_sub.add_parser(
        "build",
        help="Build the recovery binaries for host or cross-target arch.",
    )
    rb.add_argument(
        "--arch",
        choices=(
            "host",
            # Linux musl targets (Phase 21.10.b + 21.11).
            "x86_64", "aarch64", "armv7", "riscv64",
            # Windows-gnu targets via `zig cc` (Phase 21.10.b).
            "x86_64-windows", "aarch64-windows",
            # macOS targets via `zig cc -target X-macos` (Phase 21.12).
            "x86_64-macos", "aarch64-macos",
        ),
        default="host",
        help="Target architecture (default: host).  Must be one of "
             "`RecoveryBuilder.SUPPORTED_ARCHES`.",
    )
    rb.add_argument(
        "--cc", type=str, default=None,
        help="Override C compiler (default: cc for host, "
             "<arch>-linux-musl-gcc for cross).",
    )
    rb.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show full build output.",
    )

    rt = recovery_sub.add_parser(
        "test", help="Run the recovery toolchain unit-test suite.",
    )
    rt.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show full test output.",
    )

    rm = recovery_sub.add_parser(
        "manifest",
        help="Compute SHA-256 of every file in recovery/ -> MANIFEST.sha256.",
    )
    rm.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output manifest path (default: recovery/MANIFEST.sha256).",
    )

    recovery_sub.add_parser(
        "verify",
        help="Verify reproducible build: builds twice, byte-compares output.",
    )

    # --- key (Shamir / SLIP-0039 key escrow) ---
    key_p = subparsers.add_parser(
        "key",
        help="Split / combine a repo password into SLIP-0039 key shares.",
    )
    key_sub = key_p.add_subparsers(dest="key_command")

    key_split = key_sub.add_parser(
        "split",
        help="Split a repo password into K-of-N recoverable key shares.",
    )
    key_split.add_argument(
        "--repo", required=True,
        help="Repository name whose password to split.",
    )
    key_split.add_argument(
        "--threshold", "-k", type=int, default=None,
        help="K: shares required to reconstruct (default: config key_threshold).",
    )
    key_split.add_argument(
        "--shares", "-n", type=int, default=None,
        help="N: total shares to produce (default: config key_shares).",
    )
    key_split.add_argument(
        "--password-file", type=Path, default=None,
        help="Read the password from this file instead of the repo's "
             "configured password_file.",
    )
    key_split.add_argument(
        "--out", type=Path, default=None,
        help="Output directory for share files (default: ./keyshares-<repo>/).",
    )
    key_split.add_argument(
        "--no-verify-repo", action="store_true",
        help="Skip checking that the password actually unlocks the repo's "
             "key files. Escrowing an unverified password is dangerous; use "
             "only when the mirror's keys/ is unavailable at split time.",
    )

    key_verify = key_sub.add_parser(
        "verify",
        help="Verify that shares/a password actually unlock a repository "
             "(annual key drill).",
    )
    key_verify.add_argument(
        "--repo", required=True,
        help="Repository name whose key files to test against.",
    )
    key_verify.add_argument(
        "--share-file", action="append", default=None, dest="share_files",
        type=Path,
        help="A share or share-card file (repeatable). Supply at least the "
             "threshold (K) to reconstruct the password.",
    )
    key_verify.add_argument(
        "--password-file", type=Path, default=None,
        help="Verify this password file directly instead of reconstructing "
             "from shares.",
    )

    key_combine = key_sub.add_parser(
        "combine",
        help="Reconstruct a repo password from K key-share mnemonics.",
    )
    key_combine.add_argument(
        "--share-file", action="append", default=None, dest="share_files",
        type=Path,
        help="A file containing one share mnemonic (repeatable). "
             "If omitted, shares are read from stdin, one per line.",
    )
    key_combine.add_argument(
        "--out", type=Path, default=None,
        help="Write the reconstructed password here (default: stdout).",
    )

    key_card = key_sub.add_parser(
        "card",
        help="Print a paper Recovery Card for the DEFAULT (single-key) "
             "archive — for owners who do NOT split the password.",
    )
    key_card.add_argument(
        "--repo", default=None,
        help="Repository name the card is for (uses its configured "
             "password_file name and creation context).",
    )
    key_card.add_argument(
        "--out", type=Path, default=None,
        help="Write the card here (default: stdout). The password is never "
             "written — the owner fills the PASSWORD box by hand.",
    )
    key_card.add_argument(
        "--no-check-code", action="store_true",
        help="Omit the transcription check code. The check code is the first "
             "4 hex chars of SHA-256(password); it leaks ~16 bits of an "
             "oracle (negligible against a high-entropy password) and lets "
             "an heir confirm they typed the password correctly.",
    )
    key_card.add_argument(
        "--check", type=Path, default=None, metavar="PASSWORD_FILE",
        help="Verify mode: recompute the check code from a typed-in password "
             "file and print MATCH/MISMATCH against the code on the card. "
             "Requires --code.",
    )
    key_card.add_argument(
        "--code", default=None,
        help="The 4-char check code from a printed card, compared against "
             "--check's password file. rc 0 on MATCH, 1 on MISMATCH.",
    )

    estate_p = subparsers.add_parser(
        "estate",
        help="Estate-planning artifacts (whole-archive Recovery Card).",
    )
    estate_sub = estate_p.add_subparsers(dest="estate_command")
    estate_card = estate_sub.add_parser(
        "card",
        help="Generate a one-page whole-archive Recovery Card: owner, "
             "repositories, disc inventory, key scheme, and the literal "
             "first command an heir runs.",
    )
    estate_card.add_argument(
        "--output", "--out", dest="output", type=Path, default=None,
        help="Write the card here (default: stdout).",
    )

    return parser


# ---------------------------------------------------------------------------
# Helper — DB path resolution
# ---------------------------------------------------------------------------

def _resolve_db_path(
    args: argparse.Namespace,
    config: object | None = None,
) -> Path:
    """Resolve the database path from CLI args, config, or default.

    Priority: ``--db`` flag > config.db_path > ``archive.db`` (cwd).
    """
    if getattr(args, "db", None):
        return Path(args.db)
    if config is not None and hasattr(config, "db_path"):
        return Path(config.db_path)
    return Path("archive.db")


def _resolve_repo_names_to_ids(
    conn: sqlite3.Connection,
    names: list[str] | None,
) -> list[str] | None:
    """Map user-facing repo *names* to DB repo_ids (UUIDs).

    Returns ``None`` when *names* is ``None`` (no filter).  Logs a
    warning for names that don't map to any registered repository.
    """
    if names is None:
        return None
    from lcsas.db.repos import list_repos

    name_to_id = {r.name: r.repo_id for r in list_repos(conn)}
    ids: list[str] = []
    for n in names:
        rid = name_to_id.get(n)
        if rid is None:
            logger.warning(f"repository '{n}' not registered in DB, skipping.")
        else:
            ids.append(rid)
    if not ids:
        raise ValueError(
            f"None of the specified repositories exist in the DB: "
            f"{', '.join(names)}"
        )
    return ids


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize the LCSAS database.

    Resolution order for the catalog DB path:
      1. ``--db-path`` on ``init`` (when provided)
      2. global ``--db``
      3. ``paths.database`` from the TOML config (``--config``)
      4. ``archive.db`` in the current working directory
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema

    db_path: Path | None = getattr(args, "db_path", None)
    if db_path is None and getattr(args, "db", None):
        db_path = Path(args.db)
    if db_path is None and getattr(args, "config", None):
        from lcsas.config.settings import load_config
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            logger.error(f"--config path does not exist: {cfg_path}")
            return 1
        config = load_config(cfg_path)
        db_path = Path(config.db_path)
    if db_path is None:
        db_path = Path("archive.db")

    # Ensure parent directory exists (XDG paths may not exist yet)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    logger.info(f"Initialized LCSAS database at {db_path}")
    return 0


def cmd_repo_add(args: argparse.Namespace) -> int:
    """Register a new repository."""
    from lcsas.db.connection import locked_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import ensure_schema
    from lcsas.utils.fs import read_repo_key_ids
    from lcsas.utils.labels import generate_uuid

    db_path = _resolve_db_path(args)
    with locked_connection(db_path) as conn:
        ensure_schema(conn)

        repo_id = generate_uuid()
        mirror = args.mirror_path.resolve()

        # Auto-detect the encryption key ID from the repo's keys/ dir
        key_ids = read_repo_key_ids(mirror)
        encryption_key_id = key_ids[0] if key_ids else ""

        register_repo(
            conn,
            repo_id=repo_id,
            name=args.name,
            mirror_path=str(mirror),
            encryption_key_id=encryption_key_id,
        )
    logger.info(f"Registered repository '{args.name}' (id: {repo_id})")
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    """List registered repositories."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import ensure_schema

    db_path = _resolve_db_path(args)
    conn = get_connection(db_path)
    try:
        ensure_schema(conn)
        repos = list_repos(conn)
    finally:
        conn.close()

    if not repos:
        logger.info("No repositories registered.")
        return 0

    for repo in repos:
        logger.info(f"  {repo.name:<20} {repo.repo_id}  {repo.mirror_path}")
    return 0


def cmd_repo_remove(args: argparse.Namespace) -> int:
    """Remove a repository from the catalog."""
    from lcsas.db.connection import locked_connection
    from lcsas.db.packs import bulk_mark_pruned, list_packs
    from lcsas.db.repos import delete_repo, get_repo
    from lcsas.db.schema import ensure_schema
    from lcsas.db.snapshots import delete_snapshots_for_repo
    from lcsas.db.volume_packs import get_volume_ids_for_pack

    db_path = _resolve_db_path(args)
    with locked_connection(db_path) as conn:
        ensure_schema(conn)

        try:
            repo = get_repo(conn, args.repo_id)
        except ValueError:
            logger.error(f"Repository '{args.repo_id}' not found.")
            return 1

        # Check for active (non-pruned) packs
        active_packs = list_packs(conn, repo_id=repo.repo_id, include_pruned=False)

        # Check if any active packs are on active volumes
        packs_on_volumes = []
        for p in active_packs:
            vols = get_volume_ids_for_pack(conn, p.pack_id)
            if vols:
                packs_on_volumes.append(p)

        if packs_on_volumes and not args.force:
            logger.error(
                f"Repository '{repo.name}' has {len(packs_on_volumes)} "
                f"pack(s) on active volumes. Use --force to remove anyway."
            )
            return 1

        if active_packs and not args.force:
            logger.error(
                f"Repository '{repo.name}' has {len(active_packs)} "
                f"active pack(s). Use --force to mark them pruned and remove."
            )
            return 1

        # Confirmation prompt when using --force
        if args.force:
            all_packs = list_packs(conn, repo_id=repo.repo_id, include_pruned=True)
            snap_count = conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE repo_id = ?",
                (repo.repo_id,),
            ).fetchone()[0]
            logger.warning("")
            logger.warning(
                "This will DELETE from the catalog: %d pack(s) and %d snapshot(s).",
                len(all_packs), snap_count,
            )
            try:
                prompt = "Type 'yes' to confirm deletion, or anything else to cancel: "
                response = input(prompt).strip()
                if response.lower() != "yes":
                    logger.info("Removal canceled.")
                    return 0
            except EOFError:
                logger.error("No terminal available for confirmation (redirected input).")
                return 1

        # Force mode: mark all packs as pruned
        if active_packs:
            pack_ids = [p.pack_id for p in active_packs]
            pruned = bulk_mark_pruned(conn, pack_ids)
            logger.info(f"Marked {pruned} pack(s) as pruned.")

        # Delete packs (including pruned), snapshots, then the repo itself.
        # Packs must be removed before the repo to satisfy FK constraints.
        all_packs = list_packs(conn, repo_id=repo.repo_id, include_pruned=True)
        if all_packs:
            all_pack_ids = [p.pack_id for p in all_packs]
            # Remove volume_packs links first (FK on pack_id)
            for pid in all_pack_ids:
                conn.execute(
                    "DELETE FROM volume_packs WHERE pack_id = ?", (pid,)
                )
            conn.execute(
                "DELETE FROM packs WHERE repo_id = ?", (repo.repo_id,)
            )
        snap_count = delete_snapshots_for_repo(conn, repo.repo_id)
        delete_repo(conn, repo.repo_id)

    logger.info(
        f"Removed repository '{repo.name}' "
        f"({snap_count} snapshot(s) deleted)."
    )
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan mirrors for new packs and register them in the catalog."""
    import json as _json

    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import ensure_schema
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    if args.config is None:
        logger.error("--config is required for scan.")
        return 1
    config = load_config(args.config)
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    with locked_connection(config.db_path if args.db is None else args.db) as conn:
        ensure_schema(conn)

        # Map config repo names → DB repo_ids (UUIDs)
        repos_db = {r.name: r.repo_id for r in list_repos(conn)}

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

            db_repo_id = repos_db.get(repo_name)
            if db_repo_id is None:
                logger.warning(
                    f"repository '{repo_name}' not registered in DB "
                    f"(run 'repo add' first), skipping."
                )
                continue

            mirror_path = repo_cfg.mirror_path
            scan_result = scan_mirror_packs(mirror_path)
            packs_on_disk = scan_result.packs
            total_scanned += len(packs_on_disk)

            analyzer = DeltaAnalyzer(conn, packs_on_disk, db_repo_id)
            new_packs = analyzer.register_new_packs()
            unarchived = analyzer.get_unarchived()
            unarchived_bytes = analyzer.get_total_unarchived_bytes()

            total_new += len(new_packs)

            # Prune sync: detect packs removed by rustic prune (BURN-09:
            # only on a complete scan, and never en masse unconfirmed —
            # registration above is additive and always safe).
            if not getattr(args, "no_prune_sync", False):
                if scan_result.errors:
                    logger.warning(
                        f"    Scan of {repo_name} was INCOMPLETE "
                        f"({len(scan_result.errors)} unreadable path(s)) — "
                        f"prune-sync skipped for this repo. "
                        f"Fix permissions/mount and re-scan."
                    )
                else:
                    pruned = analyzer.detect_pruned()
                    if pruned:
                        from lcsas.db.packs import bulk_mark_pruned, list_packs
                        active_total = len(list_packs(
                            conn, repo_id=db_repo_id, include_pruned=False,
                        ))
                        if (len(pruned) > max(10, 0.2 * active_total)
                                and not getattr(args, "yes_prune", False)):
                            logger.warning(
                                f"    Refusing to mark {len(pruned)}/{active_total} "
                                f"packs of {repo_name} pruned in one scan — this "
                                f"usually means the mirror is partially "
                                f"unavailable. Re-run with --yes-prune to confirm "
                                f"rustic really pruned them."
                            )
                        else:
                            pruned_ids = [p.pack_id for p in pruned]
                            pruned_bytes = sum(p.size_bytes for p in pruned)
                            marked = bulk_mark_pruned(conn, pruned_ids)
                            logger.info(
                                f"    Pruned packs:   {marked} "
                                f"({pruned_bytes:,} bytes)"
                            )

            logger.info(f"  {repo_name}:")
            logger.info(f"    Packs on disk:  {len(packs_on_disk)}")
            logger.info(f"    Newly registered: {len(new_packs)}")
            logger.info(f"    Unarchived:     {len(unarchived)} ({unarchived_bytes:,} bytes)")

        # Persist snapshots (unless --no-snapshots)
        if not getattr(args, "no_snapshots", False):
            from lcsas.db.models import Snapshot
            from lcsas.db.snapshots import bulk_upsert_snapshots
            from lcsas.exceptions import BinaryError
            from lcsas.rustic.wrapper import SubprocessRusticRunner
            from lcsas.utils.subprocess import check_binary_version

            try:
                check_binary_version("rustic", min_version=(0, 9, 0))
            except BinaryError as exc:
                logger.error("%s", exc)
                return 1

            runner = SubprocessRusticRunner(tmpdir=config.staging_path)
            total_snaps = 0

            for repo_name, repo_cfg in config.repositories.items():
                if repo_filter and repo_name not in repo_filter:
                    continue
                if repo_cfg.password_file is None:
                    logger.debug(
                        f"  {repo_name}: no password_file configured, "
                        f"skipping snapshot listing"
                    )
                    continue

                repo_id = repos_db.get(repo_name)
                if repo_id is None:
                    logger.warning(
                        f"  {repo_name}: not registered in DB, skipping snapshots"
                    )
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
               f"{summary['staged']} staged, "
               f"{summary['unarchived']} unarchived")
    return 0


def cmd_pack_unprune(args: argparse.Namespace) -> int:
    """Restore a wrongly-pruned pack to the active pool (BURN-09)."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.packs import unmark_pruned
    from lcsas.db.schema import ensure_schema

    config = load_config(args.config) if args.config else None
    db_path = _resolve_db_path(args, config)

    prefix = args.sha256.strip().lower()
    if not prefix:
        logger.error("Empty SHA-256 prefix.")
        return 1

    with locked_connection(db_path) as conn:
        ensure_schema(conn)
        # substr() prefix match: immune to LIKE wildcards in user input.
        rows = conn.execute(
            "SELECT pack_id, sha256, is_pruned FROM packs "
            "WHERE substr(sha256, 1, ?) = ? ORDER BY sha256",
            (len(prefix), prefix),
        ).fetchall()

        if not rows:
            logger.error(f"No pack matches prefix '{prefix}'.")
            return 1
        if len(rows) > 1:
            logger.error(
                f"Prefix '{prefix}' is ambiguous ({len(rows)} matches) — "
                f"give more characters:"
            )
            for row in rows[:10]:
                logger.error(f"  {row['sha256']}")
            if len(rows) > 10:
                logger.error(f"  … and {len(rows) - 10} more")
            return 1

        row = rows[0]
        if not row["is_pruned"]:
            logger.info(
                f"Pack {row['sha256']} is already active — nothing to do."
            )
            return 0
        unmark_pruned(conn, row["pack_id"])
        logger.info(f"Pack {row['sha256']} restored to the active pool.")
    return 0


def _print_stale_copies(conn: sqlite3.Connection, older_than_days: int) -> int:
    """Report ACTIVE copies overdue for disc re-verification (FMA-05)."""
    from datetime import UTC, datetime

    from lcsas.db.volume_copies import find_stale_copies

    stale = find_stale_copies(conn, older_than_days)
    if not stale:
        logger.info(
            f"All ACTIVE copies verified within the last "
            f"{older_than_days} days."
        )
        return 0
    now = datetime.now(UTC)
    logger.info(
        f"{len(stale)} cop(ies) not verified in the last "
        f"{older_than_days} days:"
    )
    for label, location, last_verified_at in stale:
        if last_verified_at is None:
            age = "never verified"
        else:
            days = (now - datetime.fromisoformat(last_verified_at)).days
            age = f"{days} days ago ({last_verified_at[:10]})"
        logger.info(f"  {label:<25} {location:<15} {age}")
    logger.info("Re-verify with: lcsas verify --all --disc")
    return 0


def _print_redundancy_report(conn: sqlite3.Connection, min_copies: int) -> int:
    """Blast-radius view: under-replicated packs grouped by disc (FMA-08).

    Answers "which discs are single points of failure, and for how much
    data?" from the catalog alone.  Per-disc detail (repos, snapshots)
    lives in ``lcsas volume impact <LABEL>``.
    """
    from lcsas.db.models import Pack, Volume
    from lcsas.db.queries import get_live_volumes_for_packs, get_redundancy_report
    from lcsas.db.volume_copies import get_copies_for_volume

    under = get_redundancy_report(conn, min_copies=min_copies)
    if not under:
        logger.info(
            f"All packs have at least {min_copies} live cop(ies) — no "
            f"single disc is a point of failure at this threshold."
        )
        return 0

    live_map = get_live_volumes_for_packs(conn, [p.pack_id for p in under])

    by_volume: dict[int, list[Pack]] = {}
    vol_info: dict[int, Volume] = {}
    on_no_disc: list[Pack] = []
    for p in under:
        holders = live_map.get(p.pack_id, [])
        if not holders:
            on_no_disc.append(p)
            continue
        for v in holders:
            vol_info[v.volume_id] = v
            by_volume.setdefault(v.volume_id, []).append(p)

    total_bytes = sum(p.size_bytes for p in under)
    logger.info(
        f"{len(under)} pack(s) ({total_bytes / 1e9:.2f} GB) have fewer "
        f"than {min_copies} live cop(ies):"
    )
    for vol_id in sorted(by_volume, key=lambda v: vol_info[v].label):
        v = vol_info[vol_id]
        packs = by_volume[vol_id]
        sole = [p for p in packs if len(live_map[p.pack_id]) == 1]
        locs = sorted({c.location for c in get_copies_for_volume(conn, vol_id)})
        loc_str = ", ".join(locs) if locs else "<no ACTIVE copy recorded>"
        logger.info("")
        logger.info(f"  {v.label}  [{v.status}]  ACTIVE copies at: {loc_str}")
        if sole:
            sole_bytes = sum(p.size_bytes for p in sole)
            logger.info(
                f"    only durable holder of {len(sole)} pack(s) "
                f"({sole_bytes / 1e9:.2f} GB) — lose every copy of this "
                f"disc and they are gone"
            )
        if len(packs) > len(sole):
            logger.info(
                f"    plus {len(packs) - len(sole)} under-replicated "
                f"pack(s) shared with other discs"
            )
    if on_no_disc:
        logger.warning(
            "WARNING: %d pack(s) (%.2f GB) are on NO disc at all — see "
            "the unarchived/staged buckets in 'lcsas status'.",
            len(on_no_disc),
            sum(p.size_bytes for p in on_no_disc) / 1e9,
        )
    logger.info("")
    logger.info("Per-disc blast radius: lcsas volume impact <LABEL>")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show archive status summary."""
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import (
        get_archive_status_summary,
        get_live_volumes_for_packs,
        get_redundancy_report,
    )
    from lcsas.db.schema import ensure_schema
    from lcsas.db.sessions import list_sessions
    from lcsas.db.volume_copies import find_stale_copies
    from lcsas.db.volume_events import get_latest_event
    from lcsas.db.volumes import list_volumes

    db_path = _resolve_db_path(args)
    conn = get_connection(db_path)
    try:
        ensure_schema(conn)

        if args.stale_copies:
            return _print_stale_copies(conn, args.older_than_days)
        if args.redundancy:
            return _print_redundancy_report(conn, args.min_copies)

        stale_count = len(find_stale_copies(conn, args.older_than_days))
        summary = get_archive_status_summary(conn)
        volumes = list_volumes(conn)
        partial_sessions = list_sessions(conn, status_filter="PARTIAL")
        # FMA-08: packs that live on exactly one physical disc.  Packs in
        # the <2-copies report with at least one live holder are on one
        # disc; zero-holder packs are the unarchived/staged buckets above.
        under = get_redundancy_report(conn, min_copies=2)
        live_map = get_live_volumes_for_packs(conn, [p.pack_id for p in under])
        single_disc_packs = [p for p in under if live_map.get(p.pack_id)]
        # BURN-05: a volume whose latest event is a verify failure has no
        # copy recorded for that burn — it needs a re-burn at the location.
        needs_reburn: list[tuple[str, str]] = []
        for v in volumes:
            ev = get_latest_event(conn, v.volume_id)
            if ev is not None and ev.event_type in (
                "VERIFY_FAIL", "VERIFY_FAIL_REBURN",
            ):
                needs_reburn.append((v.label, ev.location or "<unknown>"))
    finally:
        conn.close()

    logger.info(f"Packs: {summary['total']} total, "
               f"{summary['archived']} archived, "
               f"{summary['staged']} staged (NOT yet on disc), "
               f"{summary['unarchived']} unarchived, "
               f"{summary['pruned']} pruned")
    logger.info(f"Volumes: {len(volumes)} total")
    for v in volumes:
        logger.info(f"  {v.label:<25} {v.media_type:<10} {v.status:<10} {v.location}")
    if summary["staged"] > 0:
        logger.warning(
            "WARNING: %d pack(s) are staged on volumes that were never "
            "burned — run 'lcsas catalog reconcile' or burn the pending "
            "session.",
            summary["staged"],
        )
    for s in partial_sessions:
        logger.warning(
            "WARNING: session %s is PARTIAL — at least one burn failed "
            "verification and recorded no copy. Re-run: "
            "lcsas burn --session %s --location <location>",
            s.session_id, s.session_id,
        )
    for label, loc in needs_reburn:
        logger.warning("WARNING: %s needs re-burn at %s", label, loc)
    if single_disc_packs:
        logger.warning(
            "WARNING: %d pack(s) (%.1f GB) exist on only one disc — run "
            "'lcsas status --redundancy' for the blast-radius report.",
            len(single_disc_packs),
            sum(p.size_bytes for p in single_disc_packs) / 1e9,
        )
    if stale_count > 0:
        logger.warning(
            "WARNING: %d physical cop(ies) not verified in the last %d "
            "days — run 'lcsas status --stale-copies' to list them and "
            "'lcsas verify --all --disc' to re-verify.",
            stale_count, args.older_than_days,
        )
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
    from lcsas.db.schema import ensure_schema
    from lcsas.staging.cleanup import clean_orphaned_staging, detect_orphaned_staging

    if args.config is None:
        logger.error("--config is required for staging clean.")
        return 1

    config = load_config(args.config)
    conn = get_connection(config.db_path if args.db is None else args.db)
    try:
        ensure_schema(conn)
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
    from lcsas.db.schema import ensure_schema
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.utils.shutdown import ShutdownManager

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for stage.")
        return 1
    if not _validate_config_or_exit(config):
        return 1

    shutdown = ShutdownManager()
    shutdown.install()

    try:
        with locked_connection(args.db or config.db_path) as conn:
            ensure_schema(conn)

            orch = BurnOrchestrator(
                config, conn,
                SubprocessXorrisoRunner(tmpdir=config.staging_path),
                SubprocessDVDisasterRunner(tmpdir=config.staging_path),
                allow_escrow_drift=getattr(args, "allow_escrow_drift", False),
            )

            if args.clean:
                session_ref = args.session or "latest"
                try:
                    orch.clean_session(session_ref, force=args.force)
                except ValueError as e:
                    logger.error("%s", e)
                    return 1
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

            try:
                repo_ids = _resolve_repo_names_to_ids(conn, args.repo)
            except ValueError as e:
                logger.error(str(e))
                return 1

            from lcsas.db.key_escrow import EscrowDriftError
            try:
                result = orch.stage(
                    media_type=media_type,
                    for_location=args.for_location,
                    repo_ids=repo_ids,
                    dry_run=getattr(args, "dry_run", False),
                )
            except EscrowDriftError as e:
                logger.error("%s", e)
                logger.error(
                    "Refusing to stage. Pass --allow-escrow-drift to override "
                    "(discs may print wrong share instructions)."
                )
                return 1

            if getattr(args, "dry_run", False):
                return 0

            logger.info(f"Session: {result.session_id}")
            logger.info(f"Staged {len(result.manifests)} volume(s):")
            for m in result.manifests:
                iso_size = m.iso_path.stat().st_size if m.iso_path and m.iso_path.exists() else 0
                logger.info(
                    "  %s  (%.1f GB, %d packs)",
                    m.iso_path, iso_size / 1e9, len(m.selected_packs),
                )
            logger.info(f"Manifest: {result.staging_dir / 'session.json'}")
        return 0
    finally:
        shutdown.uninstall()


def cmd_burn_session(args: argparse.Namespace) -> int:
    """Burn a staged session to disc."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for burn.")
        return 1
    if not _validate_config_or_exit(config):
        return 1

    with locked_connection(args.db or config.db_path) as conn:
        ensure_schema(conn)

        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        location = args.location or config.default_location
        # Issue #19: reject unknown --location names rather than silently
        # auto-creating a phantom row. Users opt into creation with
        # --create-location (or by pre-registering via `location add`).
        from lcsas.db.locations import UnknownLocationError, resolve_location
        try:
            resolve_location(
                conn, location,
                create=getattr(args, "create_location", False),
            )
        except UnknownLocationError as exc:
            logger.error(str(exc))
            return 1

        if getattr(args, "dry_run", False):
            from lcsas.db.sessions import get_session_volumes, resolve_session_id
            from lcsas.db.volumes import get_volume_by_id
            sid = resolve_session_id(conn, args.session or "latest")
            vols = get_session_volumes(conn, sid)
            logger.info(f"[DRY RUN] Session {sid}: {len(vols)} volume(s)")
            for sv in vols:
                vol = get_volume_by_id(conn, sv.volume_id)
                logger.info(f"  {vol.label}  status={vol.status}")
            return 0

        device = args.device or config.optical_device
        if not getattr(args, "skip_burn", False):
            import os
            if not os.path.exists(device):
                logger.error(
                    "Optical device '%s' not found.\n"
                    "Insert a disc or specify the correct device with --device.",
                    device,
                )
                return 1

        receipts = orch.burn_session(
            session_ref=args.session,
            location=location,
            device=device,
        )

    logger.info(f"Burned {len(receipts)} volume(s) to {location}:")
    for r in receipts:
        if r.verify_passed:
            logger.info(f"  {r.volume_label} → {r.pack_count} packs")
        else:
            logger.error(
                f"  {r.volume_label} → VERIFY FAILED — no copy recorded; "
                f"needs re-burn at {location}"
            )
    failed = [r for r in receipts if not r.verify_passed]
    if failed:
        # BURN-05: the session stays PARTIAL; re-burning at the same
        # location records the copy normally once verification passes.
        logger.error(
            "%d volume(s) failed verification — session %s is PARTIAL. "
            "Inspect the disc/drive and re-run: "
            "lcsas burn --session %s --location %s",
            len(failed), failed[0].session_id,
            failed[0].session_id, location,
        )
        return 1
    # BURN-06: ISOs persist so the same session can be burned at more
    # locations; cleanup is the explicit `stage --clean` step.
    logger.info(
        "ISOs retained for additional copies. After burning all "
        "locations, free the staging space with: lcsas stage --clean"
    )
    return 0


def cmd_burn_iso(args: argparse.Namespace) -> int:
    """Burn a single ISO file to optical media (standalone)."""
    from datetime import UTC, datetime

    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    iso_path = args.iso_path
    if not iso_path.exists():
        logger.error(f"ISO file not found: {iso_path}")
        return 1

    emit_receipt = getattr(args, "emit_receipt", None)
    if emit_receipt is not None and not args.location:
        logger.error("--location is required when --emit-receipt is given.")
        return 1

    runner = SubprocessXorrisoRunner()
    device = args.device

    # Hash before burn — cheap insurance against the file changing under us.
    iso_sha256 = ""
    iso_size_bytes: int | None = None
    if emit_receipt is not None:
        from lcsas.utils.hashing import sha256_file
        iso_sha256 = sha256_file(iso_path)
        iso_size_bytes = iso_path.stat().st_size

    logger.info(f"Burning {iso_path} to {device} ...")
    runner.burn_iso(iso_path, device=device)
    logger.info("Burn complete.")

    verify_passed = True
    if args.verify:
        logger.info(f"Verifying disc on {device} ...")
        verify_passed = runner.verify_disc(device=device)
        logger.info(f"  Verify: {'PASS' if verify_passed else 'FAIL'}")

    if emit_receipt is not None:
        label = args.label or iso_path.parent.name
        receipt = {
            "volume_label": label,
            "session_id": args.session,
            "location": args.location,
            "device": device,
            "burn_date": datetime.now(UTC).isoformat(),
            "iso_sha256": iso_sha256,
            "iso_size_bytes": iso_size_bytes,
            "verify_passed": verify_passed,
        }
        # Accept either a directory (auto-name) or a full file path.
        if emit_receipt.is_dir():
            out_path = emit_receipt / f"{label}_{args.location}.json"
        else:
            emit_receipt.parent.mkdir(parents=True, exist_ok=True)
            out_path = emit_receipt
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        logger.info(f"Receipt written: {out_path}")

    if args.verify and not verify_passed:
        return 1

    return 0


def cmd_location(args: argparse.Namespace) -> int:
    """Handle location subcommands."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.utils.labels import sanitize_name

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for location.")
        return 1

    with locked_connection(args.db or config.db_path) as conn:
        ensure_schema(conn)

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
                status = (
                    "all current" if s["missing"] == 0
                    else f"{s['missing']} packs behind"
                )
                logger.info(
                    "  %-20s %d volumes, %d packs, %s",
                    loc.name, s["volumes"], s["packs"], status,
                )

        elif args.location_command == "add":
            from lcsas.db.locations import create_location
            name = sanitize_name(args.name, "location name")
            create_location(conn, name, args.description)
            logger.info(f"Added location: {name}")

        elif args.location_command == "status":
            from lcsas.db.queries import get_packs_at_location, get_packs_missing_at_location

            at_loc = get_packs_at_location(conn, args.name)
            missing = get_packs_missing_at_location(conn, args.name)

            logger.info(f"Location: {args.name}")
            logger.info(f"  Packs archived here: {len(at_loc)}")
            logger.info(f"  Packs missing: {len(missing)}")
            if missing:
                # Group by repo
                by_repo: dict[str, list[Any]] = {}
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
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    """Record a physical disc copy as deprecated or destroyed (BURN-10).

    This is the supported way to tell the catalog a disc is gone.  When
    the last ACTIVE copy of a volume is lost the volume auto-demotes to
    DEPRECATED, so the redundancy report and the deprecation guard stop
    counting it as a replica.
    """
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_copies import deprecate_copy, destroy_copy
    from lcsas.db.volumes import get_volume_by_label

    config = load_config(args.config) if args.config else None
    db_path = _resolve_db_path(args, config)

    with locked_connection(db_path) as conn:
        ensure_schema(conn)
        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1
        try:
            if args.copy_command == "deprecate":
                demoted = deprecate_copy(conn, vol.volume_id, args.location)
                verb = "deprecated"
            else:
                demoted = destroy_copy(conn, vol.volume_id, args.location)
                verb = "destroyed"
        except ValueError as exc:
            logger.error(str(exc))
            return 1
        logger.info(
            f"Recorded copy of {args.volume_label} at {args.location} "
            f"as {verb.upper()}."
        )
        if demoted:
            logger.warning(
                "%s has no ACTIVE copies left — volume auto-deprecated. "
                "Its packs now count in 'lcsas status' / the redundancy "
                "report; re-burn with: lcsas stage --for-location %s",
                args.volume_label, args.location,
            )
    return 0


def _print_snapshot_impact(
    conn: sqlite3.Connection,
    config: object | None,
    at_risk_by_repo: dict[str, set[str]],
    repo_names: dict[str, str],
) -> None:
    """List snapshots that reference at-risk packs (FMA-08 phase 2).

    Snapshot→pack mapping needs the live mirror + rustic (the same
    ``restore_dry_run`` machinery the restore planner uses); the catalog
    alone cannot provide it.  Every missing prerequisite degrades to a
    message — never an error — because the pack-level report already
    printed is complete catalog-only truth.
    """
    from lcsas.config.settings import LCSASConfig
    from lcsas.db.snapshots import list_snapshots
    from lcsas.exceptions import BinaryError
    from lcsas.utils.subprocess import check_binary_version

    degrade = ("snapshot impact needs the live mirror — pack-level "
               "report above is catalog-only")

    if not isinstance(config, LCSASConfig):
        logger.warning("%s (pass --config).", degrade)
        return
    try:
        check_binary_version("rustic", min_version=(0, 9, 0))
    except BinaryError as exc:
        logger.warning("%s (%s)", degrade, exc)
        return

    from lcsas.rustic.wrapper import SubprocessRusticRunner
    runner = SubprocessRusticRunner(tmpdir=config.staging_path)

    logger.info("")
    for repo_id in sorted(at_risk_by_repo, key=lambda r: repo_names.get(r, r)):
        at_risk_shas = at_risk_by_repo[repo_id]
        name = repo_names.get(repo_id, repo_id)
        repo_cfg = config.repositories.get(name)
        if repo_cfg is None or repo_cfg.password_file is None:
            logger.warning(
                "repo %s: %s (repo or password_file missing from "
                "--config).", name, degrade,
            )
            continue
        if not repo_cfg.mirror_path.is_dir():
            logger.warning(
                "repo %s: %s (mirror not mounted at %s).",
                name, degrade, repo_cfg.mirror_path,
            )
            continue
        snaps = list_snapshots(conn, repo_id=repo_id)
        if not snaps:
            logger.warning(
                "repo %s: no snapshots registered in the catalog — run "
                "'lcsas scan' first.", name,
            )
            continue
        affected = []
        for snap in snaps:
            try:
                plan = runner.restore_dry_run(
                    snapshot_id=snap.snapshot_id,
                    repo_path=repo_cfg.mirror_path,
                    password_file=repo_cfg.password_file,
                )
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                logger.warning(
                    "repo %s snapshot %s: dry-run failed (%s) — cannot "
                    "tell whether it is affected.",
                    name, snap.snapshot_id, exc,
                )
                continue
            if at_risk_shas & set(plan.required_pack_hashes):
                affected.append(snap)
        if not affected:
            logger.info(
                "repo %s: no snapshot references an at-risk pack.", name
            )
            continue
        logger.warning(
            "repo %s: %d snapshot(s) would become unrestorable:",
            name, len(affected),
        )
        for snap in affected:
            logger.warning(
                "  %s  %s  paths=%s",
                snap.snapshot_id, snap.timestamp or "<no timestamp>",
                snap.paths,
            )


def cmd_volume_impact(args: argparse.Namespace) -> int:
    """Blast radius for one disc: what is lost if ALL its copies fail?

    Pack-level (catalog-only) report always prints; ``--snapshots`` adds
    the snapshot-level view when the live mirror is available (FMA-08).
    """
    from datetime import UTC, datetime

    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.models import Pack
    from lcsas.db.queries import get_at_risk_packs_for_volume
    from lcsas.db.repos import list_repos
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volumes import get_volume_by_label

    config = load_config(args.config) if args.config else None
    db_path = _resolve_db_path(args, config)
    conn = get_connection(db_path)
    try:
        ensure_schema(conn)
        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1

        copies = get_copies_for_volume(conn, vol.volume_id, active_only=False)
        at_risk = get_at_risk_packs_for_volume(conn, vol.volume_id)
        repo_names = {r.repo_id: r.name for r in list_repos(conn)}

        logger.info(f"Volume {vol.label}  [{vol.status}]  {vol.media_type}")
        if not any(c.status == "ACTIVE" for c in copies):
            logger.warning(
                "  No ACTIVE physical copies recorded — this disc may "
                "already be lost."
            )
        now = datetime.now(UTC)
        for c in copies:
            if c.last_verified_at:
                days = (now - datetime.fromisoformat(c.last_verified_at)).days
                age = f"last verified {days} day(s) ago ({c.last_verified_at[:10]})"
            else:
                age = "never verified"
            logger.info(f"  copy at {c.location:<20} [{c.status}]  {age}")

        logger.info("")
        if not at_risk:
            logger.info(
                "Blast radius: NONE — every pack on this volume has "
                "another live copy."
            )
            return 0

        by_repo: dict[str, list[Pack]] = {}
        for p in at_risk:
            by_repo.setdefault(p.repo_id, []).append(p)
        total = sum(p.size_bytes for p in at_risk)
        logger.warning(
            "Blast radius: if every copy of %s fails, %d pack(s) "
            "(%.2f GB) become unrestorable:",
            vol.label, len(at_risk), total / 1e9,
        )
        for repo_id in sorted(by_repo, key=lambda r: repo_names.get(r, r)):
            packs = by_repo[repo_id]
            logger.warning(
                "  repo %-15s %d pack(s) (%.2f GB)",
                repo_names.get(repo_id, repo_id), len(packs),
                sum(p.size_bytes for p in packs) / 1e9,
            )

        if args.snapshots:
            _print_snapshot_impact(
                conn, config,
                {rid: {p.sha256 for p in packs}
                 for rid, packs in by_repo.items()},
                repo_names,
            )
    finally:
        conn.close()
    return 0


def cmd_catalog_import(args: argparse.Namespace) -> int:
    """Import burn receipts from remote burns.

    Also transitions volume status for receipts of STAGING volumes:
      - verify_passed=true  → STAGING → BURNING → VERIFIED (+ mark_closed)
      - verify_passed=false → STAGING → BURNING → BURNED
    Already-VERIFIED volumes (re-burn to a new location) just get a copy added.
    """
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volume_events import add_event, get_events_for_volume
    from lcsas.db.volumes import get_volume_by_label, mark_closed, update_status

    config = load_config(args.config) if args.config else None
    if config is None:
        logger.error("--config is required for catalog.")
        return 1

    rejected = 0
    with locked_connection(args.db or config.db_path) as conn:
        ensure_schema(conn)

        imported = 0
        for receipt_file in args.receipt_files:
            try:
                with open(receipt_file, encoding="utf-8") as f:
                    receipt = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read receipt '{receipt_file}': {e}, skipping.")
                continue

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

            # Issue #18: receipt-vs-prior-receipt hash check. If a previous
            # BURN_RECEIPT_IMPORTED event for this volume recorded an
            # iso_sha256, the new receipt's iso_sha256 must match. Mismatch
            # means the receipt is for a different physical ISO (swapped /
            # tampered / re-mastered) — reject without writing anything.
            receipt_hash = receipt.get("iso_sha256") or ""
            hash_mismatch = False
            if receipt_hash:
                prior_events = get_events_for_volume(
                    conn, vol.volume_id, "BURN_RECEIPT_IMPORTED"
                )
                for prior in prior_events:
                    try:
                        prior_detail = (
                            json.loads(prior.detail) if prior.detail else {}
                        )
                    except json.JSONDecodeError:
                        continue
                    prior_hash = prior_detail.get("iso_sha256") or ""
                    if prior_hash and prior_hash != receipt_hash:
                        logger.error(
                            f"Receipt '{receipt_file}' iso_sha256 "
                            f"{receipt_hash} does not match previously "
                            f"recorded hash {prior_hash} for volume "
                            f"'{receipt['volume_label']}'; rejecting."
                        )
                        hash_mismatch = True
                        break
            if hash_mismatch:
                rejected += 1
                continue

            ensure_location(conn, receipt["location"])

            # Advance status if this is the first burn (STAGING); otherwise
            # just add a copy (handles re-burns to additional locations).
            verify_passed = bool(receipt.get("verify_passed", False))
            if vol.status == "STAGING":
                update_status(conn, vol.volume_id, "BURNING", commit=False)
                if verify_passed:
                    update_status(conn, vol.volume_id, "VERIFIED", commit=False)
                    mark_closed(conn, vol.volume_id, commit=False)
                    add_event(
                        conn, vol.volume_id, "VERIFY_PASS",
                        location=receipt["location"],
                        detail=f"Offline burn receipt: {Path(receipt_file).name}",
                        commit=False,
                    )
                else:
                    update_status(conn, vol.volume_id, "BURNED", commit=False)
                    add_event(
                        conn, vol.volume_id, "VERIFY_FAIL",
                        location=receipt["location"],
                        detail=f"Offline burn receipt: {Path(receipt_file).name}",
                        commit=False,
                    )

            receipt_size = receipt.get("iso_size_bytes")
            add_volume_copy(
                conn,
                volume_id=vol.volume_id,
                location=receipt["location"],
                burn_date=receipt.get("burn_date", ""),
                iso_sha256=receipt_hash or None,
                iso_size_bytes=(
                    int(receipt_size) if receipt_size is not None else None
                ),
                commit=False,
            )

            # Issue #18: persist receipt provenance (iso_sha256, session_id,
            # device, pack_ids) on the canonical catalog as a
            # BURN_RECEIPT_IMPORTED audit event. Captures all four fields
            # from the receipt JSON so later "which device burned this?"
            # and "does the disc hash match the recorded one?" queries can
            # be answered from the catalog alone.
            provenance = {
                "iso_sha256": receipt_hash,
                "session_id": receipt.get("session_id") or "",
                "device": receipt.get("device") or "",
                # pack_ids is emitted by the session-based burn path but NOT
                # by standalone `burn-iso` (documented limitation — see
                # docs/workflows/burn-iso-portable.md). Persist verbatim so
                # whichever path produced the receipt is preserved.
                "pack_ids": list(receipt.get("pack_ids") or []),
                "receipt_file": Path(receipt_file).name,
            }
            add_event(
                conn, vol.volume_id, "BURN_RECEIPT_IMPORTED",
                location=receipt["location"],
                detail=json.dumps(provenance),
                commit=False,
            )
            conn.commit()
            imported += 1

    logger.info(f"Imported {imported} receipt(s).")
    if rejected:
        logger.error(f"Rejected {rejected} receipt(s) due to iso_sha256 mismatch.")
        return 1
    return 0


def cmd_catalog_validate(args: argparse.Namespace) -> int:
    """Cross-check a mounted disc's data files against its embedded catalog."""
    from lcsas.db.verify import validate_disc

    disc_path = args.disc
    if not disc_path.is_dir():
        logger.error("Disc path does not exist or is not a directory: %s", disc_path)
        return 1

    logger.info("Validating disc at: %s", disc_path)
    try:
        result = validate_disc(disc_path, content=getattr(args, "content", False))
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Volume label   : %s", result.volume_label or "(unknown)")
    logger.info("Catalog packs  : %d", result.catalog_pack_count)
    logger.info("Disc packs     : %d", result.disc_pack_count)

    if result.missing_from_disc:
        logger.error(
            "%d pack(s) in catalog but MISSING from disc:",
            len(result.missing_from_disc),
        )
        for sha in result.missing_from_disc:
            logger.error("  MISSING: %s", sha)

    if result.orphaned_on_disc:
        logger.warning(
            "%d pack file(s) on disc but NOT in catalog (orphaned):",
            len(result.orphaned_on_disc),
        )
        for sha in result.orphaned_on_disc:
            logger.warning("  ORPHAN : %s", sha)

    if result.corrupt_on_disc:
        logger.error(
            "%d pack file(s) on disc are CORRUPT "
            "(content does not match filename hash):",
            len(result.corrupt_on_disc),
        )
        for sha in result.corrupt_on_disc:
            logger.error("  CORRUPT: %s", sha)

    if result.ok:
        logger.info("Catalog validation PASSED — disc and catalog are in sync.")
        return 0
    else:
        logger.error(
            "Catalog validation FAILED — %d missing, %d orphaned, %d corrupt.",
            len(result.missing_from_disc),
            len(result.orphaned_on_disc),
            len(result.corrupt_on_disc),
        )
        return 1


def cmd_catalog_reconcile(args: argparse.Namespace) -> int:
    """Report and optionally repair catalog/physical-state disagreements.

    Two checks (FMA-01):

    1. Ghost volumes — STAGING/BURNING with zero ``volume_copies`` rows,
       older than the cutoff.  These claim packs but correspond to no
       physical disc; restore pick lists offer them by label and an heir
       would hunt for a disc that never existed.  ``--fix`` deletes them
       (the migration path for catalogs predating the FMA-01 semantics).
    2. Durable volumes without an ACTIVE copy — status says a disc exists
       but no copy record backs it.  Report-only.
    """
    from lcsas.db.connection import locked_connection
    from lcsas.db.queries import (
        get_durable_volumes_without_active_copies,
        get_ghost_volumes,
        get_volume_pack_stats_by_repo,
    )
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volumes import delete_volume

    config = None
    if args.config is not None:
        from lcsas.config.settings import load_config
        config = load_config(args.config)
    db_path = _resolve_db_path(args, config)

    with locked_connection(db_path) as conn:
        ensure_schema(conn)

        ghosts = get_ghost_volumes(conn, args.older_than_hours)
        drifted = get_durable_volumes_without_active_copies(conn)

        if drifted:
            logger.warning(
                "%d volume(s) have a durable status but no ACTIVE copy "
                "record (status/copies drift — investigate, not auto-fixed):",
                len(drifted),
            )
            for v in drifted:
                logger.warning("  %s  status=%s  location=%s",
                               v.label, v.status, v.location)

        if not ghosts:
            logger.info(
                "Catalog reconcile: no ghost volumes found "
                "(STAGING/BURNING, zero copies, older than %dh).",
                args.older_than_hours,
            )
            return 0

        logger.warning(
            "%d ghost volume(s) — staged but with no record of ever being "
            "burned. Their packs are NOT on any disc:",
            len(ghosts),
        )
        ghost_pack_total = 0
        ghost_byte_total = 0
        for v in ghosts:
            stats = get_volume_pack_stats_by_repo(conn, v.volume_id)
            n_packs = sum(c for _r, c, _b in stats)
            n_bytes = sum(b for _r, _c, b in stats)
            ghost_pack_total += n_packs
            ghost_byte_total += n_bytes
            per_repo = "; ".join(
                f"{repo}: {c} pack(s), {b:,} bytes" for repo, c, b in stats
            ) or "no packs"
            logger.warning(
                "  %s  (status=%s, created %s) — %s",
                v.label, v.status, v.created_at, per_repo,
            )

        if not args.fix:
            logger.warning(
                "Run 'lcsas catalog reconcile --fix' to delete these "
                "volumes and return %d pack(s) (%s bytes) to the "
                "unarchived pool.",
                ghost_pack_total, f"{ghost_byte_total:,}",
            )
            return 1

        if not args.yes:
            try:
                response = input(
                    f"Delete {len(ghosts)} ghost volume(s) and return "
                    f"their packs to the unarchived pool? "
                    f"Type 'yes' to confirm: "
                ).strip()
            except EOFError:
                logger.error(
                    "No terminal available for confirmation — re-run with "
                    "--yes to confirm non-interactively."
                )
                return 1
            if response.lower() != "yes":
                logger.info("Reconcile canceled — no changes made.")
                return 1

        ghost_ids = [v.volume_id for v in ghosts]
        placeholders = ",".join("?" for _ in ghost_ids)
        reclaimed = conn.execute(
            f"SELECT COUNT(DISTINCT pack_id) FROM volume_packs "
            f"WHERE volume_id IN ({placeholders})",
            ghost_ids,
        ).fetchone()[0]
        for v in ghosts:
            logger.info("Deleting ghost volume %s", v.label)
            delete_volume(conn, v.volume_id)
        logger.info(
            "Deleted %d ghost volume(s); %d pack(s) returned to the "
            "unarchived pool (re-run 'lcsas stage' to re-select them).",
            len(ghosts), int(reclaimed),
        )
    return 0


def cmd_catalog_rebuild(args: argparse.Namespace) -> int:
    """Rebuild master catalog by merging disc-embedded holographic catalogs."""
    from lcsas.db.rebuild import rebuild_catalog

    disc_dirs = [Path(d) for d in args.disc_dirs]
    output_db = args.output

    # Basic sanity checks
    bad = [str(d) for d in disc_dirs if not d.is_dir()]
    if bad:
        for b in bad:
            logger.error("Not a directory (disc not mounted?): %s", b)
        return 1

    logger.info(
        "Rebuilding catalog from %d disc(s) → %s", len(disc_dirs), output_db
    )
    result = rebuild_catalog(disc_dirs, output_db)

    logger.info("Discs processed  : %d", result.discs_processed)
    if result.discs_skipped:
        logger.warning("Discs skipped    : %d (see errors above)", result.discs_skipped)
    logger.info("Repositories     : %d new", result.repositories_merged)
    logger.info("Volumes          : %d new", result.volumes_merged)
    logger.info("Packs            : %d new", result.packs_merged)
    logger.info("Snapshots        : %d new", result.snapshots_merged)

    # FMA-06: mixed-age disc boxes carry conflicting catalog views; the
    # merge keeps the freshest one and explains what it overrode here.
    if result.warnings:
        logger.warning("Rebuild produced %d warning(s):", len(result.warnings))
        for warning in result.warnings:
            logger.warning("  %s", warning)

    if result.errors:
        logger.error("Rebuild completed with %d error(s):", len(result.errors))
        for err in result.errors:
            logger.error("  %s", err)
        return 1

    logger.info("Catalog rebuild complete: %s", output_db)
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Plan and optionally execute volume consolidation."""
    from lcsas.config.media import MediaType
    from lcsas.config.settings import load_config
    from lcsas.consolidate.merger import VolumeMerger
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema

    config = load_config(args.config) if args.config else None
    db_path = _resolve_db_path(args, config)
    with locked_connection(db_path) as conn:
        ensure_schema(conn)

        try:
            media_type = MediaType[args.target_media]
        except KeyError:
            valid = ", ".join(m.name for m in MediaType)
            logger.error(f"Unknown media type '{args.target_media}'. "
                         f"Valid types: {valid}")
            return 1

        reserve = config.metadata_reserve_bytes if config else 104_857_600
        merger = VolumeMerger(conn, metadata_reserve_bytes=reserve)
        plan = merger.plan_consolidation(args.volume_ids, media_type)

        logger.info("Consolidation Plan:")
        logger.info(f"  Source volumes: {', '.join(plan.source_labels)}")
        logger.info(f"  Active packs:  {len(plan.active_packs)}")
        logger.info(f"  Total size:    {plan.total_active_bytes / 1e9:.1f} GB")
        logger.info(f"  Target media:  {plan.target_media_type.name}")
        logger.info(f"  Volumes needed: {plan.volumes_needed}")
        if plan.pruned_left_behind:
            left_bytes = sum(p.size_bytes for p in plan.pruned_left_behind)
            logger.warning(
                f"  Pruned left behind: {len(plan.pruned_left_behind)} pack(s) "
                f"({left_bytes:,} bytes) on the source volume(s) are marked "
                f"pruned and will NOT migrate. If any were pruned by mistake, "
                f"run 'lcsas pack unprune <sha256>' and re-plan first:"
            )
            for p in plan.pruned_left_behind[:20]:
                logger.warning(f"    {p.sha256}")
            if len(plan.pruned_left_behind) > 20:
                logger.warning(
                    f"    … and {len(plan.pruned_left_behind) - 20} more"
                )

        # Handle --deprecate flag (separate from --execute)
        if args.deprecate:
            if args.execute:
                logger.error("--deprecate and --execute are mutually exclusive.")
                return 1
            # Mark source volumes as DEPRECATED after consolidation succeeds
            merger.deprecate_sources(args.volume_ids)
            logger.info("Marked %d source volume(s) as DEPRECATED", len(args.volume_ids))
            return 0

        if not args.execute:
            logger.info("")
            logger.info("To execute: add --execute to stage and burn.")
            logger.info("Then verify the new volumes and run: consolidate --deprecate %s",
                       " ".join(str(v) for v in args.volume_ids))
            return 0

        if config is None:
            logger.error("--config is required for --execute.")
            return 1

        # Confirmation prompt before executing irreversible staging
        logger.warning("")
        logger.warning(
            "This will stage %d packs across %d volume(s) — an irreversible catalog change.",
            len(plan.active_packs), plan.volumes_needed,
        )
        try:
            response = input("Are you sure you want to proceed? Type 'yes' to confirm: ").strip()
            if response.lower() != "yes":
                logger.info("Consolidation canceled.")
                return 0
        except EOFError:
            logger.error("No terminal available for confirmation (redirected input).")
            logger.error("Run interactively or use lcsas restore instead.")
            return 1

        # Execute: stage the active packs via the burn orchestrator
        from lcsas.burn.orchestrator import BurnOrchestrator
        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
        from lcsas.iso.xorriso import SubprocessXorrisoRunner
        from lcsas.utils.shutdown import ShutdownManager

        shutdown = ShutdownManager()
        shutdown.install()

        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        # Mark source volumes as CONSOLIDATING before staging (atomic marker)
        merger.mark_sources_consolidating(args.volume_ids)
        logger.info("Marked %d source volume(s) as CONSOLIDATING", len(args.volume_ids))

        # Stage only the active packs from the consolidation plan
        pack_shas = [p.sha256 for p in plan.active_packs]
        try:
            session = orch.stage(
                media_type=media_type,
                pack_sha256s=pack_shas,
            )
        except Exception:
            # If staging fails, abort consolidation to revert source volumes to VERIFIED
            merger.abort_consolidation(args.volume_ids)
            logger.error("Staging failed. Reverted source volumes to VERIFIED.", exc_info=True)
            raise

        logger.info(f"Staged {len(session.manifests)} volume(s).")
        logger.info(
            "Consolidation staged.  Next steps:\n"
            "  1. burn session %s\n"
            "  2. verify the new volumes\n"
            "  3. then deprecate sources with: consolidate --deprecate %s",
            session.session_id,
            " ".join(args.volume_ids),
        )

    return 0


def _verify_disc_against_recorded_hash(
    conn: sqlite3.Connection,
    volume_id: int,
    label: str,
    device: str,
) -> bool:
    """Device read-back SHA-256 compare for ``verify --disc`` (BURN-04).

    Returns True when the disc's bytes match the ISO hash recorded at
    stage time, or when no hash was ever recorded (pre-Phase-13 volume:
    readability is all the evidence available).  Returns False on
    mismatch, on read errors, and when a hash is recorded but the ISO
    byte length cannot be determined (-check_media alone is not enough
    evidence for a volume that carries a hash).

    Hash + length come from ``session_volumes`` (written at stage time),
    falling back to ``volume_copies`` — the copy rows survive receipt
    import and catalog rebuild from disc, where session_volumes does
    not (FMA-03).
    """
    from lcsas.burn.device_verify import read_device_sha256

    expected: str | None = None
    iso_size: int | None = None

    row = conn.execute(
        "SELECT iso_path, iso_sha256, iso_size_bytes FROM session_volumes "
        "WHERE volume_id = ? AND iso_sha256 IS NOT NULL AND iso_sha256 != '' "
        "ORDER BY rowid DESC LIMIT 1",
        (volume_id,),
    ).fetchone()
    if row is not None:
        expected = str(row["iso_sha256"])
        iso_size = row["iso_size_bytes"]
        if iso_size is None and row["iso_path"]:
            iso_file = Path(row["iso_path"])
            if iso_file.exists():
                iso_size = iso_file.stat().st_size
    else:
        # Rebuilt / receipt-imported catalog: session_volumes is empty
        # but the copy rows carry the evidence (schema v8, FMA-03).
        copy_row = conn.execute(
            "SELECT iso_sha256, iso_size_bytes FROM volume_copies "
            "WHERE volume_id = ? AND iso_sha256 IS NOT NULL AND iso_sha256 != '' "
            "ORDER BY (iso_size_bytes IS NULL), id DESC LIMIT 1",
            (volume_id,),
        ).fetchone()
        if copy_row is not None:
            expected = str(copy_row["iso_sha256"])
            iso_size = copy_row["iso_size_bytes"]

    if expected is None:
        logger.warning(
            "  No recorded ISO SHA-256 for %s — device hash check skipped "
            "(pre-Phase-13 volume); check_media proved readability only.",
            label,
        )
        return True

    if iso_size is None:
        logger.warning(
            "  cannot device-verify %s: no recorded ISO size (pre-upgrade "
            "session) and the ISO file is gone — NOT passing on "
            "check_media alone.",
            label,
        )
        return False

    try:
        device_hash = read_device_sha256(device, int(iso_size))
    except OSError as exc:
        logger.error(f"  Device hash verify: FAIL (read error: {exc})")
        return False

    ok = device_hash == expected
    logger.info(f"  Device hash verify: {'PASS' if ok else 'FAIL'}")
    if not ok:
        logger.error(
            f"  device hash mismatch: expected {expected[:8]}.., "
            f"got {device_hash[:8]}.."
        )
    return ok


def _check_disc_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    label: str,
    device: str,
) -> str:
    """FMA-03 disc check chain: identity gate → readability → device hash.

    Returns ``"PASS"``, ``"FAIL"``, or ``"WRONG_DISC"``.  WRONG_DISC
    means the disc in the drive is not (provably) this volume — that is
    operator error, not evidence about the volume's burned copy, so
    callers must record NOTHING for it.
    """
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    runner = SubprocessXorrisoRunner()
    disc_id = runner.read_disc_volume_id(device=device)
    if disc_id != label:
        if disc_id:
            logger.error(
                f"Disc in {device} identifies as '{disc_id}' — "
                f"expected '{label}'. Wrong disc? "
                f"Nothing was recorded."
            )
        else:
            logger.error(
                f"Could not read a Volume ID from the disc in "
                f"{device} — cannot confirm it is "
                f"'{label}'. Nothing was recorded."
            )
        return "WRONG_DISC"
    ok = runner.verify_disc(device=device)
    logger.info(f"  Disc verify (check_media): {'PASS' if ok else 'FAIL'}")
    if not ok:
        return "FAIL"
    # BURN-04: -check_media proves only readability — any readable disc
    # in the tray passes it.  Read back the recorded ISO byte length
    # from the device and compare against the SHA-256 recorded at
    # stage time.
    if _verify_disc_against_recorded_hash(conn, volume_id, label, device):
        return "PASS"
    return "FAIL"


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a volume's ISO image or burned disc.

    Supports three modes:
    - Physical/ISO verification: check media integrity (default)
    - Manual marking: --mark-verified / --mark-failed for remote workflows
    - Batch: --all to verify all BURNED/VERIFIED volumes
    """
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import get_volume_by_label, update_status

    config = load_config(args.config) if args.config else None
    db_path = _resolve_db_path(args, config)
    with locked_connection(db_path) as conn:
        ensure_schema(conn)

        # --- Batch mode: verify --all ---
        if args.verify_all:
            if args.disc:
                return _verify_all_disc(conn, args)
            return _verify_all(conn, args, config)

        # --- Single volume mode ---
        if not args.volume_label:
            logger.error("Volume label required (or use --all for batch mode).")
            return 1

        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1

        # --- Manual marking (remote verification workflow) ---
        if args.mark_verified:
            if vol.status == "BURNED":
                update_status(conn, vol.volume_id, "VERIFIED")
                logger.info(f"Volume {vol.label}: status BURNED → VERIFIED")
            elif vol.status == "STAGING":
                update_status(conn, vol.volume_id, "VERIFIED", force=True)
                logger.info(
                    f"Volume {vol.label}: status STAGING → VERIFIED (split-machine workflow)"
                )
            else:
                logger.error(
                    f"Cannot mark volume {vol.label} VERIFIED: status is {vol.status} "
                    f"(only BURNED or STAGING allowed)"
                )
                return 1
            add_event(
                conn, vol.volume_id, "VERIFY_PASS",
                detail=args.detail or "Manual verification (remote)",
            )
            return 0

        if args.mark_failed:
            add_event(
                conn, vol.volume_id, "VERIFY_FAIL",
                detail=args.detail or "Manual failure report",
            )
            logger.info(f"Volume {vol.label}: VERIFY_FAIL event recorded")
            return 0

        # --- Physical verification ---
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

        passed = True

        if args.disc:
            from lcsas.db.volume_copies import (
                get_copies_for_volume,
                touch_last_verified,
            )

            # FMA-05: resolve which copy this disc IS before touching
            # the device — with several ACTIVE copies the operator must
            # say which one is in the drive, or the stamp would assert
            # freshness for a disc that was never read.
            stamp_location = args.location
            if stamp_location is None:
                active = get_copies_for_volume(conn, vol.volume_id)
                if len(active) == 1:
                    stamp_location = active[0].location
                elif len(active) > 1:
                    locs = ", ".join(sorted(c.location for c in active))
                    logger.error(
                        f"Volume {vol.label} has ACTIVE copies at {locs} — "
                        f"pass --location to record which disc is being "
                        f"verified."
                    )
                    return 1
            logger.info(f"Verifying disc on {args.device} ...")
            # FMA-03: identity gate first.  If the disc in the drive is
            # not this volume (or carries no readable Volume ID), record
            # NOTHING — a wrong disc is operator error, not evidence
            # about this volume's burned copy.
            outcome = _check_disc_for_volume(
                conn, vol.volume_id, vol.label, args.device,
            )
            if outcome == "WRONG_DISC":
                return 1
            passed = outcome == "PASS"
            if passed:
                if stamp_location is None:
                    logger.warning(
                        f"  No ACTIVE copy of {vol.label} recorded — "
                        f"last_verified_at not stamped"
                    )
                # Stamp uncommitted: the add_event() below commits it
                # atomically with the VERIFY_PASS event.
                elif touch_last_verified(
                    conn, vol.volume_id, stamp_location, commit=False,
                ):
                    logger.info(
                        f"  Stamped last_verified_at on copy at {stamp_location}"
                    )
                else:
                    logger.warning(
                        f"  No ACTIVE copy of {vol.label} at {stamp_location} — "
                        f"last_verified_at not updated"
                    )
        else:
            if not iso_path.exists():
                logger.error(f"ISO file not found: {iso_path}")
                return 1

            # Phase 21.3: try DVDisaster RS03 verify first (Linux primary
            # path; can detect AND repair).  If dvdisaster isn't available
            # on the host (macOS, Windows, or a stripped-down Linux box),
            # fall back to a portable SHA-256 compare against the hash
            # recorded at burn time (detect-only).  See
            # docs/CROSS_PLATFORM_META_RFC.md §6 Q2 for the rationale.
            from lcsas.db.volume_copies import get_iso_sha256_for_label
            from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
            from lcsas.restore.executor import verify_iso_sha256

            logger.info(f"Verifying ISO: {iso_path}")
            try:
                dvd_runner = SubprocessDVDisasterRunner()
                ok = dvd_runner.verify_iso(iso_path)
                logger.info(f"  ECC verify: {'PASS' if ok else 'FAIL'}")
            except (FileNotFoundError, RuntimeError) as e:
                # dvdisaster isn't installed — fall back to SHA-256.
                logger.info(
                    "  dvdisaster unavailable (%s); falling back to "
                    "portable SHA-256 verify", e.__class__.__name__,
                )
                expected = get_iso_sha256_for_label(conn, args.volume_label)
                if not expected:
                    logger.error(
                        "  No recorded ISO SHA-256 for %s — cannot verify "
                        "without dvdisaster.  (Catalog rows pre-dating "
                        "Phase 13 don't carry the hash.)",
                        args.volume_label,
                    )
                    return 1
                ok = verify_iso_sha256(iso_path, expected)
                logger.info(f"  SHA-256 verify: {'PASS' if ok else 'FAIL'}")
            if not ok:
                passed = False

        # Record event
        event_type = "VERIFY_PASS" if passed else "VERIFY_FAIL"
        detail = args.detail or ("Disc verify" if args.disc else f"ISO verify: {iso_path}")
        add_event(conn, vol.volume_id, event_type, detail=detail)

        # If verification passed on a BURNED volume, promote to VERIFIED
        if passed and vol.status == "BURNED":
            update_status(conn, vol.volume_id, "VERIFIED")
            logger.info(f"Volume {vol.label}: promoted BURNED → VERIFIED")

        return 0 if passed else 1


def _verify_all(conn: sqlite3.Connection, args: argparse.Namespace, config: Any) -> int:
    """Batch-verify all BURNED/VERIFIED volumes, optionally at a location."""
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import list_volumes, update_status

    vols_burned = list_volumes(conn, status_filter="BURNED")
    vols_verified = list_volumes(conn, status_filter="VERIFIED")
    candidates = vols_burned + vols_verified

    if args.location:
        # Filter to volumes with a copy at the given location
        filtered = []
        for vol in candidates:
            copies = get_copies_for_volume(conn, vol.volume_id)
            if any(c.location == args.location for c in copies):
                filtered.append(vol)
        candidates = filtered

    if not candidates:
        logger.info("No volumes to verify.")
        return 0

    logger.info(f"Verifying {len(candidates)} volume(s)...")
    passed_count = 0
    failed_count = 0

    # Phase 21.5.a: probe dvdisaster ONCE up front, not per-volume.  If
    # it isn't installed we fall back to SHA-256 verification against
    # the catalog-recorded hash (Phase 21.3 pattern).
    import shutil

    from lcsas.db.volume_copies import get_iso_sha256_for_label
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.restore.executor import verify_iso_sha256

    dvdisaster_available = shutil.which("dvdisaster") is not None
    dvd_runner = SubprocessDVDisasterRunner() if dvdisaster_available else None
    if not dvdisaster_available:
        logger.info(
            "  dvdisaster unavailable; falling back to portable SHA-256 "
            "verify for all volumes (Phase 21.3 / 21.5)."
        )

    for vol in candidates:
        # Try to find the ISO path
        row = conn.execute(
            "SELECT iso_path FROM session_volumes WHERE volume_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (vol.volume_id,),
        ).fetchone()

        if not row or not row["iso_path"]:
            # FMA-05: burn_session() deletes the ISO after a verified
            # burn — for a burned volume this is the NORMAL state, so
            # point at the physical-disc batch mode instead of leaving
            # a bare skip.
            logger.info(
                f"  {vol.label}: ISO deleted after burn — use "
                f"'verify --all --disc' to verify the physical disc"
            )
            continue

        iso_path = Path(row["iso_path"])
        if not iso_path.exists():
            logger.info(
                f"  {vol.label}: ISO deleted after burn ({iso_path}) — use "
                f"'verify --all --disc' to verify the physical disc"
            )
            continue

        if dvdisaster_available:
            assert dvd_runner is not None  # mypy hint
            ok = dvd_runner.verify_iso(iso_path)
            verify_kind = "Batch ISO verify"
        else:
            # SHA-256 fallback path.  No recorded hash → skip (don't
            # silently pass: integrity is unknown).
            expected = get_iso_sha256_for_label(conn, vol.label)
            if not expected:
                logger.info(
                    f"  {vol.label}: no recorded SHA-256 and dvdisaster "
                    f"unavailable — skipped"
                )
                continue
            ok = verify_iso_sha256(iso_path, expected)
            verify_kind = "Batch SHA-256 verify"
        event_type = "VERIFY_PASS" if ok else "VERIFY_FAIL"
        add_event(conn, vol.volume_id, event_type, detail=f"{verify_kind}: {iso_path}")

        if ok:
            passed_count += 1
            if vol.status == "BURNED":
                update_status(conn, vol.volume_id, "VERIFIED")
            logger.info(f"  {vol.label}: PASS")
        else:
            failed_count += 1
            logger.info(f"  {vol.label}: FAIL")

    logger.info(f"Verification complete: {passed_count} passed, {failed_count} failed, "
                f"{len(candidates) - passed_count - failed_count} skipped")
    if failed_count > 0:
        return 1
    if passed_count == 0:
        logger.warning("No volumes were actually verified (all skipped).")
        return 1
    return 0


def _verify_all_disc(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Batch re-verify physical discs copy-by-copy (FMA-05).

    Iterates ACTIVE copies of BURNED/VERIFIED volumes (optionally
    filtered to --location), prompting the operator to insert each
    disc.  A PASS stamps ``last_verified_at`` on the copy (committed
    atomically with its VERIFY_PASS event); a FAIL records VERIFY_FAIL
    and leaves the stamp untouched; a wrong disc in the drive records
    nothing (FMA-03: operator error is not evidence).
    """
    from lcsas.db.volume_copies import touch_last_verified
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import get_volume_by_id, update_status

    params: tuple[Any, ...] = ()
    where = "vc.status = 'ACTIVE' AND v.status IN ('BURNED', 'VERIFIED')"
    if args.location:
        where += " AND vc.location = ?"
        params = (args.location,)
    rows = conn.execute(
        f"SELECT vc.volume_id, vc.location, v.label "
        f"FROM volume_copies vc JOIN volumes v USING (volume_id) "
        f"WHERE {where} ORDER BY v.label, vc.location",
        params,
    ).fetchall()
    if not rows:
        logger.info("No ACTIVE copies to verify.")
        return 0

    logger.info(f"Re-verifying {len(rows)} physical cop(ies) on {args.device}")
    results: list[tuple[str, str, str]] = []
    quit_early = False
    for row in rows:
        label, location = row["label"], row["location"]
        if quit_early:
            results.append((label, location, "SKIPPED"))
            continue
        try:
            answer = input(
                f"Insert disc {label} and press Enter (s = skip, q = quit): "
            ).strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            quit_early = True
            results.append((label, location, "SKIPPED"))
            continue
        if answer == "s":
            results.append((label, location, "SKIPPED"))
            continue
        outcome = _check_disc_for_volume(
            conn, row["volume_id"], label, args.device,
        )
        if outcome == "PASS":
            # Uncommitted stamp + event commit together (one transaction).
            touch_last_verified(conn, row["volume_id"], location, commit=False)
            add_event(conn, row["volume_id"], "VERIFY_PASS",
                      location=location, detail="Batch disc verify")
            logger.info(f"  Stamped last_verified_at on copy at {location}")
            # Re-read status: an earlier copy of the same volume may
            # already have promoted it in this run.
            if get_volume_by_id(conn, row["volume_id"]).status == "BURNED":
                update_status(conn, row["volume_id"], "VERIFIED")
                logger.info(f"Volume {label}: promoted BURNED → VERIFIED")
            results.append((label, location, "PASS"))
        elif outcome == "FAIL":
            add_event(conn, row["volume_id"], "VERIFY_FAIL",
                      location=location, detail="Batch disc verify")
            results.append((label, location, "FAIL"))
        else:
            # WRONG_DISC: nothing recorded; the copy stays unverified.
            results.append((label, location, "SKIPPED"))

    logger.info("Batch disc verification results:")
    for label, location, outcome in results:
        logger.info(f"  {label:<25} {location:<15} {outcome}")
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_fail = sum(1 for r in results if r[2] == "FAIL")
    logger.info(
        f"Verification complete: {n_pass} passed, {n_fail} failed, "
        f"{len(results) - n_pass - n_fail} skipped"
    )
    if n_fail > 0:
        return 1
    if n_pass == 0:
        logger.warning("No discs were actually verified (all skipped).")
        return 1
    return 0


def cmd_recovery(args: argparse.Namespace) -> int:
    """Dispatcher for `lcsas recovery {build,test,manifest,verify}`."""
    from lcsas.recovery import RecoveryBuilder

    project_root = Path(__file__).resolve().parents[3]
    recovery_dir = project_root / "recovery"
    if not recovery_dir.is_dir():
        logger.error("recovery/ tree not found at %s", recovery_dir)
        return 1

    rb = RecoveryBuilder(recovery_dir)
    sub = getattr(args, "recovery_command", None)

    if sub == "build":
        verbose = bool(getattr(args, "verbose", False))
        try:
            if args.arch == "host":
                a = rb.build_host(verbose=verbose)
            else:
                a = rb.cross_build(args.arch, cc=args.cc, verbose=verbose)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.error("recovery build failed: %s", exc)
            return 1
        print(f"built {a.arch}: {a.lcsas_restore}")
        if a.lcsas_iso9660:
            print(f"  + {a.lcsas_iso9660}")
        if a.lcsas_init:
            print(f"  + {a.lcsas_init}")
        return 0

    if sub == "test":
        verbose = bool(getattr(args, "verbose", False))
        ok = rb.run_tests(verbose=verbose)
        if not ok:
            logger.error("recovery tests FAILED")
            return 1
        print("recovery tests: OK")
        return 0

    if sub == "manifest":
        path = rb.write_manifest(getattr(args, "output", None))
        line_count = sum(1 for _ in path.open())
        print(f"wrote {path} ({line_count} files)")
        return 0

    if sub == "verify":
        out = subprocess.run(
            ["make", "-C", str(recovery_dir), "repro-check"],
            check=False,
        )
        return out.returncode

    logger.error("Usage: lcsas recovery {build,test,manifest,verify}")
    return 1


def cmd_meta_build(args: argparse.Namespace) -> int:
    """Build a self-contained meta-volume with all restore tools."""
    from lcsas.meta.builder import MetaBuildError, MetaVolumeBuilder

    # Load config for survivability fields (START_HERE.txt, KEY_INFO.txt)
    config = None
    if hasattr(args, "config") and args.config:
        from lcsas.config.settings import load_config
        try:
            config = load_config(args.config)
        except Exception as e:
            logger.warning(f"Could not load config for START_HERE.txt: {e}")

    output = args.output.resolve()
    db_path: Path | None = None
    try:
        db_path = _resolve_db_path(args, config)
        if not db_path.is_file():
            db_path = None
    except Exception:
        db_path = None

    builder = MetaVolumeBuilder(
        output_dir=output,
        project_root=args.project_root,
        config=config,
        catalog_db_path=db_path,
        allow_no_zstd=getattr(args, "allow_no_zstd", False),
    )

    logger.info(f"Building meta-volume in {output} ...")
    try:
        builder.build()
    except FileNotFoundError as e:
        logger.error(f"{e}")
        logger.error("Ensure rustic, xorriso, and python3 are installed.")
        return 1
    except MetaBuildError as e:
        logger.error(f"{e}")
        return 1

    logger.info(f"Meta-volume built successfully at {output}")
    logger.info("Contents:")
    logger.info("  tools/          Portable rustic, xorriso, python3 + libraries")
    logger.info("  lcsas/          LCSAS source code")
    logger.info("  restore.sh      Bootstrap restore script")
    logger.info("  README_RESTORE.md  Restore instructions")
    logger.info("  START_HERE.txt  Plain-language guide for non-technical users")
    return 0


def cmd_meta_verify(args: argparse.Namespace) -> int:
    """Audit a built meta-volume against its recovery/MANIFEST.sha256.

    Phase 21.8.  Mirrors `make verify-recovery` (which audits the
    upstream-binary cache on the build host) but operates on the
    output of `lcsas meta build` — useful for catching bit-rot on
    a meta-volume directory before it's mastered into an ISO, or
    for periodic disc-health checks on a mounted meta-disc.
    """
    import hashlib

    out = args.output.resolve()
    manifest_path = out / "recovery" / "MANIFEST.sha256"
    if not manifest_path.is_file():
        logger.error(
            "No recovery/MANIFEST.sha256 under %s — is this really a meta-volume?",
            out,
        )
        return 1

    recovery_root = manifest_path.parent
    expected: dict[str, str] = {}
    for line in manifest_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("  ", 1)
        if len(parts) != 2:
            logger.warning("Malformed manifest line: %r", line)
            continue
        sha, rel = parts
        # Manifest entries look like "./scripts/restore.sh"; normalize.
        rel = rel[2:] if rel.startswith("./") else rel
        expected[rel] = sha.lower()

    if not expected:
        logger.error("Manifest %s contained no entries.", manifest_path)
        return 1

    mismatched: list[tuple[str, str, str]] = []
    missing: list[str] = []
    checked = 0
    for rel, want_sha in expected.items():
        path = recovery_root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        got = h.hexdigest()
        if got != want_sha:
            mismatched.append((rel, want_sha, got))
        else:
            checked += 1

    extras: list[str] = []
    if args.strict:
        listed = set(expected.keys())
        for path in recovery_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(recovery_root).as_posix()
            if rel == "MANIFEST.sha256":
                continue
            if rel not in listed:
                extras.append(rel)

    for rel in sorted(missing):
        logger.error("  MISSING  %s", rel)
    for rel, want, got in sorted(mismatched):
        logger.error(
            "  MISMATCH %s\n    expected %s\n    got      %s",
            rel, want, got,
        )
    for rel in sorted(extras):
        logger.error("  EXTRA    %s  (present on disk but absent from manifest)", rel)

    total_issues = len(missing) + len(mismatched) + len(extras)
    if total_issues:
        logger.error(
            "Meta-volume verify FAILED: %d issue(s) (%d missing, %d mismatched, %d extra).",
            total_issues, len(missing), len(mismatched), len(extras),
        )
        return 1

    logger.info(
        "Meta-volume verify PASSED: %d files match recovery/MANIFEST.sha256.",
        checked,
    )
    return 0


def cmd_restore_plan(args: argparse.Namespace) -> int:
    """Generate a restore pick list for a snapshot."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    if args.config is None:
        logger.error("--config is required for restore plan.")
        return 1
    config = load_config(args.config)
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    conn = get_connection(config.db_path if args.db is None else args.db)
    try:
        ensure_schema(conn)

        # Resolve repo config
        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            logger.error(f"  Available: {', '.join(config.repositories.keys())}")
            return 1

        repo_cfg = config.repositories[repo_name]
        if repo_cfg.password_file is None:
            logger.error(
                "Repository '%s' has no password_file configured.", repo_name
            )
            return 1

        # Get required pack hashes via rustic dry-run
        from lcsas.utils.subprocess import check_binary_version
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

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

    if pick_list.deprecated_disc_labels:
        logger.warning(
            "\n  WARNING: %d pack(s) are only available on DEPRECATED or "
            "DESTROYED volumes. These discs may still be physically "
            "retrievable if you have kept them.",
            sum(len(v) for v in pick_list.deprecated_disc_labels.values()),
        )
        for label, hashes in sorted(pick_list.deprecated_disc_labels.items()):
            logger.warning(
                "    %s  (%d pack(s))", label, len(hashes)
            )
        logger.warning(
            "  If you can locate these discs, mount them and re-run restore."
        )

    for label, hashes in sorted(pick_list.unconfirmed_volume_labels.items()):
        logger.warning(
            "\n  WARNING: volume %s was staged but has no record of ever "
            "being burned. If you cannot find this disc, it may never have "
            "existed. The %d pack(s) it lists may only exist on the "
            "original NAS mirror.",
            label, len(hashes),
        )

    if pick_list.missing_packs:
        logger.error(
            "\n  ERROR: %d pack(s) required for this snapshot are not found in "
            "any archived volume. Restore is impossible without these packs.",
            len(pick_list.missing_packs),
        )
        for sha in pick_list.missing_packs[:10]:
            logger.error(f"    missing: {sha}")
        if len(pick_list.missing_packs) > 10:
            logger.error(
                "    ... and %d more missing packs",
                len(pick_list.missing_packs) - 10,
            )
        logger.error(
            "  If discs are physically present but not yet scanned, run "
            "`lcsas catalog validate --disc /mnt/disc` to check each disc."
        )
        return 1

    return 0


def _find_sibling_iso(vol_dir: Path, label: str) -> Path | None:
    """Locate ``<label>.iso`` near a pre-extracted volume directory.

    Phase 21.6 helper.  When operators extract a stack of LCSAS ISOs
    into a single directory (the ``--volume-dir`` mode), the original
    ``.iso`` files often live alongside the extracted trees:

        my_recovery/
        ├── LCSAS_BD25_2026_0001/         ← extracted contents
        ├── LCSAS_BD25_2026_0001.iso      ← original ISO
        ├── LCSAS_BD25_2026_0002/
        └── LCSAS_BD25_2026_0002.iso

    Or, less commonly, the ISO might sit inside the per-label tree:

        my_recovery/LCSAS_BD25_2026_0001/LCSAS_BD25_2026_0001.iso

    Returns the first existing candidate path, or ``None`` if no ISO
    is reachable — in which case the caller skips the integrity check
    (degraded but not fatal — packs still get hash-verified
    individually during copy).
    """
    candidates = (
        vol_dir / f"{label}.iso",
        vol_dir / label / f"{label}.iso",
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


def _nearest_existing_dir(path: Path) -> Path:
    """Walk up from ``path`` to the closest ancestor that exists.

    ``shutil.disk_usage`` / ``os.stat`` need an existing path; restore
    targets and cache dirs frequently don't exist yet.  [FMA-09]
    """
    p = path.resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _fs_dev(path: Path) -> int:
    """Filesystem device id for ``path`` (nearest existing ancestor)."""
    return os.stat(_nearest_existing_dir(path)).st_dev


def _free_space_shortfalls(
    required_bytes: int, target: Path, cache_base: Path,
) -> list[tuple[Path, int]]:
    """Return ``(path, free_bytes)`` for each filesystem short on space.

    Checks the restore target, plus the pack-cache base when it lives
    on a different filesystem — packs accumulate to ~``required_bytes``
    in the cache before ``rustic restore`` runs.  [FMA-09]
    """
    import shutil

    shortfalls: list[tuple[Path, int]] = []
    target_free = shutil.disk_usage(_nearest_existing_dir(target)).free
    if target_free < required_bytes:
        shortfalls.append((target, target_free))
    if _fs_dev(cache_base) != _fs_dev(target):
        cache_free = shutil.disk_usage(_nearest_existing_dir(cache_base)).free
        if cache_free < required_bytes:
            shortfalls.append((cache_base, cache_free))
    return shortfalls


def cmd_restore_exec(args: argparse.Namespace) -> int:
    """Execute a restore operation."""
    import tempfile

    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.restore.executor import RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    if args.config is None:
        logger.error("--config is required for restore exec.")
        return 1
    config = load_config(args.config)
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    conn = get_connection(config.db_path if args.db is None else args.db)
    try:
        ensure_schema(conn)

        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            return 1

        repo_cfg = config.repositories[repo_name]

        # Early validation: password file must exist before we attempt rustic.
        if not args.password_file.exists():
            logger.error(
                "Password file not found: %s\n"
                "Provide the correct path with --password-file.",
                args.password_file,
            )
            return 1

        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import check_binary_version
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except BinaryError as exc:
            logger.error("%s", exc)
            return 1

        runner = SubprocessRusticRunner(tmpdir=config.staging_path)

        # Get required pack hashes
        plan = runner.restore_dry_run(
            snapshot_id=args.snapshot_id,
            repo_path=repo_cfg.mirror_path,
            password_file=args.password_file,
        )

        # Generate pick list with alternates for resilient restore
        planner = RestorePlanner(conn)
        pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)

        # Phase 21.6: snapshot per-volume ISO SHA-256 hashes while
        # the catalog conn is still open so the ingest loop below
        # can pass them to ingest_volume.  When the operator's
        # vol_dir holds the original .iso files next to the
        # extracted contents, ingest_volume will SHA-verify each
        # one before reading its packs — same protection as
        # dvdisaster on Linux, no toolchain required.
        from lcsas.db.volume_copies import get_iso_sha256_for_label
        iso_shas: dict[str, str | None] = {
            label: get_iso_sha256_for_label(conn, label)
            for label in pick_list.volumes
        }
    finally:
        conn.close()

    if pick_list.deprecated_disc_labels:
        logger.warning(
            "%d pack(s) are only on DEPRECATED/DESTROYED volumes: %s",
            sum(len(v) for v in pick_list.deprecated_disc_labels.values()),
            ", ".join(sorted(pick_list.deprecated_disc_labels)),
        )

    for label, hashes in sorted(pick_list.unconfirmed_volume_labels.items()):
        logger.warning(
            "WARNING: volume %s was staged but has no record of ever being "
            "burned. If you cannot find this disc, it may never have "
            "existed. The %d pack(s) it lists may only exist on the "
            "original NAS mirror.",
            label, len(hashes),
        )

    if pick_list.missing_packs:
        logger.error(f"{len(pick_list.missing_packs)} required packs not "
                     f"found in any volume.")
        return 1

    # Build alternates lookup: sha256 -> [alt_labels]
    alternates_map: dict[str, list[str]] = {}
    for sources in pick_list.volumes.values():
        for src in sources:
            if src.alternates:
                alternates_map[src.pack.sha256] = list(src.alternates)

    # ── Disk-space preflight [FMA-09] ──────────────────────────────
    # Refuse (or hard-confirm) BEFORE the first disc prompt: an heir
    # restoring onto a too-small disk must learn that NOW, not via
    # ENOSPC after a long disc-swapping session.  The pack cache
    # needs ~total_bytes too — packs accumulate there before rustic
    # restore runs — so its filesystem is checked when distinct.
    cache_base = (
        args.cache_dir if args.cache_dir is not None else config.staging_path
    )
    shortfalls = _free_space_shortfalls(
        pick_list.total_bytes, args.target_path, cache_base,
    )
    if shortfalls:
        need_gb = pick_list.total_bytes / 1e9
        for short_path, free_bytes in shortfalls:
            logger.error(
                "Restoring ~%.1f GB but %s has only %.1f GB free.",
                need_gb, short_path, free_bytes / 1e9,
            )
        if not sys.stdin.isatty():
            logger.error(
                "Refusing to start: free up space or choose a different "
                "target / --cache-dir, then re-run."
            )
            return 1
        ans = input("Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            logger.error("Aborted: free up space and re-run.")
            return 1

    # Set up cache directory
    _tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.cache_dir is None:
        config.staging_path.mkdir(parents=True, exist_ok=True)
        _tmp_dir = tempfile.TemporaryDirectory(
            prefix="lcsas-restore-", dir=str(config.staging_path)
        )
        cache_dir = Path(_tmp_dir.name)
    else:
        cache_dir = args.cache_dir
    ensure_dir(cache_dir)

    from lcsas.utils.shutdown import ShutdownManager
    shutdown = ShutdownManager()
    if _tmp_dir is not None:
        shutdown.register(_tmp_dir.cleanup)
    shutdown.install()

    try:
        executor = RestoreExecutor(runner)

        # Prepare cache with metadata from the repo mirror
        metadata_source = repo_cfg.mirror_path
        executor.prepare_cache(cache_dir, metadata_source)

        logger.info(f"Restore cache: {cache_dir}")
        logger.info(f"Need packs from {len(pick_list.volumes)} volumes")

        all_failed: list[str] = []  # packs that failed on primary volume

        # Ingest packs from volumes
        if args.volume_dir:
            # Non-interactive: all volume data is pre-extracted in one directory
            vol_dir = args.volume_dir
            for label, sources in pick_list.volumes.items():
                pack_hashes = [s.pack.sha256 for s in sources]
                vol_path = vol_dir / label
                if not vol_path.is_dir():
                    vol_path = vol_dir
                # Phase 21.6: if a sibling <label>.iso file sits next to
                # the extracted contents, hand it (and the recorded
                # hash) to ingest_volume so the verify-or-die check
                # fires before any pack is copied out.
                iso_path = _find_sibling_iso(vol_dir, label)
                result = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                    verify=not args.skip_verify,
                    collect_failures=True,
                    iso_path=iso_path,
                    expected_sha256=iso_shas.get(label),
                )
                logger.info(f"  {label}: ingested {result.ingested} packs")
                if result.failed:
                    logger.warning(
                        f"  {label}: {len(result.failed)} packs failed verification"
                    )
                    all_failed.extend(result.failed)

            # Retry failed packs from alternate volumes.  Prune against
            # the cache first: never fail a restore whose cache is
            # already complete (RST-01 — keeps the invariant uniform
            # across the disc-only and config-driven restore paths).
            all_failed = _prune_recovered(cache_dir, all_failed)
            if all_failed:
                logger.info(f"\nRetrying {len(all_failed)} failed packs "
                            f"from alternate volumes...")
                still_failed = _retry_from_alternates_batch(
                    executor, cache_dir, vol_dir,
                    all_failed, alternates_map,
                    verify=not args.skip_verify,
                    iso_shas=iso_shas,
                )
                still_failed = _prune_recovered(cache_dir, still_failed)
                if still_failed:
                    from lcsas.restore.executor import PackCorruptionError
                    discs = _discs_for_packs(still_failed, pick_list.volumes)
                    where = (
                        f" They live on disc(s): {', '.join(discs)}. "
                        "Re-mount those discs (check for damage) and retry."
                        if discs else ""
                    )
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"recovered.{where}"
                    )
        else:
            # Interactive: prompt user to mount each volume
            if not sys.stdin.isatty():
                logger.error(
                    "Interactive restore requires a TTY. "
                    "Use --volume-dir to specify a directory of pre-extracted "
                    "volume data for non-interactive (scripted) restores."
                )
                return 1
            for label, sources in sorted(pick_list.volumes.items()):
                pack_hashes = [s.pack.sha256 for s in sources]
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
                    # Phase 21.9: when --iso-dir is supplied, probe it
                    # for <label>.iso so the verify-or-die check fires
                    # for the interactive path too.
                    iso_path = (
                        _find_sibling_iso(args.iso_dir, label)
                        if args.iso_dir is not None
                        else None
                    )
                    result = executor.ingest_volume(
                        cache_dir, vol_path, pack_hashes,
                        verify=not args.skip_verify,
                        collect_failures=True,
                        iso_path=iso_path,
                        expected_sha256=iso_shas.get(label),
                    )
                    logger.info(f"  Ingested {result.ingested} packs from {label}")
                    if result.failed:
                        logger.warning(
                            f"  {len(result.failed)} packs failed verification"
                        )
                        all_failed.extend(result.failed)
                    break

            # Interactive retry for failed packs
            all_failed = _prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = _retry_from_alternates_interactive(
                    executor, cache_dir,
                    all_failed, alternates_map,
                    verify=not args.skip_verify,
                    iso_dir=args.iso_dir,
                    iso_shas=iso_shas,
                )
                still_failed = _prune_recovered(cache_dir, still_failed)
                if still_failed:
                    from lcsas.restore.executor import PackCorruptionError
                    discs = _discs_for_packs(still_failed, pick_list.volumes)
                    where = (
                        f" They live on disc(s): {', '.join(discs)}. "
                        "Re-mount those discs (check for damage) and retry."
                        if discs else ""
                    )
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"recovered.{where}"
                    )

        # ── Post-ingest completeness check ──────────────────────────
        # Verify every required pack was actually ingested before
        # running rustic, which would fail with an opaque error.
        all_required = plan.required_pack_hashes
        missing = RestoreExecutor.verify_cache_completeness(
            cache_dir, all_required,
        )
        if missing:
            logger.error(
                f"\n{len(missing)} of {len(all_required)} required packs "
                f"missing from cache after ingestion."
            )
            for sha in missing[:10]:
                logger.error(f"  missing: {sha}")
            if len(missing) > 10:
                logger.error(f"  ... and {len(missing) - 10} more")
            logger.error(
                "\nRestore cannot proceed — mount the missing volumes "
                "and retry, or check for damaged discs."
            )
            return 1

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
        if _tmp_dir is not None:
            _tmp_dir.cleanup()
        shutdown.uninstall()

    return 0


def cmd_restore_from_disc(args: argparse.Namespace) -> int:
    """Restore from optical discs without a config file or mirror.

    Reads ``catalog.db`` and repository metadata directly from a mounted
    or extracted disc.  No ``--config``, local mirror, or original
    database required — everything needed is embedded on each disc.

    Typical usage::

        lcsas restore standalone /mnt/disc1 ~/restored/ \\
            --password-file ~/secret.key

    For batch (scripted) restores with pre-extracted discs::

        lcsas restore standalone /tmp/disc1 ~/restored/ \\
            --password-file ~/secret.key \\
            --volume-dir /tmp/extracted_discs/
    """
    import shutil
    import sys
    import tempfile

    from lcsas.db.connection import get_connection
    from lcsas.restore.executor import PackCorruptionError, RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    disc_path = args.disc
    if not disc_path.is_dir():
        logger.error("Disc path '%s' is not a directory.", disc_path)
        return 1

    # Early validation: password file must exist.
    if not args.password_file.exists():
        logger.error(
            "Password file not found: %s\n"
            "Provide the correct path with --password-file.",
            args.password_file,
        )
        return 1

    # Locate catalog.db on the disc
    catalog_path = args.catalog if args.catalog else disc_path / "catalog.db"
    if not catalog_path.is_file():
        logger.error(
            "No catalog.db found at '%s'.\n"
            "Each LCSAS disc contains a catalog.db.  "
            "If this disc is damaged, try another disc from the same archive.",
            catalog_path,
        )
        return 1

    # Use a temp directory for scratch files; keeps any --cache-dir separate.
    tmp_dir_obj = tempfile.TemporaryDirectory(prefix="lcsas-fromdisc-")
    try:
        from lcsas.utils.shutdown import ShutdownManager
        tmp_dir = Path(tmp_dir_obj.name)

        # Register cleanup on SIGINT/KeyboardInterrupt
        shutdown = ShutdownManager()
        shutdown.register(tmp_dir_obj.cleanup)
        shutdown.install()

        # Copy catalog.db to temp to avoid locking a mounted disc/ISO.
        tmp_catalog = tmp_dir / "catalog.db"
        shutil.copy2(str(catalog_path), str(tmp_catalog))

        disc_conn = get_connection(tmp_catalog)
        try:
            repo_rows = disc_conn.execute(
                "SELECT repo_id, name, mirror_path FROM repositories"
            ).fetchall()
        finally:
            disc_conn.close()

        if not repo_rows:
            logger.error(
                "Disc catalog contains no repositories.\n"
                "This may not be a valid LCSAS disc."
            )
            return 1

        # Select the repository to restore
        if args.repo:
            repo_row = next((r for r in repo_rows if r["name"] == args.repo), None)
            if repo_row is None:
                names = [r["name"] for r in repo_rows]
                logger.error(
                    "Repository '%s' not found in disc catalog.  Available: %s",
                    args.repo, ", ".join(names),
                )
                return 1
        elif len(repo_rows) == 1:
            repo_row = repo_rows[0]
            logger.info("Repository: %s", repo_row["name"])
        else:
            logger.error(
                "Multiple repositories found in disc catalog — "
                "use --repo NAME to select one.\nAvailable: %s",
                ", ".join(r["name"] for r in repo_rows),
            )
            return 1

        repo_name = repo_row["name"]

        # Locate metadata on the disc (written as metadata/<repo_name>/).
        disc_meta = disc_path / "metadata" / repo_name
        if not disc_meta.is_dir():
            logger.error(
                "Metadata for repository '%s' not found on disc.\n"
                "Expected at: %s\n"
                "This disc may be damaged or from a different archive.",
                repo_name, disc_meta,
            )
            return 1

        logger.info("Found metadata for '%s' on disc.", repo_name)

        # Build restore cache and populate it with disc metadata.
        cache_dir = args.cache_dir if args.cache_dir else (tmp_dir / "cache")
        ensure_dir(cache_dir)

        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import check_binary_version

        rustic_available = True
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except BinaryError:
            rustic_available = False

        runner = SubprocessRusticRunner(tmpdir=tmp_dir)
        executor = RestoreExecutor(runner)

        logger.info("Copying repository metadata from disc to cache...")
        executor.prepare_cache(cache_dir, disc_meta)

        if not rustic_available:
            # Auto-fallback: use the pure-Python restorer bundled with LCSAS.
            # Link the disc data dir into the cache so PurePythonRestorer can
            # find the pack files without copying gigabytes.
            from lcsas.restore.restic_fallback import PurePythonRestorer

            data_link = cache_dir / "data"
            disc_data = disc_path / "data"
            # If a stale symlink exists (e.g. resumed on a different system),
            # remove it so we can re-link to the current disc.
            if (data_link.is_symlink() and
                    (not data_link.exists() or
                     data_link.resolve() != disc_data.resolve())):
                logger.warning(
                    "Stale data symlink detected (%s → %s). Re-linking to current disc.",
                    data_link, data_link.resolve(),
                )
                data_link.unlink()
            if not data_link.exists() and disc_data.is_dir():
                data_link.symlink_to(disc_data)
            elif not disc_data.is_dir():
                logger.error(
                    "Disc data directory not found at '%s'. "
                    "Ensure the disc is mounted and the path is correct.",
                    disc_data,
                )
                return 1

            logger.warning(
                "rustic binary not found — falling back to pure-Python restorer.\n"
                "  This is ~100x slower and only reads packs from the mounted disc.\n"
                "  For multi-disc snapshots, mount all discs and copy their data/ "
                "directories into %s/data/ before running.",
                cache_dir,
            )
            target = args.target_path.resolve()
            snap_arg = None if args.snapshot == "latest" else args.snapshot
            restorer = PurePythonRestorer(
                repo_path=cache_dir,
                password_file=args.password_file,
            )
            try:
                meta = restorer.restore(target=target, snapshot_id=snap_arg)
            except Exception as exc:
                # Setup-level failure (bad key, missing repo) — fail fast.
                logger.error("Pure-Python restore failed: %s", exc)
                return 1
            if restorer.failures:
                # Tolerant traversal skipped one or more files; the bulk
                # of the data is restored and a manifest lists the rest.
                logger.error(
                    "Pure-Python restore finished with %d skipped file(s). "
                    "See %s/RESTORE_FAILURES.txt; the rest of your data is "
                    "intact. Re-run from a newer meta disc or an undamaged "
                    "copy of the affected disc to recover them.",
                    restorer.failures, target,
                )
                return 2
            logger.info(
                "Restore complete (pure-Python fallback). Snapshot: %s, "
                "hostname: %s",
                meta.snapshot_id, meta.hostname,
            )
            return 0

        # Resolve "latest" to an actual snapshot ID so dry-run gets an
        # explicit ID that rustic/restic can handle without ambiguity.
        snapshot_id = args.snapshot
        if snapshot_id == "latest":
            try:
                snap_list = runner.snapshots(
                    repo_path=cache_dir,
                    password_file=args.password_file,
                )
            except Exception as exc:
                logger.error(
                    "Could not list snapshots to resolve 'latest': %s", exc
                )
                return 1
            if not snap_list:
                logger.error(
                    "No snapshots found in repository '%s' on this disc.",
                    repo_name,
                )
                return 1
            # SnapshotInfo.timestamp is an ISO-8601 string; lexicographic sort is safe.
            snap_list.sort(key=lambda s: s.timestamp)
            snapshot_id = snap_list[-1].snapshot_id
            logger.info(
                "Resolved 'latest' to snapshot %s (%s)",
                snapshot_id, snap_list[-1].timestamp,
            )

        # Run rustic dry-run against the cache to determine required packs.
        logger.info(
            "Determining required packs for snapshot '%s'...", snapshot_id
        )
        try:
            plan = runner.restore_dry_run(
                snapshot_id=snapshot_id,
                repo_path=cache_dir,
                password_file=args.password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Install rustic (https://rustic.cli.rs/) or "
                "restic (https://restic.net/),\n"
                "or use the standalone restorer bundled on the disc:\n"
                "  python3 %s/standalone_restorer.py "
                "--password-file %s --target %s",
                disc_path, args.password_file, args.target_path,
            )
            return 1
        except Exception as exc:
            logger.error(
                "Failed to determine required packs: %s\n"
                "Verify the password file is correct and disc metadata is intact.",
                exc,
            )
            return 1

        logger.info(
            "Snapshot '%s' requires %d pack file(s).",
            snapshot_id, len(plan.required_pack_hashes),
        )

        # Look up which disc has which pack via the disc's catalog.
        disc_conn = get_connection(tmp_catalog)
        try:
            planner = RestorePlanner(disc_conn)
            pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)
        finally:
            disc_conn.close()

        if pick_list.missing_packs:
            missing_preview = ", ".join(pick_list.missing_packs[:5])
            overflow = (
                f" ... and {len(pick_list.missing_packs) - 5} more"
                if len(pick_list.missing_packs) > 5 else ""
            )
            deprecated_hint = ""
            if pick_list.deprecated_disc_labels:
                disc_list = ", ".join(sorted(pick_list.deprecated_disc_labels))
                deprecated_hint = (
                    f"\n  These pack(s) were last seen on DEPRECATED disc(s): "
                    f"{disc_list}. "
                    "If you still have those physical discs, mount them and re-run."
                )
            logger.error(
                "%d required pack(s) not found in the disc catalog. "
                "If these packs are on other discs not yet mounted, "
                "add them via --disc-dir and re-run.%s\n"
                "  Missing hashes: %s%s",
                len(pick_list.missing_packs),
                deprecated_hint,
                missing_preview,
                overflow,
            )
            return 1

        # Build alternates lookup for resilient pack collection.
        alternates_map: dict[str, list[str]] = {}
        for sources in pick_list.volumes.values():
            for src in sources:
                if src.alternates:
                    alternates_map[src.pack.sha256] = list(src.alternates)

        all_failed: list[str] = []

        if args.volume_dir:
            # Batch mode: all discs pre-extracted into a directory tree.
            vol_dir = args.volume_dir

            # Always try the initial --disc path first.
            result = executor.ingest_volume(
                cache_dir, disc_path, plan.required_pack_hashes,
                verify=not args.skip_verify, collect_failures=True,
            )
            if result.ingested:
                logger.info("  Initial disc: ingested %d pack(s).", result.ingested)
            all_failed.extend(result.failed)

            for label, vol_sources in pick_list.volumes.items():
                pack_hashes = [s.pack.sha256 for s in vol_sources]
                vol_path = vol_dir / label
                if not vol_path.is_dir():
                    logger.warning(
                        "Volume directory not found: %s — skipping", vol_path
                    )
                    vol_path = vol_dir
                result = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                    verify=not args.skip_verify, collect_failures=True,
                )
                if result.ingested:
                    logger.info("  %s: ingested %d pack(s).", label, result.ingested)
                if result.failed:
                    logger.warning(
                        "  %s: %d pack(s) failed verification.", label, len(result.failed)
                    )
                    all_failed.extend(result.failed)

            # The cache is the source of truth: packs that live on a
            # later disc were seeded into all_failed by the initial-disc
            # ingest, then ingested for real in the loop above.  Prune
            # them before any retry/raise so a multi-disc snapshot whose
            # cache is complete never falsely fails (RST-01).
            all_failed = _prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = _retry_from_alternates_batch(
                    executor, cache_dir, vol_dir,
                    all_failed, alternates_map,
                    verify=not args.skip_verify,
                )
                still_failed = _prune_recovered(cache_dir, still_failed)
                if still_failed:
                    discs = _discs_for_packs(still_failed, pick_list.volumes)
                    where = (
                        f" They live on disc(s): {', '.join(discs)}. "
                        "Re-mount those discs (check for damage) and retry."
                        if discs else ""
                    )
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"recovered.{where}"
                    )

        else:
            # Interactive mode: prompt user to mount each disc.
            if not sys.stdin.isatty():
                logger.error(
                    "Interactive restore requires a TTY.\n"
                    "Use --volume-dir with a directory of extracted discs "
                    "for non-interactive (scripted) restores."
                )
                return 1

            # First: ingest from the initial disc provided by the user.
            logger.info("\nIngesting packs from initial disc...")
            result = executor.ingest_volume(
                cache_dir, disc_path, plan.required_pack_hashes,
                verify=not args.skip_verify, collect_failures=True,
            )
            logger.info("  Ingested %d pack(s) from initial disc.", result.ingested)
            all_failed.extend(result.failed)

            # Determine which volumes are still needed.
            still_missing = RestoreExecutor.verify_cache_completeness(
                cache_dir, plan.required_pack_hashes
            )
            if still_missing:
                missing_set = set(still_missing)
                for label, vol_sources in sorted(pick_list.volumes.items()):
                    pack_hashes = [
                        s.pack.sha256 for s in vol_sources
                        if s.pack.sha256 in missing_set
                    ]
                    if not pack_hashes:
                        continue
                    logger.info(
                        "\nNeed %d pack(s) from disc '%s'.",
                        len(pack_hashes), label,
                    )
                    while True:
                        mount_path = input(
                            f"Mount disc '{label}' and enter its path "
                            "(or 'skip'): "
                        ).strip()
                        if mount_path.lower() == "skip":
                            logger.info("  Skipping %s", label)
                            break
                        vol_path = Path(mount_path)
                        if not vol_path.is_dir():
                            logger.info(
                                "  '%s' is not a directory — try again.", mount_path
                            )
                            continue
                        result = executor.ingest_volume(
                            cache_dir, vol_path, pack_hashes,
                            verify=not args.skip_verify, collect_failures=True,
                        )
                        logger.info(
                            "  Ingested %d pack(s) from %s", result.ingested, label
                        )
                        if result.failed:
                            logger.warning(
                                "  %d pack(s) failed verification", len(result.failed)
                            )
                            all_failed.extend(result.failed)
                        break

            all_failed = _prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = _retry_from_alternates_interactive(
                    executor, cache_dir,
                    all_failed, alternates_map,
                    verify=not args.skip_verify,
                )
                still_failed = _prune_recovered(cache_dir, still_failed)
                if still_failed:
                    discs = _discs_for_packs(still_failed, pick_list.volumes)
                    where = (
                        f" They live on disc(s): {', '.join(discs)}. "
                        "Re-mount those discs (check for damage) and retry."
                        if discs else ""
                    )
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"recovered.{where}"
                    )

        # Completeness check before running rustic.
        all_required = plan.required_pack_hashes
        missing = RestoreExecutor.verify_cache_completeness(cache_dir, all_required)
        if missing:
            logger.error(
                "\n%d of %d required pack(s) missing from cache.",
                len(missing), len(all_required),
            )
            for sha in missing[:10]:
                logger.error("  missing: %s", sha)
            if len(missing) > 10:
                logger.error("  ... and %d more", len(missing) - 10)
            logger.error(
                "\nRestore cannot proceed — mount the missing discs and retry,\n"
                "or check for damaged discs."
            )
            return 1

        # Execute restore.
        target = args.target_path.resolve()
        logger.info("\nRestoring snapshot '%s' \u2192 %s", snapshot_id, target)
        try:
            executor.execute_restore(
                cache_dir=cache_dir,
                snapshot_id=snapshot_id,
                target_path=target,
                password_file=args.password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Use standalone_restorer.py bundled on the disc as a fallback:\n"
                "  python3 %s/standalone_restorer.py "
                "--password-file %s --target %s",
                disc_path, args.password_file, target,
            )
            return 1

        logger.info("Restore complete!")
        return 0

    finally:
        shutdown.uninstall()
        tmp_dir_obj.cleanup()


def _retry_from_alternates_batch(
    executor: Any,
    cache_dir: Path,
    vol_dir: Path,
    failed_packs: list[str],
    alternates_map: dict[str, list[str]],
    *,
    verify: bool = True,
    iso_shas: dict[str, str | None] | None = None,
) -> list[str]:
    """Retry failed packs from alternate volumes (batch/non-interactive).

    Returns list of packs that could not be recovered.

    Phase 21.7: ``iso_shas`` is the same per-label SHA-256 dict
    computed once in cmd_restore_exec from the catalog.  When
    supplied, each alternate volume's ISO is integrity-checked
    (against the sibling .iso file, if present) before its packs
    are read — so a corrupt alternate that would otherwise silently
    spread bad packs into the cache is rejected up front, with the
    standard "try another location" recovery hint.
    """
    remaining = list(failed_packs)

    # Group by alternate volume to minimise disc access
    for alt_label in _collect_alt_labels(remaining, alternates_map):
        packs_on_alt = [
            sha for sha in remaining
            if alt_label in alternates_map.get(sha, [])
        ]
        if not packs_on_alt:
            continue

        vol_path = vol_dir / alt_label
        if not vol_path.is_dir():
            logger.warning(
                "Alternate volume directory not found: %s — skipping %s",
                vol_path, alt_label,
            )
            vol_path = vol_dir
            if not vol_path.is_dir():
                continue

        alt_iso = _find_sibling_iso(vol_dir, alt_label)
        alt_sha = iso_shas.get(alt_label) if iso_shas else None
        result = executor.ingest_volume(
            cache_dir, vol_path, packs_on_alt,
            verify=verify, collect_failures=True,
            iso_path=alt_iso, expected_sha256=alt_sha,
        )
        if result.ingested:
            logger.info(f"  Recovered {result.ingested} packs from {alt_label}")
        recovered = set(packs_on_alt) - set(result.failed)
        remaining = [sha for sha in remaining if sha not in recovered]

    return remaining


def _retry_from_alternates_interactive(
    executor: Any,
    cache_dir: Path,
    failed_packs: list[str],
    alternates_map: dict[str, list[str]],
    *,
    verify: bool = True,
    iso_dir: Path | None = None,
    iso_shas: dict[str, str | None] | None = None,
) -> list[str]:
    """Retry failed packs from alternate volumes (interactive).

    Prompts user to mount alternate volumes. Returns unrecoverable packs.

    Phase 21.9: ``iso_dir`` + ``iso_shas`` activate the same SHA-256
    verify-or-die check that the --volume-dir batch path gets.  When
    both are supplied, the alternate volume's <label>.iso under
    ``iso_dir`` is hashed against the catalog-recorded SHA before its
    packs are read.  Defaults are None for backward-compat — direct
    callers without iso info continue to work unchanged.
    """
    remaining = list(failed_packs)

    for alt_label in _collect_alt_labels(remaining, alternates_map):
        packs_on_alt = [
            sha for sha in remaining
            if alt_label in alternates_map.get(sha, [])
        ]
        if not packs_on_alt:
            continue

        logger.info(f"\n{len(packs_on_alt)} failed packs may be on "
                     f"alternate volume '{alt_label}'")
        mount_path = input(
            f"Mount '{alt_label}' and enter path (or 'skip'): "
        ).strip()
        if mount_path.lower() == "skip":
            continue
        vol_path = Path(mount_path)
        if not vol_path.is_dir():
            logger.info(f"  '{mount_path}' not a directory, skipping")
            continue

        alt_iso = (
            _find_sibling_iso(iso_dir, alt_label)
            if iso_dir is not None else None
        )
        alt_sha = iso_shas.get(alt_label) if iso_shas else None
        result = executor.ingest_volume(
            cache_dir, vol_path, packs_on_alt,
            verify=verify, collect_failures=True,
            iso_path=alt_iso, expected_sha256=alt_sha,
        )
        if result.ingested:
            logger.info(f"  Recovered {result.ingested} packs from {alt_label}")
        recovered = set(packs_on_alt) - set(result.failed)
        remaining = [sha for sha in remaining if sha not in recovered]

    return remaining


def _collect_alt_labels(
    packs: list[str], alternates_map: dict[str, list[str]]
) -> list[str]:
    """Collect unique alternate labels covering the given packs."""
    seen: set[str] = set()
    labels: list[str] = []
    for sha in packs:
        for alt in alternates_map.get(sha, []):
            if alt not in seen:
                seen.add(alt)
                labels.append(alt)
    return labels


def _prune_recovered(cache_dir: Path, failed: list[str]) -> list[str]:
    """Drop packs already present + valid in the cache (deduped).

    The cache is the source of truth: a pack ingested from any volume is
    recovered no matter which disc reported it as "not found on this
    volume".  Pruning ``failed`` against the cache before any retry/raise
    is what prevents a multi-disc snapshot from falsely failing — packs
    that live on a *later* disc are seeded into ``failed`` by the initial
    ingest, then ingested for real later in the loop, but nothing else
    removes them.  Also heals the verify-on-A / ingest-from-B case.
    """
    if not failed:
        return []
    from lcsas.restore.executor import RestoreExecutor

    unique = list(dict.fromkeys(failed))
    return RestoreExecutor.verify_cache_completeness(cache_dir, unique)


def _discs_for_packs(
    failed: list[str], volumes: dict[str, Any]
) -> list[str]:
    """Catalog volume label(s) holding the given (unrecovered) packs.

    ``volumes`` is a pick-list ``{label: [PackSource | Pack, ...]}`` map.
    Returns sorted, de-duped labels so a genuine failure names the discs
    to re-mount rather than only opaque SHA-256 hashes.  (Full label-map
    UX is RST-07; this is the minimal actionable list.)
    """
    wanted = set(failed)
    labels: set[str] = set()
    for label, entries in volumes.items():
        for entry in entries:
            sha = getattr(entry, "pack", entry).sha256
            if sha in wanted:
                labels.add(label)
                break
    return sorted(labels)


def dispatch(args: argparse.Namespace) -> int:
    """Route parsed args to the appropriate command handler."""
    if args.command == "init":
        return cmd_init(args)
    elif args.command == "repo":
        if args.repo_command == "add":
            return cmd_repo_add(args)
        elif args.repo_command == "list":
            return cmd_repo_list(args)
        elif args.repo_command == "remove":
            return cmd_repo_remove(args)
        else:
            logger.error("Usage: lcsas repo {add,list,remove}")
            return 1
    elif args.command == "scan":
        return cmd_scan(args)
    elif args.command == "pack":
        if args.pack_command == "unprune":
            return cmd_pack_unprune(args)
        else:
            logger.error("Usage: lcsas pack {unprune}")
            return 1
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "config":
        if args.config_command == "check":
            return cmd_config_check(args)
        else:
            logger.error("Usage: lcsas config {check}")
            return 1
    elif args.command == "stage":
        return cmd_stage(args)
    elif args.command == "burn":
        return cmd_burn_session(args)
    elif args.command == "burn-iso":
        return cmd_burn_iso(args)
    elif args.command == "staging":
        if args.staging_command == "clean":
            return cmd_staging_clean(args)
        else:
            logger.error("Usage: lcsas staging {clean}")
            return 1
    elif args.command == "location":
        return cmd_location(args)
    elif args.command == "copy":
        if args.copy_command in ("deprecate", "destroy"):
            return cmd_copy(args)
        else:
            logger.error("Usage: lcsas copy {deprecate,destroy}")
            return 1
    elif args.command == "volume":
        if args.volume_command == "impact":
            return cmd_volume_impact(args)
        else:
            logger.error("Usage: lcsas volume {impact}")
            return 1
    elif args.command == "catalog":
        if args.catalog_command == "import-receipts":
            return cmd_catalog_import(args)
        elif args.catalog_command == "validate":
            return cmd_catalog_validate(args)
        elif args.catalog_command == "reconcile":
            return cmd_catalog_reconcile(args)
        elif args.catalog_command == "rebuild":
            return cmd_catalog_rebuild(args)
        else:
            logger.error(
                "Usage: lcsas catalog {import-receipts|validate|reconcile|rebuild}"
            )
            return 1
    elif args.command == "session":
        if args.session_command == "list":
            return cmd_session_list(args)
        elif args.session_command == "abort":
            return cmd_session_abort(args)
        else:
            logger.error("Usage: lcsas session {list,abort}")
            return 1
    elif args.command == "restore":
        if args.restore_command == "plan":
            return cmd_restore_plan(args)
        elif args.restore_command == "exec":
            return cmd_restore_exec(args)
        elif args.restore_command in ("standalone", "from-disc"):
            return cmd_restore_from_disc(args)
        else:
            logger.error("Usage: lcsas restore {plan,exec,standalone}")
            return 1
    elif args.command == "meta":
        if args.meta_command == "build":
            return cmd_meta_build(args)
        elif args.meta_command == "verify":
            return cmd_meta_verify(args)
        else:
            logger.error("Usage: lcsas meta {build,verify}")
            return 1
    elif args.command == "recovery":
        return cmd_recovery(args)
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command == "consolidate":
        return cmd_consolidate(args)
    elif args.command == "key":
        if args.key_command == "split":
            return cmd_key_split(args)
        elif args.key_command == "combine":
            return cmd_key_combine(args)
        elif args.key_command == "verify":
            return cmd_key_verify(args)
        elif args.key_command == "card":
            return cmd_key_card(args)
        else:
            logger.error("Usage: lcsas key {split,combine,verify,card}")
            return 1
    elif args.command == "estate":
        if args.estate_command == "card":
            return cmd_estate_card(args)
        else:
            logger.error("Usage: lcsas estate card")
            return 1

    logger.error(f"Command '{args.command}' not yet implemented.")
    return 1


def _write_private_file(path: Path, data: bytes) -> None:
    """Write *data* to *path* with owner-only (0600) permissions, TOCTOU-safe.

    The file is created with ``O_CREAT | O_EXCL`` and mode 0600 so it is never
    even briefly readable by other users (mirrors ``db/connection.py``).  An
    existing file is a hard error rather than a silent overwrite — share files
    are sensitive and clobbering them could destroy the only copy.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _share_card_text(
    repo: str,
    index: int,
    threshold: int,
    count: int,
    mnemonic: str,
    split_date: str,
    split_id: int,
) -> str:
    """Build a plain-language printable 'share card' for one key share.

    The ``Split on`` date and ``Split ID`` (SLIP-0039 identifier, shared by
    every card of one split) let a holder spot a superseded card set after a
    re-key — see docs/ESTATE_PLANNING.md "ROTATION".
    """
    return (
        "================ LCSAS KEY SHARE ================\n"
        f"Repository : {repo}\n"
        f"Share      : {index} of {count}\n"
        f"Split on   : {split_date}\n"
        f"Split ID   : {split_id:05d}\n"
        "\n"
        "WHAT THIS IS\n"
        "  This card holds ONE share of the password that unlocks the\n"
        f"  '{repo}' backup archive.  The password was split into {count}\n"
        "  shares using Shamir Secret Sharing (SLIP-0039).\n"
        "\n"
        "HOW TO USE IT\n"
        f"  You need ANY {threshold} of the {count} shares to rebuild the\n"
        "  password.  Fewer than that reveal NOTHING about it.  Gather at\n"
        f"  least {threshold} share cards and run:\n"
        "      lcsas key combine --share-file <share1> --share-file <share2> ...\n"
        "  (or follow the recovery disc's instructions).\n"
        "\n"
        "  Other people each hold one of the remaining shares.  Losing more\n"
        f"  than {count - threshold} of the {count} shares means the archive\n"
        "  password can never be recovered.\n"
        "\n"
        "THE SHARE WORDS (keep every word, in order)\n"
        f"  {mnemonic}\n"
        "\n"
        "  Tip: you may type just the first 4 letters of each word — the\n"
        "  combiner expands any unambiguous prefix to the full word.\n"
        "================================================\n"
    )


def _check_code(password: bytes) -> str:
    """4-char transcription check code: first 4 hex of SHA-256(password).

    Lets an owner (and later an heir) confirm a hand-copied password was
    transcribed correctly without printing the password itself.  Discloses
    ~16 bits of an oracle on the password — negligible against a
    high-entropy password but stated honestly on the card.
    """
    import hashlib

    return hashlib.sha256(password).hexdigest()[:4]


def _recovery_card_text(
    repo: str,
    key_file_name: str,
    storage_hints: str,
    card_date: str,
    label_prefix: str,
    check_code: str | None,
) -> str:
    """Build a paper Recovery Card for a DEFAULT (single-key) archive.

    Mirrors the visual style of ``_share_card_text``.  The password itself
    is NEVER rendered — the owner writes it by hand into the PASSWORD box.
    When *check_code* is given, an heir can confirm a correct transcription
    with ``lcsas key card --check <typed-file> --code <code>``.
    """
    hints = storage_hints if storage_hints else "(fill in where copies are kept)"
    lines = [
        "=============== LCSAS RECOVERY CARD ===============",
        f"Repository : {repo}",
        f"Key file   : {key_file_name}",
        f"Made on    : {card_date}",
        f"Discs      : labeled {label_prefix}_*",
        "",
        "WHAT THIS IS",
        "  This card records the password that unlocks the",
        f"  '{repo}' backup archive.  WITHOUT it the discs cannot",
        "  be decrypted — there is no recovery.  Store this card",
        "  physically SEPARATE from the discs (key/data separation).",
        "",
        "THE PASSWORD (write it here by hand — do NOT type it into a",
        "computer to print this card)",
        "  ┌────────────────────────────────────────────────────┐",
        "  │                                                      │",
        "  │                                                      │",
        "  └────────────────────────────────────────────────────┘",
        "",
        "WHERE COPIES ARE KEPT",
        f"  {hints}",
        "",
        "HOW TO USE IT",
        "  Mount any archive disc and follow START_HERE.txt; supply",
        f"  the password above as the key for repository '{repo}'.",
    ]
    if check_code is not None:
        lines += [
            "",
            "TRANSCRIPTION CHECK CODE",
            f"  {check_code}",
            "  After writing the password, save it to a file and run:",
            "      lcsas key card --check <file> --code "
            f"{check_code}",
            "  MATCH means you copied it correctly; MISMATCH means a typo.",
            "  (The code is the first 4 hex of SHA-256(password). It leaks",
            "  ~16 bits of an oracle — negligible for a strong password.)",
        ]
    lines.append("===================================================")
    return "\n".join(lines) + "\n"


def cmd_key_card(args: argparse.Namespace) -> int:
    """Print (or verify) a paper Recovery Card for a single-key archive."""
    # ── Verify mode: recompute the check code from a typed password file ──
    if args.check is not None:
        if args.code is None:
            logger.error("--check requires --code (the code printed on the card).")
            return 1
        check_file: Path = args.check
        if not check_file.exists():
            logger.error("Password file does not exist: %s", check_file)
            return 1
        # Match the split path's read convention (drop one trailing newline).
        password = check_file.read_bytes().rstrip(b"\n")
        computed = _check_code(password)
        expected = args.code.strip().lower()
        if computed == expected:
            print(f"MATCH: {computed}")
            return 0
        print(f"MISMATCH: typed file -> {computed}, card -> {expected}")
        return 1

    if args.code is not None:
        logger.error("--code is only valid with --check (verify mode).")
        return 1

    # ── Render mode ──────────────────────────────────────────────────────
    from datetime import date

    from lcsas.config.settings import load_config

    if args.config is None:
        logger.error("--config is required to render a recovery card.")
        return 1
    config = load_config(args.config)

    repo_name = args.repo
    if repo_name is None:
        if len(config.repositories) != 1:
            logger.error(
                "--repo is required (config defines %d repositories).",
                len(config.repositories),
            )
            return 1
        repo_name = next(iter(config.repositories))

    repo_cfg = config.repositories.get(repo_name)
    if repo_cfg is None:
        logger.error(
            "Repository '%s' is not defined in the config file.", repo_name
        )
        return 1
    if repo_cfg.password_file is None:
        logger.error(
            "Repository '%s' has no password_file configured.", repo_name
        )
        return 1

    check_code: str | None = None
    if not args.no_check_code:
        pw_file = repo_cfg.password_file
        if not pw_file.exists():
            logger.error(
                "Password file does not exist: %s (use --no-check-code to "
                "render a card without a transcription check).", pw_file,
            )
            return 1
        password = pw_file.read_bytes().rstrip(b"\n")
        check_code = _check_code(password)

    card = _recovery_card_text(
        repo=repo_name,
        key_file_name=repo_cfg.password_file.name,
        storage_hints=config.key_storage_hints,
        card_date=date.today().isoformat(),
        label_prefix=config.label_prefix,
        check_code=check_code,
    )

    if args.out is not None:
        _write_private_file(args.out, card.encode("utf-8"))
        print(f"Wrote recovery card for repo '{repo_name}' to {args.out}")
        print(
            "  Mode 0600. The password is NOT on the card — write it in by "
            "hand and store the card separate from the discs.",
            file=sys.stderr,
        )
    else:
        print(card, end="")
    return 0


def _estate_card_text(
    *,
    owner: str,
    description: str,
    technical_contact: str,
    repositories: list[str],
    key_storage_hints: str,
    key_split: bool,
    key_threshold: int,
    key_shares: int,
    label_prefix: str,
    disc_count: int | None,
    card_date: str,
) -> str:
    """Build the one-page whole-archive Recovery Card (UX-09).

    Unlike the per-repo ``key card`` (which records ONE password), this is
    the heir's starting sheet for the entire estate: who owns it, what is on
    the discs, how many discs there are, how the key is stored, and the
    literal first command to run.  No secret is ever printed.  Every field
    degrades to a fill-in blank when its source (config or catalog) is
    absent, so the card is always printable.
    """
    blank = "______________________________"

    def or_blank(value: str) -> str:
        return value if value else blank

    repos_line = ", ".join(repositories) if repositories else blank
    if disc_count is None:
        disc_line = f"{blank}  (labeled {label_prefix}_*)"
    else:
        disc_line = f"{disc_count} disc(s), labeled {label_prefix}_*"

    lines = [
        "============== LCSAS RECOVERY CARD (ARCHIVE) ==============",
        f"Owner       : {or_blank(owner)}",
        f"Description : {or_blank(description)}",
        f"Made on     : {card_date}",
        "",
        "WHAT THIS IS",
        "  This is the starting sheet for an entire LCSAS backup",
        "  archive.  Keep it WITH the discs; keep the password(s)",
        "  SOMEWHERE ELSE (key/data separation).  WITHOUT the",
        "  password the discs cannot be decrypted — there is no",
        "  recovery.",
        "",
        "WHAT IS ON THE DISCS",
        f"  Repositories : {repos_line}",
        f"  Discs        : {disc_line}",
        "",
        "WHERE THE KEY IS KEPT",
        f"  {or_blank(key_storage_hints)}",
    ]
    if key_split:
        lines += [
            "",
            "THE KEY IS SPLIT INTO SHARE CARDS",
            f"  Any {key_threshold} of {key_shares} share cards reconstruct the",
            "  password; fewer reveal nothing.  Share holders:",
            "    1. ______________________   2. ______________________",
            "    3. ______________________   4. ______________________",
            "    5. ______________________",
            "  Gather any K share cards and, on the LCSAS_META disc, run:",
            "      python3 keyshare_combine.py <card1> <card2>",
            "  It prints the password; enter it at the restore prompt.",
        ]
    lines += [
        "",
        "FIRST STEP — recover the files",
        "  Insert the META disc, then for your computer:",
        "    Windows : open the LCSAS_META disc and double-click",
        "              restore.bat",
        "    macOS   : open Terminal and run",
        f"              sh /Volumes/{label_prefix}_META/restore.sh ~/restored",
        "    Linux   : open a terminal and run",
        f"              sh /media/$USER/{label_prefix}_META/restore.sh ~/restored",
        "  Then follow the on-screen prompts (START_HERE.txt on any",
        "  disc has the full walkthrough).",
        "",
        "IF YOU GET STUCK",
        f"  Technical contact: {or_blank(technical_contact)}",
        "  Or take ALL the discs AND the password to any computer",
        "  professional — the instructions travel on the discs.",
        "",
        "STORE THIS SHEET WITH THE DISCS — STORE THE PASSWORD ELSEWHERE.",
        "==========================================================",
    ]
    return "\n".join(lines) + "\n"


def cmd_estate_card(args: argparse.Namespace) -> int:
    """Generate the whole-archive Recovery Card (UX-09).

    Reads survivability fields from the config and, when a catalog is
    reachable, the burned-disc count.  Never fails on a missing/old
    catalog — the disc inventory degrades to a fill-in blank.
    """
    from datetime import date

    from lcsas.config.settings import load_config

    if args.config is None:
        logger.error("--config is required to generate an estate Recovery Card.")
        return 1
    config = load_config(args.config)

    disc_count: int | None = None
    db_path = _resolve_db_path(args, config)
    if db_path.exists():
        from lcsas.constants import (
            STATUS_BURNED,
            STATUS_DEPRECATED,
            STATUS_VERIFIED,
        )
        from lcsas.db.connection import get_connection
        from lcsas.db.schema import ensure_schema
        from lcsas.db.volumes import list_volumes

        conn = get_connection(db_path)
        try:
            ensure_schema(conn)
            # Count discs that physically exist (burned and beyond), not
            # in-progress STAGING/BURNING volumes an heir won't hold, nor
            # DESTROYED ones that no longer exist.
            burned = {STATUS_BURNED, STATUS_VERIFIED, STATUS_DEPRECATED}
            disc_count = sum(
                1 for v in list_volumes(conn) if v.status in burned
            )
        finally:
            conn.close()

    card = _estate_card_text(
        owner=config.archive_owner,
        description=config.archive_description,
        technical_contact=config.technical_contact,
        repositories=sorted(config.repositories),
        key_storage_hints=config.key_storage_hints,
        key_split=config.key_split,
        key_threshold=config.key_threshold,
        key_shares=config.key_shares,
        label_prefix=config.label_prefix,
        disc_count=disc_count,
        card_date=date.today().isoformat(),
    )

    if args.output is not None:
        args.output.write_text(card, encoding="utf-8")
        print(f"Wrote whole-archive Recovery Card to {args.output}")
        print(
            "  Store this sheet WITH the discs; store the password "
            "SOMEWHERE ELSE.",
            file=sys.stderr,
        )
    else:
        print(card, end="")
    return 0


def _verify_password_unlocks_repo(
    keys_dir: Path, password: bytes, repo: str,
) -> str | None:
    """Authenticate *password* against a repo's ``keys/`` directory.

    Returns ``None`` on success; otherwise a human-readable error string
    naming the repo, the keys dir, and how to override.  Fails closed: a
    missing or empty ``keys/`` is an error (escrow is exactly where
    "probably fine" is wrong).
    """
    from lcsas.restore.restic_fallback import IntegrityError, _try_keys

    if not keys_dir.is_dir():
        return (
            f"keys directory not found for repository '{repo}': {keys_dir}. "
            "Cannot verify that the password unlocks the repo. Make the "
            "mirror's keys/ reachable, or pass --no-verify-repo to skip "
            "(escrowing an unverified password is dangerous)."
        )
    if not any(p.is_file() for p in keys_dir.iterdir()):
        return (
            f"no key files under {keys_dir} for repository '{repo}'. "
            "Cannot verify that the password unlocks the repo. "
            "Pass --no-verify-repo to skip (dangerous)."
        )
    try:
        _try_keys(keys_dir, password)
    except IntegrityError:
        n = sum(1 for p in keys_dir.iterdir() if p.is_file())
        return (
            f"the password in this file does NOT unlock repository "
            f"'{repo}' ({n} key file(s) checked under {keys_dir}). "
            "You were about to escrow a wrong password. If you really "
            "intend this, pass --no-verify-repo."
        )
    return None


def _recombine_roundtrip_error(
    mnemonics: list[str], expected: bytes,
) -> str | None:
    """Recombine K-subsets of *mnemonics* and confirm they reproduce *expected*.

    Returns ``None`` on success, else an error naming the failing subset.
    Bounds the C(N,K) blow-up at 64 subsets.  *mnemonics[0]*'s threshold is
    read from the share metadata so this works without the caller passing K.
    """
    from itertools import combinations

    from lcsas.keyshare import KeyShareError, decode_master_secret, recover_secret
    from lcsas.keyshare.slip39 import _Share

    # Read K from share metadata.  A corrupted/short mnemonic here is itself a
    # verification failure (its checksum won't parse).
    threshold: int | None = None
    for i, mn in enumerate(mnemonics):
        try:
            threshold = _Share.from_mnemonic(mn).member_threshold
            break
        except KeyShareError as e:
            return f"share {i + 1} is not a valid SLIP-0039 share: {e}"
    assert threshold is not None  # mnemonics is non-empty; loop above returns
    subsets = list(combinations(range(len(mnemonics)), threshold))
    if len(subsets) > 64:
        subsets = subsets[:64]
    for idx in subsets:
        subset = [mnemonics[i] for i in idx]
        try:
            got = decode_master_secret(recover_secret(subset))
        except KeyShareError as e:
            return (
                f"shares {[i + 1 for i in idx]} failed to reconstruct: {e}"
            )
        if got != expected:
            return (
                f"shares {[i + 1 for i in idx]} do not reconstruct the input "
                "password"
            )
    return None


def cmd_key_split(args: argparse.Namespace) -> int:
    """Split a repository password into K-of-N SLIP-0039 key shares."""
    from datetime import date

    from lcsas.config.settings import load_config
    from lcsas.keyshare import (
        KeyShareError,
        encode_master_secret,
        extract_mnemonic,
        share_identifier,
        split_secret,
    )

    # Resolve K/N: explicit flags override config defaults.
    threshold = args.threshold
    shares = args.shares
    repo_pw_file: Path | None = args.password_file

    config = None
    if args.config is not None:
        config = load_config(args.config)
    if threshold is None:
        threshold = config.key_threshold if config is not None else 2
    if shares is None:
        shares = config.key_shares if config is not None else 5

    # Resolve the password source.
    if repo_pw_file is None:
        if config is None:
            logger.error(
                "No password source: pass --password-file, or pass --config "
                "so the repo's configured password_file can be used."
            )
            return 1
        repo_cfg = config.repositories.get(args.repo)
        if repo_cfg is None:
            logger.error(
                "Repository '%s' is not defined in the config file.", args.repo
            )
            return 1
        if repo_cfg.password_file is None:
            logger.error(
                "Repository '%s' has no password_file configured; "
                "pass --password-file instead.", args.repo
            )
            return 1
        repo_pw_file = repo_cfg.password_file

    if not repo_pw_file.exists():
        logger.error("Password file does not exist: %s", repo_pw_file)
        return 1

    # Read the password and drop a single trailing newline (key files often
    # have one; rustic's --password-file ignores it).
    password = repo_pw_file.read_bytes().rstrip(b"\n")

    try:
        master_secret = encode_master_secret(password)
        mnemonics = split_secret(master_secret, threshold, shares)
    except (KeyShareError, ValueError) as e:
        # KeyShareError: oversized password / bad master secret.
        # ValueError: invalid threshold/shares (e.g. K>N, non-positive).
        logger.error("Could not split password: %s", e)
        return 1

    # ── Repo-unlock verification (default on) ─────────────────────────
    # Authenticate the password against the real key files BEFORE writing
    # anything, so a failed split leaves no wrong-password card on disk.
    if args.no_verify_repo:
        logger.warning(
            "Skipping repo-unlock check (--no-verify-repo): the password "
            "in %s was NOT verified to unlock repository '%s'.",
            repo_pw_file, args.repo,
        )
    elif config is None:
        logger.warning(
            "No --config given: the password was NOT verified against any "
            "repository. Run `lcsas key verify --config ... --password-file "
            "%s` once the mirror is reachable.", repo_pw_file,
        )
    else:
        repo_cfg = config.repositories.get(args.repo)
        if repo_cfg is None:
            logger.error(
                "Repository '%s' is not defined in the config file.", args.repo
            )
            return 1
        err = _verify_password_unlocks_repo(
            repo_cfg.mirror_path / "keys", password, args.repo
        )
        if err is not None:
            logger.error("%s", err)
            return 1

    # ── In-memory recombine round-trip (always on) ────────────────────
    err = _recombine_roundtrip_error(mnemonics, password)
    if err is not None:
        logger.error("SPLIT FAILED VERIFICATION: %s. Nothing written.", err)
        return 1

    split_date = date.today().isoformat()
    try:
        split_id = share_identifier(mnemonics[0])
    except KeyShareError as e:  # pragma: no cover - just-built share is valid
        logger.error("Could not read share identifier: %s", e)
        return 1

    out_dir: Path = args.out if args.out is not None else Path(f"./keyshares-{args.repo}")
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, mnemonic in enumerate(mnemonics, start=1):
        share_path = out_dir / f"{args.repo}-share-{i}.txt"
        card_path = out_dir / f"{args.repo}-share-{i}-card.txt"
        _write_private_file(share_path, (mnemonic + "\n").encode("utf-8"))
        card = _share_card_text(
            args.repo, i, threshold, shares, mnemonic, split_date, split_id
        )
        _write_private_file(card_path, card.encode("utf-8"))

    # ── Post-write round-trip (write-path check) ──────────────────────
    # Re-read the CARD files from disk and verify the printed artifact —
    # not just the bare files — reconstructs the password.  Catches a
    # corrupted/short write before any card is distributed.
    card_files = [
        out_dir / f"{args.repo}-share-{i}-card.txt"
        for i in range(1, shares + 1)
    ]
    try:
        written_mnemonics = [
            extract_mnemonic(p.read_text(encoding="utf-8"), source=str(p))
            for p in card_files
        ]
    except KeyShareError as e:
        logger.error(
            "SPLIT FAILED VERIFICATION: written card unreadable: %s. "
            "Do NOT distribute these cards.", e,
        )
        return 1
    err = _recombine_roundtrip_error(written_mnemonics, password)
    if err is not None:
        logger.error(
            "SPLIT FAILED VERIFICATION: written cards %s. Do NOT distribute "
            "these cards.", err,
        )
        return 1

    # ── Record the split durably (KEY-08) ─────────────────────────────
    # The catalog row is what the burn pipeline reconciles against and what
    # disc share-instructions are derived from.  Without --config there is no
    # catalog to write to, so drift-checking is unavailable — warn loudly.
    if config is not None:
        from lcsas.db.connection import locked_connection
        from lcsas.db.key_escrow import record_split
        from lcsas.db.schema import ensure_schema

        try:
            with locked_connection(config.db_path) as conn:
                ensure_schema(conn)
                record_split(conn, args.repo, threshold, shares, split_id)
        except Exception as e:  # noqa: BLE001 - cards are written; don't fail the split
            logger.warning(
                "Split succeeded but recording it in the catalog failed: %s. "
                "Run 'lcsas migrate' then re-run 'lcsas key split' so burn "
                "drift-checking works.", e,
            )
    else:
        logger.warning(
            "No --config given: the split was NOT recorded in any catalog, so "
            "burn-time key-escrow drift checking is unavailable. Re-run with "
            "--config once the catalog is reachable.",
        )

    print(
        f"Wrote {shares} share(s) ({threshold}-of-{shares}) for repo "
        f"'{args.repo}' to {out_dir}"
    )
    print(
        "  Each share and its plain-language card is mode 0600.\n"
        f"  Distribute the {shares} shares to separate holders/locations."
    )
    print(
        f"NEXT STEP: set key_split = true (and key_threshold = {threshold}, "
        f"key_shares = {shares}) under [defaults] in lcsas.toml so burned "
        "discs print share instructions."
    )
    print(
        "SECURITY: the password was never printed. Store shares apart; any "
        f"{threshold} of {shares} reconstruct it, fewer reveal nothing.",
        file=sys.stderr,
    )
    return 0


def cmd_key_combine(args: argparse.Namespace) -> int:
    """Reconstruct a repository password from K SLIP-0039 key-share mnemonics."""
    from lcsas.keyshare import (
        KeyShareError,
        check_share,
        decode_master_secret,
        extract_mnemonic,
        is_mnemonic_line,
        recover_secret,
    )

    mnemonics: list[str] = []
    labels: list[str] = []
    if args.share_files:
        for sf in args.share_files:
            if not sf.exists():
                logger.error("Share file does not exist: %s", sf)
                return 1
            # Each file is one share: a bare mnemonic file or a printed
            # share card.  extract_mnemonic ignores card header/prose and
            # raises KeyShareError (naming the file + word count) on a
            # truncated/prose-only file.
            text = sf.read_text(encoding="utf-8")
            try:
                mnemonics.append(extract_mnemonic(text, source=str(sf)))
            except KeyShareError as e:
                logger.error("%s", e)
                return 1
            labels.append(str(sf))
    else:
        # No --share-file: read shares from stdin.  Skip non-mnemonic
        # lines (blanks, card prose) so `cat card1 card2 | ...` works.
        for lineno, line in enumerate(sys.stdin, start=1):
            if is_mnemonic_line(line):
                mnemonics.append(" ".join(line.split()))
                labels.append(f"stdin line {lineno}")

    if not mnemonics:
        logger.error(
            "No shares supplied. Pass one or more --share-file, or pipe "
            "shares on stdin (one mnemonic per line)."
        )
        return 1

    # Per-share pre-pass: validate each share independently and print a
    # named verdict, so a single mistyped card is pinpointed (file + word
    # position + token) instead of collapsing into one generic failure.
    # Verdicts go to stderr so stdout stays password-only (the combiner's
    # raw-bytes contract); mirrors the C lcsas-keyshare pre-pass.
    any_bad = False
    for label, mnemonic in zip(labels, mnemonics, strict=True):
        reason = check_share(mnemonic)
        if reason is None:
            print(f"share ({label}): OK", file=sys.stderr)
        else:
            print(f"share ({label}): {reason}", file=sys.stderr)
            any_bad = True
    if any_bad:
        logger.error(
            "One or more shares failed individual validation; fix the "
            "flagged words above and retry."
        )
        return 1

    try:
        master_secret = recover_secret(mnemonics)
        password = decode_master_secret(master_secret)
    except KeyShareError as e:
        logger.error(
            "Could not reconstruct the password: %s\n"
            "Check that you supplied at least the threshold (K) of valid, "
            "uncorrupted shares from the SAME split.", e
        )
        return 1

    if args.out is not None:
        _write_private_file(args.out, password)
        print(f"Reconstructed password written to {args.out} (mode 0600).")
    else:
        # Raw bytes: a password may not be valid UTF-8 and must not gain a
        # trailing newline, or it would no longer match the original.
        sys.stdout.buffer.write(password)
        sys.stdout.buffer.flush()
    return 0


def cmd_key_verify(args: argparse.Namespace) -> int:
    """Verify that shares (or a password file) unlock a repository.

    The annual key drill (recovery/docs/READINESS_CHECKLIST.txt): reconstruct
    the password from the supplied shares/cards (or read --password-file), then
    authenticate it against the repo's real ``keys/`` files.  Exit code is the
    contract — rc 0 only when the password actually unlocks the repo.
    """
    from lcsas.config.settings import load_config
    from lcsas.keyshare import (
        KeyShareError,
        decode_master_secret,
        extract_mnemonic,
        recover_secret,
    )

    if args.config is None:
        logger.error("--config is required for key verify.")
        return 1
    if bool(args.share_files) == bool(args.password_file):
        logger.error(
            "Pass EITHER one or more --share-file (to reconstruct), OR a "
            "single --password-file (to test directly)."
        )
        return 1

    config = load_config(args.config)
    repo_cfg = config.repositories.get(args.repo)
    if repo_cfg is None:
        logger.error(
            "Repository '%s' is not defined in the config file.", args.repo
        )
        return 1

    # Resolve the candidate password.
    if args.password_file is not None:
        if not args.password_file.exists():
            logger.error("Password file does not exist: %s", args.password_file)
            return 1
        password = args.password_file.read_bytes().rstrip(b"\n")
        n_shares = 0
    else:
        mnemonics: list[str] = []
        for sf in args.share_files:
            if not sf.exists():
                logger.error("Share file does not exist: %s", sf)
                return 1
            try:
                mnemonics.append(
                    extract_mnemonic(
                        sf.read_text(encoding="utf-8"), source=str(sf)
                    )
                )
            except KeyShareError as e:
                logger.error("%s", e)
                return 1
        try:
            password = decode_master_secret(recover_secret(mnemonics))
        except KeyShareError as e:
            logger.error(
                "Reconstruction failed: %s\n"
                "Supply at least the threshold (K) of valid shares from the "
                "SAME split.", e,
            )
            return 1
        n_shares = len(mnemonics)

    keys_dir = repo_cfg.mirror_path / "keys"
    err = _verify_password_unlocks_repo(keys_dir, password, args.repo)
    if err is not None:
        logger.error("%s", err)
        return 1

    n_keys = sum(1 for p in keys_dir.iterdir() if p.is_file())
    if n_shares:
        print(
            f"OK: these {n_shares} share(s) reconstruct the password that "
            f"unlocks '{args.repo}' ({n_keys} key file(s) checked)."
        )
    else:
        print(
            f"OK: the password unlocks '{args.repo}' "
            f"({n_keys} key file(s) checked)."
        )
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    """List burn sessions stored in the catalog."""
    from lcsas.config.settings import load_config
    from lcsas.db.connection import get_connection
    from lcsas.db.sessions import get_session_volumes, list_sessions

    if args.config is None:
        logger.error("--config is required for session list.")
        return 1
    config = load_config(args.config)
    conn = get_connection(config.db_path)
    try:
        sessions = list_sessions(conn, status_filter=args.status)

        if not sessions:
            status_filter = f" with status={args.status}" if args.status else ""
            logger.info("No sessions found%s.", status_filter)
            return 0

        logger.info("%-36s  %-10s  %-10s  %s", "SESSION ID", "STATUS", "MEDIA", "CREATED")
        logger.info("-" * 80)

        for s in sessions:
            vols = get_session_volumes(conn, s.session_id)
            vol_labels = ", ".join(sv.volume_id and str(sv.volume_id) or "?" for sv in vols)
            logger.info(
                "%-36s  %-10s  %-10s  %s",
                s.session_id, s.status, s.media_type,
                s.created_at[:19] if s.created_at else "",
            )
            if vols:
                logger.info("  volumes(%d): %s", len(vols), vol_labels)
    finally:
        conn.close()

    return 0


def cmd_session_abort(args: argparse.Namespace) -> int:
    """Abort a never-burned session (or one stranded volume), reclaiming packs."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.settings import load_config
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    if args.config is None:
        logger.error("--config is required for session abort.")
        return 1
    config = load_config(args.config)

    with locked_connection(args.db or config.db_path) as conn:
        ensure_schema(conn)
        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )
        try:
            if args.volume:
                orch.abort_volume(args.volume)
            else:
                orch.abort_session(args.ref)
        except ValueError as e:
            logger.error("%s", e)
            return 1
    return 0


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
        from lcsas.exceptions import LcsasError
        if isinstance(e, LcsasError):
            logger.error("%s", e)
            if e.recovery_hint:
                logger.error("Hint: %s", e.recovery_hint)
        else:
            logger.error("Unexpected error: %s", e)
            if getattr(args, "verbose", False):
                traceback.print_exc()
            else:
                logger.error("Run with --verbose for a full traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
