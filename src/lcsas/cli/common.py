"""Helpers the command handlers share: config, catalog path, catalog
connection, confirmation prompts.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.config.settings import LCSASConfig

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


# ---------------------------------------------------------------------------
# Helpers — resolving the context a command runs in (config, catalog, device)
#
# Every command resolves these through the helpers below rather than reaching
# into args or the config directly, so a handler cannot end up addressing a
# different catalog or drive than the rest of the CLI.
# ---------------------------------------------------------------------------

def _default_config_path() -> Path:
    """``$XDG_CONFIG_HOME/lcsas/lcsas.toml``, else
    ``~/.config/lcsas/lcsas.toml`` (#964).  Read at call time."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        try:
            base = Path.home() / ".config"
        except (RuntimeError, KeyError):
            # No HOME and no passwd entry (a container run as a bare
            # uid): there is no default path, only the name of one.
            base = Path("~/.config")
    return base / "lcsas" / "lcsas.toml"


def _look_up_config(args: argparse.Namespace) -> None:
    """Fill ``args.config`` when ``--config`` was not given (#964).

    The order is ``--config``, then ``$LCSAS_CONFIG``, then
    :func:`_default_config_path` when a file is there.  ``main`` calls
    this once, before the handler runs, so every handler (and every one
    that reads ``args.config`` itself) sees the same config.
    ``args.config_source`` records where it came from, for the messages:
    ``--config``, ``LCSAS_CONFIG``, ``default`` or None (no config).
    """
    if getattr(args, "config", None):
        args.config_source = "--config"
        return
    env = os.environ.get("LCSAS_CONFIG")
    if env:
        args.config = Path(env)
        args.config_source = "LCSAS_CONFIG"
        return
    default = _default_config_path()
    if default.exists():
        args.config = default
        args.config_source = "default"
        return
    args.config = None
    args.config_source = None


def _no_config_hint() -> str:
    """The three ways to say where the config is (#964)."""
    return (
        "Name the config with --config PATH or the LCSAS_CONFIG "
        f"variable, or put it at {_default_config_path()}."
    )


def _log_no_config(what: str) -> None:
    """The refusal of a command that needs a config and found none."""
    logger.error(f"A config is required for {what}, and none was found.")
    logger.error(f"Hint: {_no_config_hint()}")


def _load_config_opt(args: argparse.Namespace) -> LCSASConfig | None:
    """Load the config ``args.config`` names, else ``None``.

    ``args.config`` is ``--config``, or what :func:`_look_up_config`
    found.  Every handler that accepts an optional config funnels through
    here so config failures — a path that is not there, a file that will
    not parse, a value the loader rejects — surface as an actionable
    ConfigError instead of a bare traceback or a silent fallback to a
    different catalog.
    """
    cfg = getattr(args, "config", None)
    if not cfg:
        return None

    import tomllib

    from lcsas.config.settings import load_config
    from lcsas.exceptions import ConfigError

    cfg_path = Path(cfg)
    # Let load_config do the opening — a stat() first would only add a
    # TOCTOU gap for the same answer.
    try:
        return load_config(cfg_path)
    except FileNotFoundError as e:
        if getattr(args, "config_source", None) == "LCSAS_CONFIG":
            raise ConfigError(
                f"LCSAS_CONFIG names {cfg_path}, which does not exist.",
                recovery_hint="Correct the variable, or unset it.",
            ) from e
        raise ConfigError(
            f"--config path does not exist: {cfg_path}",
            recovery_hint="Check the path.",
        ) from e
    except tomllib.TOMLDecodeError as e:
        # MUST precede the ValueError arm below — TOMLDecodeError is a
        # ValueError subclass, so the order here is load-bearing.
        raise ConfigError(f"Malformed TOML in {cfg_path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Cannot read config {cfg_path}: {e}") from e
    except ValueError as e:
        # load_config rejects bad *values* with ValueError — today only an
        # unknown [defaults].media_type, which already names the valid set
        # in its message.  Without this arm a plain typo in the owner's
        # TOML reaches main()'s generic handler and is reported as
        # "Unexpected error:", which reads like an internal crash rather
        # than the one-word fix it is (#429).
        raise ConfigError(
            f"Invalid value in {cfg_path}: {e}",
            recovery_hint="Correct the offending key in the config file.",
        ) from e


def _resolve_db_path(
    args: argparse.Namespace,
    config: object | None = None,
) -> Path:
    """Resolve the catalog path from CLI args or config.

    Priority: ``--catalog`` flag > config ``[paths].catalog``.  With
    neither, a :class:`ConfigError` that names the ways to give one
    (#964): there is no current-folder fallback, which made a forgotten
    ``--config`` create ``./catalog.db`` wherever the owner stood.

    *config* is an optimisation, not a switch: handlers that already
    loaded it pass it in to avoid a second parse.  When it is omitted this
    still honours the config, because a handler that forgets to load one
    must not end up addressing a *different* catalog than the rest of the
    CLI.
    """
    if getattr(args, "db", None):
        return Path(args.db)
    if config is None:
        config = _load_config_opt(args)
    if config is not None and hasattr(config, "db_path"):
        return Path(config.db_path)
    from lcsas.exceptions import ConfigError

    raise ConfigError(
        "No config found, so no catalog is named.",
        recovery_hint=(
            f"{_no_config_hint()}  Or name the catalog with --catalog PATH."
        ),
    )


def _resolve_device(
    args: argparse.Namespace,
    config: object | None = None,
) -> str:
    """Resolve the optical device from CLI args, config, or default.

    Priority: ``--device`` flag > config ``optical_device`` > ``/dev/sr0``.

    Commands that resolve a device must leave ``--device`` defaulting to
    ``None`` in the parser; a hard-coded parser default is indistinguishable
    from an owner's choice here and would shadow the configured drive.
    """
    device = getattr(args, "device", None)
    if device:
        return str(device)
    if config is None:
        config = _load_config_opt(args)
    configured = getattr(config, "optical_device", None)
    if configured:
        return str(configured)
    return "/dev/sr0"


def _resolve_lock_timeout(args: argparse.Namespace) -> float | None:
    """Resolve the catalog-lock wait timeout in seconds.

    Priority: ``--lock-timeout`` flag > ``LCSAS_LOCK_TIMEOUT`` env >
    ``None`` (wait forever, the interactive default).
    """
    val: float | None = getattr(args, "lock_timeout", None)
    if val is not None:
        return float(val)
    env = os.environ.get("LCSAS_LOCK_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning(
                "Ignoring invalid LCSAS_LOCK_TIMEOUT=%r (not a number).", env
            )
    return None


def _old_catalog_beside(db_path: Path) -> Path | None:
    """An ``archive.db`` beside a missing catalog path, or None.

    The master catalog's default name was ``archive.db`` until rc11
    (#934).  An install that renamed ``[paths] catalog`` but not the file
    has one there.
    """
    old = db_path.with_name("archive.db")
    if db_path.name != "archive.db" and old.exists():
        return old
    return None


def _missing_catalog_hint(db_path: Path) -> str:
    """The hint for a catalog path that does not exist."""
    old = _old_catalog_beside(db_path)
    if old is None:
        return "Run `lcsas init` first."
    return (
        f"{old} is there: the master catalog's default name changed "
        "from archive.db to catalog.db (#934).  Rename that file to "
        f"{db_path.name}, or point [paths] catalog at it.  Do not run "
        "`lcsas init`, which starts an empty catalog."
    )


def _refuse_beside_old_catalog(db_path: Path | str) -> None:
    """Refuse to create *db_path* while an old ``archive.db`` is beside it.

    A new, empty catalog there would hide every volume of the real one.
    """
    from lcsas.exceptions import CatalogError

    db_str = str(db_path)
    if db_str == ":memory:" or Path(db_str).exists():
        return
    if _old_catalog_beside(Path(db_str)) is not None:
        raise CatalogError(
            f"No catalog at {db_path}.",
            recovery_hint=_missing_catalog_hint(Path(db_str)),
        )


def _open_existing_catalog(db_path: Path | str) -> sqlite3.Connection:
    """Open a catalog read-only-ish, failing if it does not yet exist.

    Read-only commands must NOT create the catalog as a side effect (that
    masks a missing/mistyped path and writes outside the lock).  This opens
    an existing catalog and verifies its schema without migrating it; an
    uninitialized path raises :class:`CatalogError` telling the owner to
    run ``lcsas init``.
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import get_schema_version
    from lcsas.exceptions import CatalogError

    db_str = str(db_path)
    if db_str != ":memory:" and not Path(db_str).exists():
        raise CatalogError(
            f"No catalog at {db_path}.",
            recovery_hint=_missing_catalog_hint(Path(db_str)),
        )
    conn = get_connection(db_path)
    if get_schema_version(conn) == 0:
        conn.close()
        raise CatalogError(
            f"No catalog at {db_path}.",
            recovery_hint="Run `lcsas init` first.",
        )
    _refuse_too_old_or_close(conn)
    return conn


def _note_newer_catalog(conn: sqlite3.Connection, db_path: Path | str) -> None:
    """One line when a command that does not migrate reads a catalog newer
    than this code (#968): writing commands refuse it, and what a read
    shows may leave out what the newer version records."""
    from lcsas.db.schema import CURRENT_SCHEMA_VERSION, get_schema_version

    version = get_schema_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        logger.warning(
            "Note: the catalog at %s is schema v%d, newer than this LCSAS "
            "(v%d). What this command shows may be incomplete; use a newer "
            "LCSAS.", db_path, version, CURRENT_SCHEMA_VERSION,
        )


def _refuse_too_old_or_close(conn: sqlite3.Connection) -> None:
    """Refuse a catalog older than this build migrates (#822), as
    ``ensure_schema`` does for a write.  A command that reads without
    migrating met it as "no such column" instead (#968)."""
    from lcsas.db.schema import refuse_too_old

    try:
        refuse_too_old(conn)
    except Exception:
        conn.close()
        raise


_CatalogMode = Literal[
    "create", "write", "write-existing", "read", "unlocked-migrate", "unlocked",
]


@contextmanager
def _open_catalog(
    args: argparse.Namespace,
    config: object | None = None,
    *,
    mode: _CatalogMode = "write",
    db_path: Path | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open the catalog a command addresses; close it (and the lock) on exit.

    The path is *db_path* when the handler already has it, else
    :func:`_resolve_db_path` of *args* and *config*.  Only ``create``
    makes a catalog that is not there (#941, #964): for every other mode
    a missing or uninitialised catalog is a :class:`CatalogError` ("No
    catalog at X.  Run `lcsas init` first."), so a mistyped path is an
    error and not a new, empty catalog.  *mode*:

    ``create``
        ``lcsas init`` alone.  Take the catalog lock, create the catalog
        when it is not there, and bring its schema up to date.
    ``write`` (the default), ``write-existing``
        Take the catalog lock (waiting up to ``--lock-timeout``) and bring
        the schema up to date.  The two are the same since #964.
    ``read``
        No lock.  The catalog is not migrated
        (:func:`_open_existing_catalog`).
    ``unlocked-migrate``, ``unlocked``
        No lock; migrated or not.  Only ``volume impact`` and ``session
        list`` open it this way, as they did before this helper existed.

    ``create`` refuses too while an ``archive.db`` (the catalog's default
    name before rc11, #934) sits beside the missing path: that is an
    install whose config was renamed but not its catalog file, and a new,
    empty catalog beside the real one would hide every volume
    (:func:`_refuse_beside_old_catalog`).
    """
    from lcsas.db.connection import locked_connection
    from lcsas.db.schema import ensure_schema

    if db_path is None:
        db_path = _resolve_db_path(args, config)
    _refuse_beside_old_catalog(db_path)
    if mode in ("create", "write", "write-existing"):
        if mode != "create":
            _open_existing_catalog(db_path).close()
        with locked_connection(db_path, timeout=_resolve_lock_timeout(args)) as conn:
            ensure_schema(conn)
            yield conn
        return
    # _open_existing_catalog refuses a catalog too old to read (#968).
    conn = _open_existing_catalog(db_path)
    try:
        if mode == "unlocked-migrate":
            ensure_schema(conn)
        else:
            _note_newer_catalog(conn, db_path)
        yield conn
    finally:
        conn.close()


def _confirm(prompt: str, answer: str = "yes") -> bool:
    """Ask *prompt* on the terminal: True when the reply is *answer*.

    The reply is compared stripped and in any case.  EOFError (no terminal:
    input is redirected) reaches the caller, which words that refusal and
    its exit code.
    """
    return input(prompt).strip().lower() == answer


def _resolve_repo_names_to_ids(
    conn: sqlite3.Connection,
    names: list[str] | None,
    config: LCSASConfig | None = None,
) -> list[str] | None:
    """Map owner-facing repository *names* to catalog repo_ids (UUIDs).

    Each name goes through :func:`lcsas.db.repos.resolve_repo` (#687), with
    its configured mirror path when *config* lists it.  Returns ``None``
    when *names* is ``None`` (no filter).  Logs a warning for names that
    don't map to any registered repository.  Raises ValueError when a name
    matches more than one repository (#685).
    """
    if names is None:
        return None
    from lcsas.db.repos import resolve_repo

    ids: list[str] = []
    for n in names:
        repo = resolve_repo(conn, n, _config_mirror(config, n))
        if repo is None:
            logger.warning(f"repository '{n}' not registered in the catalog, skipping.")
        else:
            ids.append(repo.repo_id)
    if not ids:
        raise ValueError(
            f"None of the specified repositories exist in the catalog: "
            f"{', '.join(names)}"
        )
    return ids


def _config_if_loadable(args: argparse.Namespace) -> LCSASConfig | None:
    """``--config`` when it loads, else None: for commands that only use it
    as a hint (the mirror path of ``[repos.<name>]``) and work with ``--catalog``
    alone, so a broken config must not stop them."""
    try:
        return _load_config_opt(args)
    except Exception:  # noqa: BLE001 - advisory only
        return None


def _config_mirror(config: LCSASConfig | None, name: str) -> Path | None:
    """The mirror path ``[repos.<name>]`` configures, or None (#687)."""
    if config is None:
        return None
    repo_cfg = config.repositories.get(name)
    return repo_cfg.mirror_path if repo_cfg is not None else None


def _restage_hint(location: str) -> str:
    """The one next step for a disc that failed verification (#671).

    It is what `lcsas stage --for-location` already does: packs of a
    volume whose disc failed verification count as missing at that
    location (``get_unarchived_or_missing_at_location``), so they are
    staged again on a new volume.
    """
    return (
        f"Stage it again for that location with 'lcsas stage "
        f"--for-location {location}', then burn the new volume."
    )


def _reburn_hint(
    conn: sqlite3.Connection, volume_id: int, location: str,
) -> str:
    """The one next step for a burn that failed verification and recorded
    no disc (#728).

    While the session's ISO is still in the staging area, burn the same
    session again: the step the burn command itself prints, and it keeps
    the volume and its label.  Once the ISO is gone, stage the packs again
    for that location (:func:`_restage_hint`).
    """
    from lcsas.db.sessions import retained_iso_session
    from lcsas.db.volume_copies import has_verified_disc_at

    session_id = retained_iso_session(conn, volume_id)
    if session_id is None:
        return _restage_hint(location)
    # #814: the burn skips a volume whose earlier disc there is still
    # recorded as verified, unless --reburn.
    reburn = (" --reburn" if has_verified_disc_at(conn, volume_id, location)
              else "")
    return (
        f"Its ISO is still in the staging area: burn it again with "
        f"'lcsas burn --session {session_id} --location {location}"
        f"{reburn}'."
    )


def _unknown_location_msg(exc: Any) -> str:
    """Error for an unknown --location that this command will not create."""
    msg = f"Unknown location '{exc.name}'"
    if exc.suggestions:
        msg += f" (did you mean: {', '.join(exc.suggestions)}?)"
    return msg + ". Add it with 'lcsas location add <name>' first."
