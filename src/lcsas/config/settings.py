"""LCSAS configuration loading and settings."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lcsas.config.media import MediaType

_logger = logging.getLogger("lcsas")


@dataclass(frozen=True)
class LCSASConfig:
    """Central configuration for LCSAS.

    All paths are absolute. Relative paths in the TOML config file are
    resolved against the config file's parent directory.
    """

    # Core paths
    mirror_base_path: Path          # Root of local mirror repos (Tier 1)
    staging_path: Path              # Transient staging area (Tier 2)
    db_path: Path                   # Path to archive_master.db

    # Defaults
    default_media_type: MediaType = MediaType.BD25
    default_ecc_redundancy_pct: int = 15
    default_location: str = "Home_Shelf"

    # Optional device paths
    optical_device: str = "/dev/sr0"

    # Volume label prefix
    label_prefix: str = "LCSAS"

    # Metadata overhead reserved during bin packing (bytes)
    metadata_reserve_bytes: int = 104_857_600  # 100 MB

    # Repository definitions (populated from config file)
    repositories: dict[str, RepositoryConfig] = field(default_factory=dict)

    # ── Survivability / human documentation fields ───────────────
    # These appear on START_HERE.txt on every disc to help a non-
    # technical person understand the archive after the archivist's death.
    archive_owner: str = ""
    archive_description: str = ""
    key_storage_hints: str = ""
    technical_contact: str = ""


@dataclass(frozen=True)
class RepositoryConfig:
    """Configuration for a single backup repository (tenant)."""

    name: str
    mirror_path: Path               # e.g. /mnt/mirror/family
    password_file: Path | None = None
    encryption_key_id: str = ""


def _xdg_db_path() -> str:
    """Return XDG-compliant default database path.

    Uses ``$XDG_DATA_HOME/lcsas/archive.db``, falling back to
    ``~/.local/share/lcsas/archive.db``.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return str(Path(xdg) / "lcsas" / "archive.db")
    return str(Path.home() / ".local" / "share" / "lcsas" / "archive.db")


def _validate_toml_keys(raw: dict[str, Any]) -> None:
    """Warn about unknown TOML sections or keys (likely typos)."""

    known_sections = {"paths", "defaults", "repos", "survivability"}
    known_paths = {"mirror_base", "staging", "database"}
    known_defaults = {
        "media_type", "ecc_redundancy_pct", "location",
        "optical_device", "label_prefix", "metadata_reserve_mb",
    }
    known_survive = {
        "archive_owner", "archive_description",
        "key_storage_hints", "technical_contact",
    }
    known_repo_keys = {"mirror_path", "password_file", "encryption_key_id"}

    unknown_sections = set(raw.keys()) - known_sections
    if unknown_sections:
        _logger.warning("Unknown config sections: %s (typo?)", sorted(unknown_sections))

    unknown_paths = set(raw.get("paths", {}).keys()) - known_paths
    if unknown_paths:
        _logger.warning("Unknown [paths] keys: %s (typo?)", sorted(unknown_paths))

    unknown_defaults = set(raw.get("defaults", {}).keys()) - known_defaults
    if unknown_defaults:
        _logger.warning("Unknown [defaults] keys: %s (typo?)", sorted(unknown_defaults))

    unknown_survive = set(raw.get("survivability", {}).keys()) - known_survive
    if unknown_survive:
        _logger.warning("Unknown [survivability] keys: %s (typo?)", sorted(unknown_survive))

    for repo_name, repo_cfg in raw.get("repos", {}).items():
        if isinstance(repo_cfg, dict):
            unknown_repo = set(repo_cfg.keys()) - known_repo_keys
            if unknown_repo:
                _logger.warning(
                    "Unknown [repos.%s] keys: %s (typo?)",
                    repo_name, sorted(unknown_repo),
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
        key_storage_hints = "Paper copy in the home safe; USB copy in safe deposit box #1234"
        technical_contact = "Jane Doe (jane@example.com) or any Linux-savvy IT professional"

        [repos.family]
        mirror_path = "/mnt/mirror/family"
        password_file = "/root/keys/family.key"

        [repos.work]
        mirror_path = "/mnt/mirror/work"
        password_file = "/root/keys/work.key"
    """
    config_path = config_path.resolve()
    base_dir = config_path.parent

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    # Validate known sections and keys
    _validate_toml_keys(raw)

    paths = raw.get("paths", {})
    defaults = raw.get("defaults", {})

    def resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (base_dir / path).resolve()

    repos: dict[str, RepositoryConfig] = {}
    for repo_name, repo_cfg in raw.get("repos", {}).items():
        pw_file = repo_cfg.get("password_file")
        repos[repo_name] = RepositoryConfig(
            name=repo_name,
            mirror_path=resolve(repo_cfg["mirror_path"]),
            password_file=resolve(pw_file) if pw_file else None,
            encryption_key_id=repo_cfg.get("encryption_key_id", ""),
        )

    # Warn early about missing mirror paths; scan/burn will silently produce
    # no work if the mirror is absent, which is very hard to diagnose later.
    for repo_name, repo_cfg in repos.items():
        try:
            mirror_exists = repo_cfg.mirror_path.exists()
        except OSError:
            mirror_exists = True  # Can't check — don't warn about access errors
        if not mirror_exists:
            _logger.warning(
                "Config: repo '%s' mirror_path does not exist: %s",
                repo_name, repo_cfg.mirror_path,
            )
        if repo_cfg.password_file is not None:
            try:
                pw_exists = repo_cfg.password_file.exists()
            except OSError:
                pw_exists = True  # Can't check — don't warn
            if not pw_exists:
                _logger.warning(
                    "Config: repo '%s' password_file does not exist: %s",
                    repo_name, repo_cfg.password_file,
                )

    media_str = defaults.get("media_type", "BD25")
    try:
        media_type = MediaType[media_str]
    except KeyError as err:
        valid = [m.name for m in MediaType]
        raise ValueError(
            f"Unknown media type '{media_str}'. Valid: {valid}"
        ) from err

    # Survivability fields
    survive = raw.get("survivability", {})

    return LCSASConfig(
        mirror_base_path=resolve(paths.get("mirror_base", "/mnt/mirror")),
        staging_path=resolve(paths.get("staging", "/mnt/staging")),
        db_path=resolve(paths.get("database", _xdg_db_path())),
        default_media_type=media_type,
        default_ecc_redundancy_pct=defaults.get("ecc_redundancy_pct", 15),
        default_location=defaults.get("location", "Home_Shelf"),
        optical_device=defaults.get("optical_device", "/dev/sr0"),
        label_prefix=defaults.get("label_prefix", "LCSAS"),
        metadata_reserve_bytes=defaults.get("metadata_reserve_mb", 100) * 1_048_576,
        repositories=repos,
        archive_owner=survive.get("archive_owner", ""),
        archive_description=survive.get("archive_description", ""),
        key_storage_hints=survive.get("key_storage_hints", ""),
        technical_contact=survive.get("technical_contact", ""),
    )


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

    # metadata_reserve_bytes (must be positive)
    if config.metadata_reserve_bytes <= 0:
        errors.append(
            f"metadata_reserve_bytes must be positive: "
            f"{config.metadata_reserve_bytes}"
        )

    # Per-repo checks
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

    return errors


def _is_writable(path: Path) -> bool:
    """Check if a path is writable using os.access."""
    return os.access(path, os.W_OK)
