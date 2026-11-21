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

    ``plan`` is the rustic dry run (the packs the snapshot needs),
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
    returns None when the repository or rustic is not usable.
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

    # Get required pack IDs via rustic dry-run
    from lcsas.utils.subprocess import check_binary_version
    try:
        check_binary_version("rustic", min_version=(0, 9, 0))
    except RuntimeError as exc:
        logger.error("%s", exc)
        return None

    runner = SubprocessRusticRunner(tmpdir=config.staging_path)
    plan = runner.restore_dry_run(
        snapshot_id=snapshot_id,
        repo_path=repo_cfg.mirror_path,
        password_file=repo_cfg.password_file,
    )

    # Generate pick list with alternate-volume info (RST-07).
    planner = RestorePlanner(conn)
    pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)
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

    # Get required pack IDs
    plan = runner.restore_dry_run(
        snapshot_id=snapshot_id,
        repo_path=repo_cfg.mirror_path,
        password_file=password_file,
    )

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

        # Execute restore
        target = target_path.resolve()
        logger.info(f"\nRestoring snapshot {snapshot_id} → {target}")
        executor.execute_restore(
            cache_dir=cache_dir,
            snapshot_id=snapshot_id,
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

    The work of ``lcsas restore from-disc``.  Reads ``catalog.db`` (or
    *catalog*) and repository metadata directly from the disc at
    *disc_path*, then collects the packs from *volume_dir* or by asking
    for each disc to be mounted.  Falls back to the Python reader
    (tier 3) when rustic is not installed.  Returns the exit code;
    raises :class:`~lcsas.restore.executor.PackCorruptionError` when
    packs cannot be retrieved from any disc.
    """
    import shutil
    import tempfile

    from lcsas.db.connection import get_connection
    from lcsas.restore.executor import RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    if not disc_path.is_dir():
        logger.error("Disc path '%s' is not a directory.", disc_path)
        return 1

    # Early validation: password file must exist.
    if not password_file.exists():
        logger.error(
            "Password file not found: %s\n"
            "Provide the correct path with --password-file.",
            password_file,
        )
        return 1

    # Locate catalog.db on the disc
    catalog_path = catalog if catalog else disc_path / "catalog.db"
    if not catalog_path.is_file():
        logger.error(
            "No catalog.db found at '%s'.\n"
            "Each LCSAS disc contains a catalog.db.  "
            "If this disc is damaged, try another disc from the same archive.",
            catalog_path,
        )
        return 1

    # Use a temp directory for scratch files; keeps any --cache-dir separate.
    tmp_dir_obj = tempfile.TemporaryDirectory(prefix="lcsas-fromdisc-")
    try:
        from lcsas.utils.shutdown import ShutdownManager
        tmp_dir = Path(tmp_dir_obj.name)

        # Register cleanup on SIGINT/KeyboardInterrupt
        shutdown = ShutdownManager()
        shutdown.register(tmp_dir_obj.cleanup)
        shutdown.install()

        # Copy catalog.db to temp to avoid locking a mounted disc/ISO.
        tmp_catalog = tmp_dir / "catalog.db"
        shutil.copy2(str(catalog_path), str(tmp_catalog))

        disc_conn = get_connection(tmp_catalog)
        try:
            repo_rows = disc_conn.execute(
                "SELECT repo_id, name, mirror_path FROM repositories"
            ).fetchall()
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

        logger.info("Found metadata for '%s' on disc.", repo_name)

        # Build restore cache and populate it with disc metadata.
        cache_dir = cache_dir if cache_dir else (tmp_dir / "cache")
        ensure_dir(cache_dir)

        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import check_binary_version

        rustic_available = True
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except BinaryError:
            rustic_available = False

        runner = SubprocessRusticRunner(tmpdir=tmp_dir)
        # FMT-01: ECC verify/repair on restore — dvdisaster if present,
        # else the in-house lcsas-ecc fallback.
        from lcsas.ecc.dvdisaster import select_ecc_runner
        executor = RestoreExecutor(runner, ecc_runner=select_ecc_runner())

        logger.info("Copying repository metadata from disc to cache...")
        executor.prepare_cache(cache_dir, disc_meta)

        if not rustic_available:
            return _restore_with_python_reader(
                disc_path, cache_dir, target_path, password_file, snapshot,
            )

        # Resolve "latest" to an actual snapshot ID so dry-run gets an
        # explicit ID that rustic/restic can handle without ambiguity.
        snapshot_id = snapshot
        if snapshot_id == "latest":
            resolved = _resolve_latest(runner, cache_dir, password_file, repo_name)
            if resolved is None:
                return 1
            snapshot_id = resolved

        # Run rustic dry-run against the cache to determine required packs.
        logger.info(
            "Determining required packs for snapshot '%s'...", snapshot_id
        )
        try:
            plan = runner.restore_dry_run(
                snapshot_id=snapshot_id,
                repo_path=cache_dir,
                password_file=password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Install rustic (https://rustic.cli.rs/) or "
                "restic (https://restic.net/),\n"
                "or use the Python reader (tier 3) bundled on the disc:\n"
                "  python3 %s/python_reader.py "
                "--password-file %s --restore-folder %s",
                disc_path, password_file, target_path,
            )
            return 1
        except Exception as exc:
            logger.error(
                "Failed to determine required packs: %s\n"
                "Check the password file is correct and the disc's repository "
                "metadata is intact.",
                exc,
            )
            return 1

        logger.info(
            "Snapshot '%s' requires %d pack file(s).",
            snapshot_id, len(plan.required_pack_hashes),
        )

        # Look up which disc has which pack via the disc's catalog.
        disc_conn = get_connection(tmp_catalog)
        try:
            planner = RestorePlanner(disc_conn)
            pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)
        finally:
            disc_conn.close()

        if pick_list.missing_packs:
            missing_preview = ", ".join(pick_list.missing_packs[:5])
            overflow = (
                f" ... and {len(pick_list.missing_packs) - 5} more"
                if len(pick_list.missing_packs) > 5 else ""
            )
            deprecated_hint = ""
            if pick_list.deprecated_disc_labels:
                disc_list = ", ".join(sorted(pick_list.deprecated_disc_labels))
                deprecated_hint = (
                    f"\n  These pack(s) were last seen on DEPRECATED disc(s): "
                    f"{disc_list}. "
                    "If you still have those physical discs, mount them and re-run."
                )
            logger.error(
                "%d required pack(s) not found in the disc catalog. "
                "If these packs are on other discs not yet mounted, "
                "add them via --disc-dir and re-run.%s\n"
                "  Missing pack IDs: %s%s",
                len(pick_list.missing_packs),
                deprecated_hint,
                missing_preview,
                overflow,
            )
            return 1

        # Build alternates lookup for resilient pack collection.
        alternates_map, primary_labels = _alternates_for(pick_list)

        all_failed: list[str] = []

        if volume_dir:
            # Non-interactive mode: all discs pre-extracted into a directory tree.
            vol_dir = volume_dir

            # Always try the initial --disc path first.
            result = executor.ingest_volume(
                cache_dir, disc_path, plan.required_pack_hashes,
                verify=not skip_verify, collect_failures=True,
            )
            if result.ingested:
                logger.info("  Initial disc: ingested %d pack(s).", result.ingested)
            all_failed.extend(result.failed)

            for label, vol_sources in pick_list.volumes.items():
                pack_hashes = [s.pack.sha256 for s in vol_sources]
                vol_path = vol_dir / label
                if not vol_path.is_dir():
                    logger.warning(
                        "Volume directory not found: %s — skipping", vol_path
                    )
                    vol_path = vol_dir
                result = executor.ingest_volume(
                    cache_dir, vol_path, pack_hashes,
                    verify=not skip_verify, collect_failures=True,
                )
                if result.ingested:
                    logger.info("  %s: ingested %d pack(s).", label, result.ingested)
                if result.failed:
                    logger.warning(
                        "  %s: %d pack(s) failed verification.", label, len(result.failed)
                    )
                    all_failed.extend(result.failed)

            # The cache is the source of truth: packs that live on a
            # later disc were seeded into all_failed by the initial-disc
            # ingest, then ingested for real in the loop above.  Drop
            # them before any retry/raise so a multi-disc snapshot whose
            # cache is complete never falsely fails (RST-01).
            all_failed = prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = retry_from_alternates_batch(
                    executor, cache_dir, vol_dir,
                    all_failed, alternates_map,
                    verify=not skip_verify,
                )
                _raise_if_unrecovered(cache_dir, pick_list, still_failed)

        else:
            # Interactive mode: prompt for each disc to be mounted.
            if not _is_tty(stdin_is_tty):
                logger.error(
                    "Interactive restore requires a TTY.\n"
                    "Use --volume-dir with a directory of extracted discs "
                    "for non-interactive (scripted) restores."
                )
                return 1

            # First: ingest from the initial disc named on the command line.
            logger.info("\nIngesting packs from initial disc...")
            result = executor.ingest_volume(
                cache_dir, disc_path, plan.required_pack_hashes,
                verify=not skip_verify, collect_failures=True,
            )
            logger.info("  Ingested %d pack(s) from initial disc.", result.ingested)
            all_failed.extend(result.failed)

            # Determine which volumes are still needed.
            still_missing = RestoreExecutor.verify_cache_completeness(
                cache_dir, plan.required_pack_hashes
            )
            if still_missing:
                missing_set = set(still_missing)
                for label, vol_sources in sorted(pick_list.volumes.items()):
                    pack_hashes = [
                        s.pack.sha256 for s in vol_sources
                        if s.pack.sha256 in missing_set
                    ]
                    if not pack_hashes:
                        continue
                    logger.info(
                        "\nNeed %d pack(s) from disc '%s'.",
                        len(pack_hashes), label,
                    )
                    while True:
                        mount_path = _ask(
                            prompt,
                            f"Mount disc '{label}' and enter its path "
                            "(or 'skip'): "
                        ).strip()
                        if mount_path.lower() == "skip":
                            logger.info("  Skipping %s", label)
                            # RST-07: enqueue the skipped disc's packs so
                            # the alternates retry can offer other volumes.
                            all_failed.extend(pack_hashes)
                            break
                        vol_path = Path(mount_path)
                        if not vol_path.is_dir():
                            logger.info(
                                "  '%s' is not a directory — try again.", mount_path
                            )
                            continue
                        result = executor.ingest_volume(
                            cache_dir, vol_path, pack_hashes,
                            verify=not skip_verify, collect_failures=True,
                        )
                        logger.info(
                            "  Ingested %d pack(s) from %s", result.ingested, label
                        )
                        if result.failed:
                            logger.warning(
                                "  %d pack(s) failed verification", len(result.failed)
                            )
                            all_failed.extend(result.failed)
                        break

            all_failed = prune_recovered(cache_dir, all_failed)
            if all_failed:
                still_failed = retry_from_alternates_interactive(
                    executor, cache_dir,
                    all_failed, alternates_map,
                    verify=not skip_verify,
                    primary_labels=primary_labels,
                    prompt=prompt,
                )
                _raise_if_unrecovered(cache_dir, pick_list, still_failed)

        # Completeness check before running rustic.
        all_required = plan.required_pack_hashes
        missing = RestoreExecutor.verify_cache_completeness(cache_dir, all_required)
        if missing:
            logger.error(
                "\n%d of %d required pack(s) missing from cache.",
                len(missing), len(all_required),
            )
            report_missing_by_label(pick_list, missing)
            logger.error(
                "\nRestore cannot proceed — mount the missing discs and retry,\n"
                "or check for damaged discs."
            )
            return 1

        # Execute restore.
        target = target_path.resolve()
        logger.info("\nRestoring snapshot '%s' \u2192 %s", snapshot_id, target)
        try:
            executor.execute_restore(
                cache_dir=cache_dir,
                snapshot_id=snapshot_id,
                target_path=target,
                password_file=password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Use python_reader.py bundled on the disc as a fallback:\n"
                "  python3 %s/python_reader.py "
                "--password-file %s --restore-folder %s",
                disc_path, password_file, target,
            )
            return 1

        logger.info("Restore complete!")
        return 0

    finally:
        shutdown.uninstall()
        tmp_dir_obj.cleanup()


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
    disc_path: Path,
    cache_dir: Path,
    target_path: Path,
    password_file: Path,
    snapshot: str,
) -> int:
    """Restore with the Python reader (tier 3) when rustic is not installed.

    Returns the exit code, as the bundled python_reader.py exits:
    0 done, 4 finished but some items not restored (#875), 74 the disk
    is full, 75 stopped at a disc prompt, 77 a key file rejected the
    password (#805), 1 for any other failure.
    """
    # Auto-fallback: use the Python reader (tier 3) bundled with LCSAS.
    # Link the disc data dir into the cache so PurePythonRestorer can
    # find the pack files without copying gigabytes.
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

    data_link = cache_dir / "data"
    disc_data = disc_path / "data"
    # If a stale symlink exists (e.g. resumed on a different system),
    # remove it so we can re-link to the current disc.
    if (data_link.is_symlink() and
            (not data_link.exists() or
             data_link.resolve() != disc_data.resolve())):
        logger.warning(
            "Stale data symlink detected (%s → %s). Re-linking to current disc.",
            data_link, data_link.resolve(),
        )
        data_link.unlink()
    if not data_link.exists() and disc_data.is_dir():
        data_link.symlink_to(disc_data)
    elif not disc_data.is_dir():
        logger.error(
            "Disc data directory not found at '%s'. "
            "Ensure the disc is mounted and the path is correct.",
            disc_data,
        )
        return 1

    logger.warning(
        "rustic binary not found — falling back to the Python reader (tier 3).\n"
        "  This is ~100x slower and only reads packs from the mounted disc.\n"
        "  For multi-disc snapshots, mount all discs and copy their data/ "
        "directories into %s/data/ before running.",
        cache_dir,
    )
    target = target_path.resolve()
    snap_arg = None if snapshot == "latest" else snapshot
    restorer = PurePythonRestorer(
        repo_path=cache_dir,
        password_file=password_file,
    )
    try:
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
        # Setup-level failure (wrong password, missing repo) — fail fast.
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
            "intact. Re-run from a newer meta disc or an undamaged "
            "disc of the affected volume to restore them.",
            restorer.failures, target,
        )
        return EXIT_NOT_ALL_RESTORED
    logger.info(
        "Restore complete (Python reader fallback). Snapshot: %s, "
        "hostname: %s",
        meta.snapshot_id, meta.hostname,
    )
    return 0


def _resolve_latest(
    runner: RusticRunner, cache_dir: Path, password_file: Path, repo_name: str,
) -> str | None:
    """The newest snapshot's ID in the cache's repository, or None (logged)."""
    try:
        snap_list = runner.snapshots(
            repo_path=cache_dir,
            password_file=password_file,
        )
    except Exception as exc:
        logger.error(
            "Could not list snapshots to resolve 'latest': %s", exc
        )
        return None
    if not snap_list:
        logger.error(
            "No snapshots found in repository '%s' on this disc.",
            repo_name,
        )
        return None
    # #821: compare instants, not text: times with different UTC
    # offsets sort wrongly as strings.  The same key as tier 3
    # (#663): a time that does not parse sorts first, so it is
    # never taken as "latest".
    from lcsas.restore.restic_fallback import time_sort_key

    snap_list.sort(key=lambda snap: time_sort_key(snap.timestamp))
    snapshot_id = snap_list[-1].snapshot_id
    logger.info(
        "Resolved 'latest' to snapshot %s (%s)",
        snapshot_id, snap_list[-1].timestamp,
    )
    return snapshot_id
