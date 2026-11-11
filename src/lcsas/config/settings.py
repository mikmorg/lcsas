"""LCSAS configuration loading and settings."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lcsas.config.media import MediaType
from lcsas.utils.labels import generate_volume_label

_logger = logging.getLogger("lcsas")


@dataclass(frozen=True)
class LCSASConfig:
    """Central configuration for LCSAS.

    All paths are absolute. Relative paths in the TOML config file are
    resolved against the config file's parent directory.
    """

    # Core paths
    mirror_base_path: Path          # Root of the local mirrors
    staging_path: Path              # The staging area
    db_path: Path                   # The master catalog (archive.db by default)

    # Defaults
    default_media_type: MediaType = MediaType.BD25
    # Deprecated: ignored by RS03 augmented images (BURN-07) — dvdisaster
    # pads to the smallest fitting medium and the padding IS the redundancy.
    # Kept so existing TOMLs keep parsing; validate_config warns if non-15.
    default_ecc_redundancy_pct: int = 15
    default_location: str = "Home_Shelf"

    # Optional device paths
    optical_device: str = "/dev/sr0"

    # Volume label prefix
    label_prefix: str = "LCSAS"

    # Metadata overhead reserved during bin packing (bytes)
    metadata_reserve_bytes: int = 104_857_600  # 100 MB

    # ── Password split (Shamir / SLIP-0039) defaults ─────────────
    # Default K-of-N split parameters for `lcsas key split`.  Bias toward
    # restorability (a backup's dominant risk is loss, not theft).
    key_threshold: int = 2          # K: shares needed to reconstruct
    key_shares: int = 5             # N: total shares produced
    # Whether THIS archive's password is actually split into shares.  Gates
    # the heir-facing share-combining instructions (PASSWORD_INFO / START_HERE):
    # K/N always carry defaults, so they alone don't indicate a real split.
    # All three are the archive-wide defaults: a repository's own
    # ``[repos.<name>]`` table may override each of them (#587, repo_split).
    key_split: bool = False

    # ── Format-drift gate (FMT-03) ───────────────────────────────
    # Burn refuses to master discs from a repository it cannot prove
    # restorable by the pinned recovery readers (config version 1-2 + an
    # in-process decode proof).  Set true to burn anyway when a repository
    # has no password_file configured — strongly discouraged: such discs are
    # unproven at burn time and may be unrestorable.
    allow_unverified_repo_format: bool = False

    # Repository definitions (populated from config file)
    repositories: dict[str, RepositoryConfig] = field(default_factory=dict)

    # Collections (#701): [collections.<name>] tables, each a location +
    # media type + minimum fill + the repositories staged for it.
    collections: dict[str, CollectionConfig] = field(default_factory=dict)

    # ── Survivability / human documentation fields ───────────────
    # These appear on START_HERE.txt on every disc to help a non-
    # technical person understand the archive after the owner's death.
    archive_owner: str = ""
    archive_description: str = ""
    key_storage_hints: str = ""
    technical_contact: str = ""

    def repo_split(self, name: str) -> tuple[bool, int, int]:
        """``(split, K, N)`` in effect for repository *name* (#587).

        Each key set in the repository's ``[repos.<name>]`` table overrides
        the same key under ``[defaults]``; a name the config doesn't list
        gets the ``[defaults]`` values.
        """
        repo = self.repositories.get(name)
        if repo is None:
            return (self.key_split, self.key_threshold, self.key_shares)
        return (
            self.key_split if repo.key_split is None else repo.key_split,
            self.key_threshold if repo.key_threshold is None
            else repo.key_threshold,
            self.key_shares if repo.key_shares is None else repo.key_shares,
        )


@dataclass(frozen=True)
class RepositoryConfig:
    """Configuration for a single backup repository."""

    name: str
    mirror_path: Path               # e.g. /mnt/mirror/family
    password_file: Path | None = None
    encryption_key_id: str = ""
    # Per-repository password split (#587).  None = use the [defaults]
    # value; LCSASConfig.repo_split resolves the pair.
    key_split: bool | None = None
    key_threshold: int | None = None
    key_shares: int | None = None
    # Folders backed up into this repository (#701).  Descriptive only:
    # LCSAS does not run backups; it shows these so one file describes
    # the repository.  They are never written to a disc.
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionConfig:
    """One ``[collections.<name>]`` table (#701).

    A collection is the set of discs kept at one location: which
    repositories go there, on which media type, and how full the last
    volume of a stage must be.  ``lcsas stage --collection <name>``
    applies all four.
    """

    name: str
    location: str
    repos: tuple[str, ...]
    # None = the [defaults] media_type.
    media_type: MediaType | None = None
    # Percent of a volume's pack space (0 = no minimum).
    min_fill_pct: float = 0.0


def _xdg_db_path() -> str:
    """Return the XDG-compliant default master catalog path.

    Uses ``$XDG_DATA_HOME/lcsas/archive.db``, falling back to
    ``~/.local/share/lcsas/archive.db``.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return str(Path(xdg) / "lcsas" / "archive.db")
    return str(Path.home() / ".local" / "share" / "lcsas" / "archive.db")


def unknown_toml_keys(raw: dict[str, Any]) -> list[str]:
    """Describe unknown TOML sections or keys (likely typos), one per line.

    load_config only warns about these, so a typo never blocks a command.
    `lcsas config check` treats them as errors (#515): a `[repo.x]`
    instead of `[repos.x]` otherwise reads as "valid" and `scan` quietly
    scans nothing.
    """
    found: list[str] = []

    known_sections = {
        "paths", "defaults", "repos", "survivability", "collections",
    }
    known_paths = {"mirror_base", "staging", "database"}
    known_defaults = {
        "media_type", "ecc_redundancy_pct", "location",
        "optical_device", "label_prefix", "metadata_reserve_mb",
        "key_threshold", "key_shares", "key_split",
        "allow_unverified_repo_format",
    }
    known_survive = {
        "archive_owner", "archive_description",
        "key_storage_hints", "technical_contact",
    }
    known_repo_keys = {
        "mirror_path", "password_file", "encryption_key_id",
        "key_split", "key_threshold", "key_shares", "sources",
    }

    unknown_sections = set(raw.keys()) - known_sections
    if unknown_sections:
        found.append(f"Unknown config sections: {sorted(unknown_sections)} (typo?)")

    unknown_paths = set(raw.get("paths", {}).keys()) - known_paths
    if unknown_paths:
        found.append(f"Unknown [paths] keys: {sorted(unknown_paths)} (typo?)")

    unknown_defaults = set(raw.get("defaults", {}).keys()) - known_defaults
    if unknown_defaults:
        found.append(f"Unknown [defaults] keys: {sorted(unknown_defaults)} (typo?)")

    unknown_survive = set(raw.get("survivability", {}).keys()) - known_survive
    if unknown_survive:
        found.append(f"Unknown [survivability] keys: {sorted(unknown_survive)} (typo?)")

    for repo_name, repo_cfg in raw.get("repos", {}).items():
        if isinstance(repo_cfg, dict):
            unknown_repo = set(repo_cfg.keys()) - known_repo_keys
            if unknown_repo:
                found.append(
                    f"Unknown [repos.{repo_name}] keys: {sorted(unknown_repo)} (typo?)"
                )
    collections = raw.get("collections", {})
    if isinstance(collections, dict):
        for coll_name, coll_cfg in collections.items():
            if isinstance(coll_cfg, dict):
                unknown_coll = set(coll_cfg.keys()) - set(_COLLECTION_TYPES)
                if unknown_coll:
                    found.append(
                        f"Unknown [collections.{coll_name}] keys: "
                        f"{sorted(unknown_coll)} (typo?)"
                    )
    return found


def _validate_toml_keys(raw: dict[str, Any]) -> None:
    """Warn about unknown TOML sections or keys (likely typos)."""
    for message in unknown_toml_keys(raw):
        _logger.warning("%s", message)


# Expected TOML type of each known key (#682), by section.  "str" keys take
# a quoted string, "int" a bare whole number, "number" a bare whole or
# decimal number, "bool" a bare true/false.
_PATHS_TYPES = {"mirror_base": "str", "staging": "str", "database": "str"}
_DEFAULTS_TYPES = {
    "media_type": "str", "ecc_redundancy_pct": "int", "location": "str",
    "optical_device": "str", "label_prefix": "str",
    "metadata_reserve_mb": "number", "key_threshold": "int",
    "key_shares": "int", "key_split": "bool",
    "allow_unverified_repo_format": "bool",
}
_SURVIVE_TYPES = {
    "archive_owner": "str", "archive_description": "str",
    "key_storage_hints": "str", "technical_contact": "str",
}
_REPO_TYPES = {
    "mirror_path": "str", "password_file": "str", "encryption_key_id": "str",
    "key_split": "bool", "key_threshold": "int", "key_shares": "int",
    "sources": "strlist",
}
_COLLECTION_TYPES = {
    "location": "str", "media_type": "str", "min_fill_pct": "number",
    "repos": "strlist",
}
_TYPE_WORDS = {
    "str": "a quoted string",
    "int": "a whole number without quotes",
    "number": "a number without quotes",
    "bool": "true or false without quotes",
    "strlist": 'a list of quoted strings, like ["a", "b"]',
}

# Largest share count `lcsas key split` makes (SLIP-0039 allows 16 shares).
MAX_KEY_SHARES = 16


def _type_ok(kind: str, value: object) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "strlist":
        return isinstance(value, list) and all(
            isinstance(v, str) for v in value
        )
    # bool is an int subclass: `key_threshold = true` is not a number.
    if isinstance(value, bool):
        return False
    if kind == "int":
        return isinstance(value, int)
    return isinstance(value, (int, float))


def _type_errors(raw: dict[str, Any]) -> list[str]:
    """One message per known key whose value has the wrong TOML type (#682).

    Each message names the key and the type it needs, so the owner can fix
    the line instead of meeting a TypeError later (``key_split = "false"``
    used to read as true, ``key_threshold = "3"`` crashed `key split`).
    """
    errors: list[str] = []

    def check(where: str, table: object, types: dict[str, str]) -> None:
        if not isinstance(table, dict):
            return
        for key, kind in types.items():
            if key in table and not _type_ok(kind, table[key]):
                errors.append(
                    f"{where} {key} must be {_TYPE_WORDS[kind]}, "
                    f"not {table[key]!r}"
                )

    for section in ("paths", "defaults", "survivability", "repos",
                    "collections"):
        if section in raw and not isinstance(raw[section], dict):
            errors.append(f"[{section}] must be a table")
    check("[paths]", raw.get("paths", {}), _PATHS_TYPES)
    check("[defaults]", raw.get("defaults", {}), _DEFAULTS_TYPES)
    check("[survivability]", raw.get("survivability", {}), _SURVIVE_TYPES)
    repos = raw.get("repos", {})
    if isinstance(repos, dict):
        for name, repo_cfg in repos.items():
            check(f"[repos.{name}]", repo_cfg, _REPO_TYPES)
    collections = raw.get("collections", {})
    if isinstance(collections, dict):
        for name, coll_cfg in collections.items():
            if not isinstance(coll_cfg, dict):
                errors.append(f"[collections.{name}] must be a table")
                continue
            check(f"[collections.{name}]", coll_cfg, _COLLECTION_TYPES)
    return errors


def key_split_pair_error(threshold: int, shares: int) -> str | None:
    """Why *threshold*-of-*shares* is not a usable split, or None (#682).

    A split needs 2 <= K <= N <= 16; K = N = 1 means the password is not
    split.  ``lcsas key split`` checks its own -k/-n with the same rule.
    """
    if (threshold, shares) == (1, 1):
        return None
    if 2 <= threshold <= shares <= MAX_KEY_SHARES:
        return None
    return (
        f"{threshold}-of-{shares} is not a usable split: it needs "
        f"2 <= key_threshold <= key_shares <= {MAX_KEY_SHARES}, or "
        f"key_threshold = key_shares = 1 for no split"
    )


def load_config(config_path: Path) -> LCSASConfig:  # noqa: C901
    """Load LCSAS configuration from a TOML file.

    Example TOML structure::

        [paths]
        mirror_base = "/mnt/mirror"
        staging = "/mnt/staging"
        database = "~/.local/share/lcsas/archive.db"

        [defaults]
        media_type = "BD25"
        ecc_redundancy_pct = 15
        location = "Home_Shelf"
        optical_device = "/dev/sr0"
        label_prefix = "LCSAS"
        metadata_reserve_mb = 100

        [survivability]
        archive_owner = "John Doe"
        archive_description = "Family photos, videos, and documents 2000-2025"
        # The hint is printed verbatim on EVERY disc, so it must point an heir
        # to the password without handing it to a disc thief — name a custodian,
        # not a findable place.  See docs/DISC_CONFIDENTIALITY.md.
        key_storage_hints = "Sealed envelope with the family attorney (see my will)"
        technical_contact = "Jane Doe (jane@example.com) or any Linux-savvy IT professional"

        [repos.family]
        mirror_path = "/mnt/mirror/family"
        password_file = "/root/keys/family.key"
        # Optional, descriptive only (#701): the folders backed up into
        # this repository.  LCSAS does not run backups.
        sources = ["/srv/photos", "/srv/documents"]

        [repos.work]
        mirror_path = "/mnt/mirror/work"
        password_file = "/root/keys/work.key"
        # Optional: split only this repository's password (#587); each
        # key overrides the same [defaults] key for this repository.
        key_split = true
        key_threshold = 2
        key_shares = 3

        # Optional (#701): `lcsas stage --collection bank` stages family
        # and work for Bank_Box on MDISC100, holding back a last volume
        # that fills less than 90% of its pack space.
        [collections.bank]
        location = "Bank_Box"
        media_type = "MDISC100"
        min_fill_pct = 90
        repos = ["family", "work"]
    """
    config_path = config_path.resolve()
    base_dir = config_path.parent

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    # Validate known sections and keys
    _validate_toml_keys(raw)
    # Wrong value types are errors, named by key (#682).  _load_config_opt
    # turns the ValueError into a ConfigError: no traceback.
    type_errors = _type_errors(raw)
    if type_errors:
        raise ValueError("; ".join(type_errors))

    paths = raw.get("paths", {})
    defaults = raw.get("defaults", {})

    def resolve(p: str) -> Path:
        """Make *p* absolute **without touching the filesystem**.

        Relative paths are joined to the config file's directory and
        normalised lexically rather than with ``Path.resolve()``:
        ``resolve()`` lstats every component, which on a stale NFS/CIFS
        mount blocks for the mount's timeout instead of returning.  Since
        every command now reads the config to find its catalog, that would
        hang commands that never touch the mirror at all (#427).  Absolute
        paths were already returned untouched, so this only makes the two
        branches consistent.
        """
        path = Path(p)
        if path.is_absolute():
            return path
        return Path(os.path.normpath(base_dir / path))

    repos: dict[str, RepositoryConfig] = {}
    for repo_name, repo_cfg in raw.get("repos", {}).items():
        # Named errors, not a bare KeyError from repo_cfg["mirror_path"]
        # ("Unexpected error: 'mirror_path'", #515).  _load_config_opt
        # turns the ValueError into an actionable ConfigError.
        if not isinstance(repo_cfg, dict):
            raise ValueError(
                f"[repos.{repo_name}] must be a table with a mirror_path"
            )
        if "mirror_path" not in repo_cfg:
            raise ValueError(f"[repos.{repo_name}] has no mirror_path (required)")
        pw_file = repo_cfg.get("password_file")
        repo_split = repo_cfg.get("key_split")
        repos[repo_name] = RepositoryConfig(
            name=repo_name,
            mirror_path=resolve(repo_cfg["mirror_path"]),
            password_file=resolve(pw_file) if pw_file else None,
            encryption_key_id=repo_cfg.get("encryption_key_id", ""),
            key_split=None if repo_split is None else bool(repo_split),
            key_threshold=repo_cfg.get("key_threshold"),
            key_shares=repo_cfg.get("key_shares"),
            sources=tuple(repo_cfg.get("sources", ())),
        )

    collections = _load_collections(raw.get("collections", {}), repos)

    # NB: loading a config deliberately performs no I/O on the repository paths.
    # A missing mirror_path / password_file is reported by validate_config()
    # as a hard error, and every command that actually reads a mirror (scan,
    # stage, burn, restore plan/exec) gates on it via _validate_config_or_exit
    # — so probing here was pure duplication.  It was also a liability: after
    # bdcbd31 every command resolves its catalog through load_config, so a
    # stale mount turned `repo list` and `status` into a hang (#427).

    media_str = defaults.get("media_type", "BD25")
    try:
        media_type = MediaType[media_str.upper()]
    except KeyError as err:
        valid = [m.name for m in MediaType]
        raise ValueError(
            f"Unknown media type '{media_str}'. Valid: {valid}"
        ) from err

    # Survivability fields
    survive = raw.get("survivability", {})

    if "mirror_base" not in paths:
        _logger.warning(
            "Config: 'paths.mirror_base' not set in '%s'; "
            "defaulting to /mnt/mirror — set this explicitly to avoid surprises.",
            config_path,
        )
    if "staging" not in paths:
        _logger.warning(
            "Config: 'paths.staging' not set in '%s'; "
            "defaulting to /mnt/staging — set this explicitly to avoid surprises.",
            config_path,
        )

    config = LCSASConfig(
        mirror_base_path=resolve(paths.get("mirror_base", "/mnt/mirror")),
        staging_path=resolve(paths.get("staging", "/mnt/staging")),
        db_path=resolve(paths.get("database", _xdg_db_path())),
        default_media_type=media_type,
        default_ecc_redundancy_pct=defaults.get("ecc_redundancy_pct", 15),
        default_location=defaults.get("location", "Home_Shelf"),
        optical_device=defaults.get("optical_device", "/dev/sr0"),
        label_prefix=defaults.get("label_prefix", "LCSAS"),
        metadata_reserve_bytes=int(
            defaults.get("metadata_reserve_mb", 100) * 1_048_576
        ),
        key_threshold=defaults.get("key_threshold", 2),
        key_shares=defaults.get("key_shares", 5),
        key_split=bool(defaults.get("key_split", False)),
        allow_unverified_repo_format=bool(
            defaults.get("allow_unverified_repo_format", False)
        ),
        repositories=repos,
        collections=collections,
        archive_owner=survive.get("archive_owner", ""),
        archive_description=survive.get("archive_description", ""),
        key_storage_hints=survive.get("key_storage_hints", ""),
        technical_contact=survive.get("technical_contact", ""),
    )
    problems = _split_and_mirror_errors(config)
    if problems:
        raise ValueError("; ".join(problems))
    return config


def _load_collections(
    raw: dict[str, Any],
    repos: dict[str, RepositoryConfig],
) -> dict[str, CollectionConfig]:
    """Build the ``[collections.<name>]`` tables (#701).

    Checked lexically, with no catalog: each collection needs a location
    and at least one repository, every repository it names must have a
    ``[repos.<name>]`` table, the media type must exist and the minimum
    fill must be 0 to 100.  Whether the location and the repositories are
    registered in the catalog is checked by ``lcsas config check`` and by
    ``lcsas stage --collection``.  _type_errors has already checked the
    value types.
    """
    errors: list[str] = []
    collections: dict[str, CollectionConfig] = {}
    for name, cfg in raw.items():
        where = f"[collections.{name}]"
        location = cfg.get("location", "")
        if not location:
            errors.append(f"{where} has no location (required)")
        names = list(dict.fromkeys(cfg.get("repos", [])))
        if not names:
            errors.append(
                f"{where} has no repos (required: a list of [repos.<name>] "
                f"names)"
            )
        unknown = [n for n in names if n not in repos]
        if unknown:
            errors.append(
                f"{where} repos names {', '.join(repr(n) for n in unknown)}, "
                f"which has no [repos.<name>] table"
            )
        media: MediaType | None = None
        media_str = cfg.get("media_type")
        if media_str is not None:
            try:
                media = MediaType[media_str.upper()]
            except KeyError:
                valid = ", ".join(m.name for m in MediaType)
                errors.append(
                    f"{where} media_type '{media_str}' is unknown. "
                    f"Valid: {valid}"
                )
        min_fill = cfg.get("min_fill_pct", 0)
        if not 0 <= min_fill <= 100:
            errors.append(
                f"{where} min_fill_pct must be between 0 and 100, "
                f"not {min_fill!r}"
            )
        collections[name] = CollectionConfig(
            name=name,
            location=location,
            repos=tuple(names),
            media_type=media,
            min_fill_pct=float(min_fill),
        )
    if errors:
        raise ValueError("; ".join(errors))
    return collections


def collection_password_errors(
    config: LCSASConfig, names: list[str] | None = None,
) -> list[str]:
    """Collection repositories without a readable password file (#701).

    A burn proves each repository restorable with its password file
    (FMT-03), so a collection repository needs one unless
    ``allow_unverified_repo_format`` is set.  A file that does not exist
    is already reported by validate_config.  *names* limits the check to
    those collections.
    """
    errors: list[str] = []
    for cname, coll in config.collections.items():
        if names is not None and cname not in names:
            continue
        for rname in coll.repos:
            repo = config.repositories.get(rname)
            if repo is None:
                continue  # the loader already refused this
            pw = repo.password_file
            if pw is None:
                if not config.allow_unverified_repo_format:
                    errors.append(
                        f"collection '{cname}': repo '{rname}' has no "
                        f"password_file"
                    )
            elif pw.exists() and not os.access(pw, os.R_OK):
                errors.append(
                    f"collection '{cname}': repo '{rname}': password_file "
                    f"is not readable: {pw}"
                )
    return errors


def _split_and_mirror_errors(config: LCSASConfig) -> list[str]:
    """Impossible K/N pairs and shared mirror paths (#682).

    Checked in the loader, lexically (no I/O), so every command — not just
    `config check` — refuses a config that cannot work.
    """
    errors: list[str] = []
    problem = key_split_pair_error(config.key_threshold, config.key_shares)
    if problem is not None:
        errors.append(f"[defaults] key_threshold/key_shares: {problem}")
    for name, repo in config.repositories.items():
        if repo.key_threshold is None and repo.key_shares is None:
            continue  # uses [defaults], checked above
        _, k, n = config.repo_split(name)
        problem = key_split_pair_error(k, n)
        if problem is not None:
            errors.append(
                f"[repos.{name}] key_threshold/key_shares: {problem}"
            )
    # Two repositories on one mirror would make the catalog unable to tell
    # them apart (repositories are matched by mirror path first, #687).
    seen: dict[str, str] = {}
    for name, repo in config.repositories.items():
        key = os.path.normpath(str(repo.mirror_path))
        if key in seen:
            errors.append(
                f"[repos.{name}] mirror_path is the same as "
                f"[repos.{seen[key]}] mirror_path ({key}); each repository "
                f"needs its own mirror"
            )
        else:
            seen[key] = name
    return errors


def default_config(
    mirror_base: Path,
    staging: Path,
    db_path: Path,
    media_type: MediaType = MediaType.BD25,
) -> LCSASConfig:
    """Create a minimal config without a TOML file (useful for testing)."""
    return LCSASConfig(
        mirror_base_path=mirror_base,
        staging_path=staging,
        db_path=db_path,
        default_media_type=media_type,
    )


def validate_config(config: LCSASConfig) -> list[str]:
    """Validate an ``LCSASConfig`` and return a list of error strings.

    Returns an empty list when the configuration is valid.
    """
    errors: list[str] = []

    # mirror_base_path
    if not config.mirror_base_path.exists():
        errors.append(
            f"mirror_base_path does not exist: {config.mirror_base_path}"
        )
    elif not config.mirror_base_path.is_dir():
        errors.append(
            f"mirror_base_path is not a directory: {config.mirror_base_path}"
        )

    # staging_path
    if not config.staging_path.exists():
        errors.append(
            f"staging_path does not exist: {config.staging_path}"
        )
    elif not config.staging_path.is_dir():
        errors.append(
            f"staging_path is not a directory: {config.staging_path}"
        )
    elif not _is_writable(config.staging_path):
        errors.append(
            f"staging_path is not writable: {config.staging_path}"
        )

    # db_path parent
    db_parent = config.db_path.parent
    if not db_parent.exists():
        errors.append(
            f"db_path parent directory does not exist: {db_parent}"
        )
    elif not _is_writable(db_parent):
        errors.append(
            f"db_path parent directory is not writable: {db_parent}"
        )

    # ecc_redundancy_pct
    if not 0 <= config.default_ecc_redundancy_pct <= 100:
        errors.append(
            f"default_ecc_redundancy_pct out of range (0-100): "
            f"{config.default_ecc_redundancy_pct}"
        )
    elif config.default_ecc_redundancy_pct != 15:
        # Deprecated knob, kept so existing TOMLs parse.  RS03 augmented
        # images cannot take a redundancy setting (man dvdisaster), so a
        # non-default value must not silently masquerade as extra (or
        # reduced) protection.
        _logger.warning(
            "default_ecc_redundancy_pct (%d) has no effect on RS03 augmented "
            "images; effective redundancy is the padding of the smallest "
            "fitting medium",
            config.default_ecc_redundancy_pct,
        )

    # metadata_reserve_bytes (must be non-negative; zero is allowed but risky)
    if config.metadata_reserve_bytes < 0:
        errors.append(
            f"metadata_reserve_bytes must not be negative: "
            f"{config.metadata_reserve_bytes}"
        )
    elif config.metadata_reserve_bytes >= config.default_media_type.usable_bytes:
        errors.append(
            f"metadata_reserve_bytes ({config.metadata_reserve_bytes:,}) must be "
            f"less than the usable capacity of the configured media type "
            f"({config.default_media_type.name}: "
            f"{config.default_media_type.usable_bytes:,} bytes)"
        )

    # label_prefix must be non-empty and use only ISO 9660-safe characters.
    import re as _re
    if not config.label_prefix:
        errors.append("label_prefix must not be empty")
    elif not _re.fullmatch(r"[A-Z0-9_]+", config.label_prefix):
        errors.append(
            f"label_prefix '{config.label_prefix}' contains characters not "
            f"allowed in ISO 9660 volume identifiers. Use only A-Z, 0-9, and '_'."
        )
    else:
        # Pre-validate that the prefix won't produce labels exceeding 32 chars.
        # Ask the real generator: the media token's length varies (BD25 vs
        # CD700 / BX100 / MD100), so a fixed BD25 overhead let a prefix pass
        # here and then fail at stage time (#523).
        media = config.default_media_type
        try:
            generate_volume_label(config.label_prefix, media.label_name, 1)
        except ValueError:
            errors.append(
                f"label_prefix '{config.label_prefix}' is too long "
                f"({len(config.label_prefix)} chars) for {media.name}; generated "
                f"volume labels would exceed the ISO 9660 32-character limit. "
                f"Use a shorter prefix."
            )
        else:
            # `stage --media` / `burn --media` can pick another medium, so
            # say up front which ones this prefix rules out.
            too_long = []
            for other in MediaType:
                if other.is_test:
                    continue
                try:
                    generate_volume_label(config.label_prefix, other.label_name, 1)
                except ValueError:
                    too_long.append(other.name)
            if too_long:
                _logger.warning(
                    "label_prefix '%s' is too long for --media %s (32-character "
                    "ISO 9660 label limit)",
                    config.label_prefix, ", ".join(too_long),
                )

    # Per-repository checks
    for name, repo in config.repositories.items():
        if not repo.mirror_path.exists():
            errors.append(
                f"repo '{name}': mirror_path does not exist: {repo.mirror_path}"
            )
        elif not repo.mirror_path.is_dir():
            errors.append(
                f"repo '{name}': mirror_path is not a directory: "
                f"{repo.mirror_path}"
            )
        if repo.password_file is not None and not repo.password_file.exists():
            errors.append(
                f"repo '{name}': password_file does not exist: "
                f"{repo.password_file}"
            )

    # Check for critical path overlaps that could cause data loss during cleanup.
    # The staging area must not hold, or sit inside, a repository mirror:
    # cleaning the staging area would destroy it.  #645: a staging area
    # inside mirror_base is fine when it overlaps no repository's
    # mirror_path (<base>/<repo> + <base>/staging keeps staging on the
    # mirrors' filesystem, so packs are hardlinked, not copied).  With no
    # repositories configured, mirror_base itself is the fallback mirror
    # (BurnOrchestrator._get_mirror_paths), so it is checked as one.
    def _overlap(a: Path, b: Path) -> str | None:
        if a == b:
            return "identical"
        if a in b.parents or b in a.parents:
            return "overlap"
        return None

    try:
        staging_real = config.staging_path.resolve()
        mirror_real = config.mirror_base_path.resolve()
        if staging_real == mirror_real:
            errors.append(
                f"staging_path and mirror_base_path cannot be identical: {staging_real}"
            )
        elif staging_real in mirror_real.parents or (
            mirror_real in staging_real.parents and not config.repositories
        ):
            errors.append(
                f"staging_path and mirror_base_path cannot overlap: "
                f"{staging_real} vs {mirror_real} "
                f"(cleaning the staging area would affect mirrors)"
            )
        for name, repo in config.repositories.items():
            repo_real = repo.mirror_path.resolve()
            kind = _overlap(staging_real, repo_real)
            if kind == "identical":
                errors.append(
                    f"staging_path and repo '{name}' mirror_path cannot be "
                    f"identical: {staging_real}"
                )
            elif kind == "overlap":
                errors.append(
                    f"staging_path and repo '{name}' mirror_path cannot "
                    f"overlap: {staging_real} vs {repo_real} "
                    f"(cleaning the staging area would affect the mirror)"
                )
    except (OSError, RuntimeError):
        # Path resolution failed (e.g., due to symlink issues)
        pass

    return errors


def _is_writable(path: Path) -> bool:
    """Check if a path is writable using os.access."""
    return os.access(path, os.W_OK)
