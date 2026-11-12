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

def _load_config_opt(args: argparse.Namespace) -> LCSASConfig | None:
    """Load ``--config`` when given, else ``None``.

    Every handler that accepts an optional config funnels through here so
    config failures — a path that is not there, a file that will not
    parse, a value the loader rejects — surface as an actionable
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
        raise ConfigError(
            f"--config path does not exist: {cfg_path}",
            recovery_hint=(
                "Check the path, or omit --config to use ./archive.db."
            ),
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
    """Resolve the catalog path from CLI args, config, or default.

    Priority: ``--db`` flag > config ``[paths].database`` > ``archive.db``
    relative to the current working directory.

    *config* is an optimisation, not a switch: handlers that already
    loaded it pass it in to avoid a second parse.  When it is omitted this
    still honours ``--config``, because a handler that forgets to load one
    must not end up addressing a *different* catalog than the rest of the
    CLI — that silently creates an empty ``archive.db`` in whatever
    directory the owner happened to be standing in, while ``scan`` and
    ``stage`` keep using the configured path.
    """
    if getattr(args, "db", None):
        return Path(args.db)
    if config is None:
        config = _load_config_opt(args)
    if config is not None and hasattr(config, "db_path"):
        return Path(config.db_path)
    return Path("archive.db")


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
            recovery_hint="Run `lcsas init` first.",
        )
    conn = get_connection(db_path)
    if get_schema_version(conn) == 0:
        conn.close()
        raise CatalogError(
            f"No catalog at {db_path}.",
            recovery_hint="Run `lcsas init` first.",
        )
    return conn


_CatalogMode = Literal["write", "write-existing", "read", "unlocked-migrate", "unlocked"]


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
    :func:`_resolve_db_path` of *args* and *config*.  *mode*:

    ``write`` (the default)
        Take the catalog lock (waiting up to ``--lock-timeout``), create
        the catalog when it is not there, and bring its schema up to date.
    ``write-existing``
        The same, but a missing or uninitialised catalog is a
        :class:`CatalogError` ("Run `lcsas init` first") instead of a new
        catalog.
    ``read``
        No lock.  The catalog must exist and is not migrated
        (:func:`_open_existing_catalog`).
    ``unlocked-migrate``, ``unlocked``
        No lock; the catalog is created when missing, and migrated or not.
        Only ``volume impact`` and ``session list`` open it this way, as
        they did before this helper existed.
    """
    from lcsas.db.connection import get_connection, locked_connection
    from lcsas.db.schema import ensure_schema

    if db_path is None:
        db_path = _resolve_db_path(args, config)
    if mode in ("write", "write-existing"):
        if mode == "write-existing":
            _open_existing_catalog(db_path).close()
        with locked_connection(db_path, timeout=_resolve_lock_timeout(args)) as conn:
            ensure_schema(conn)
            yield conn
        return
    conn = _open_existing_catalog(db_path) if mode == "read" else get_connection(db_path)
    try:
        if mode == "unlocked-migrate":
            ensure_schema(conn)
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
    as a hint (the mirror path of ``[repos.<name>]``) and work with ``--db``
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


def _unknown_location_msg(exc: Any) -> str:
    """Error for an unknown --location that this command will not create."""
    msg = f"Unknown location '{exc.name}'"
    if exc.suggestions:
        msg += f" (did you mean: {', '.join(exc.suggestions)}?)"
    return msg + ". Add it with 'lcsas location add <name>' first."
