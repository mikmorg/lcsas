"""Where a repository's mirror is: the config owns the path (#958).

A repository's mirror path is in two places: the config's
``[repos.<name>] mirror_path`` and the catalog's
``repositories.mirror_path``.  The config is the one owner.  The catalog
column is a record of the last path ``lcsas scan`` saw (scan refreshes
it), read only where no config is in play, and blank in every disc copy
of the catalog, so a catalog rebuilt from discs has none.

Every command that opens a mirror with a config at hand finds it with
:func:`mirror_path`: the config's path, else the catalog's, and a worded
refusal naming both when they differ and the config's does not exist.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from lcsas.exceptions import ConfigError

if TYPE_CHECKING:
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.models import Repository


class MirrorPathError(ConfigError):
    """The config's mirror path does not exist and the catalog records
    another one: which is right is the owner's to say."""


def config_for_repo(
    config: LCSASConfig, name: str, catalog_path: str,
) -> RepositoryConfig | None:
    """The ``[repos.<key>]`` of a catalog repository, or None.

    Joined on the mirror path first (the catalog ``name`` may differ from
    the config key), then the name, then the name ignoring case.  A blank
    catalog path (a catalog rebuilt from discs) joins on the name alone:
    ``Path('')`` is the current folder, never a mirror.
    """
    from lcsas.db.repos import same_mirror_path

    if catalog_path:
        # Lexical first: no file system access while the path is the
        # same, so a stale mount cannot hang the join (#427).
        for cfg in config.repositories.values():
            if same_mirror_path(cfg.mirror_path, catalog_path):
                return cfg
        try:
            db_resolved = Path(catalog_path).resolve()
        except OSError:
            db_resolved = Path(catalog_path)
        for cfg in config.repositories.values():
            try:
                if cfg.mirror_path.resolve() == db_resolved:
                    return cfg
            except OSError:
                continue
    if name in config.repositories:
        return config.repositories[name]
    for key, cfg in config.repositories.items():
        if key.lower() == name.lower():
            return cfg
    return None


def mirror_path(config: LCSASConfig | None, repo: Repository) -> Path | None:
    """Where *repo*'s mirror is, or None when nothing says.

    The config's ``mirror_path`` when the config has the repository;
    else the path the catalog records (None when it is blank).  Raises
    :class:`MirrorPathError` when the config's path does not exist and
    the catalog records a different one.
    """
    from lcsas.db.repos import same_mirror_path

    catalog = Path(repo.mirror_path) if repo.mirror_path else None
    cfg = (config_for_repo(config, repo.name, repo.mirror_path)
           if config is not None else None)
    if cfg is None:
        return catalog
    configured = cfg.mirror_path
    if (catalog is None or same_mirror_path(configured, catalog)
            or configured.exists()):
        return configured
    raise MirrorPathError(
        f"Repository '{repo.name}': the config's [repos.{cfg.name}] "
        f"mirror_path is {configured}, which does not exist; the catalog "
        f"last saw the mirror at {catalog}. Nothing was done.",
        recovery_hint=(
            f"Set [repos.{cfg.name}] mirror_path to the folder the mirror "
            f"is in now (mount it if it is on a NAS), then run "
            f"'lcsas scan'."
        ),
    )


def mirror_paths(
    conn: sqlite3.Connection, config: LCSASConfig | None,
    repo_ids: set[str] | None = None,
) -> dict[str, Path]:
    """:func:`mirror_path` of every catalog repository (or only
    *repo_ids*), by repo_id.  A repository nothing places is left out."""
    from lcsas.db.repos import list_repos

    paths: dict[str, Path] = {}
    for repo in list_repos(conn):
        if repo_ids is not None and repo.repo_id not in repo_ids:
            continue
        path = mirror_path(config, repo)
        if path is not None:
            paths[repo.repo_id] = path
    return paths
