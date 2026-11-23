"""Restore orchestration: the steps between a pick list and a restored tree.

The ``lcsas restore`` handlers in :mod:`lcsas.cli.commands.restore` parse
arguments and call this module.  It decides the restore order, retries
packs from alternate discs, asks for mount paths and prunes the cache, and
reports progress through the ``lcsas`` logger.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lcsas.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from lcsas.config.settings import LCSASConfig
    from lcsas.restore.planner import PickListV2
    from lcsas.rustic.types import RestorePlan
    from lcsas.rustic.wrapper import RusticRunner

# The restore steps import RestoreExecutor, RestorePlanner,
# SubprocessRusticRunner and the like inside each function, not here:
# callers (and the tests) patch them in their home modules.

logger = get_logger()


Prompt = Callable[[str], str]
"""Asks one question on the terminal and returns the reply (``input``)."""


def _ask(prompt: Prompt | None, text: str) -> str:
    """Ask *text* through *prompt*, or through ``input`` when it is None.

    ``input`` is looked up at call time, not bound as a default, so a
    caller that replaces ``builtins.input`` (the tests do) still answers.
    """
    return (prompt if prompt is not None else input)(text)


def _is_tty(stdin_is_tty: Callable[[], bool] | None) -> bool:
    """Whether a person can answer prompts (``sys.stdin.isatty()``)."""
    return stdin_is_tty() if stdin_is_tty is not None else sys.stdin.isatty()


@dataclass(frozen=True)
class PlannedRestore:
    """What a restore from the mirror's catalog needs before any disc is read.

    ``plan`` lists the packs the snapshot needs (``packs_for_snapshot``;
    ``plan.snapshot_id`` is the full ID it resolved to),
    ``pick_list`` says which discs hold them, and ``iso_shas`` maps each
    picked disc label to its recorded ISO SHA-256 (``prepare_restore``
    only).
    """

    plan: RestorePlan
    pick_list: PickListV2
    runner: RusticRunner
    mirror_path: Path
    iso_shas: dict[str, str | None] = field(default_factory=dict)


def plan_restore(
    config: LCSASConfig,
    conn: sqlite3.Connection,
    *,
    repo_name: str,
    snapshot_id: str,
) -> PlannedRestore | None:
    """Build the pick list ``lcsas restore plan`` shows.

    Uses the repository's configured password file.  Logs the reason and
    returns None when the repository cannot be read or has no such
    snapshot.  Needs no rustic: the snapshot's trees are walked with the
    Python reader (#948).
    """
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    # Resolve repo config
    if repo_name not in config.repositories:
        logger.error(f"repository '{repo_name}' not found in config.")
        logger.error(f"  Available: {', '.join(config.repositories.keys())}")
        return None

    repo_cfg = config.repositories[repo_name]
    if repo_cfg.password_file is None:
        logger.error(
            "Repository '%s' has no password_file configured.", repo_name
        )
        return None

    # Which packs the snapshot needs: a walk of its trees in the mirror
    # (#948).  No rustic is needed to plan.
    from lcsas.packs.snapshot_coverage import (
        SnapshotPacksError,
        packs_for_snapshot,
    )
    try:
        plan = packs_for_snapshot(
            repo_cfg.mirror_path, repo_cfg.password_file, snapshot_id,
        )
    except SnapshotPacksError as exc:
        logger.error("%s", exc)
        return None

    # Generate pick list with alternate-volume info (RST-07).
    planner = RestorePlanner(conn)
    pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)
    runner = SubprocessRusticRunner(tmpdir=config.staging_path)
    return PlannedRestore(plan, pick_list, runner, repo_cfg.mirror_path)


def prepare_restore(
    config: LCSASConfig,
    conn: sqlite3.Connection,
    *,
    repo_name: str,
    snapshot_id: str,
    password_file: Path,
) -> PlannedRestore | None:
    """Plan ``lcsas restore exec``: the pick list plus each disc's ISO hash.

    Everything that needs the catalog happens here, so the caller can
    close it before the (long) disc-swapping part in :func:`run_restore`.
    Logs the reason and returns None when the restore cannot start.
    """
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    if repo_name not in config.repositories:
        logger.error(f"repository '{repo_name}' not found in config.")
        return None

    repo_cfg = config.repositories[repo_name]

    # Early validation: password file must exist before we attempt rustic.
    if not password_file.exists():
        logger.error(
            "Password file not found: %s\n"
            "Provide the correct path with --password-file.",
            password_file,
        )
        return None

    from lcsas.exceptions import BinaryError
    from lcsas.utils.subprocess import check_binary_version
    try:
        check_binary_version("rustic", min_version=(0, 9, 0))
    except BinaryError as exc:
        logger.error("%s", exc)
        return None

    runner = SubprocessRusticRunner(tmpdir=config.staging_path)

    # Which packs the snapshot needs (#948).  The plan carries the full
    # ID that 'latest' or a prefix resolved to; rustic restores that one.
    from lcsas.packs.snapshot_coverage import (
        SnapshotPacksError,
        packs_for_snapshot,
    )
    try:
        plan = packs_for_snapshot(
            repo_cfg.mirror_path, password_file, snapshot_id,
        )
    except SnapshotPacksError as exc:
        logger.error("%s", exc)
        return None

    # Generate pick list with alternates for resilient restore
    planner = RestorePlanner(conn)
    pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)

    # Phase 21.6: capture per-volume ISO SHA-256 hashes while
    # the catalog conn is still open so the ingest loop in
    # run_restore can pass them to ingest_volume.  When the owner's
    # vol_dir holds the original .iso files next to the
    # extracted contents, ingest_volume will SHA-verify each
    # one before reading its packs — same protection as
    # dvdisaster on Linux, no toolchain required.
    from lcsas.db.volume_copies import get_iso_sha256_for_label
    iso_shas: dict[str, str | None] = {
        label: get_iso_sha256_for_label(conn, label)
        for label in pick_list.volumes
    }
    return PlannedRestore(
        plan, pick_list, runner, repo_cfg.mirror_path, iso_shas,
    )


def run_restore(
    planned: PlannedRestore,
    *,
    staging_path: Path,
    snapshot_id: str,
    target_path: Path,
    password_file: Path,
    cache_dir: Path | None,
    volume_dir: Path | None,
    iso_dir: Path | None,
    skip_verify: bool,
    prompt: Prompt | None = None,
    stdin_is_tty: Callable[[], bool] | None = None,
) -> int:
    """Run ``lcsas restore exec`` from a :func:`prepare_restore` result.

    Collects the packs into a cache (from *volume_dir*, or by asking for
    each disc to be mounted), retries failed packs from alternate discs,
    checks the cache is complete and runs ``rustic restore`` into
    *target_path*.  Returns the exit code; raises
    :class:`~lcsas.restore.executor.PackCorruptionError` when packs
    cannot be retrieved from any disc.
    """
    import tempfile

    from lcsas.restore.executor import RestoreExecutor
    from lcsas.utils.fs import ensure_dir

    plan = planned.plan
    pick_list = planned.pick_list
    runner = planned.runner
    iso_shas = planned.iso_shas

    if pick_list.deprecated_disc_labels:
        logger.warning(
            "%d pack(s) are only on DEPRECATED/DESTROYED volumes: %s",
            sum(len(v) for v in pick_list.deprecated_disc_labels.values()),
            ", ".join(sorted(pick_list.deprecated_disc_labels)),
        )

    for label, hashes in sorted(pick_list.unconfirmed_volume_labels.items()):
        logger.warning(
            "WARNING: volume %s was staged but has no record of ever being "
            "burned. If you cannot find this disc, it may never have "
            "existed. The %d pack(s) it lists may only exist on the "
            "mirror.",
            label, len(hashes),
        )

    if pick_list.missing_packs:
        logger.error(f"{len(pick_list.missing_packs)} required packs not "
                     f"found in any volume.")
        return 1

    # Build alternates lookup: pack ID -> [alt_labels]
    alternates_map, primary_labels = _alternates_for(pick_list)

    # ── Disk-space preflight [FMA-09] ──────────────────────────────
    # Refuse (or hard-confirm) BEFORE the first disc prompt: an heir
    # restoring onto a too-small disk must learn that NOW, not via
    # ENOSPC after a long disc-swapping session.  The pack cache
    # needs ~total_bytes too — packs accumulate there before rustic
    # restore runs — so its filesystem is checked when distinct.
    cache_base = (
        cache_dir if cache_dir is not None else staging_path
    )
    shortfalls = free_space_shortfalls(
        pick_list.total_bytes, target_path, cache_base,
    )
    if shortfalls:
        need_gb = pick_list.total_bytes / 1e9
        for short_path, free_bytes in shortfalls:
            logger.error(
                "Restoring ~%.1f GB but %s has only %.1f GB free.",
                need_gb, short_path, free_bytes / 1e9,
            )
        if not _is_tty(stdin_is_tty):
            logger.error(
                "Refusing to start: free up space or choose a different "
                "restore folder / --cache-dir, then re-run."
            )
            return 1
        ans = _ask(prompt, "Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            logger.error("Aborted: free up space and re-run.")
            return 1

    # Set up cache directory
    _tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    if cache_dir is None:
        staging_path.mkdir(parents=True, exist_ok=True)
        _tmp_dir = tempfile.TemporaryDirectory(
            prefix="lcsas-restore-", dir=str(staging_path)
        )
        cache_dir = Path(_tmp_dir.name)
    ensure_dir(cache_dir)

    from lcsas.utils.shutdown import ShutdownManager
    shutdown = ShutdownManager()
    if _tmp_dir is not None:
        shutdown.register(_tmp_dir.cleanup)
    shutdown.install()

    try:
        # FMT-01: prefer real dvdisaster, else the in-house lcsas-ecc
        # (decode-only) so restore can verify/repair RS03 parity on a
        # host without dvdisaster.  None → executor degrades to SHA-256.
        from lcsas.ecc.dvdisaster import select_ecc_runner
        executor = RestoreExecutor(runner, ecc_runner=select_ecc_runner())

        # Prepare cache with repository metadata from the mirror
        metadata_source = planned.mirror_path
        executor.prepare_cache(cache_dir, metadata_source)

        logger.info(f"Restore cache: {cache_dir}")
        logger.info(f"Need packs from {len(pick_list.volumes)} volumes")

        all_failed: list[str] = []  # packs that failed on primary volume

        # Ingest packs from volumes
        if volume_dir:
            # Non-interactive: all volume data is pre-extracted in one directory
            vol_dir = volume_dir
            for label, sources in pick_list.volumes.items():
                pack_hashes = [s.pack.sha256 for s in sources]
                vol_path = vol_dir / label
                if not vol_path.is_dir():
                    vol_path = vol_dir
                # Phase 21.6: if a sibling <label>.iso file sits next to
                # the extracted contents, hand it (and the recorded
                # hash) to ingest_volume so the verify-or-die check
                # fires before any pack is copied out.
                iso_path = find_sibling_iso(vol_dir, label)
                result = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                    verify=not skip_verify,
                    collect_failures=True,
                    iso_path=iso_path,
                    expected_sha256=iso_shas.get(label),
                )
                logger.info(f"  {label}: ingested {result.ingested} packs")
                if result.failed:
                    logger.warning(
                        f"  {label}: {len(result.failed)} packs failed verification"
                    )
                    all_failed.extend(result.failed)

            # Retry failed packs from alternate volumes.  Drop packs the
            # cache already holds first: never fail a restore whose cache is
            # already complete (RST-01 — keeps the invariant uniform
            # across the disc-only and config-driven restore paths).
            all_failed = prune_recovered(cache_dir, all_failed)
            if all_failed:
                logger.info(f"\nRetrying {len(all_failed)} failed packs "
                            f"from alternate volumes...")
                still_failed = retry_from_alternates_batch(
                    executor, cache_dir, vol_dir,
                    all_failed, alternates_map,
                    verify=not skip_verify,
                    iso_shas=iso_shas,
                )
                _raise_if_unrecovered(cache_dir, pick_list, still_failed)
        else:
            # Interactive: prompt the owner to mount a disc of each volume
            if not _is_tty(stdin_is_tty):
                logger.error(
                    "Interactive restore requires a TTY. "
                    "Use --volume-dir to specify a directory of pre-extracted "
                    "volume data for non-interactive (scripted) restores."
                )
                return 1
            for label, sources in sorted(pick_list.volumes.items()):
                pack_hashes = [s.pack.sha256 for s in sources]
                while True:
                    mount_path = _ask(
                        prompt,
                        f"\nMount disc '{label}' and enter mount path "
                        f"(or 'skip' to skip): ",
                    ).strip()
                    if mount_path.lower() == "skip":
                        logger.info(f"  Skipping {label}")
                        # RST-07: enqueue the skipped disc's packs so the
                        # alternates retry can offer other volumes.
                        all_failed.extend(pack_hashes)
                        break
                    vol_path = Path(mount_path)
                    if not vol_path.is_dir():
                        logger.info(f"  '{mount_path}' is not a directory, try again.")
                        continue
                    # Phase 21.9: when --iso-dir is supplied, probe it
                    # for <label>.iso so the verify-or-die check fires
                    # for the interactive path too.
                    iso_path = (
                        find_sibling_iso(iso_dir, label)
                        if iso_dir is not None
                        else None
                    )
                    result = executor.ingest_volume(
                        cache_dir, vol_path, pack_hashes,
                        verify=not skip_verify,
                        collect_failures=True,
                        iso_path=iso_path,
                        expected_sha256=iso_shas.get(label),
                    )
                    logger.info(f"  Ingested {result.ingested} packs from {label}")
                    if result.failed:
                        logger.warning(
                            f"  {len(result.failed)} packs failed verification"
                        )
                        all_failed.extend(result.failed)
                    break

            # Interactive retry for failed packs
            all_failed = prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = retry_from_alternates_interactive(
                    executor, cache_dir,
                    all_failed, alternates_map,
                    verify=not skip_verify,
                    iso_dir=iso_dir,
                    iso_shas=iso_shas,
                    primary_labels=primary_labels,
                    prompt=prompt,
                )
                _raise_if_unrecovered(cache_dir, pick_list, still_failed)

        # ── Post-ingest completeness check ──────────────────────────
        # Check every required pack was actually ingested before
        # running rustic, which would fail with an opaque error.
        all_required = plan.required_pack_hashes
        missing = RestoreExecutor.verify_cache_completeness(
            cache_dir, all_required,
        )
        if missing:
            logger.error(
                f"\n{len(missing)} of {len(all_required)} required packs "
                f"missing from cache after ingestion."
            )
            report_missing_by_label(pick_list, missing)
            logger.error(
                "\nRestore cannot proceed — mount the missing discs "
                "and retry, or check for damaged discs."
            )
            return 1

        # Execute restore: the snapshot the plan resolved, so rustic
        # restores the one whose packs were collected (#948).
        target = target_path.resolve()
        logger.info(f"\nRestoring snapshot {plan.snapshot_id} → {target}")
        executor.execute_restore(
            cache_dir=cache_dir,
            snapshot_id=plan.snapshot_id,
            target_path=target,
            password_file=password_file,
        )
        logger.info("Restore complete!")
    finally:
        # Cleanup temporary cache
        if _tmp_dir is not None:
            _tmp_dir.cleanup()
        shutdown.uninstall()

    return 0


def restore_from_disc(
    disc_path: Path,
    target_path: Path,
    password_file: Path,
    *,
    catalog: Path | None = None,
    repo: str | None = None,
    snapshot: str = "latest",
    cache_dir: Path | None = None,
    volume_dir: Path | None = None,
    skip_verify: bool = False,
    prompt: Prompt | None = None,
    stdin_is_tty: Callable[[], bool] | None = None,
) -> int:
    """Restore from optical discs without a config file or mirror.

    The work of ``lcsas restore from-disc``: a front for the Python
    reader (tier 3), whose own disc loop finds each pack (#948).  The
    repository's keys, index and snapshots are read from the disc at
    *disc_path*; packs are looked for on that disc and under
    *volume_dir* (a folder of discs, one per sub-folder); the disc's
    ``catalog.db`` (or *catalog*), opened read-only, names the disc a
    missing pack is on.  Without *volume_dir*, and with a person at the
    terminal, the reader asks for each further disc.

    *cache_dir* is the reader's pack cache: each disc's packs are copied
    there the first time the disc is read, so each disc is needed once.
    *skip_verify* has no effect: the reader authenticates every blob.

    No rustic is used: on a multi-disc archive the tree packs are on
    discs not yet read, so the packs cannot be planned before reading.

    Returns the exit code, as the bundled python_reader.py exits: 0 done,
    4 finished but some items not restored (#875), 74 the disk is full,
    75 stopped at a disc prompt, 77 a key file rejected the password
    (#805), 1 for any other failure.
    """
    import sqlite3

    from lcsas.db.disc_catalog import DiscCatalogError, open_disc_catalog

    if not disc_path.is_dir():
        logger.error("Disc path '%s' is not a directory.", disc_path)
        return 1

    if not password_file.exists():
        logger.error(
            "Password file not found: %s\n"
            "Provide the correct path with --password-file.",
            password_file,
        )
        return 1

    catalog_path = catalog if catalog else disc_path / "catalog.db"
    if not catalog_path.is_file():
        logger.error(
            "No catalog.db found at '%s'.\n"
            "Each LCSAS disc contains a catalog.db.  "
            "If this disc is damaged, try another disc from the same archive.",
            catalog_path,
        )
        return 1
    if volume_dir is not None and not volume_dir.is_dir():
        logger.error("--volume-dir '%s' is not a directory.", volume_dir)
        return 1

    # The disc is read-only (a mounted disc, or an extracted image whose
    # files are 0444): open its catalog read-only, never copy it (#948).
    try:
        disc_conn = open_disc_catalog(catalog_path)
    except DiscCatalogError as exc:
        logger.error("%s", exc)
        return 1
    try:
        repo_rows = disc_conn.execute(
            "SELECT repo_id, name, mirror_path FROM repositories"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error(
            "Cannot read the repositories in the disc catalog %s: %s.\n"
            "The disc may be damaged: try another disc from the same "
            "archive.",
            catalog_path, exc,
        )
        return 1
    finally:
        disc_conn.close()

    if not repo_rows:
        logger.error(
            "Disc catalog contains no repositories.\n"
            "This may not be a valid LCSAS disc."
        )
        return 1

    repo_row = _select_disc_repo(repo_rows, repo)
    if repo_row is None:
        return 1
    repo_name = repo_row["name"]

    disc_meta = _find_disc_metadata(disc_path, repo_row)
    if disc_meta is None:
        return 1
    snap_dir = disc_meta / "snapshots"
    if not snap_dir.is_dir() or not any(snap_dir.iterdir()):
        logger.error(
            "This disc carries no snapshot of repository '%s' (%s is "
            "empty).\nA disc carries only the snapshots whose packs were "
            "all on discs when it was staged: use a newer disc of the "
            "archive.",
            repo_name, snap_dir,
        )
        return 1
    logger.info("Found metadata for '%s' on disc.", repo_name)
    if skip_verify:
        logger.info(
            "--skip-verify has no effect: the Python reader checks every "
            "blob it reads."
        )

    search = [disc_path]
    if volume_dir is not None:
        search.append(volume_dir)
    interactive = volume_dir is None and _is_tty(stdin_is_tty)
    return _restore_with_python_reader(
        disc_meta,
        target_path,
        password_file,
        snapshot,
        search_paths=search,
        catalog_path=catalog_path,
        pack_cache_dir=cache_dir,
        interactive=interactive,
        prompt=prompt,
    )


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
    computed once in prepare_restore from the catalog.  When
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
    prompt: Prompt | None = None,
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

    ``prompt`` asks for each mount path (``input`` when None).
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
        mount_path = _ask(
            prompt, f"Mount '{alt_label}' and enter path (or 'skip'): "
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


def _alternates_for(
    pick_list: PickListV2,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Map each picked pack to its alternate discs and to its primary disc.

    Returns ``(alternates_map, primary_labels)``: pack ID -> the other
    discs that hold it (only packs that have some), and pack ID -> the
    disc it was picked from.  The alternate retries take both.  [RST-07]
    """
    alternates_map: dict[str, list[str]] = {}
    primary_labels: dict[str, str] = {}
    for label, sources in pick_list.volumes.items():
        for src in sources:
            primary_labels[src.pack.sha256] = label
            if src.alternates:
                alternates_map[src.pack.sha256] = list(src.alternates)
    return alternates_map, primary_labels


def _raise_if_unrecovered(
    cache_dir: Path, pick_list: PickListV2, still_failed: list[str],
) -> None:
    """Raise when packs that failed every disc are still not in the cache.

    The error names the discs that hold them (see
    :func:`disc_summary_for_packs`).  Packs the cache already holds are
    dropped first (RST-01).
    """
    from lcsas.restore.executor import PackCorruptionError

    still_failed = prune_recovered(cache_dir, still_failed)
    if still_failed:
        where = disc_summary_for_packs(pick_list, still_failed)
        raise PackCorruptionError(
            f"{len(still_failed)} pack(s) could not be "
            f"retrieved.{where}"
        )


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


def _select_disc_repo(
    repo_rows: list[sqlite3.Row], repo: str | None,
) -> sqlite3.Row | None:
    """The disc catalog's repository row to restore: *repo*, or the only one.

    Logs why and returns None when *repo* is not there, or when there
    are several and none was named.
    """
    if repo:
        repo_row = next((r for r in repo_rows if r["name"] == repo), None)
        if repo_row is None:
            names = [r["name"] for r in repo_rows]
            logger.error(
                "Repository '%s' not found in disc catalog.  Available: %s",
                repo, ", ".join(names),
            )
            return None
    elif len(repo_rows) == 1:
        repo_row = repo_rows[0]
        logger.info("Repository: %s", repo_row["name"])
    else:
        logger.error(
            "Multiple repositories found in disc catalog — "
            "use --repo NAME to select one.\nAvailable: %s",
            ", ".join(r["name"] for r in repo_rows),
        )
        return None
    return repo_row


def _find_disc_metadata(disc_path: Path, repo_row: sqlite3.Row) -> Path | None:
    """The repository's metadata directory on the disc, or None (logged)."""
    repo_name = repo_row["name"]
    # Locate metadata on the disc.  The injector writes it as
    # metadata/<repo_id>/ (#743); a repository added with `repo add`
    # has a UUID id, so the name alone does not find it.  The name
    # is still tried for a repository whose id equals its name.
    disc_meta: Path = disc_path / "metadata" / repo_row["repo_id"]
    if not disc_meta.is_dir():
        by_name = disc_path / "metadata" / repo_name
        if by_name.is_dir():
            disc_meta = by_name
    if not disc_meta.is_dir():
        logger.error(
            "Metadata for repository '%s' not found on disc.\n"
            "Expected at: %s\n"
            "This disc may be damaged or from a different archive.",
            repo_name, disc_meta,
        )
        return None
    return disc_meta


def _restore_with_python_reader(
    repo_meta: Path,
    target_path: Path,
    password_file: Path,
    snapshot: str,
    *,
    search_paths: list[Path],
    catalog_path: Path | None,
    pack_cache_dir: Path | None,
    interactive: bool,
    prompt: Prompt | None,
) -> int:
    """Restore with the Python reader (tier 3), as ``restore.sh`` does.

    *repo_meta* is the repository's keys/index/snapshots folder on the
    disc; packs are found under *search_paths* (discs, or folders of
    discs one level down).  Returns the exit code, as the bundled
    python_reader.py exits: 0 done, 4 finished but some items not
    restored (#875), 74 the disk is full, 75 stopped at a disc prompt,
    77 a key file rejected the password (#805), 1 for any other failure.
    """
    from lcsas.restore.restic_fallback import (
        EXIT_NOT_ALL_RESTORED,
        EXIT_WRONG_PASSWORD,
        NoKeyUnlockedError,
        PurePythonRestorer,
        RestoreAbortedError,
        allow_openssl_command,
    )

    # The tier-3 restore may use the `openssl` command for AES
    # (#774), as the standalone reader on the meta disc does.
    allow_openssl_command()

    prompt_in: Callable[[], str] | None = None
    if prompt is not None:
        def prompt_in() -> str:
            # The reader wants one line, "" at the end of input.
            try:
                return _ask(prompt, "") + "\n"
            except EOFError:
                return ""

    target = target_path.resolve()
    snap_arg = None if snapshot == "latest" else snapshot
    logger.info(
        "Restoring with the Python reader; looking for packs in: %s",
        ", ".join(str(p) for p in search_paths),
    )
    try:
        restorer = PurePythonRestorer(
            repo_path=repo_meta,
            password_file=password_file,
            pack_search_paths=search_paths,
            interactive=interactive,
            catalog_path=catalog_path,
            pack_cache_dir=pack_cache_dir,
            prompt_in=prompt_in,
        )
        meta = restorer.restore(target=target, snapshot_id=snap_arg)
    except RestoreAbortedError as exc:
        # q (or the end of input) at the disc prompt exits 75, a
        # full disk 74, as the bundled python_reader.py does
        # (#658).
        logger.error("Python reader restore stopped: %s", exc)
        return exc.exit_code
    except NoKeyUnlockedError as exc:
        # A key file rejected the password: exit 77, as the bundled
        # python_reader.py and tier 1 do (#805).  Key files that
        # are all unreadable are no password verdict: exit 1.
        if not exc.rejected:
            logger.error("Python reader restore failed: %s", exc)
            return 1
        logger.error("Python reader restore failed: wrong password: %s", exc)
        return EXIT_WRONG_PASSWORD
    except Exception as exc:
        # Setup-level failure (a snapshot that is not there, a damaged
        # index): fail fast.  Not a password verdict.
        logger.error("Python reader restore failed: %s", exc)
        return 1
    if restorer.failures:
        # Tolerant traversal could not restore one or more items;
        # the bulk of the data is restored and a manifest lists
        # the rest (special files alone do not end up here, #783).
        logger.error(
            "Python reader restore finished with %d item(s) not "
            "restored. "
            "See %s/RESTORE_FAILURES.txt; the rest of your data is "
            "intact. Re-run with the missing discs at hand (--volume-dir, "
            "or answer the disc prompt), or from an undamaged disc of the "
            "affected volume.",
            restorer.failures, target,
        )
        return EXIT_NOT_ALL_RESTORED
    logger.info(
        "Restore complete (Python reader). Snapshot: %s, hostname: %s",
        meta.snapshot_id, meta.hostname,
    )
    return 0
