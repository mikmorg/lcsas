"""``lcsas repo``: add, list, retire, activate, remove."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lcsas.cli.common import (
    _config_if_loadable,
    _config_mirror,
    _confirm,
    _load_config_opt,
    _open_catalog,
    _resolve_db_path,
)
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.config.settings import LCSASConfig

logger = get_logger()


def cmd_repo_add(args: argparse.Namespace) -> int:
    """Register a new repository."""
    from lcsas.db.repos import register_repo
    from lcsas.utils.fs import read_repo_key_ids
    from lcsas.utils.labels import generate_uuid

    with _open_catalog(args) as conn:
        repo_id = generate_uuid()
        mirror = args.mirror_path.resolve()

        # #685: names and mirror paths must be unique.  The schema has no
        # UNIQUE index on either (v10 is burned on discs), so check here:
        # a second 'photos' made every later `--repo photos` ambiguous.
        from lcsas.db.repos import list_repos, same_mirror_path
        for existing in list_repos(conn):
            if existing.name == args.name:
                clash = f"name '{args.name}'"
            elif same_mirror_path(existing.mirror_path, mirror):
                clash = f"mirror path {mirror}"
            else:
                continue
            logger.error(
                f"A repository with the {clash} is already registered: "
                f"'{existing.name}' (id: {existing.repo_id}, mirror: "
                f"{existing.mirror_path}). Nothing was registered. Use "
                f"another name, or `lcsas repo list` to see the existing "
                f"repositories."
            )
            return 1

        # Auto-detect the key ID from the repository's keys/ dir
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
    from lcsas.db.repos import list_repos

    with _open_catalog(args, mode="read") as conn:
        repos = list_repos(conn)

    if not repos:
        logger.info("No repositories registered.")
        return 0

    config = _config_if_loadable(args)
    for repo in repos:
        logger.info(
            f"  {repo.name:<20} {repo.status:<8} {repo.repo_id}  "
            f"{repo.mirror_path}"
        )
        # #701: the folders lcsas.toml says are backed up into it.
        sources = _config_sources(config, repo)
        if sources:
            logger.info(f"    sources: {', '.join(sources)}")
    return 0


def _config_sources(config: LCSASConfig | None, repo: Any) -> tuple[str, ...]:
    """The ``sources`` of the ``[repos.<name>]`` table for catalog *repo*.

    Matched by mirror path first, then by name, as resolve_repo does.
    """
    if config is None:
        return ()
    from lcsas.db.repos import same_mirror_path

    for rc in config.repositories.values():
        if same_mirror_path(rc.mirror_path, repo.mirror_path):
            return rc.sources
    named = config.repositories.get(repo.name)
    return named.sources if named is not None else ()


def _cmd_repo_set_status(
    args: argparse.Namespace, status: str
) -> tuple[int, str | None]:
    """Shared implementation of `repo retire` / `repo activate` (#437).

    No confirmation prompt: unlike `repo remove --force`, which destroys
    pack and snapshot history, this only flips a flag and is reversible
    with the opposite verb.

    Returns ``(exit_code, repo_name)``; *repo_name* is None when the
    repository could not be resolved, so callers can base follow-up advice
    on its config name without re-opening the catalog.
    """
    from lcsas.db.repos import set_repo_status

    db_path = _resolve_db_path(args)
    # _open_catalog's write mode (not mode="read"): a
    # v9 catalog has no `status` column yet, so it must be migrated before
    # the UPDATE — otherwise this would fail on exactly the catalogs that
    # most need retiring.
    with _open_catalog(args, db_path=db_path) as conn:
        try:
            repo = _find_repo(
                conn, args.repo_id,
                _config_mirror(_config_if_loadable(args), args.repo_id),
            )
        except ValueError as exc:
            logger.error(str(exc))
            return 1, None
        if repo is None:
            logger.error(f"Repository '{args.repo_id}' not found.")
            return 1, None
        if repo.status == status:
            logger.info(
                f"Repository '{repo.name}' ({repo.repo_id}) is already "
                f"{status}."
            )
            return 0, repo.name
        set_repo_status(conn, repo.repo_id, status)

    logger.info(f"Repository '{repo.name}' ({repo.repo_id}) is now {status}.")
    return 0, repo.name


def _warn_if_still_in_config(args: argparse.Namespace, repo_name: str) -> None:
    """Warn when a just-retired repository is still a live ``[repos.*]`` block.

    Retirement is a CATALOG fact; mirror-existence validation is a CONFIG
    one (``validate_config`` in config/settings.py makes a missing
    ``mirror_path`` a hard error, and scan/burn/verify/status gate on it
    via ``_validate_config_or_exit``).  So an owner who follows the
    meta-build key gate's advice — "`lcsas repo retire <repo>` if a mirror
    is gone for good" — gets a clean `meta build` and then walks into a
    hard config error on their next `scan`, because the dead mirror is
    still listed in lcsas.toml.  Close that loop here, where the owner
    actually is, rather than leaving it to be discovered mid-restore.

    Advisory only: a config that is absent or unparseable must never turn
    a successful retirement into a failure — the catalog write already
    happened.
    """
    try:
        config = _load_config_opt(args)
    except Exception:  # noqa: BLE001 - advisory path, never fatal
        return
    if config is None or repo_name not in getattr(config, "repositories", {}):
        return
    logger.warning(
        "Repository '%s' is still listed as [repos.%s] in your config. "
        "Retirement is recorded in the catalog, not the config — until you "
        "remove that block, commands that read mirrors (scan, burn, verify, "
        "status) will still fail with 'mirror_path does not exist'.",
        repo_name, repo_name,
    )


def cmd_repo_retire(args: argparse.Namespace) -> int:
    """Mark a repository retired — its mirror is gone for good (#437)."""
    from lcsas.db.repos import REPO_STATUS_RETIRED

    rc, repo_name = _cmd_repo_set_status(args, REPO_STATUS_RETIRED)
    if rc == 0:
        logger.info(
            "Its packs and snapshots are untouched — retirement records "
            "that the mirror is gone for good, it does not delete history. "
            "Re-enable with `lcsas repo activate %s`.",
            repo_name or args.repo_id,
        )
        if repo_name:
            _warn_if_still_in_config(args, repo_name)
    return rc


def cmd_repo_activate(args: argparse.Namespace) -> int:
    """Mark a retired repository active again (#437)."""
    from lcsas.db.repos import REPO_STATUS_ACTIVE

    rc, _repo_name = _cmd_repo_set_status(args, REPO_STATUS_ACTIVE)
    return rc


def _find_repo(conn: Any, ref: str, mirror_path: Path | None = None) -> Any:
    """The repository *ref* names: its name or its catalog ID (#581).

    A thin wrapper over :func:`lcsas.db.repos.resolve_repo`, the one
    resolver (#687): the configured mirror path of ``[repos.<ref>]`` first
    when given, then the name, the catalog ID, and the name ignoring case.
    Returns None when nothing matches; raises ValueError when a name is
    ambiguous.
    """
    from lcsas.db.repos import resolve_repo

    return resolve_repo(conn, ref, mirror_path)


def cmd_repo_remove(args: argparse.Namespace) -> int:
    """Remove a repository from the catalog."""
    from lcsas.db.packs import bulk_mark_pruned, list_packs
    from lcsas.db.repos import delete_repo
    from lcsas.db.snapshots import delete_snapshots_for_repo
    from lcsas.db.volume_packs import get_volume_ids_for_pack

    with _open_catalog(args) as conn:
        try:
            repo = _find_repo(
                conn, args.repo_id,
                _config_mirror(_config_if_loadable(args), args.repo_id),
            )
        except ValueError as exc:
            logger.error(str(exc))
            return 1
        if repo is None:
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
                if not _confirm(prompt):
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

        # Delete packs (including pruned), snapshots, then the repository itself.
        # Packs must be removed before the repository to satisfy FK constraints.
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
        # #573: the repository's recorded password split goes with it, under
        # its repo_id and under its name (splits recorded before #571).
        from lcsas.db.key_escrow import clear_split
        clear_split(conn, repo.repo_id, commit=False)
        if conn.execute(
            "SELECT 1 FROM repositories WHERE repo_id = ? AND repo_id != ?",
            (repo.name, repo.repo_id),
        ).fetchone() is None:  # the name isn't another repository's id
            clear_split(conn, repo.name, commit=False)
        delete_repo(conn, repo.repo_id)

    logger.info(
        f"Removed repository '{repo.name}' "
        f"({snap_count} snapshot(s) deleted)."
    )
    return 0
