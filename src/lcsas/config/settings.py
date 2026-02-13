"""LCSAS configuration loading and settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lcsas.config.media import MediaType


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


@dataclass(frozen=True)
class RepositoryConfig:
    """Configuration for a single backup repository (tenant)."""

    name: str
    mirror_path: Path               # e.g. /mnt/mirror/family
    password_file: Path | None = None
    encryption_key_id: str = ""


def load_config(config_path: Path) -> LCSASConfig:
    """Load LCSAS configuration from a TOML file.

    Example TOML structure::

        [paths]
        mirror_base = "/mnt/mirror"
        staging = "/mnt/staging"
        database = "/var/lib/lcsas/archive.db"

        [defaults]
        media_type = "BD25"
        ecc_redundancy_pct = 15
        location = "Home_Shelf"
        optical_device = "/dev/sr0"
        label_prefix = "LCSAS"
        metadata_reserve_mb = 100

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
            password_file=Path(pw_file) if pw_file else None,
            encryption_key_id=repo_cfg.get("encryption_key_id", ""),
        )

    media_str = defaults.get("media_type", "BD25")
    try:
        media_type = MediaType[media_str]
    except KeyError as err:
        valid = [m.name for m in MediaType]
        raise ValueError(
            f"Unknown media type '{media_str}'. Valid: {valid}"
        ) from err

    return LCSASConfig(
        mirror_base_path=resolve(paths.get("mirror_base", "/mnt/mirror")),
        staging_path=resolve(paths.get("staging", "/mnt/staging")),
        db_path=resolve(paths.get("database", "/var/lib/lcsas/archive.db")),
        default_media_type=media_type,
        default_ecc_redundancy_pct=defaults.get("ecc_redundancy_pct", 15),
        default_location=defaults.get("location", "Home_Shelf"),
        optical_device=defaults.get("optical_device", "/dev/sr0"),
        label_prefix=defaults.get("label_prefix", "LCSAS"),
        metadata_reserve_bytes=defaults.get("metadata_reserve_mb", 100) * 1_048_576,
        repositories=repos,
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
