"""Burn-time gate against rustic writer / pinned-reader format drift [FMT-03].

The burn pipeline treats pack files as opaque bytes; nothing else in the
pipeline ever checks the repository format version, KDF, or compression
framing.  Meanwhile the *writer* is whatever rustic the operator has on the
NAS, while every shipped recovery tier hard-codes restic/rustic v1/v2
semantics.  If a future rustic migrates the mirror to a v3 format, every disc
burned afterward would be undecodable by all three recovery tiers — and the
operator's live test-restores would keep passing because they use the same
live rustic that wrote the data.  The drift is silently masked.

This module proves, *before any ISO is mastered*, that the mirror still
decodes with the pinned readers.  It uses :class:`PurePythonRestorer` — the
tier-3 code — because it is the one reader importable in a plain
``pip install lcsas`` (the tier-1 C binaries are not shipped with the wheel)
and it exercises the same v1/v2 assumptions tier-1 hard-codes:

  1. decrypt + parse ``config``; assert ``version`` in SUPPORTED_REPO_VERSIONS;
  2. load one index file (proves index crypto + framing decode);
  3. fetch + MAC-verify + content-hash-verify one blob (proves blob crypto +
     compression framing decode).

A failure here raises :class:`FormatDriftError`, which the orchestrator turns
into a hard refusal to burn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lcsas.restore.restic_fallback import IntegrityError, PurePythonRestorer

_logger = logging.getLogger(__name__)

# Frozen contract: the repository format versions every recovery tier
# (tier-1 C reader, tier-2 pinned rustic, tier-3 PurePythonRestorer) can
# decode.  v1 and v2 only.  Bumping this is a deliberate cross-tier change,
# not an operator-side rustic upgrade — see docs/RESTIC_FORMAT_SPEC.md.
SUPPORTED_REPO_VERSIONS = (1, 2)


class FormatDriftError(Exception):
    """The mirror cannot be proven restorable by the pinned recovery readers.

    Raised by :func:`check_repo_recoverable` when the repository format has
    drifted past what the shipped restore tiers understand, when no password
    is available to prove decodability, or when the in-process decode proof
    itself fails.  The orchestrator refuses to burn on this error.
    """


def check_repo_recoverable(mirror_path: Path, password_file: Path) -> None:
    """Raise :class:`FormatDriftError` unless *mirror_path* decodes with the
    pinned readers.

    Args:
        mirror_path: Root of the Rustic mirror repository (the directory
            containing ``config``, ``keys/``, ``index/``, ``data/``).
        password_file: File whose first line is the repository password.

    Raises:
        FormatDriftError: config version unsupported, an unknown compression
            framing byte was sampled, the password cannot unlock the repo, or
            the index/blob decode proof failed.
    """
    if not password_file.exists():
        # Defensive: the orchestrator already gates on a configured
        # password_file, but a configured-yet-missing file must also fail
        # loud rather than blow up with an opaque FileNotFoundError.
        raise FormatDriftError(
            f"password_file does not exist: {password_file} — cannot prove "
            f"discs burned from {mirror_path} will be restorable."
        )

    restorer = PurePythonRestorer(
        mirror_path,
        password_file=password_file,
        interactive=False,
    )

    # Step 0 — the password must unlock the master key.  Without it we cannot
    # decrypt config/index/blobs at all, so we cannot prove anything.
    if not restorer.verify_key():
        raise FormatDriftError(
            f"repository at {mirror_path} could not be unlocked with the "
            f"configured password_file ({password_file}). Discs burned now "
            f"cannot be proven restorable. Check the password_file."
        )

    # Step 1 — config version must be a frozen-contract version.
    version = _read_config_version(restorer, mirror_path)
    if version not in SUPPORTED_REPO_VERSIONS:
        raise FormatDriftError(
            f"repository at {mirror_path} is format version {version}; the "
            f"pinned recovery readers support versions "
            f"{'-'.join(str(v) for v in SUPPORTED_REPO_VERSIONS)}. Discs "
            f"burned now would be unreadable by every bundled restore tier. "
            f"Refusing to burn. See docs/RESTIC_FORMAT_SPEC.md."
        )

    # Steps 2 & 3 — load the index and decode one blob end-to-end (crypto +
    # MAC + content hash + compression framing).  An empty repo (no index or
    # no blobs yet) cannot be decode-proven, but its version is already known
    # good, so we proceed and log that the byte-level proof was skipped.
    try:
        restorer._load_index()
    except FileNotFoundError:
        _logger.info(
            "repo at %s has no index yet — decode proof skipped (version "
            "%s is a supported format)", mirror_path, version,
        )
        return
    except (IntegrityError, ValueError, KeyError, OSError) as exc:
        raise FormatDriftError(
            f"repository at {mirror_path} (version {version}) has an index "
            f"the pinned readers cannot decode: {exc}. This usually means an "
            f"unknown compression or crypto framing — discs burned now would "
            f"be unreadable by every bundled restore tier. Refusing to burn. "
            f"See docs/RESTIC_FORMAT_SPEC.md."
        ) from exc

    blob_index = restorer._blob_index
    if not blob_index:
        _logger.info(
            "repo at %s has no blobs yet — blob decode proof skipped "
            "(version %s + index decoded cleanly)", mirror_path, version,
        )
        return

    sample_blob_id = next(iter(blob_index))
    try:
        restorer._read_blob(sample_blob_id)
    except (IntegrityError, ValueError, KeyError, OSError) as exc:
        raise FormatDriftError(
            f"repository at {mirror_path} (version {version}) has a blob the "
            f"pinned readers cannot decode: {exc}. This usually means an "
            f"unknown compression framing — discs burned now would be "
            f"unreadable by every bundled restore tier. Refusing to burn. "
            f"See docs/RESTIC_FORMAT_SPEC.md."
        ) from exc

    _logger.info(
        "format preflight OK for %s: config version %s, index + blob "
        "decoded by the pinned readers", mirror_path, version,
    )


def _read_config_version(restorer: PurePythonRestorer, mirror_path: Path) -> int:
    """Decrypt + parse the repo ``config`` and return its ``version`` as int.

    Raises FormatDriftError if the config is absent, undecryptable, or carries
    a non-integer version.
    """
    import json

    config_path = mirror_path / "config"
    if not config_path.is_file():
        raise FormatDriftError(
            f"repository at {mirror_path} has no 'config' file — cannot prove "
            f"its format version. Refusing to burn."
        )
    try:
        config_doc = json.loads(restorer._decrypt_file(config_path))
    except (IntegrityError, ValueError, OSError) as exc:
        raise FormatDriftError(
            f"repository config at {config_path} could not be decoded: {exc}. "
            f"Refusing to burn."
        ) from exc

    raw_version = config_doc.get("version")
    if not isinstance(raw_version, int):
        raise FormatDriftError(
            f"repository config at {config_path} has a non-integer version "
            f"{raw_version!r}; the pinned readers support versions "
            f"{'-'.join(str(v) for v in SUPPORTED_REPO_VERSIONS)}. "
            f"Refusing to burn."
        )
    return raw_version
