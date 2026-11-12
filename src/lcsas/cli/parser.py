"""The ``lcsas`` argument parser.

Each (sub)parser names its handler with ``set_defaults(func=...)``;
:func:`lcsas.cli.main.dispatch` calls it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from lcsas import __version__
from lcsas.cli.commands.burn import cmd_burn_iso, cmd_burn_session
from lcsas.cli.commands.catalog import (
    cmd_catalog_import,
    cmd_catalog_rebuild,
    cmd_catalog_reconcile,
    cmd_catalog_validate,
)
from lcsas.cli.commands.config import cmd_config_check
from lcsas.cli.commands.consolidate import cmd_consolidate
from lcsas.cli.commands.copy import cmd_copy
from lcsas.cli.commands.estate import cmd_estate_card
from lcsas.cli.commands.init import cmd_init
from lcsas.cli.commands.key import (
    cmd_key_card,
    cmd_key_combine,
    cmd_key_split,
    cmd_key_unsplit,
    cmd_key_verify,
)
from lcsas.cli.commands.location import cmd_location
from lcsas.cli.commands.meta import cmd_meta_build, cmd_meta_verify
from lcsas.cli.commands.pack import cmd_pack_unprune
from lcsas.cli.commands.recovery import cmd_recovery
from lcsas.cli.commands.repo import (
    cmd_repo_activate,
    cmd_repo_add,
    cmd_repo_list,
    cmd_repo_remove,
    cmd_repo_retire,
)
from lcsas.cli.commands.restore import cmd_restore_exec, cmd_restore_from_disc, cmd_restore_plan
from lcsas.cli.commands.scan import cmd_scan
from lcsas.cli.commands.session import cmd_session_abort, cmd_session_list
from lcsas.cli.commands.stage import cmd_stage
from lcsas.cli.commands.staging import cmd_staging_clean
from lcsas.cli.commands.status import cmd_status
from lcsas.cli.commands.verify import cmd_verify
from lcsas.cli.commands.volume import cmd_volume_impact
from lcsas.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger()


# Options only the top-level parser knows; `lcsas scan --config x.toml`
# is an argparse "unrecognized arguments" error.
_TOP_LEVEL_OPTIONS = ("--config", "--db", "--lock-timeout", "--verbose", "-v")


class _LcsasArgumentParser(argparse.ArgumentParser):
    """Says where a misplaced top-level option goes (#517).

    argparse reports ``lcsas scan --config x.toml`` as a bare
    "unrecognized arguments: --config x.toml", which never says the flag
    belongs before the subcommand.  The exit stays argparse's (status 2,
    nothing run); only the message gains the hint.
    """

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments:"):
            given = {tok.split("=", 1)[0] for tok in message.split(":", 1)[1].split()}
            misplaced = [opt for opt in _TOP_LEVEL_OPTIONS if opt in given]
            if misplaced:
                opts = " ".join(misplaced)
                message += (
                    f"\n{opts}: top-level option(s); put them before the "
                    f"subcommand, e.g. 'lcsas {misplaced[0]} ... scan', "
                    f"not 'lcsas scan {misplaced[0]} ...'"
                )
        super().error(message)


def _volume_ref(text: str) -> int | str:
    """argparse type for a volume: a catalog ID (all digits) or a label."""
    return int(text) if text.isdigit() else text


def _usage(line: str) -> Callable[[argparse.Namespace], int]:
    """Handler for a group given no subcommand: log *line*, exit 1."""
    def handler(args: argparse.Namespace) -> int:
        logger.error(line)
        return 1
    return handler


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands.

    Each (sub)parser names its handler with ``set_defaults(func=...)``;
    :func:`dispatch` calls it.
    """
    parser = _LcsasArgumentParser(
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
    parser.add_argument(
        "--lock-timeout", type=float, default=None,
        help="Seconds to wait for the catalog lock before failing with exit "
             "code 75 (default: wait forever). Overrides LCSAS_LOCK_TIMEOUT.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- init ---
    init_p = subparsers.add_parser("init", help="Initialize the LCSAS catalog and config.")
    init_p.set_defaults(func=cmd_init)
    init_p.add_argument("--db-path", type=Path, default=None,
                        help="Path for the SQLite catalog file. "
                             "Overrides --config's [paths].database when set.")

    # --- repo ---
    repo_p = subparsers.add_parser("repo", help="Manage backup repositories.")
    repo_p.set_defaults(func=_usage("Usage: lcsas repo {add,list,remove,retire,activate}"))
    repo_sub = repo_p.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Register a new repository.")
    repo_add.set_defaults(func=cmd_repo_add)
    repo_add.add_argument("name", help="Repository name (e.g., 'family').")
    repo_add.add_argument("mirror_path", type=Path, help="Path to the local mirror.")

    repo_list = repo_sub.add_parser("list", help="List registered repositories.")
    repo_list.set_defaults(func=cmd_repo_list)

    repo_rm = repo_sub.add_parser("remove", help="Remove a repository.")
    repo_rm.set_defaults(func=cmd_repo_remove)
    repo_rm.add_argument("repo_id", metavar="REPO",
                         help="Repository name (or its catalog ID) to remove.")
    repo_rm.add_argument("--force", action="store_true",
                         help="Force removal even if packs exist (marks them pruned).")

    repo_retire = repo_sub.add_parser(
        "retire",
        help="Mark a repository retired (its mirror is gone for good).",
        description="Mark a repository retired: its mirror is gone for good, "
                    "so `lcsas meta build` stops requiring its Rustic keys. "
                    "The catalog row and its pack history are kept intact "
                    "(unlike `repo remove --force`). Reversible with "
                    "`lcsas repo activate`.",
    )
    repo_retire.set_defaults(func=cmd_repo_retire)
    repo_retire.add_argument("repo_id", metavar="REPO",
                             help="Repository name (or its catalog ID) to retire.")

    repo_activate = repo_sub.add_parser(
        "activate",
        help="Mark a retired repository active again.",
    )
    repo_activate.set_defaults(func=cmd_repo_activate)
    repo_activate.add_argument(
        "repo_id", metavar="REPO",
        help="Repository name (or its catalog ID) to activate.")

    # --- scan ---
    scan_p = subparsers.add_parser(
        "scan",
        help="Scan mirrors for new packs and register them in the catalog.",
    )
    scan_p.set_defaults(func=cmd_scan)
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
             "max(10, 20%% of a repository's active packs) as pruned.",
    )

    # --- pack ---
    pack_p = subparsers.add_parser("pack", help="Manage individual packs in the catalog.")
    pack_p.set_defaults(func=_usage("Usage: lcsas pack {unprune}"))
    pack_sub = pack_p.add_subparsers(dest="pack_command")

    pack_unprune_p = pack_sub.add_parser(
        "unprune",
        help="Restore a wrongly-pruned pack to the active pool.",
    )
    pack_unprune_p.set_defaults(func=cmd_pack_unprune)
    pack_unprune_p.add_argument(
        "sha256",
        help="Pack ID: the pack's SHA-256 (a unique prefix is accepted).",
    )

    # --- status ---
    status_p = subparsers.add_parser("status", help="Show archive status summary.")
    status_p.set_defaults(func=cmd_status)
    status_p.add_argument("--stale-copies", action="store_true", default=False,
                          help="List discs never verified or not "
                               "verified within the threshold (FMA-05).")
    status_p.add_argument("--older-than-days", type=int, default=365,
                          help="Staleness threshold in days for --stale-copies "
                               "and the summary warning (default: 365).")
    status_p.add_argument("--redundancy", action="store_true", default=False,
                          help="Blast-radius report: under-replicated packs "
                               "grouped by the volume holding them (FMA-08).")
    status_p.add_argument("--min-copies", type=int, default=2,
                          help="Copy threshold for --redundancy (default: 2).")
    status_p.add_argument("--json", action="store_true", default=False,
                          help="Print the summary as one JSON document on "
                               "stdout (messages go to stderr), with the "
                               "volumes waiting to be burned per location. "
                               "For scripts; see docs/workflows/"
                               "collections.md.")

    # --- burn ---
    burn_p = subparsers.add_parser("burn", help="Burn staged ISOs to disc.")
    burn_p.set_defaults(func=cmd_burn_session)
    burn_p.add_argument("--media", type=str, default=None,
                        help="Accepted but ignored: a burn session keeps the "
                             "media type it was staged with (set it with "
                             "`lcsas stage --media`).")
    burn_p.add_argument("--repo", type=str, default=None, nargs="*",
                        help="Accepted but ignored: a burn session burns every "
                             "volume it was staged with (choose repositories "
                             "with `lcsas stage --repo`).")
    burn_p.add_argument("--session", type=str, required=True,
                        help="Burn a previously staged session (ID or 'latest'). "
                             "Required; use `lcsas stage` first to create a session.")
    burn_p.add_argument("--location", type=str, default=None,
                        help="Location where these discs are kept. Must "
                             "already be registered (use `lcsas location "
                             "add` first or pass --create-location).")
    burn_p.add_argument("--create-location", action="store_true", default=False,
                        help="Create the --location row if it does not "
                             "already exist (otherwise unknown names are "
                             "rejected to guard against typos).")
    burn_p.add_argument("--device", type=str, default=None,
                        help="Optical device path (overrides config).")
    burn_p.add_argument("--no-prompt", action="store_true", default=False,
                        help="Do not pause between discs for the owner "
                             "(for scripted/cdemu runs). Without this, a "
                             "multi-disc burn pauses before each disc to load "
                             "a blank and after each to write the label.")
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
    stage_p.set_defaults(func=cmd_stage)
    stage_p.add_argument("--media", type=str, default=None,
                         help="Media type (BD25, MDISC100, TEST_TINY, etc.).")
    stage_p.add_argument("--for-location", type=str, default=None,
                         help="Stage only packs missing at this location.")
    stage_p.add_argument("--repo", type=str, default=None, nargs="*",
                         help="Names of the repositories to stage (default: all).")
    stage_p.add_argument("--collection", type=str, default=None,
                         metavar="NAME",
                         help="Stage the [collections.NAME] of the config: "
                              "its repos, for its location (as "
                              "--for-location), on its media_type, with "
                              "its min_fill_pct. Not with --repo, "
                              "--for-location or --media.")
    stage_p.add_argument("--min-fill", type=float, default=None,
                         metavar="PCT",
                         help="Do not stage a last volume whose packs fill "
                              "less than PCT%% of its pack space (0-100); "
                              "the full volumes before it are staged. "
                              "Overrides a collection's min_fill_pct.")
    stage_p.add_argument("--partial", action="store_true", default=False,
                         help="Stage the last volume even if it is below "
                              "the minimum fill.")
    stage_p.add_argument("--clean", action="store_true",
                         help="Clean up staged ISOs for a session.")
    stage_p.add_argument("--force", action="store_true",
                         help="With --clean: also abort a never-burned "
                              "session, deleting its volumes and returning "
                              "their packs to the unarchived pool. A "
                              "burned volume is never deleted.")
    stage_p.add_argument("--session", type=str, default=None,
                         help="Session ID (for --clean).")
    stage_p.add_argument("--dry-run", "-n", action="store_true", default=False,
                         help="Show staging plan without creating ISOs or catalog rows.")
    stage_p.add_argument("--allow-escrow-drift", action="store_true", default=False,
                         help="Proceed even if lcsas.toml's key_split/K/N "
                              "disagrees with the recorded split (KEY-08). "
                              "Logged as a volume event. For multi-config "
                              "edge cases only — discs may print share "
                              "instructions that do not match the real split.")
    stage_p.add_argument("--allow-low-redundancy", action="store_true",
                         default=False,
                         help="Stage a volume even if its RS03 error "
                              "correction would be below the 5%% floor, or "
                              "its content exceeds the medium's usable "
                              "bytes. Recorded as a volume event. NOT "
                              "recommended.")

    # --- burn-iso ---
    burniso_p = subparsers.add_parser(
        "burn-iso",
        help="Burn a single ISO file (standalone, no catalog access required).",
        description=(
            "Burn a single ISO to disc. Useful for split-machine workflows: "
            "stage on one machine, transfer ISOs to a faster burner, then "
            "import the emitted receipts back with 'catalog import-receipts'."
        ),
    )
    burniso_p.set_defaults(func=cmd_burn_iso)
    burniso_p.add_argument("iso_path", type=Path, help="Path to .iso file.")
    burniso_p.add_argument("--device", type=str, default=None,
                           help="Optical device path (overrides config; "
                                "default /dev/sr0 when neither is set).")
    burniso_p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                           help="Verify after burning: read the disc back and "
                                "compare its SHA-256 with the ISO's "
                                "(use --no-verify to skip).")
    burniso_p.add_argument("--verify-only", action="store_true", default=False,
                           help="Do not burn: check the disc already in the "
                                "drive against the ISO, as --verify does, and "
                                "write a receipt for it (a lost receipt).")
    # str, not Path: Path() drops a trailing separator, which marks a folder.
    burniso_p.add_argument("--emit-receipt", type=str, default=None,
                           help="Write a burn receipt JSON for later catalog "
                                "import.  A folder (existing, ending in a "
                                "path separator, or a name with no extension) "
                                "gets <label>_<location>.json and is created "
                                "if missing; any other path is the file.")
    burniso_p.add_argument("--label", type=str, default=None,
                           help="Volume label for the receipt. Defaults to the "
                                "ISO's parent directory name.")
    burniso_p.add_argument("--location", type=str, default=None,
                           help="Location tag for the receipt "
                                "(required with --emit-receipt).")
    burniso_p.add_argument("--session", type=str, default="",
                           help="Optional session ID to record in the receipt.")

    # --- staging ---
    staging_p = subparsers.add_parser("staging", help="Manage the staging area.")
    staging_p.set_defaults(func=_usage("Usage: lcsas staging {clean}"))
    staging_sub = staging_p.add_subparsers(dest="staging_command")
    staging_clean_p = staging_sub.add_parser(
        "clean", help="Remove orphaned directories from the staging area.",
    )
    staging_clean_p.set_defaults(func=cmd_staging_clean)
    staging_clean_p.add_argument(
        "--force", action="store_true", default=False,
        help="Skip confirmation prompt.",
    )

    # --- location ---
    loc_p = subparsers.add_parser("location", help="Manage the locations where discs are kept.")
    loc_p.set_defaults(func=cmd_location)
    loc_sub = loc_p.add_subparsers(dest="location_command")

    loc_sub.add_parser("list", help="List all locations and their status.")

    loc_add_p = loc_sub.add_parser("add", help="Register a new location.")
    loc_add_p.add_argument("name", help="Location name (e.g. Offsite_Safe).")
    loc_add_p.add_argument("--description", type=str, default="",
                           help="Optional description.")

    loc_status_p = loc_sub.add_parser("status",
                                      help="Show packs present/missing at a location.")
    loc_status_p.add_argument("name", help="Location name.")

    loc_rename_p = loc_sub.add_parser(
        "rename", help="Rename a location; its discs and history follow.")
    loc_rename_p.add_argument("old_name", help="Current location name.")
    loc_rename_p.add_argument("new_name", help="New location name.")

    loc_remove_p = loc_sub.add_parser(
        "remove",
        help="Remove a location that no disc record or history names.")
    loc_remove_p.add_argument("name", help="Location name.")

    loc_move_p = loc_sub.add_parser("move",
                                    help="Record a disc moving between locations.")
    loc_move_p.add_argument("volume_label",
                            help="Volume label (e.g. LCSAS_BD25_2026_0001).")
    loc_move_p.add_argument("--from", dest="from_location", required=True,
                            help="Source location.")
    loc_move_p.add_argument("--to", dest="to_location", required=True,
                            help="Destination location.")

    # --- copy ---
    copy_p = subparsers.add_parser(
        "copy",
        help="Record the fate of a disc (deprecate/destroy).",
    )
    copy_p.set_defaults(func=_usage("Usage: lcsas copy {deprecate,destroy}"))
    copy_sub = copy_p.add_subparsers(dest="copy_command")

    copy_dep_p = copy_sub.add_parser(
        "deprecate",
        help="Record a disc as deprecated (e.g. damaged, or its volume deprecated).",
    )
    copy_dep_p.set_defaults(func=cmd_copy)
    copy_dep_p.add_argument("volume_label",
                            help="Volume label (e.g. LCSAS_BD25_2026_0001).")
    copy_dep_p.add_argument("location",
                            help="Location holding the disc (e.g. Home_Shelf).")

    copy_des_p = copy_sub.add_parser(
        "destroy",
        help="Record a disc as destroyed, or with --lost as lost.",
    )
    copy_des_p.set_defaults(func=cmd_copy)
    copy_des_p.add_argument("volume_label",
                            help="Volume label (e.g. LCSAS_BD25_2026_0001).")
    copy_des_p.add_argument("location",
                            help="Location that held the disc (e.g. Home_Shelf).")
    copy_des_p.add_argument(
        "--lost", action="store_true", default=False,
        help="The disc is lost (whereabouts unknown), not destroyed by you. "
             "Its status is DESTROYED either way; the volume's events "
             "record it as lost.",
    )

    # --- volume ---
    volume_p = subparsers.add_parser(
        "volume", help="Per-volume catalog queries.",
    )
    volume_p.set_defaults(func=_usage("Usage: lcsas volume {impact}"))
    volume_sub = volume_p.add_subparsers(dest="volume_command")

    vol_impact_p = volume_sub.add_parser(
        "impact",
        help="Blast radius: what becomes unrestorable if every disc of "
             "this volume fails? (FMA-08)",
    )
    vol_impact_p.set_defaults(func=cmd_volume_impact)
    vol_impact_p.add_argument("volume_label",
                              help="Volume label (e.g. LCSAS_BD25_2026_0001).")
    vol_impact_p.add_argument(
        "--snapshots", action="store_true", default=False,
        help="Also list snapshots referencing at-risk packs (needs the "
             "live mirror, the rustic binary, and --config with the "
             "repository's password_file).",
    )

    # --- catalog ---
    cat_p = subparsers.add_parser("catalog", help="Catalog management.")
    cat_p.set_defaults(func=_usage(
        "Usage: lcsas catalog {import-receipts|validate|reconcile|rebuild}"
    ))
    cat_sub = cat_p.add_subparsers(dest="catalog_command")
    cat_import_p = cat_sub.add_parser("import-receipts",
                                      help="Import burn receipts from remote burns.")
    cat_import_p.set_defaults(func=cmd_catalog_import)
    cat_import_p.add_argument("receipt_files", nargs="+",
                              help="Receipt JSON files.")
    cat_import_p.add_argument(
        "--allow-unhashed", action="store_true", default=False,
        help="Let a passing receipt make its volume VERIFIED even when the "
             "receipt's ISO hash cannot be matched to the staged ISO (one "
             "of the two hashes is missing). Check the disc first.",
    )
    cat_import_p.add_argument(
        "--allow-incomplete-staging", action="store_true", default=False,
        help="Import a receipt for a volume whose staging did not finish "
             "(no ISO hash recorded; its ISO may lack RS03 error "
             "correction). Check the disc first (#665).",
    )
    cat_import_p.add_argument(
        "--allow-other-location", action="store_true", default=False,
        help="Accept a first disc at a location other than the one its "
             "volume was staged for.",
    )

    cat_validate_p = cat_sub.add_parser(
        "validate",
        help="Cross-check a mounted disc's data files against its disc catalog.",
    )
    cat_validate_p.set_defaults(func=cmd_catalog_validate)
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
             "against the pack ID in its filename (detects bit-rot; reads the full "
             "data payload of the disc).",
    )

    cat_reconcile_p = cat_sub.add_parser(
        "reconcile",
        help="Report (and optionally fix) catalog/physical disagreements: "
             "ghost volumes that were never burned, and durable volumes "
             "with no ACTIVE disc recorded.",
    )
    cat_reconcile_p.set_defaults(func=cmd_catalog_reconcile)
    cat_reconcile_p.add_argument(
        "--fix", action="store_true", default=False,
        help="Delete ghost volumes (never-burned STAGING/BURNING volumes "
             "with zero copies whose staged ISO is gone), returning their "
             "packs to the unarchived pool. A volume whose events show a "
             "disc of it was burned is never deleted. Asks for "
             "confirmation unless --yes is given.",
    )
    cat_reconcile_p.add_argument(
        "--include-waiting", action="store_true", default=False,
        help="With --fix: also delete volumes whose staged ISO still exists "
             "(waiting to be burned, e.g. on another machine). A receipt "
             "for a deleted volume can no longer be imported.",
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
        help="Rebuild the master catalog by merging disc catalogs.",
    )
    cat_rebuild_p.set_defaults(func=cmd_catalog_rebuild)
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
    restore_p.set_defaults(func=_usage("Usage: lcsas restore {plan,exec,standalone}"))
    restore_sub = restore_p.add_subparsers(dest="restore_command")

    plan_p = restore_sub.add_parser("plan", help="Generate a restore pick list.")
    plan_p.set_defaults(func=cmd_restore_plan)
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
    exec_p.set_defaults(func=cmd_restore_exec)
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
            "Falls back to the tier-3 Python reader if rustic is not available."
        ),
    )
    fromdisc_p.set_defaults(func=cmd_restore_from_disc)
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
        help="Path to the repository password file.",
    )
    fromdisc_p.add_argument(
        "--repo", type=str, default=None,
        help="Repository name to restore (auto-selected if only one repository is on the disc).",
    )
    fromdisc_p.add_argument(
        "--snapshot", type=str, default="latest",
        help="Snapshot ID to restore (default: latest).",
    )
    fromdisc_p.add_argument(
        "--volume-dir", type=Path, default=None,
        help="Directory containing all pre-extracted volumes (no disc prompts).",
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
    cons_p.set_defaults(func=cmd_consolidate)
    cons_p.add_argument("volume_ids", nargs="+", metavar="VOLUME",
                        type=_volume_ref,
                        help="Volume labels (or catalog volume IDs) to "
                             "consolidate.")
    cons_p.add_argument("--target-media", type=str, default="MDISC100",
                        help="Target media type for consolidated volume.")
    cons_p.add_argument("--execute", action="store_true", default=False,
                        help="Stage active packs (without deprecating source volumes).")
    cons_p.add_argument("--deprecate", action="store_true", default=False,
                        help="Mark source volumes as DEPRECATED (run after --execute succeeds).")
    cons_p.add_argument("--location", default=None,
                        help="With --execute: the location the new volumes "
                             "are staged for (default: the configured "
                             "default location). A location given here "
                             "must already exist.")

    # --- verify ---
    verify_p = subparsers.add_parser("verify", help="Verify a volume's ISO or disc.")
    verify_p.set_defaults(func=cmd_verify)
    verify_p.add_argument("volume_label", nargs="?", default=None,
                          help="Label of the volume to verify (omit with --all).")
    verify_p.add_argument("--iso", type=Path, default=None,
                          help="Path to the ISO file (auto-detected from session if omitted).")
    verify_p.add_argument("--disc", action="store_true", default=False,
                          help="Verify a burned disc instead of an ISO file.")
    verify_p.add_argument("--device", default=None,
                          help="Optical drive device (overrides config; "
                               "default /dev/sr0 when neither is set).")
    verify_p.add_argument("--mark-verified", action="store_true", default=False,
                          help="Manually mark the volume as verified (remote "
                               "workflow). Needs --location when the volume "
                               "has no disc recorded.")
    verify_p.add_argument("--mark-failed", action="store_true", default=False,
                          help="Record a verification failure without checking media.")
    verify_p.add_argument("--detail", default="",
                          help="Detail text for --mark-verified/--mark-failed event.")
    verify_p.add_argument("--all", action="store_true", default=False, dest="verify_all",
                          help="Verify all BURNED/VERIFIED volumes in one run. "
                               "Combine with --disc to re-verify each disc "
                               "one at a time, stamping last_verified_at.")
    verify_p.add_argument("--location", default=None,
                          help="Filter --all to discs/volumes at this location; "
                               "with single-volume --disc, stamp the disc at "
                               "this location (defaults to the volume's only "
                               "ACTIVE disc when unambiguous); with --disc, "
                               "record the disc there when none of the volume "
                               "is recorded at it; with --mark-verified on a "
                               "volume with no disc recorded (required there), "
                               "record its disc at this location.")

    # --- session ---
    session_p = subparsers.add_parser("session", help="Manage burn sessions.")
    session_p.set_defaults(func=_usage("Usage: lcsas session {list,abort}"))
    session_sub = session_p.add_subparsers(dest="session_command")
    sess_list_p = session_sub.add_parser("list", help="List all burn sessions.")
    sess_list_p.set_defaults(func=cmd_session_list)
    sess_list_p.add_argument(
        "--status", type=str, default=None,
        help="Filter by status (STAGED, PARTIAL, COMPLETE, CLEANED). "
             "An aborted burn session is recorded as CLEANED.",
    )
    sess_abort_p = session_sub.add_parser(
        "abort",
        help="Abort a never-burned session: delete its volumes and return "
             "their packs to the unarchived pool. Refused when a volume of "
             "it was burned but its disc is not recorded yet.",
    )
    sess_abort_p.set_defaults(func=cmd_session_abort)
    sess_abort_p.add_argument(
        "ref", nargs="?", default="latest",
        help="Session ID or 'latest' (default).",
    )
    sess_abort_p.add_argument(
        "--volume", type=str, default=None,
        help="Abort a single stranded STAGING volume by label instead of "
             "a whole session (for volumes with no session).",
    )
    sess_abort_p.add_argument(
        "--never-burned", action="store_true", default=False,
        help="Confirm that no disc was ever burned of the volumes a catalog "
             "rebuild left with no record of whether they were burned, so "
             "they are deleted too. Never deletes a volume that was "
             "burned but has no disc recorded.",
    )

    # --- config ---
    config_p = subparsers.add_parser("config", help="Configuration management.")
    config_p.set_defaults(func=_usage("Usage: lcsas config {check}"))
    config_sub = config_p.add_subparsers(dest="config_command")
    config_check = config_sub.add_parser("check", help="Validate TOML config file.")
    config_check.set_defaults(func=cmd_config_check)

    # --- meta ---
    meta_p = subparsers.add_parser(
        "meta",
        help="Build a self-contained meta disc (recovery tools + source).",
    )
    meta_p.set_defaults(func=_usage("Usage: lcsas meta {build,verify}"))
    meta_sub = meta_p.add_subparsers(dest="meta_command")

    meta_build = meta_sub.add_parser(
        "build",
        help="Build a meta disc directory with all the recovery tools.",
    )
    meta_build.set_defaults(func=cmd_meta_build)
    meta_build.add_argument(
        "--output", "-o", type=Path, required=True,
        help="Output directory for the meta disc.",
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
            "tier-3 restore of compressed repositories (slower); without this flag "
            "the build fails loud so the missing fast path is not a "
            "surprise."
        ),
    )
    meta_build.add_argument(
        "--allow-no-dvdisaster-source", action="store_true",
        help=(
            "Build even if the pinned dvdisaster RS03 source tarball is "
            "absent from the recovery cache. Without this flag the build "
            "fails loud, because docs/DVDISASTER_RS03_FORMAT.md is only "
            "re-implementable against the source it transcribes — that "
            "source must travel on the meta disc. Run "
            "`sh recovery/scripts/fetch_upstream.sh` to populate the cache."
        ),
    )
    meta_build.add_argument(
        "--allow-incomplete", action="store_true",
        help=(
            "Permit a meta disc missing per-target binaries or root "
            "restore artifacts (single-arch developer builds). By default "
            "the build fails loud and lists every missing artifact so an "
            "incomplete meta disc never ships unnoticed (RST-05)."
        ),
    )
    meta_build.add_argument(
        "--allow-missing-metadata", action="store_true",
        help=(
            "Permit a meta disc that carries no Rustic keys for a live "
            "repository whose packs are on discs. By default that fails the "
            "build (#437) — the disc could not decrypt that repository. Deliberately "
            "separate from --allow-incomplete, which means 'dev build lacking "
            "per-target binaries': a habitual flag must not silence a "
            "survivability gap. The acknowledgement is recorded on the meta disc "
            "in volume_info.json."
        ),
    )

    meta_verify = meta_sub.add_parser(
        "verify",
        help="Audit a built meta disc against its recovery/MANIFEST.sha256.",
        description=(
            "Reads every file listed in <output>/recovery/MANIFEST.sha256 "
            "and confirms each one's SHA-256 matches.  Reports missing "
            "files, mismatched hashes, and (when --strict) any files "
            "present under recovery/ that aren't listed in the manifest. "
            "Exits 0 when the meta disc is intact, 1 on any issue."
        ),
    )
    meta_verify.set_defaults(func=cmd_meta_verify)
    meta_verify.add_argument(
        "output", type=Path,
        help="The meta disc directory built by `lcsas meta build`.",
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
    recovery_p.set_defaults(func=cmd_recovery)
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
            "x86_64", "aarch64", "armv7",
            # Windows-gnu target via `zig cc` (Phase 21.10.b).
            "x86_64-windows",
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

    # --- key (Shamir / SLIP-0039 password split) ---
    key_p = subparsers.add_parser(
        "key",
        help="Split a repository password into SLIP-0039 shares, or combine them.",
    )
    key_p.set_defaults(func=_usage("Usage: lcsas key {split,unsplit,combine,verify,card}"))
    key_sub = key_p.add_subparsers(dest="key_command")

    key_split = key_sub.add_parser(
        "split",
        help="Split a repository password into K-of-N shares.",
    )
    key_split.set_defaults(func=cmd_key_split)
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
        help="Read the password from this file instead of the repository's "
             "configured password_file.",
    )
    key_split.add_argument(
        "--out", type=Path, default=None,
        help="Output directory for share files (default: ./keyshares-<repo>/).",
    )
    key_split.add_argument(
        "--no-verify-repo", action="store_true",
        help="Skip checking that the password actually unlocks the repository's "
             "key files. Splitting an unchecked password is dangerous; use "
             "only when the mirror's keys/ is unavailable at split time.",
    )

    key_verify = key_sub.add_parser(
        "verify",
        help="Check that shares/a password actually unlock a repository "
             "(annual password drill).",
    )
    key_verify.set_defaults(func=cmd_key_verify)
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
        help="Reconstruct a repository password from K share mnemonics.",
    )
    key_combine.set_defaults(func=cmd_key_combine)
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

    key_unsplit = key_sub.add_parser(
        "unsplit",
        help="Clear the catalog's record of a repository's password split "
             "(after the split was abandoned).",
        description=(
            "Remove the recorded K-of-N split for one repository from the "
            "catalog, so burned discs stop telling the heir to gather share "
            "cards. It does not touch any share card or the password. Use it "
            "when the split was abandoned and key_split is false for the "
            "repository."
        ),
    )
    key_unsplit.set_defaults(func=cmd_key_unsplit)
    key_unsplit.add_argument(
        "--repo", required=True,
        help="Repository whose recorded split to clear.",
    )
    key_unsplit.add_argument(
        "--yes", action="store_true",
        help="Do not ask for confirmation.",
    )

    key_card = key_sub.add_parser(
        "card",
        help="Print a paper password card for the DEFAULT (unsplit) "
             "archive — for owners who do NOT split the password.",
    )
    key_card.set_defaults(func=cmd_key_card)
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
        help="Check mode: recompute the check code from a typed-in password "
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
        help="Estate-planning artifacts (the whole-archive estate card).",
    )
    estate_p.set_defaults(func=_usage("Usage: lcsas estate card"))
    estate_sub = estate_p.add_subparsers(dest="estate_command")
    estate_card = estate_sub.add_parser(
        "card",
        help="Generate the one-page estate card for the whole archive: owner, "
             "repositories, disc inventory, whether the password is split, and the literal "
             "first command an heir runs.",
    )
    estate_card.set_defaults(func=cmd_estate_card)
    estate_card.add_argument(
        "--output", "--out", dest="output", type=Path, default=None,
        help="Write the card here (default: stdout).",
    )

    return parser
