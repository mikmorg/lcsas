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

    # ── Survivability / human documentation fields ───────────────
    # These appear on START_HERE.txt on every disc to help a non-
    # technical person understand the archive after the owner's death.
    archive_owner: str = ""
    archive_description: str = ""
    key_storage_hints: str = ""
    technical_contact: str = ""


@dataclass(frozen=True)
class RepositoryConfig:
    """Configuration for a single backup repository."""

    name: str
    mirror_path: Path               # e.g. /mnt/mirror/family
    password_file: Path | None = None
    encryption_key_id: str = ""


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

    known_sections = {"paths", "defaults", "repos", "survivability"}
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
    known_repo_keys = {"mirror_path", "password_file", "encryption_key_id"}

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
    return found


def _validate_toml_keys(raw: dict[str, Any]) -> None:
    """Warn about unknown TOML sections or keys (likely typos)."""
    for message in unknown_toml_keys(raw):
        _logger.warning("%s", message)


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
        repos[repo_name] = RepositoryConfig(
            name=repo_name,
            mirror_path=resolve(repo_cfg["mirror_path"]),
            password_file=resolve(pw_file) if pw_file else None,
            encryption_key_id=repo_cfg.get("encryption_key_id", ""),
        )

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
        key_threshold=defaults.get("key_threshold", 2),
        key_shares=defaults.get("key_shares", 5),
        key_split=bool(defaults.get("key_split", False)),
        allow_unverified_repo_format=bool(
            defaults.get("allow_unverified_repo_format", False)
        ),
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
    # Specifically: staging_path and mirror_base_path must be separate
    # (cleanup routines would destroy mirrors if the staging area is inside a
    # mirror).
    try:
        staging_real = config.staging_path.resolve()
        mirror_real = config.mirror_base_path.resolve()
        if staging_real == mirror_real:
            errors.append(
                f"staging_path and mirror_base_path cannot be identical: {staging_real}"
            )
        elif staging_real in mirror_real.parents or mirror_real in staging_real.parents:
            errors.append(
                f"staging_path and mirror_base_path cannot overlap: "
                f"{staging_real} vs {mirror_real} "
                f"(cleaning the staging area would affect mirrors)"
            )
    except (OSError, RuntimeError):
        # Path resolution failed (e.g., due to symlink issues)
        pass

    return errors


def _is_writable(path: Path) -> bool:
    """Check if a path is writable using os.access."""
    return os.access(path, os.W_OK)
