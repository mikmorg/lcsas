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

        # Auto-detect the key ID from the repository's keys/ dir
        key_ids = read_repo_key_ids(mirror)
        encryption_key_id = key_ids[0] if key_ids else ""

        # #962: a retired repository is not registered again: its id is on
        # its discs, and a new id would burn every pack a second time.
        # encryption_key_id holds the first key file name `repo add` saw in
        # the mirror's keys/, so a mirror holding that key is that
        # repository, wherever it now lives and whatever it is called (its
        # recorded mirror path stands in when no key was recorded).
        from lcsas.db.repos import REPO_STATUS_RETIRED, list_repos, same_mirror_path
        existing_repos = list_repos(conn)
        for existing in existing_repos:
            if existing.status != REPO_STATUS_RETIRED:
                continue
            if existing.encryption_key_id:
                same_repo = existing.encryption_key_id in key_ids
            else:
                same_repo = same_mirror_path(existing.mirror_path, mirror)
            if same_repo:
                logger.error(
                    f"The mirror {mirror} is the retired repository "
                    f"'{existing.name}' (id: {existing.repo_id}, mirror: "
                    f"{existing.mirror_path}). Its packs are on discs under "
                    f"that id, so it is not registered again under a new "
                    f"one. Nothing was registered. To back it up again, run "
                    f"`lcsas repo activate {existing.name}`; if its mirror "
                    f"has moved, put it back at {existing.mirror_path} "
                    f"first."
                )
                return 1

        # #685: names and mirror paths must be unique.  The schema has no
        # UNIQUE index on either (v10 is burned on discs), so check here:
        # a second 'photos' made every later `--repo photos` ambiguous.
        for existing in existing_repos:
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


def _burned_volume_labels(conn: Any, repo_id: str) -> list[str]:
    """Labels of the burned volumes holding a pack of *repo_id* (#962).

    Pruned packs count: a pack pruned from the mirror is still on its
    discs.  A volume counts as burned once it has left STAGING (a disc may
    exist from BURNING on, and DEPRECATED, CONSOLIDATING and DESTROYED
    volumes were all burned) or once any disc of it is recorded.
    """
    rows = conn.execute(
        """SELECT DISTINCT v.label FROM volumes v
           JOIN volume_packs vp ON vp.volume_id = v.volume_id
           JOIN packs p ON p.pack_id = vp.pack_id
           WHERE p.repo_id = ?
             AND (v.status != 'STAGING'
                  OR EXISTS (SELECT 1 FROM volume_copies c
                             WHERE c.volume_id = v.volume_id))
           ORDER BY v.label""",
        (repo_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def cmd_repo_remove(args: argparse.Namespace) -> int:
    """Delete a repository from the catalog.

    Refused, with or without --force, once any of its packs is on a burned
    volume (#962): its id is on those discs, and a repository added again
    gets a new one, so every pack would be burned a second time under a
    second identity.  `repo retire` is the verb for a repository whose
    mirror is gone for good.  --force is for a repository whose packs are
    on no burned volume, such as one added by mistake.
    """
    from lcsas.db.packs import list_packs
    from lcsas.db.repos import delete_repo
    from lcsas.db.snapshots import delete_snapshots_for_repo

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

        burned = _burned_volume_labels(conn, repo.repo_id)
        if burned:
            shown = ", ".join(burned[:5]) + (", ..." if len(burned) > 5 else "")
            logger.error(
                f"Repository '{repo.name}' has packs on {len(burned)} burned "
                f"volume(s) ({shown}). Its catalog ID {repo.repo_id} is on "
                f"those discs, so it cannot be deleted from the catalog: "
                f"added again, it would get a new ID and every pack would be "
                f"burned a second time. Nothing was deleted. If its mirror "
                f"is gone for good, run `lcsas repo retire {repo.name}`, "
                f"which keeps its packs and snapshots."
            )
            return 1

        all_packs = list_packs(conn, repo_id=repo.repo_id, include_pruned=True)
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE repo_id = ?",
            (repo.repo_id,),
        ).fetchone()[0]

        if all_packs and not args.force:
            logger.error(
                f"Repository '{repo.name}' has {len(all_packs)} pack(s) in "
                f"the catalog, none on a burned volume. Use --force to "
                f"delete them, its snapshots and the repository from the "
                f"catalog."
            )
            return 1

        if args.force:
            logger.warning("")
            logger.warning(
                "This deletes from the catalog: repository '%s', %d pack(s) "
                "and %d snapshot(s).",
                repo.name, len(all_packs), snap_count,
            )
            try:
                prompt = "Type 'yes' to confirm deletion, or anything else to cancel: "
                if not _confirm(prompt):
                    logger.info("Removal canceled.")
                    return 0
            except EOFError:
                logger.error("No terminal available for confirmation (redirected input).")
                return 1

        # Delete packs, snapshots, then the repository itself.  Packs must
        # be removed before the repository to satisfy FK constraints.
        staged: list[tuple[int, str]] = []
        if all_packs:
            # Only staged (never-burned) volumes can hold them here; each
            # one's used_bytes is recomputed once its links are gone.
            staged = [(int(r[0]), str(r[1])) for r in conn.execute(
                "SELECT DISTINCT v.volume_id, v.label FROM volumes v "
                "JOIN volume_packs vp ON vp.volume_id = v.volume_id "
                "JOIN packs p ON p.pack_id = vp.pack_id WHERE p.repo_id = ? "
                "ORDER BY v.label",
                (repo.repo_id,),
            ).fetchall()]
            conn.execute(
                "DELETE FROM volume_packs WHERE pack_id IN "
                "(SELECT pack_id FROM packs WHERE repo_id = ?)",
                (repo.repo_id,),
            )
            conn.execute(
                "DELETE FROM packs WHERE repo_id = ?", (repo.repo_id,)
            )
            from lcsas.db.volumes import update_used_bytes
            for volume_id, _label in staged:
                used = conn.execute(
                    "SELECT COALESCE(SUM(p.size_bytes), 0) FROM volume_packs vp "
                    "JOIN packs p ON p.pack_id = vp.pack_id "
                    "WHERE vp.volume_id = ?",
                    (volume_id,),
                ).fetchone()[0]
                update_used_bytes(conn, volume_id, int(used), commit=False)
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
        f"Deleted repository '{repo.name}' from the catalog, with "
        f"{len(all_packs)} pack(s) and {snap_count} snapshot(s)."
    )
    if staged:
        logger.warning(
            "The ISO images of staged volume(s) %s still hold its packs. "
            "Run `lcsas session abort <session>` for their burn session and "
            "`lcsas stage` again before burning, or the discs will carry "
            "packs the catalog no longer records.",
            ", ".join(label for _vid, label in staged),
        )
    return 0
