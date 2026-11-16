"""Restore orchestration: the steps between a pick list and a restored tree.

The ``lcsas restore`` handlers in :mod:`lcsas.cli.commands.restore` parse
arguments and call this module.  It decides the restore order, retries
packs from alternate discs, asks for mount paths and prunes the cache, and
reports progress through the ``lcsas`` logger.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lcsas.log import get_logger

logger = get_logger()


def find_sibling_iso(vol_dir: Path, label: str) -> Path | None:
    """Locate ``<label>.iso`` near a pre-extracted volume directory.

    Phase 21.6 helper.  When a stack of LCSAS ISOs is extracted
    into a single directory (the ``--volume-dir`` mode), the original
    ``.iso`` files often live alongside the extracted trees:

        my_recovery/
        ├── LCSAS_BD25_2026_0001/         ← extracted contents
        ├── LCSAS_BD25_2026_0001.iso      ← original ISO
        ├── LCSAS_BD25_2026_0002/
        └── LCSAS_BD25_2026_0002.iso

    Or, less commonly, the ISO might sit inside the per-label tree:

        my_recovery/LCSAS_BD25_2026_0001/LCSAS_BD25_2026_0001.iso

    Returns the first existing candidate path, or ``None`` if no ISO
    is reachable — in which case the caller skips the integrity check
    (degraded but not fatal — packs still get hash-checked
    individually during copy).
    """
    candidates = (
        vol_dir / f"{label}.iso",
        vol_dir / label / f"{label}.iso",
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


def nearest_existing_dir(path: Path) -> Path:
    """Walk up from ``path`` to the closest ancestor that exists.

    ``shutil.disk_usage`` / ``os.stat`` need an existing path; restore
    targets and cache dirs frequently don't exist yet.  [FMA-09]
    """
    p = path.resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def fs_dev(path: Path) -> int:
    """Filesystem device id for ``path`` (nearest existing ancestor)."""
    return os.stat(nearest_existing_dir(path)).st_dev


def free_space_shortfalls(
    required_bytes: int, target: Path, cache_base: Path,
) -> list[tuple[Path, int]]:
    """Return ``(path, free_bytes)`` for each filesystem short on space.

    Checks the restore target, plus the pack-cache base when it lives
    on a different filesystem — packs accumulate to ~``required_bytes``
    in the cache before ``rustic restore`` runs.  [FMA-09]
    """
    import shutil

    shortfalls: list[tuple[Path, int]] = []
    target_free = shutil.disk_usage(nearest_existing_dir(target)).free
    if target_free < required_bytes:
        shortfalls.append((target, target_free))
    if fs_dev(cache_base) != fs_dev(target):
        cache_free = shutil.disk_usage(nearest_existing_dir(cache_base)).free
        if cache_free < required_bytes:
            shortfalls.append((cache_base, cache_free))
    return shortfalls


def retry_from_alternates_batch(
    executor: Any,
    cache_dir: Path,
    vol_dir: Path,
    failed_packs: list[str],
    alternates_map: dict[str, list[str]],
    *,
    verify: bool = True,
    iso_shas: dict[str, str | None] | None = None,
) -> list[str]:
    """Retry failed packs from alternate volumes (non-interactive).

    Returns list of packs that could not be retrieved.

    Phase 21.7: ``iso_shas`` is the same per-label SHA-256 dict
    computed once in cmd_restore_exec from the catalog.  When
    supplied, each alternate volume's ISO is integrity-checked
    (against the sibling .iso file, if present) before its packs
    are read — so a corrupt alternate that would otherwise silently
    spread bad packs into the cache is rejected up front, with the
    standard "try another location" hint.
    """
    remaining = list(failed_packs)

    # Group by alternate volume to minimise disc access
    for alt_label in collect_alt_labels(remaining, alternates_map):
        packs_on_alt = [
            sha for sha in remaining
            if alt_label in alternates_map.get(sha, [])
        ]
        if not packs_on_alt:
            continue

        vol_path = vol_dir / alt_label
        if not vol_path.is_dir():
            logger.warning(
                "Alternate volume directory not found: %s — skipping %s",
                vol_path, alt_label,
            )
            vol_path = vol_dir
            if not vol_path.is_dir():
                continue

        alt_iso = find_sibling_iso(vol_dir, alt_label)
        alt_sha = iso_shas.get(alt_label) if iso_shas else None
        result = executor.ingest_volume(
            cache_dir, vol_path, packs_on_alt,
            verify=verify, collect_failures=True,
            iso_path=alt_iso, expected_sha256=alt_sha,
        )
        if result.ingested:
            logger.info(f"  Retrieved {result.ingested} packs from {alt_label}")
        recovered = set(packs_on_alt) - set(result.failed)
        remaining = [sha for sha in remaining if sha not in recovered]

    return remaining


def retry_from_alternates_interactive(
    executor: Any,
    cache_dir: Path,
    failed_packs: list[str],
    alternates_map: dict[str, list[str]],
    *,
    verify: bool = True,
    iso_dir: Path | None = None,
    iso_shas: dict[str, str | None] | None = None,
    primary_labels: dict[str, str] | None = None,
) -> list[str]:
    """Retry failed packs from alternate volumes (interactive).

    Prompts for a disc of each alternate volume. Returns packs still missing.

    Phase 21.9: ``iso_dir`` + ``iso_shas`` activate the same SHA-256
    verify-or-die check that the --volume-dir path gets.  When
    both are supplied, the alternate volume's <label>.iso under
    ``iso_dir`` is hashed against the catalog-recorded SHA before its
    packs are read.  Defaults are None for backward-compat — direct
    callers without iso info continue to work unchanged.

    RST-07: ``primary_labels`` maps each pack ID to the disc it was
    originally assigned to (the one skipped/failed), so the prompt can
    explain *why* the alternate is being offered.
    """
    remaining = list(failed_packs)
    primary_labels = primary_labels or {}

    for alt_label in collect_alt_labels(remaining, alternates_map):
        packs_on_alt = [
            sha for sha in remaining
            if alt_label in alternates_map.get(sha, [])
        ]
        if not packs_on_alt:
            continue

        skipped = sorted({
            primary_labels[sha] for sha in packs_on_alt
            if sha in primary_labels and primary_labels[sha] != alt_label
        })
        if skipped:
            logger.info(
                "\nDisc(s) %s were skipped/failed; %d of their packs are "
                "also on '%s'.",
                ", ".join(skipped), len(packs_on_alt), alt_label,
            )
        else:
            logger.info(f"\n{len(packs_on_alt)} failed packs may be on "
                         f"alternate volume '{alt_label}'")
        mount_path = input(
            f"Mount '{alt_label}' and enter path (or 'skip'): "
        ).strip()
        if mount_path.lower() == "skip":
            continue
        vol_path = Path(mount_path)
        if not vol_path.is_dir():
            logger.info(f"  '{mount_path}' not a directory, skipping")
            continue

        alt_iso = (
            find_sibling_iso(iso_dir, alt_label)
            if iso_dir is not None else None
        )
        alt_sha = iso_shas.get(alt_label) if iso_shas else None
        result = executor.ingest_volume(
            cache_dir, vol_path, packs_on_alt,
            verify=verify, collect_failures=True,
            iso_path=alt_iso, expected_sha256=alt_sha,
        )
        if result.ingested:
            logger.info(f"  Retrieved {result.ingested} packs from {alt_label}")
        recovered = set(packs_on_alt) - set(result.failed)
        remaining = [sha for sha in remaining if sha not in recovered]

    return remaining


def collect_alt_labels(
    packs: list[str], alternates_map: dict[str, list[str]]
) -> list[str]:
    """Collect unique alternate labels covering the given packs."""
    seen: set[str] = set()
    labels: list[str] = []
    for sha in packs:
        for alt in alternates_map.get(sha, []):
            if alt not in seen:
                seen.add(alt)
                labels.append(alt)
    return labels


def prune_recovered(cache_dir: Path, failed: list[str]) -> list[str]:
    """Drop packs already present + valid in the cache (deduped).

    The cache is the source of truth: a pack ingested from any volume is
    in hand no matter which disc reported it as "not found on this
    volume".  Filtering ``failed`` against the cache before any retry/raise
    is what prevents a multi-disc snapshot from falsely failing — packs
    that live on a *later* disc are seeded into ``failed`` by the initial
    ingest, then ingested for real later in the loop, but nothing else
    removes them.  Also heals the verify-on-A / ingest-from-B case.
    """
    if not failed:
        return []
    from lcsas.restore.executor import RestoreExecutor

    unique = list(dict.fromkeys(failed))
    return RestoreExecutor.verify_cache_completeness(cache_dir, unique)


def disc_summary_for_packs(pick_list: Any, packs: list[str]) -> str:
    """Human sentence naming primary disc(s) and alternate(s) for packs.

    Used in the raised-error path so a genuine failure names both the disc
    the packs were assigned to and any alternate that also holds them —
    not only opaque pack IDs.  [RST-07]
    """
    label_map = labels_for_packs(pick_list, packs)
    primaries = sorted({labels[0] for labels in label_map.values() if labels})
    alternates = sorted({
        alt for labels in label_map.values() for alt in labels[1:]
    })
    if not primaries:
        return ""
    msg = f" They live on disc(s): {', '.join(primaries)}."
    if alternates:
        msg += f" Other discs that also hold them: {', '.join(alternates)}."
    msg += " Re-mount those discs (check for damage) and retry."
    return msg


def labels_for_packs(
    pick_list: Any, hashes: list[str]
) -> dict[str, list[str]]:
    """Map each pack ID to ``[primary_label, *alternates]``.

    Works on a ``PickListV2`` (``volumes`` holds ``PackSource`` entries
    carrying ``.alternates``).  Packs not in any source come back absent
    from the result — the caller treats those as truly missing.  [RST-07]
    """
    wanted = set(hashes)
    out: dict[str, list[str]] = {}
    for label, sources in pick_list.volumes.items():
        for src in sources:
            sha = src.pack.sha256
            if sha not in wanted:
                continue
            labels = [label]
            labels.extend(
                alt for alt in getattr(src, "alternates", [])
                if alt != label
            )
            out[sha] = labels
    return out


def report_missing_by_label(pick_list: Any, missing: list[str]) -> None:
    """Log a completeness failure grouped by disc label, not by pack ID.

    Names the disc each missing pack lives on plus its alternates and any
    deprecated/destroyed sources, so the person restoring knows which discs
    to re-mount.  Raw pack IDs are demoted to debug level.  [RST-07]
    """
    label_map = labels_for_packs(pick_list, missing)

    # pack ID -> deprecated/destroyed labels that still hold it (physically
    # retrievable but not active in the catalog).
    depr_for: dict[str, list[str]] = {}
    for dlabel, dhashes in pick_list.deprecated_disc_labels.items():
        for sha in dhashes:
            depr_for.setdefault(sha, []).append(dlabel)

    # Group missing packs by primary disc.
    by_primary: dict[str, list[str]] = {}
    unknown: list[str] = []
    for sha in missing:
        labels = label_map.get(sha)
        if labels:
            by_primary.setdefault(labels[0], []).append(sha)
        else:
            unknown.append(sha)

    for primary in sorted(by_primary):
        shas = by_primary[primary]
        alts: list[str] = []
        depr: list[str] = []
        for sha in shas:
            alts.extend(label_map.get(sha, [])[1:])
            depr.extend(depr_for.get(sha, []))
        suffix = ""
        if alts:
            suffix += f" — alternates: {', '.join(sorted(set(alts)))}"
        if depr:
            suffix += (
                f" — also on DEPRECATED/DESTROYED disc(s): "
                f"{', '.join(sorted(set(depr)))}"
            )
        logger.error(
            "  still need disc %s (%d pack(s))%s", primary, len(shas), suffix
        )

    if unknown:
        depr_only: dict[str, list[str]] = {}
        for sha in unknown:
            for dlabel in depr_for.get(sha, []):
                depr_only.setdefault(dlabel, []).append(sha)
        for dlabel in sorted(depr_only):
            logger.error(
                "  packs only on DEPRECATED/DESTROYED disc %s (%d pack(s)) "
                "— locate it physically if possible",
                dlabel, len(depr_only[dlabel]),
            )
        truly_missing = [
            sha for sha in unknown if sha not in depr_for
        ]
        if truly_missing:
            logger.error(
                "  %d pack(s) are on no known disc at all",
                len(truly_missing),
            )

    for sha in missing:
        logger.debug("  missing pack: %s", sha)
