"""``lcsas restore``: plan, exec, standalone."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lcsas.cli.common import (
    _load_config_opt,
    _open_existing_catalog,
    _resolve_db_path,
    _validate_config_or_exit,
)
from lcsas.log import get_logger
from lcsas.restore.orchestrator import (
    disc_summary_for_packs,
    find_sibling_iso,
    free_space_shortfalls,
    prune_recovered,
    report_missing_by_label,
    retry_from_alternates_batch,
    retry_from_alternates_interactive,
)

logger = get_logger()


def cmd_restore_plan(args: argparse.Namespace) -> int:
    """Generate a restore pick list for a snapshot."""
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for restore plan.")
        return 1
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    conn = _open_existing_catalog(_resolve_db_path(args, config))
    try:
        # Resolve repo config
        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            logger.error(f"  Available: {', '.join(config.repositories.keys())}")
            return 1

        repo_cfg = config.repositories[repo_name]
        if repo_cfg.password_file is None:
            logger.error(
                "Repository '%s' has no password_file configured.", repo_name
            )
            return 1

        # Get required pack IDs via rustic dry-run
        from lcsas.utils.subprocess import check_binary_version
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

        runner = SubprocessRusticRunner(tmpdir=config.staging_path)
        plan = runner.restore_dry_run(
            snapshot_id=args.snapshot_id,
            repo_path=repo_cfg.mirror_path,
            password_file=repo_cfg.password_file,
        )

        # Generate pick list with alternate-volume info (RST-07).
        planner = RestorePlanner(conn)
        pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)
    finally:
        conn.close()

    # Display results
    logger.info(f"Restore Pick List for snapshot {args.snapshot_id}")
    logger.info(f"  Repository: {repo_name}")
    logger.info(f"  Required packs: {len(plan.required_pack_hashes)}")
    logger.info("")

    if pick_list.volumes:
        for label, sources in sorted(pick_list.volumes.items()):
            total = sum(s.pack.size_bytes for s in sources)
            also_on = sorted({
                alt for s in sources for alt in s.alternates if alt != label
            })
            also = f"  also on: {', '.join(also_on)}" if also_on else ""
            logger.info(f"  {label:<30} {len(sources):>4} packs  "
                       f"({total / (1024 * 1024):.1f} MB){also}")
        logger.info("")
        logger.info(f"  Total: {pick_list.total_packs} packs across "
                   f"{len(pick_list.volumes)} volumes "
                   f"({pick_list.total_bytes / (1024 * 1024):.1f} MB)")

    if pick_list.deprecated_disc_labels:
        logger.warning(
            "\n  WARNING: %d pack(s) are only available on DEPRECATED or "
            "DESTROYED volumes. These discs may still be physically "
            "retrievable if you have kept them.",
            sum(len(v) for v in pick_list.deprecated_disc_labels.values()),
        )
        for label, hashes in sorted(pick_list.deprecated_disc_labels.items()):
            logger.warning(
                "    %s  (%d pack(s))", label, len(hashes)
            )
        logger.warning(
            "  If you can locate these discs, mount them and re-run restore."
        )

    for label, hashes in sorted(pick_list.unconfirmed_volume_labels.items()):
        logger.warning(
            "\n  WARNING: volume %s was staged but has no record of ever "
            "being burned. If you cannot find this disc, it may never have "
            "existed. The %d pack(s) it lists may only exist on the "
            "mirror.",
            label, len(hashes),
        )

    if pick_list.missing_packs:
        logger.error(
            "\n  ERROR: %d pack(s) required for this snapshot are not found in "
            "any archived volume. Restore is impossible without these packs.",
            len(pick_list.missing_packs),
        )
        for sha in pick_list.missing_packs[:10]:
            logger.error(f"    missing: {sha}")
        if len(pick_list.missing_packs) > 10:
            logger.error(
                "    ... and %d more missing packs",
                len(pick_list.missing_packs) - 10,
            )
        logger.error(
            "  If discs are physically present but not yet scanned, run "
            "`lcsas catalog validate /mnt/disc` to check each disc."
        )
        return 1

    return 0


def cmd_restore_exec(args: argparse.Namespace) -> int:
    """Execute a restore operation."""
    import tempfile

    from lcsas.restore.executor import RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for restore exec.")
        return 1
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    conn = _open_existing_catalog(_resolve_db_path(args, config))
    try:
        repo_name = args.repo
        if repo_name not in config.repositories:
            logger.error(f"repository '{repo_name}' not found in config.")
            return 1

        repo_cfg = config.repositories[repo_name]

        # Early validation: password file must exist before we attempt rustic.
        if not args.password_file.exists():
            logger.error(
                "Password file not found: %s\n"
                "Provide the correct path with --password-file.",
                args.password_file,
            )
            return 1

        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import check_binary_version
        try:
            check_binary_version("rustic", min_version=(0, 9, 0))
        except BinaryError as exc:
            logger.error("%s", exc)
            return 1

        runner = SubprocessRusticRunner(tmpdir=config.staging_path)

        # Get required pack IDs
        plan = runner.restore_dry_run(
            snapshot_id=args.snapshot_id,
            repo_path=repo_cfg.mirror_path,
            password_file=args.password_file,
        )

        # Generate pick list with alternates for resilient restore
        planner = RestorePlanner(conn)
        pick_list = planner.generate_pick_list_v2(plan.required_pack_hashes)

        # Phase 21.6: capture per-volume ISO SHA-256 hashes while
        # the catalog conn is still open so the ingest loop below
        # can pass them to ingest_volume.  When the owner's
        # vol_dir holds the original .iso files next to the
        # extracted contents, ingest_volume will SHA-verify each
        # one before reading its packs — same protection as
        # dvdisaster on Linux, no toolchain required.
        from lcsas.db.volume_copies import get_iso_sha256_for_label
        iso_shas: dict[str, str | None] = {
            label: get_iso_sha256_for_label(conn, label)
            for label in pick_list.volumes
        }
    finally:
        conn.close()

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
    alternates_map: dict[str, list[str]] = {}
    primary_labels: dict[str, str] = {}
    for label, sources in pick_list.volumes.items():
        for src in sources:
            primary_labels[src.pack.sha256] = label
            if src.alternates:
                alternates_map[src.pack.sha256] = list(src.alternates)

    # ── Disk-space preflight [FMA-09] ──────────────────────────────
    # Refuse (or hard-confirm) BEFORE the first disc prompt: an heir
    # restoring onto a too-small disk must learn that NOW, not via
    # ENOSPC after a long disc-swapping session.  The pack cache
    # needs ~total_bytes too — packs accumulate there before rustic
    # restore runs — so its filesystem is checked when distinct.
    cache_base = (
        args.cache_dir if args.cache_dir is not None else config.staging_path
    )
    shortfalls = free_space_shortfalls(
        pick_list.total_bytes, args.target_path, cache_base,
    )
    if shortfalls:
        need_gb = pick_list.total_bytes / 1e9
        for short_path, free_bytes in shortfalls:
            logger.error(
                "Restoring ~%.1f GB but %s has only %.1f GB free.",
                need_gb, short_path, free_bytes / 1e9,
            )
        if not sys.stdin.isatty():
            logger.error(
                "Refusing to start: free up space or choose a different "
                "target / --cache-dir, then re-run."
            )
            return 1
        ans = input("Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            logger.error("Aborted: free up space and re-run.")
            return 1

    # Set up cache directory
    _tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.cache_dir is None:
        config.staging_path.mkdir(parents=True, exist_ok=True)
        _tmp_dir = tempfile.TemporaryDirectory(
            prefix="lcsas-restore-", dir=str(config.staging_path)
        )
        cache_dir = Path(_tmp_dir.name)
    else:
        cache_dir = args.cache_dir
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
        metadata_source = repo_cfg.mirror_path
        executor.prepare_cache(cache_dir, metadata_source)

        logger.info(f"Restore cache: {cache_dir}")
        logger.info(f"Need packs from {len(pick_list.volumes)} volumes")

        all_failed: list[str] = []  # packs that failed on primary volume

        # Ingest packs from volumes
        if args.volume_dir:
            # Non-interactive: all volume data is pre-extracted in one directory
            vol_dir = args.volume_dir
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
                    verify=not args.skip_verify,
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
                    verify=not args.skip_verify,
                    iso_shas=iso_shas,
                )
                still_failed = prune_recovered(cache_dir, still_failed)
                if still_failed:
                    from lcsas.restore.executor import PackCorruptionError
                    where = disc_summary_for_packs(pick_list, still_failed)
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"retrieved.{where}"
                    )
        else:
            # Interactive: prompt the owner to mount a disc of each volume
            if not sys.stdin.isatty():
                logger.error(
                    "Interactive restore requires a TTY. "
                    "Use --volume-dir to specify a directory of pre-extracted "
                    "volume data for non-interactive (scripted) restores."
                )
                return 1
            for label, sources in sorted(pick_list.volumes.items()):
                pack_hashes = [s.pack.sha256 for s in sources]
                while True:
                    mount_path = input(
                        f"\nMount disc '{label}' and enter mount path "
                        f"(or 'skip' to skip): "
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
                        find_sibling_iso(args.iso_dir, label)
                        if args.iso_dir is not None
                        else None
                    )
                    result = executor.ingest_volume(
                        cache_dir, vol_path, pack_hashes,
                        verify=not args.skip_verify,
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
                    verify=not args.skip_verify,
                    iso_dir=args.iso_dir,
                    iso_shas=iso_shas,
                    primary_labels=primary_labels,
                )
                still_failed = prune_recovered(cache_dir, still_failed)
                if still_failed:
                    from lcsas.restore.executor import PackCorruptionError
                    where = disc_summary_for_packs(pick_list, still_failed)
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"retrieved.{where}"
                    )

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
        target = args.target_path.resolve()
        logger.info(f"\nRestoring snapshot {args.snapshot_id} → {target}")
        executor.execute_restore(
            cache_dir=cache_dir,
            snapshot_id=args.snapshot_id,
            target_path=target,
            password_file=args.password_file,
        )
        logger.info("Restore complete!")
    finally:
        # Cleanup temporary cache
        if _tmp_dir is not None:
            _tmp_dir.cleanup()
        shutdown.uninstall()

    return 0


def cmd_restore_from_disc(args: argparse.Namespace) -> int:
    """Restore from optical discs without a config file or mirror.

    Reads ``catalog.db`` and repository metadata directly from a mounted
    or extracted disc.  No ``--config``, local mirror, or master
    catalog required — everything needed is embedded on each disc.

    Typical usage::

        lcsas restore standalone /mnt/disc1 ~/restored/ \\
            --password-file ~/secret.key

    For scripted restores with pre-extracted discs::

        lcsas restore standalone /tmp/disc1 ~/restored/ \\
            --password-file ~/secret.key \\
            --volume-dir /tmp/extracted_discs/
    """
    import shutil
    import sys
    import tempfile

    from lcsas.db.connection import get_connection
    from lcsas.restore.executor import PackCorruptionError, RestoreExecutor
    from lcsas.restore.planner import RestorePlanner
    from lcsas.rustic.wrapper import SubprocessRusticRunner
    from lcsas.utils.fs import ensure_dir

    disc_path = args.disc
    if not disc_path.is_dir():
        logger.error("Disc path '%s' is not a directory.", disc_path)
        return 1

    # Early validation: password file must exist.
    if not args.password_file.exists():
        logger.error(
            "Password file not found: %s\n"
            "Provide the correct path with --password-file.",
            args.password_file,
        )
        return 1

    # Locate catalog.db on the disc
    catalog_path = args.catalog if args.catalog else disc_path / "catalog.db"
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

        # Select the repository to restore
        if args.repo:
            repo_row = next((r for r in repo_rows if r["name"] == args.repo), None)
            if repo_row is None:
                names = [r["name"] for r in repo_rows]
                logger.error(
                    "Repository '%s' not found in disc catalog.  Available: %s",
                    args.repo, ", ".join(names),
                )
                return 1
        elif len(repo_rows) == 1:
            repo_row = repo_rows[0]
            logger.info("Repository: %s", repo_row["name"])
        else:
            logger.error(
                "Multiple repositories found in disc catalog — "
                "use --repo NAME to select one.\nAvailable: %s",
                ", ".join(r["name"] for r in repo_rows),
            )
            return 1

        repo_name = repo_row["name"]

        # Locate metadata on the disc.  The injector writes it as
        # metadata/<repo_id>/ (#743); a repository added with `repo add`
        # has a UUID id, so the name alone does not find it.  The name
        # is still tried for a repository whose id equals its name.
        disc_meta = disc_path / "metadata" / repo_row["repo_id"]
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
            return 1

        logger.info("Found metadata for '%s' on disc.", repo_name)

        # Build restore cache and populate it with disc metadata.
        cache_dir = args.cache_dir if args.cache_dir else (tmp_dir / "cache")
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
            # Auto-fallback: use the Python reader (tier 3) bundled with LCSAS.
            # Link the disc data dir into the cache so PurePythonRestorer can
            # find the pack files without copying gigabytes.
            from lcsas.restore.restic_fallback import (
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
            target = args.target_path.resolve()
            snap_arg = None if args.snapshot == "latest" else args.snapshot
            restorer = PurePythonRestorer(
                repo_path=cache_dir,
                password_file=args.password_file,
            )
            try:
                meta = restorer.restore(target=target, snapshot_id=snap_arg)
            except RestoreAbortedError as exc:
                # q (or the end of input) at the disc prompt exits 75, a
                # full disk 1, as the bundled standalone_restorer.py does
                # (#658).
                logger.error("Python reader restore stopped: %s", exc)
                return exc.exit_code
            except Exception as exc:
                # Setup-level failure (wrong password, missing repo) — fail fast.
                logger.error("Python reader restore failed: %s", exc)
                return 1
            if restorer.failures:
                # Tolerant traversal skipped one or more files; the bulk
                # of the data is restored and a manifest lists the rest.
                logger.error(
                    "Python reader restore finished with %d skipped file(s). "
                    "See %s/RESTORE_FAILURES.txt; the rest of your data is "
                    "intact. Re-run from a newer meta disc or an undamaged "
                    "disc of the affected volume to restore them.",
                    restorer.failures, target,
                )
                return 2
            logger.info(
                "Restore complete (Python reader fallback). Snapshot: %s, "
                "hostname: %s",
                meta.snapshot_id, meta.hostname,
            )
            return 0

        # Resolve "latest" to an actual snapshot ID so dry-run gets an
        # explicit ID that rustic/restic can handle without ambiguity.
        snapshot_id = args.snapshot
        if snapshot_id == "latest":
            try:
                snap_list = runner.snapshots(
                    repo_path=cache_dir,
                    password_file=args.password_file,
                )
            except Exception as exc:
                logger.error(
                    "Could not list snapshots to resolve 'latest': %s", exc
                )
                return 1
            if not snap_list:
                logger.error(
                    "No snapshots found in repository '%s' on this disc.",
                    repo_name,
                )
                return 1
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

        # Run rustic dry-run against the cache to determine required packs.
        logger.info(
            "Determining required packs for snapshot '%s'...", snapshot_id
        )
        try:
            plan = runner.restore_dry_run(
                snapshot_id=snapshot_id,
                repo_path=cache_dir,
                password_file=args.password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Install rustic (https://rustic.cli.rs/) or "
                "restic (https://restic.net/),\n"
                "or use the Python reader (tier 3) bundled on the disc:\n"
                "  python3 %s/standalone_restorer.py "
                "--password-file %s --target %s",
                disc_path, args.password_file, args.target_path,
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
        alternates_map: dict[str, list[str]] = {}
        primary_labels: dict[str, str] = {}
        for label, sources in pick_list.volumes.items():
            for src in sources:
                primary_labels[src.pack.sha256] = label
                if src.alternates:
                    alternates_map[src.pack.sha256] = list(src.alternates)

        all_failed: list[str] = []

        if args.volume_dir:
            # Non-interactive mode: all discs pre-extracted into a directory tree.
            vol_dir = args.volume_dir

            # Always try the initial --disc path first.
            result = executor.ingest_volume(
                cache_dir, disc_path, plan.required_pack_hashes,
                verify=not args.skip_verify, collect_failures=True,
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
                    verify=not args.skip_verify, collect_failures=True,
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
                    verify=not args.skip_verify,
                )
                still_failed = prune_recovered(cache_dir, still_failed)
                if still_failed:
                    where = disc_summary_for_packs(pick_list, still_failed)
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"retrieved.{where}"
                    )

        else:
            # Interactive mode: prompt for each disc to be mounted.
            if not sys.stdin.isatty():
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
                verify=not args.skip_verify, collect_failures=True,
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
                        mount_path = input(
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
                            verify=not args.skip_verify, collect_failures=True,
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
                    verify=not args.skip_verify,
                    primary_labels=primary_labels,
                )
                still_failed = prune_recovered(cache_dir, still_failed)
                if still_failed:
                    where = disc_summary_for_packs(pick_list, still_failed)
                    raise PackCorruptionError(
                        f"{len(still_failed)} pack(s) could not be "
                        f"retrieved.{where}"
                    )

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
        target = args.target_path.resolve()
        logger.info("\nRestoring snapshot '%s' \u2192 %s", snapshot_id, target)
        try:
            executor.execute_restore(
                cache_dir=cache_dir,
                snapshot_id=snapshot_id,
                target_path=target,
                password_file=args.password_file,
            )
        except FileNotFoundError:
            logger.error(
                "rustic/restic binary not found.\n"
                "Use standalone_restorer.py bundled on the disc as a fallback:\n"
                "  python3 %s/standalone_restorer.py "
                "--password-file %s --target %s",
                disc_path, args.password_file, target,
            )
            return 1

        logger.info("Restore complete!")
        return 0

    finally:
        shutdown.uninstall()
        tmp_dir_obj.cleanup()
