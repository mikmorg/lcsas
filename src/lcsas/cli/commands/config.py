"""``lcsas config check``."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from typing import TYPE_CHECKING

from lcsas.cli.commands.stage import _collection_catalog_check
from lcsas.cli.common import _load_config_opt, _log_no_config, _open_catalog, _resolve_db_path
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.config.settings import LCSASConfig

logger = get_logger()


def cmd_config_check(args: argparse.Namespace) -> int:
    """Validate a TOML configuration file."""
    import tomllib

    from lcsas.config.settings import unknown_toml_keys, validate_config

    config = _load_config_opt(args)
    if config is None:
        _log_no_config("config check")
        return 1
    errors = validate_config(config)
    # Every other command only warns about an unknown key, so a typo never
    # blocks it; the check is where a typo must not read as "valid" (#515).
    # load_config has already printed each one as a warning.
    with open(args.config, "rb") as f:
        raw = tomllib.load(f)
        if unknown_toml_keys(raw):
            errors.append(
                "unknown sections or keys (listed above): fix the typo or "
                "remove them"
            )
    if config.collections:
        errors.extend(_config_check_collections(args, config))
    defaults = raw.get("defaults")
    errors.extend(_config_check_default_location(
        args, config,
        explicit=isinstance(defaults, dict) and "location" in defaults,
    ))
    _warn_repo_names(args, config)
    # #960: the hint goes on every disc; meet the problem here, not at
    # the first stage.
    from lcsas.config.settings import password_hint_problem
    hint_problem = password_hint_problem(config)
    if hint_problem is not None:
        errors.append(hint_problem)
    for key, value in (("archive_owner", config.archive_owner),
                       ("technical_contact", config.technical_contact)):
        if not value.strip():
            logger.warning(
                "[survivability] %s is not set: the discs and the estate "
                "card leave it out.", key,
            )

    if not errors:
        if not config.repositories:
            logger.warning(
                "No repositories configured yet: add a [repos.<name>] "
                "section with a mirror_path."
            )
        logger.info("Configuration is valid.")
        return 0

    for err in errors:
        logger.error(f"  {err}")
    return 1


def _config_check_default_location(
    args: argparse.Namespace, config: LCSASConfig, *, explicit: bool,
) -> list[str]:
    """``config check`` error for a ``[defaults] location`` the catalog
    does not know (#968): after ``location rename``, plain ``stage`` still
    stages for the old name and ``burn`` refuses it.  A default the config
    does not set (an install that stages by collection only) gets a
    warning instead.  Not checked when there is no catalog yet; the
    catalog is never created here."""
    from lcsas.db.locations import list_locations
    from lcsas.exceptions import CatalogError

    db_path = _resolve_db_path(args, config)
    try:
        with _open_catalog(args, mode="read", db_path=db_path) as conn:
            known = [loc.name for loc in list_locations(conn)]
    except CatalogError:
        return []
    name = config.default_location
    if name in known:
        return []
    registered = ", ".join(known) if known else "none"
    if not explicit:
        logger.warning(
            "The config sets no [defaults] location, so plain 'lcsas "
            "stage' and 'lcsas burn' use %s, which is not a registered "
            "location (registered: %s).", name, registered,
        )
        return []
    return [
        f"[defaults] location '{name}' is not a registered location "
        f"(registered: {registered}). If it was renamed, change the "
        f"config to the new name; otherwise register it with 'lcsas "
        f"location add {name}'."
    ]


def _warn_repo_names(args: argparse.Namespace, config: LCSASConfig) -> None:
    """Warn for a ``[repos.X]`` table whose mirror the catalog registered
    under another name (#968).

    New discs name a backup set by its config key, while the catalog and
    the discs already burned keep the name it was registered under: a
    table renamed in the config only splits one set into two names.  A
    warning, not an error: a key that differs from the catalog name is
    resolved by its mirror path on purpose (#687).  Nothing to check
    without a catalog.
    """
    from lcsas.db.repos import list_repos, same_mirror_path
    from lcsas.exceptions import CatalogError

    if not config.repositories:
        return
    db_path = _resolve_db_path(args, config)
    if not db_path.exists():
        return
    try:
        with _open_catalog(args, mode="read", db_path=db_path) as conn:
            registered = list_repos(conn)
    except CatalogError:
        return  # no catalog yet, or one the collections check reports
    for key, repo_cfg in config.repositories.items():
        for repo in registered:
            if (repo.name != key and repo.mirror_path
                    and same_mirror_path(repo.mirror_path, repo_cfg.mirror_path)):
                logger.warning(
                    f"[repos.{key}]: its mirror {repo_cfg.mirror_path} is "
                    f"registered in the catalog as '{repo.name}'. New discs "
                    f"name the backup set '{key}' while the catalog and the "
                    f"discs already burned name it '{repo.name}'. If the "
                    f"table was renamed, rename it back to "
                    f"[repos.{repo.name}]."
                )


def _config_check_collections(
    args: argparse.Namespace, config: LCSASConfig,
) -> list[str]:
    """``config check`` errors for the ``[collections.*]`` tables (#701).

    The loader has already checked that each collection names only
    configured repositories.  This adds the password files and, when the
    catalog exists, that each location and repository is registered.
    The catalog is never created here.
    """
    from lcsas.config.settings import collection_password_errors
    from lcsas.exceptions import CatalogError, CatalogUnreadableError

    errors = collection_password_errors(config)
    db_path = _resolve_db_path(args, config)
    with ExitStack() as stack:
        try:
            conn = stack.enter_context(
                _open_catalog(args, mode="read", db_path=db_path)
            )
        except CatalogUnreadableError as e:  # #968: there, but damaged
            return [*errors, str(e)]
        except CatalogError:
            logger.warning(
                "No catalog at %s yet: the collections' locations and "
                "repositories are not checked against it. Run this check "
                "again after 'lcsas init', 'lcsas repo add' and 'lcsas "
                "location add'.", db_path,
            )
            return errors
        for coll in config.collections.values():
            errors.extend(_collection_catalog_check(conn, config, coll)[1])
    return errors
