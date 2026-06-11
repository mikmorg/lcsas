"""Burn orchestrator — the central pipeline for archiving packs to media.

Supports two modes:
  1. Legacy single-volume: prepare() → execute() (one volume at a time)
  2. Session-based: stage() → burn_session() (multi-volume, multi-copy)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lcsas.binpack.algorithm import first_fit_decreasing
from lcsas.burn.device_verify import read_device_sha256
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig
from lcsas.db.locations import ensure_location
from lcsas.db.models import Pack, SessionVolume, Volume
from lcsas.db.queries import get_unarchived_or_missing_at_location, get_unarchived_packs
from lcsas.db.repos import list_repos
from lcsas.db.sessions import (
    add_session_volume,
    create_session,
    get_session_volumes,
    resolve_session_id,
    update_session_status,
    update_session_volume_iso,
)
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_events import add_event
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import (
    create_volume,
    delete_volume,
    get_volume_by_id,
    list_volumes,
    mark_closed,
    update_status,
    update_used_bytes,
)
from lcsas.ecc.dvdisaster import DVDisasterRunner
from lcsas.iso.xorriso import XorrisoRunner
from lcsas.staging.builder import MirrorUnavailableError, StagingBuilder
from lcsas.staging.metadata import HolographicInjector
from lcsas.utils.fs import ensure_dir, safe_remove_tree
from lcsas.utils.hashing import sha256_file
from lcsas.utils.labels import (
    generate_session_id,
    generate_uuid,
    generate_volume_label,
    next_seq_num,
)

_logger = logging.getLogger(__name__)


@dataclass
class BurnManifest:
    """Describes a prepared burn operation (single volume)."""

    volume_label: str
    volume_uuid: str
    volume_id: int
    media_type: MediaType
    selected_packs: list[Pack]
    total_data_bytes: int
    staging_path: Path
    iso_path: Path | None = None


@dataclass
class StageResult:
    """Result of a multi-volume staging operation."""

    session_id: str
    media_type: MediaType
    staging_dir: Path
    manifests: list[BurnManifest] = field(default_factory=list)
    iso_paths: list[Path] = field(default_factory=list)


@dataclass
class AbortSummary:
    """What an abort returned to the unarchived pool."""

    volumes_deleted: int
    packs_reclaimed: int
    bytes_reclaimed: int
    labels: list[str] = field(default_factory=list)


@dataclass
class BurnReceipt:
    """Receipt emitted after burning a single volume."""

    volume_label: str
    volume_id: int
    session_id: str
    location: str
    device: str
    burn_date: str
    iso_sha256: str
    verify_passed: bool
    pack_count: int
    pack_ids: list[int] = field(default_factory=list)
    # Post-ECC ISO byte length — travels with the hash so an imported
    # receipt (or a catalog rebuilt from disc copies) can still device-
    # verify the disc after the ISO file is gone (FMA-03).
    iso_size_bytes: int | None = None


class BurnOrchestrator:
    """Orchestrates the full burn pipeline: delta → binpack → stage → ISO → burn.

    All external dependencies are injected via the constructor, enabling
    complete mock-based testing.
    """

    def __init__(
        self,
        config: LCSASConfig,
        conn: sqlite3.Connection,
        xorriso: XorrisoRunner,
        dvdisaster: DVDisasterRunner,
        device_reader: Callable[[str, int], str] = read_device_sha256,
    ) -> None:
        self._config = config
        self._conn = conn
        self._xorriso = xorriso
        self._dvdisaster = dvdisaster
        # Injected for testability (BURN-04), matching the protocol-
        # injection pattern used for xorriso/dvdisaster: reads N bytes
        # from a device and returns the hex SHA-256.
        self._device_reader = device_reader

    def prepare(
        self,
        media_type: MediaType | None = None,
        repo_ids: list[str] | None = None,
    ) -> BurnManifest:
        """Identify unarchived packs and prepare a staging directory.

        Steps:
          1. Query unarchived packs (all repos or specific ones).
          2. Bin-pack them to fit the target media.
          3. Build staging directory with hardlinked packs.
          4. Inject holographic metadata.
          5. Register volume in DB with STAGING status.

        Returns:
            BurnManifest describing the prepared burn.
        """
        mt = media_type or self._config.default_media_type

        # 1. Gather unarchived packs
        all_unarchived: list[Pack] = []
        if repo_ids:
            for rid in repo_ids:
                all_unarchived.extend(get_unarchived_packs(self._conn, rid))
        else:
            all_unarchived = get_unarchived_packs(self._conn)

        if not all_unarchived:
            raise ValueError("No unarchived packs to burn.")

        # 2. Bin-pack
        items = [(p.sha256, p.size_bytes) for p in all_unarchived]
        selected_items, remaining_items = first_fit_decreasing(
            items,
            capacity=mt.usable_bytes,
            reserved=self._config.metadata_reserve_bytes,
        )

        # Detect packs that can never fit on any single volume of this media type.
        usable = mt.usable_bytes - self._config.metadata_reserve_bytes
        oversized = [
            p for p in all_unarchived
            if p.size_bytes > usable and any(sha == p.sha256 for sha, _ in remaining_items)
        ]
        if oversized:
            details = ", ".join(
                f"{p.sha256[:12]} ({p.size_bytes:,} bytes)" for p in oversized
            )
            raise ValueError(
                f"{len(oversized)} pack(s) exceed {mt.name} usable capacity "
                f"({usable:,} bytes) and can never be archived on this media type: "
                f"{details}. Consider using a larger media type (e.g. BDXL100)."
            )

        if not selected_items:
            raise ValueError(
                f"No packs fit in {mt.name} "
                f"(usable={mt.usable_bytes}, reserved={self._config.metadata_reserve_bytes})"
            )

        selected_hashes = {sha for sha, _size in selected_items}
        selected_packs = [p for p in all_unarchived if p.sha256 in selected_hashes]
        total_bytes = sum(s for _, s in selected_items)

        # 3. Generate volume identity
        existing_labels = [
            v.label for v in
            list_volumes(self._conn)
        ]
        seq = next_seq_num(existing_labels, self._config.label_prefix)
        vol_label = generate_volume_label(
            self._config.label_prefix, mt.label_name, seq
        )
        vol_uuid = generate_uuid()

        # 4-7. Stage, register, inject metadata
        staging_root = self._config.staging_path / vol_label
        manifest = self._stage_single_volume(
            selected_packs=selected_packs,
            total_bytes=total_bytes,
            media_type=mt,
            vol_label=vol_label,
            vol_uuid=vol_uuid,
            staging_root=staging_root,
        )

        return manifest

    def execute(
        self,
        manifest: BurnManifest,
        iso_output: Path | None = None,
        skip_burn: bool = False,
    ) -> Volume:
        """Execute the burn: create ISO, add ECC, burn to disc.

        ECC is always applied to production media (any ``MediaType`` with
        ``ecc_overhead_pct > 0``). Test media (``TEST_TINY``, 0% overhead) is
        implicitly skipped because RS03 has a minimum image size that the
        1 MB test ISO cannot meet — see ``MediaType.ecc_overhead_pct`` in
        ``src/lcsas/config/media.py``.

        Args:
            manifest: A BurnManifest from prepare().
            iso_output: Override path for the ISO file.
            skip_burn: If True, create ISO but don't burn to physical media.

        Returns:
            The finalized Volume object.
        """
        apply_ecc = manifest.media_type.ecc_overhead_pct > 0

        # Preflight: verify required binaries exist and meet minimum versions.
        from lcsas.utils.subprocess import SubprocessRunnerBase, check_binary_version
        if isinstance(self._xorriso, SubprocessRunnerBase):
            # xorriso 1.4.0+ required for reliable ISO-9660 level 3 support.
            check_binary_version(self._xorriso._binary, min_version=(1, 4, 0))
        if apply_ecc and isinstance(self._dvdisaster, SubprocessRunnerBase):
            # dvdisaster 0.79+ required for RS03 augmentation mode.
            check_binary_version(self._dvdisaster._binary, min_version=(0, 79, 0))

        # Update status
        update_status(self._conn, manifest.volume_id, "BURNING")

        iso_path = iso_output or (self._config.staging_path / f"{manifest.volume_label}.iso")
        ensure_dir(iso_path.parent)

        # Pre-flight: verify the staging directory will fit in the media.
        from lcsas.utils.fs import dir_size_bytes
        estimated_bytes = dir_size_bytes(manifest.staging_path)
        media_capacity = manifest.media_type.capacity_bytes
        if estimated_bytes > media_capacity:
            raise ValueError(
                f"Staging directory for {manifest.volume_label} is too large: "
                f"{estimated_bytes:,} bytes > {media_capacity:,} bytes capacity "
                f"({manifest.media_type.name}). Reduce pack count or use larger media."
            )

        try:
            # Create ISO
            self._xorriso.create_iso(
                manifest.staging_path,
                iso_path,
                manifest.volume_label,
                expected_bytes=estimated_bytes,
            )
            manifest.iso_path = iso_path

            # Add ECC
            if apply_ecc:
                self._dvdisaster.augment_iso(
                    iso_path,
                    self._config.default_ecc_redundancy_pct,
                )

            # Post-ECC size validation: the augmented ISO must fit on the media.
            if iso_path.exists():
                iso_size = iso_path.stat().st_size
                if iso_size > manifest.media_type.capacity_bytes:
                    raise ValueError(
                        f"ISO {iso_path.name} is {iso_size:,} bytes after ECC, "
                        f"exceeding {manifest.media_type.name} capacity of "
                        f"{manifest.media_type.capacity_bytes:,} bytes. "
                        "Increase metadata_reserve_bytes or use larger media."
                    )

            # Burn to disc
            if not skip_burn:
                device = self._config.optical_device
                self._xorriso.burn_iso(iso_path, device)
                # Post-burn verification [FMA-03]: readability smoke
                # test, then disc identity (PVD Volume ID vs label),
                # then exact-length device read-back hashed against the
                # ISO just burned.  A readable disc that is not THIS
                # image must never reach VERIFIED.
                if not self._xorriso.verify_disc(device):
                    raise ValueError(
                        "Post-burn verification failed: "
                        "-check_media read-back failed"
                    )
                disc_id = self._xorriso.read_disc_volume_id(device)
                if disc_id != manifest.volume_label:
                    raise ValueError(
                        f"Disc in {device} identifies as '{disc_id}' — "
                        f"expected '{manifest.volume_label}'. Wrong disc?"
                    )
                expected_hash = sha256_file(iso_path)
                try:
                    device_hash = self._device_reader(
                        device, iso_path.stat().st_size,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"Post-burn verification failed: "
                        f"device read-back failed: {exc}"
                    ) from exc
                if device_hash != expected_hash:
                    raise ValueError(
                        f"Post-burn verification failed: device hash "
                        f"mismatch (expected {expected_hash[:8]}.., "
                        f"got {device_hash[:8]}..)"
                    )

            # Finalize (atomic: status + close)
            update_status(self._conn, manifest.volume_id, "VERIFIED", commit=False)
            mark_closed(self._conn, manifest.volume_id, commit=False)
            self._conn.commit()

        except Exception as original_exc:
            try:
                self._conn.rollback()
                update_status(self._conn, manifest.volume_id, "STAGING")
            except Exception as cleanup_exc:
                _logger.error(
                    "Error during exception cleanup: %s",
                    cleanup_exc,
                    exc_info=True,
                )
            raise original_exc

        return get_volume_by_id(self._conn, manifest.volume_id)

    def abort(self, manifest: BurnManifest) -> None:
        """Clean up a failed or cancelled burn operation.

        Removes staging directory and reverts DB state.
        """
        # Remove volume_packs links and the volume itself
        from lcsas.db.volumes import delete_volume
        delete_volume(self._conn, manifest.volume_id)

        # Clean up staging
        safe_remove_tree(manifest.staging_path)
        if manifest.iso_path and manifest.iso_path.exists():
            manifest.iso_path.unlink()

    def _stage_single_volume(
        self,
        selected_packs: list[Pack],
        total_bytes: int,
        media_type: MediaType,
        vol_label: str,
        vol_uuid: str,
        staging_root: Path,
        iso_output: Path | None = None,
        session_id: str | None = None,
    ) -> BurnManifest:
        """Build staging dir, register volume, inject metadata, optionally create ISO.

        This is the shared core of :meth:`prepare` (iso_output=None) and
        :meth:`stage` (iso_output set).  Returns a :class:`BurnManifest`
        describing the result.

        ECC is applied automatically whenever ``iso_output`` is set and the
        media type carries non-zero ECC overhead (i.e. all production media).
        Test media (``TEST_TINY``, 0% overhead) is implicitly skipped — see
        ``MediaType.ecc_overhead_pct`` in ``src/lcsas/config/media.py``.

        Args:
            selected_packs: Packs to include on this volume.
            total_bytes: Sum of pack sizes.
            media_type: Target media.
            vol_label: Generated volume label.
            vol_uuid: Generated volume UUID.
            staging_root: Directory to stage files into.
            iso_output: If set, create an ISO at this path (+ ECC for
                production media).
            session_id: If set, link the volume into session_volumes in
                the SAME transaction that creates it, so no crash can
                leave a volume row invisible to ``burn_session``.

        Returns:
            BurnManifest describing the staged volume.
        """
        # 1. Build staging directory
        builder = StagingBuilder(staging_root)
        builder.initialize()

        mirror_paths = self._get_mirror_paths()
        # Group packs by repo so we look for each pack only in its own mirror.
        # This avoids MissingPacksError when multiple repos share a volume.
        from collections import defaultdict
        packs_by_repo: dict[str, list[Pack]] = defaultdict(list)
        for p in selected_packs:
            packs_by_repo[p.repo_id].append(p)

        for repo_id, repo_packs in packs_by_repo.items():
            mirror_path = mirror_paths.get(repo_id)
            data_dir = mirror_path / "data" if mirror_path is not None else None
            if data_dir is None or not data_dir.is_dir():
                # Fail loud BEFORE any catalog write: a silent skip here
                # would link packs to a volume that physically lacks them.
                raise MirrorUnavailableError(
                    repo_id, mirror_path, [p.sha256[:12] for p in repo_packs],
                )
            builder.stage_packs(repo_packs, data_dir)

        # Post-stage invariant: every selected pack must be present in the
        # staging tree before anything is written to the catalog.
        builder.assert_staged_complete(selected_packs)

        # 2. Inject holographic metadata — strict for repos with packs on
        #    this volume (the disc must stay self-describing for them).
        injector = HolographicInjector(staging_root)
        injector.inject_metadata(mirror_paths, required_repos=set(packs_by_repo))

        # 3. Register volume in DB (atomic transaction)
        volume = create_volume(
            self._conn,
            label=vol_label,
            uuid=vol_uuid,
            media_type=media_type.name,
            capacity_bytes=media_type.capacity_bytes,
            location=self._config.default_location,
            status="STAGING",
            commit=False,
        )

        pack_ids = [p.pack_id for p in selected_packs]
        bulk_link_packs(self._conn, volume.volume_id, pack_ids, commit=False)
        update_used_bytes(self._conn, volume.volume_id, total_bytes, commit=False)
        if session_id is not None and iso_output is not None:
            # Same transaction as the volume row: a volume can never exist
            # outside session_volumes (the ISO hash is filled in later via
            # update_session_volume_iso once the ISO has been mastered).
            add_session_volume(
                self._conn,
                session_id=session_id,
                volume_id=volume.volume_id,
                iso_path=str(iso_output),
                iso_sha256="",
                commit=False,
            )
        self._conn.commit()

        # Steps 4-7 run AFTER the volume + volume_packs commit.  Any failure
        # past this point (catalog injection, xorriso, dvdisaster, size
        # checks, Ctrl-C) must compensate by deleting the committed volume:
        # otherwise its packs stay falsely "archived" on a phantom STAGING
        # volume that will never become a disc, and `lcsas stage` skips
        # them forever.  BaseException so KeyboardInterrupt during a
        # multi-minute dvdisaster run also compensates.  The staging tree
        # is left on disk for diagnosis (abort/clean removes it).
        try:
            # 4. Inject catalog AFTER DB commit.
            #    Checkpoint WAL so all committed data is in the main .db
            #    file, then copy it to staging, keeping DB and disc catalog
            #    in sync.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                injector.inject_catalog(self._config.db_path)
            except Exception as exc:
                _logger.error(
                    "Catalog injection into staging failed; rolling back volume "
                    "registration for %s: %s",
                    vol_label, exc,
                )
                raise RuntimeError(
                    f"Failed to inject catalog into staging for volume {vol_label}. "
                    f"Volume registration has been rolled back."
                ) from exc

            # 5. Write volume info files
            vol = get_volume_by_id(self._conn, volume.volume_id)
            injector.write_volume_info(vol, packs=selected_packs)
            injector.write_restore_instructions()
            injector.write_standalone_restorer()
            if not media_type.is_test:
                injector.write_lcsas_source()
            injector.write_start_here(self._config)
            injector.write_key_info(self._config)
            injector.write_config_summary(self._config)
            injector.write_disc_care()

            # 6. Optionally create ISO + ECC
            iso_path: Path | None = None
            if iso_output is not None:
                # Pre-flight: verify staging dir fits in the target media.
                from lcsas.utils.fs import dir_size_bytes
                estimated_bytes = dir_size_bytes(staging_root)
                if estimated_bytes > media_type.capacity_bytes:
                    raise ValueError(
                        f"Staging directory for {vol_label} is too large: "
                        f"{estimated_bytes:,} bytes > {media_type.capacity_bytes:,} bytes "
                        f"capacity ({media_type.name}). Reduce pack count or use larger media."
                    )
                self._xorriso.create_iso(
                    staging_root, iso_output, vol_label,
                    expected_bytes=estimated_bytes,
                )
                iso_path = iso_output

                if media_type.ecc_overhead_pct > 0:
                    self._dvdisaster.augment_iso(
                        iso_path, self._config.default_ecc_redundancy_pct,
                    )

                # 7. Validate ISO size against media capacity  [O4]
                if not iso_path.exists():
                    raise FileNotFoundError(
                        f"ISO not created by xorriso: {iso_path}. "
                        f"Check xorriso output for errors."
                    )
                iso_size = iso_path.stat().st_size
                if iso_size > media_type.capacity_bytes:
                    raise ValueError(
                        f"ISO {iso_path.name} is {iso_size:,} bytes, exceeds "
                        f"{media_type.name} capacity of "
                        f"{media_type.capacity_bytes:,} bytes"
                    )
        except BaseException:
            self._conn.rollback()  # drop any uncommitted partial state
            # Removes volume_packs links and the session_volumes row too,
            # so the packs reappear in get_unarchived_packs().
            delete_volume(self._conn, volume.volume_id)
            raise

        return BurnManifest(
            volume_label=vol_label,
            volume_uuid=vol_uuid,
            volume_id=volume.volume_id,
            media_type=media_type,
            selected_packs=selected_packs,
            total_data_bytes=total_bytes,
            staging_path=staging_root,
            iso_path=iso_path,
        )

    def _get_mirror_paths(self) -> dict[str, Path]:
        """Build a dict of {repo_id: mirror_path} from database repositories."""
        paths: dict[str, Path] = {}
        for repo in list_repos(self._conn):
            paths[repo.repo_id] = Path(repo.mirror_path)

        # Fallback: if no repos in DB, use mirror_base_path as "default"
        if not paths:
            paths["default"] = self._config.mirror_base_path

        return paths

    # =================================================================
    # Session-based staging and burning (multi-volume, multi-copy)
    # =================================================================

    def stage(
        self,
        media_type: MediaType | None = None,
        for_location: str | None = None,
        repo_ids: list[str] | None = None,
        pack_sha256s: list[str] | None = None,
        dry_run: bool = False,
    ) -> StageResult:
        """Stage all unarchived packs into ISOs, creating a burn session.

        Handles multi-volume scenarios: if data exceeds one disc, multiple
        volumes and ISOs are created within a single session.

        ECC is applied automatically for production media (any ``MediaType``
        with ``ecc_overhead_pct > 0``). Test media (``TEST_TINY``, 0%
        overhead) is implicitly skipped because RS03 has a minimum image
        size — see ``MediaType.ecc_overhead_pct`` in
        ``src/lcsas/config/media.py``.

        Args:
            media_type: Target media type (defaults to config).
            for_location: If set, stage only packs missing at this location.
            repo_ids: Optional filter to specific repositories.
            pack_sha256s: If set, stage only packs with these SHA-256 hashes.
            dry_run: If True, compute the plan but skip all side effects.

        Returns:
            StageResult with session ID, manifests, and ISO paths.
        """
        from lcsas.log import get_logger
        logger = get_logger()

        mt = media_type or self._config.default_media_type

        # 1. Gather packs to stage
        packs_to_stage = self._gather_packs_for_staging(
            for_location=for_location,
            repo_ids=repo_ids,
        )

        # Apply explicit pack filter (used by consolidate --execute)
        if pack_sha256s is not None:
            allowed = set(pack_sha256s)
            packs_to_stage = [p for p in packs_to_stage if p.sha256 in allowed]

        if not packs_to_stage:
            raise ValueError("No packs need staging.")

        # 2. Bin-pack into multiple volumes
        volume_plans = self._multi_bin_pack(packs_to_stage, mt)

        # --- Dry-run: report the plan without side effects ---
        if dry_run:
            total_bytes = sum(b for _, b in volume_plans)
            logger.info(f"[DRY RUN] {len(volume_plans)} volume(s) planned "
                        f"on {mt.name}")
            for i, (packs, vol_bytes) in enumerate(volume_plans, 1):
                fill_pct = (vol_bytes / mt.capacity_bytes) * 100
                logger.info(f"  Volume {i}: {len(packs)} packs, "
                            f"{vol_bytes:,} bytes ({fill_pct:.1f}% fill)")
            logger.info(f"  Total data: {total_bytes:,} bytes")
            return StageResult(
                session_id="dry-run",
                media_type=mt,
                staging_dir=Path("/dev/null"),
                manifests=[],
                iso_paths=[],
            )

        # 3. Disk space pre-flight check
        total_data_bytes = sum(b for _, b in volume_plans)
        # Headroom: ISO filesystem overhead (~5%) + ECC overhead +
        # the staging directory copy.  Use actual ECC percentage.
        ecc_pct = self._config.default_ecc_redundancy_pct
        overhead_factor = 1.05 * (1 + ecc_pct / 100) + 1  # ISO+ECC + staging copy
        required_bytes = int(total_data_bytes * overhead_factor)
        staging_usage = shutil.disk_usage(self._config.staging_path)
        if staging_usage.free < required_bytes:
            avail_gb = staging_usage.free / 1e9
            need_gb = required_bytes / 1e9
            raise OSError(
                f"Insufficient disk space for staging: "
                f"{avail_gb:.1f} GB available, ~{need_gb:.1f} GB needed "
                f"(at {self._config.staging_path})"
            )

        # 4. Create session
        session_id = generate_session_id()
        session_dir = self._config.staging_path / session_id.replace(":", "-")
        ensure_dir(session_dir)

        create_session(
            self._conn,
            media_type=mt.name,
            staging_dir=str(session_dir),
            session_id=session_id,
        )

        # 5. Build staging dirs, create ISOs, apply ECC for each volume
        manifests: list[BurnManifest] = []
        iso_paths: list[Path] = []

        existing_labels = [v.label for v in list_volumes(self._conn)]
        seq = next_seq_num(existing_labels, self._config.label_prefix)

        for i, (selected_packs, total_bytes) in enumerate(volume_plans):
            vol_label = generate_volume_label(
                self._config.label_prefix, mt.label_name, seq + i,
            )
            vol_uuid = generate_uuid()

            staging_root = session_dir / vol_label
            iso_path = session_dir / f"{vol_label}.iso"

            # Stage, register, inject metadata, create ISO + ECC.  The
            # session_volumes row is committed atomically with the volume
            # row inside _stage_single_volume (iso_sha256='' until hashed).
            manifest = self._stage_single_volume(
                selected_packs=selected_packs,
                total_bytes=total_bytes,
                media_type=mt,
                vol_label=vol_label,
                vol_uuid=vol_uuid,
                staging_root=staging_root,
                iso_output=iso_path,
                session_id=session_id,
            )

            # Compute ISO hash + byte length and record them on the
            # session volume.  The length is what device read-back
            # verification hashes against once the ISO file itself has
            # been cleaned up (BURN-04).
            iso_hash = ""
            iso_size: int | None = None
            if manifest.iso_path and manifest.iso_path.exists():
                iso_hash = sha256_file(manifest.iso_path)
                iso_size = manifest.iso_path.stat().st_size

            update_session_volume_iso(
                self._conn,
                session_id=session_id,
                volume_id=manifest.volume_id,
                iso_path=str(manifest.iso_path or iso_path),
                iso_sha256=iso_hash,
                iso_size_bytes=iso_size,
            )

            manifests.append(manifest)
            if manifest.iso_path:
                iso_paths.append(manifest.iso_path)

        # Write session manifest JSON
        self._write_session_manifest(session_id, session_dir, manifests)

        return StageResult(
            session_id=session_id,
            media_type=mt,
            staging_dir=session_dir,
            manifests=manifests,
            iso_paths=iso_paths,
        )

    def burn_session(
        self,
        session_ref: str = "latest",
        location: str = "Home_Shelf",
        device: str | None = None,
        skip_burn: bool = False,
    ) -> list[BurnReceipt]:
        """Burn all ISOs in a session to disc, tagged with a location.

        Args:
            session_ref: Session ID or 'latest'.
            location: Physical location tag for this copy.
            device: Optical device (overrides config).
            skip_burn: If True, skip physical burn (for testing).

        Returns:
            List of BurnReceipt objects.
        """
        session_id = resolve_session_id(self._conn, session_ref)
        session_vols = get_session_volumes(self._conn, session_id)
        device = device or self._config.optical_device

        # Ensure location exists
        ensure_location(self._conn, location)

        receipts: list[BurnReceipt] = []

        for sv in session_vols:
            iso_path = Path(sv.iso_path)
            if not skip_burn and not iso_path.exists():
                raise FileNotFoundError(
                    f"ISO file missing for volume {sv.volume_id}: {iso_path}. "
                    f"Was the staging directory cleaned prematurely?"
                )
            vol = get_volume_by_id(self._conn, sv.volume_id)

            # For multi-location re-burns, skip status transitions if
            # the volume is already VERIFIED (just add another copy).
            is_reburn = vol.status == "VERIFIED"

            if not is_reburn:
                # Update status
                update_status(self._conn, sv.volume_id, "BURNING", commit=False)
                self._conn.commit()

            try:
                # Burn
                if not skip_burn:
                    self._xorriso.burn_iso(iso_path, device)

                # Post-burn verification  [S1][BURN-04][FMA-03]
                # Step 1: -check_media readability smoke test.
                # Step 2: PVD Volume ID must match the volume label
                # (wrong-disc guard).
                # Step 3: exact-length device read-back hashed against
                # the ISO SHA-256 recorded at stage time — a readable
                # disc that doesn't carry the mastered image (wrong
                # disc, silent mis-burn, truncation) must never reach
                # VERIFIED.
                verify_passed = True
                verified_at: str | None = None
                if not skip_burn:
                    verify_passed = self._verify_burned_disc(
                        sv, vol.label, device, location, iso_path,
                    )
                    if verify_passed:
                        verified_at = datetime.now(UTC).isoformat()

                if not is_reburn:
                    if verify_passed:
                        # Finalize volume status (atomic: status + close + copy)
                        update_status(self._conn, sv.volume_id, "VERIFIED", commit=False)
                        mark_closed(self._conn, sv.volume_id, commit=False)
                    else:
                        # Stay at BURNED — user must investigate / re-burn
                        update_status(self._conn, sv.volume_id, "BURNED", commit=False)
                else:
                    # Re-burn case: volume stays VERIFIED (it passed before)
                    # Record the verify failure for this location's copy
                    if not verify_passed:
                        add_event(
                            self._conn, sv.volume_id, "VERIFY_FAIL_REBURN",
                            location=location,
                            detail="Post-burn read-back failed on re-burn attempt",
                            commit=False,
                        )

                if verify_passed:
                    # Record copy at location, with the verification
                    # evidence (BURN-04: previously iso_sha256 was omitted —
                    # every copy row got NULL, and a re-burn UPSERT blanked
                    # any stored hash; last_verified_at was never written).
                    add_volume_copy(
                        self._conn,
                        volume_id=sv.volume_id,
                        location=location,
                        iso_sha256=sv.iso_sha256 or None,
                        iso_size_bytes=sv.iso_size_bytes,
                        last_verified_at=verified_at,
                        commit=False,
                    )
                else:
                    # BURN-05: a failed disc is not a copy. Recording one
                    # would satisfy the ACTIVE-copy location queries and
                    # `stage --for-location` would never re-stage these
                    # packs — a phantom copy in the catalog forever. The
                    # VERIFY_FAIL event above carries the location for
                    # the audit trail.
                    _logger.error(
                        "Burn at %s FAILED verification for %s. NO copy "
                        "was recorded — this location still needs this "
                        "volume. Inspect the disc/drive and re-run: "
                        "lcsas burn --session %s --location %s",
                        location, vol.label, session_id, location,
                    )
                self._conn.commit()

                # Build receipt (before ISO cleanup, in case unlink fails)
                from lcsas.db.volume_packs import get_pack_ids_for_volume
                pack_ids = get_pack_ids_for_volume(self._conn, sv.volume_id)

                receipt = BurnReceipt(
                    volume_label=vol.label,
                    volume_id=sv.volume_id,
                    session_id=session_id,
                    location=location,
                    device=device,
                    burn_date=datetime.now(UTC).isoformat(),
                    iso_sha256=sv.iso_sha256 or "",
                    verify_passed=verify_passed,
                    pack_count=len(pack_ids),
                    pack_ids=pack_ids,
                    iso_size_bytes=sv.iso_size_bytes,
                )
                receipts.append(receipt)

                # ISO cleanup moved outside main try block (see below)
                # to prevent unlink failures from rolling back the burn.

            except Exception as original_exc:
                try:
                    self._conn.rollback()
                    if not is_reburn:
                        update_status(self._conn, sv.volume_id, "STAGING")
                except Exception as cleanup_exc:
                    _logger.error(
                        "Error during exception cleanup: %s",
                        cleanup_exc,
                        exc_info=True,
                    )
                    raise original_exc from cleanup_exc
                # If at least one volume was burned, mark session PARTIAL
                if receipts:
                    update_session_status(self._conn, session_id, "PARTIAL")
                raise original_exc

            # Remove ISO after successful verified burn to free staging space.
            # This is outside the main try/except to avoid rolling back verified burns
            # if the ISO file deletion fails (e.g., permission error, stale NFS handle).
            if verify_passed and not skip_burn and iso_path.exists():
                try:
                    iso_path.unlink()
                    _logger.debug("Deleted ISO after successful burn: %s", iso_path)
                except OSError as exc:
                    _logger.warning(
                        "Failed to delete ISO after burn (disc is safe): %s — %s",
                        iso_path, exc,
                    )

        # Update session status — any failed verify leaves the session
        # PARTIAL so it can never masquerade as a completed copy set;
        # re-running the burn at the same location is the recovery
        # path.  [BURN-05]
        any_failed = any(not r.verify_passed for r in receipts)
        update_session_status(
            self._conn, session_id, "PARTIAL" if any_failed else "COMPLETE",
        )

        # Write receipts JSON
        session_vols_info = get_session_volumes(self._conn, session_id)
        if session_vols_info:
            session_dir = Path(session_vols_info[0].iso_path).parent
            self._write_receipts(receipts, session_dir, location)

        return receipts

    def _verify_burned_disc(
        self,
        sv: SessionVolume,
        label: str,
        device: str,
        location: str,
        iso_path: Path,
    ) -> bool:
        """Three-step post-burn verification of the disc in *device*
        (BURN-04 + FMA-03).

        Step 1 — ``xorriso -check_media``: readability smoke test.
        Step 2 — PVD Volume ID must equal the volume label (wrong-disc
        guard).
        Step 3 — read back exactly the recorded ISO byte length from the
        device and compare its SHA-256 to the hash recorded at stage
        time.  ``-check_media`` alone never grants a pass when a hash is
        on record: any readable disc sitting in the tray would pass it.

        Records VERIFY_PASS/VERIFY_FAIL events with ``commit=False``
        (the caller owns the transaction).  Returns True only when every
        applicable step passed.
        """
        if not self._xorriso.verify_disc(device):
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location,
                detail="Post-burn read-back failed",
                commit=False,
            )
            return False

        # Disc identity: the PVD Volume ID was written from the volume
        # label at mastering time, so a mismatch means the disc in the
        # drive is not this volume at all (wrong disc never swapped, or
        # a silently failed burn left an old disc behind).  [FMA-03]
        disc_id = self._xorriso.read_disc_volume_id(device)
        if disc_id != label:
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location,
                detail=(
                    f"Disc in {device} identifies as '{disc_id}' — "
                    f"expected '{label}'. Wrong disc?"
                ),
                commit=False,
            )
            return False

        if not sv.iso_sha256:
            # No hash recorded (pre-Phase-13 session row): readability
            # is all the evidence available — preserve old semantics.
            add_event(
                self._conn, sv.volume_id, "VERIFY_PASS",
                location=location, detail="Post-burn read-back",
                commit=False,
            )
            return True

        iso_size = sv.iso_size_bytes
        if iso_size is None and iso_path.exists():
            # Pre-v7 session row: the ISO file is still around — take
            # the authoritative length from it.
            iso_size = iso_path.stat().st_size
        if iso_size is None:
            _logger.warning(
                "cannot device-verify %s: no recorded ISO size "
                "(pre-upgrade session) and the ISO file is gone — "
                "-check_media alone never grants VERIFIED when a hash "
                "is on record",
                label,
            )
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location,
                detail="device hash check impossible: no recorded ISO size "
                       "(pre-upgrade session); -check_media alone is "
                       "insufficient evidence",
                commit=False,
            )
            return False

        try:
            device_hash = self._device_reader(device, iso_size)
        except OSError as exc:
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location,
                detail=f"device read-back failed: {exc}",
                commit=False,
            )
            return False

        if device_hash != sv.iso_sha256:
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location,
                detail=(
                    f"device hash mismatch: expected {sv.iso_sha256[:8]}.., "
                    f"got {device_hash[:8]}.."
                ),
                commit=False,
            )
            return False

        add_event(
            self._conn, sv.volume_id, "VERIFY_PASS",
            location=location,
            detail=("Post-burn read-back + device hash match "
                    f"({device_hash[:8]}..)"),
            commit=False,
        )
        return True

    def clean_session(
        self,
        session_ref: str = "latest",
        *,
        force: bool = False,
    ) -> None:
        """Remove staged ISOs and staging directories for a session.

        Refuses to clean a session whose volumes were never burned: the
        ISOs/staging tree are the only burnable artifacts, while the catalog
        would keep claiming the packs "archived" on phantom STAGING volumes.
        With ``force=True`` those volumes are deleted first, returning their
        packs to the unarchived pool, then cleaning proceeds.
        """
        session_id = resolve_session_id(self._conn, session_ref)
        session_vols = get_session_volumes(self._conn, session_id)

        unburned = self._unburned_session_volumes(session_id)
        if unburned and not force:
            labels = ", ".join(label for _vid, label in unburned)
            raise ValueError(
                f"Session {session_id} has {len(unburned)} volume(s) that "
                f"were never burned ({labels}). Cleaning now would "
                f"permanently strand their packs as falsely 'archived'. "
                f"Burn the session first ('lcsas burn --session {session_id}'), "
                f"or abort it ('lcsas session abort {session_id}') to return "
                f"the packs to the unarchived pool. "
                f"Use --force to clean AND abort in one step."
            )

        for vid, label in unburned:
            reclaim = self._reclaim_summary([vid])
            _logger.info(
                "Volume %s was never burned — deleting; %d pack(s) return "
                "to the unarchived pool",
                label, reclaim.packs_reclaimed,
            )
            delete_volume(self._conn, vid)

        for sv in session_vols:
            iso_path = Path(sv.iso_path)
            if iso_path.exists():
                iso_path.unlink()

        # Get session staging dir from the session
        from lcsas.db.sessions import get_session
        session = get_session(self._conn, session_id)
        staging_dir = Path(session.staging_dir)
        if staging_dir.exists():
            safe_remove_tree(staging_dir)

        # Aborted sessions reuse CLEANED: the sessions.status CHECK allows
        # only STAGED/PARTIAL/COMPLETE/CLEANED (no table rebuild for an
        # ABORTED value); the log carries the distinction.
        update_session_status(self._conn, session_id, "CLEANED")

    def abort_session(self, session_ref: str = "latest") -> AbortSummary:
        """Abort a never-burned session: reclaim its packs, then clean it.

        Deletes the session's STAGING/BURNING volumes (returning their packs
        to the unarchived pool so the next `lcsas stage` picks them up),
        removes the ISOs and staging tree, and marks the session CLEANED.
        """
        from lcsas.log import get_logger
        logger = get_logger()

        session_id = resolve_session_id(self._conn, session_ref)
        unburned = self._unburned_session_volumes(session_id)
        summary = self._reclaim_summary([vid for vid, _label in unburned])
        summary.labels = [label for _vid, label in unburned]

        self.clean_session(session_id, force=True)

        logger.info(
            "Aborted session %s: deleted %d volume(s)%s, reclaimed %d "
            "pack(s), %s bytes returned to the unarchived pool.",
            session_id,
            summary.volumes_deleted,
            f" ({', '.join(summary.labels)})" if summary.labels else "",
            summary.packs_reclaimed,
            f"{summary.bytes_reclaimed:,}",
        )
        return summary

    def abort_volume(self, label: str) -> AbortSummary:
        """Delete a single stranded never-burned volume by label.

        Covers volumes with no session (crash-window strandings from old
        catalogs).  Refuses anything not in STAGING/BURNING.
        """
        from lcsas.db.volumes import get_volume_by_label
        from lcsas.log import get_logger
        logger = get_logger()

        vol = get_volume_by_label(self._conn, label)
        if vol is None:
            raise ValueError(f"Volume '{label}' not found in catalog.")
        if vol.status not in ("STAGING", "BURNING"):
            raise ValueError(
                f"Volume {label} has status {vol.status} — only never-burned "
                f"(STAGING/BURNING) volumes can be aborted."
            )
        has_copy = self._conn.execute(
            "SELECT 1 FROM volume_copies WHERE volume_id = ? LIMIT 1",
            (vol.volume_id,),
        ).fetchone()
        if has_copy is not None:
            raise ValueError(
                f"Volume {label} has recorded copies — a physical disc "
                f"exists, refusing to abort it."
            )

        summary = self._reclaim_summary([vol.volume_id])
        summary.labels = [label]
        delete_volume(self._conn, vol.volume_id)

        logger.info(
            "Aborted volume %s: reclaimed %d pack(s), %s bytes returned "
            "to the unarchived pool.",
            label, summary.packs_reclaimed, f"{summary.bytes_reclaimed:,}",
        )
        return summary

    def _unburned_session_volumes(self, session_id: str) -> list[tuple[int, str]]:
        """Return (volume_id, label) for a session's never-burned volumes.

        Never-burned = status STAGING/BURNING AND zero volume_copies rows.
        A volume with any copy row corresponds to a physical disc (however
        stale its status) and is never deleted by clean/abort (FMA-01).
        """
        rows = self._conn.execute(
            """SELECT v.volume_id, v.label FROM volumes v
               JOIN session_volumes sv ON sv.volume_id = v.volume_id
               WHERE sv.session_id = ?
                 AND v.status IN ('STAGING', 'BURNING')
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_copies vc
                     WHERE vc.volume_id = v.volume_id
                 )
               ORDER BY v.label""",
            (session_id,),
        ).fetchall()
        return [(row["volume_id"], row["label"]) for row in rows]

    def _reclaim_summary(self, volume_ids: list[int]) -> AbortSummary:
        """Count the distinct packs/bytes linked to the given volumes."""
        if not volume_ids:
            return AbortSummary(
                volumes_deleted=0, packs_reclaimed=0, bytes_reclaimed=0,
            )
        placeholders = ",".join("?" for _ in volume_ids)
        row = self._conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM (
                    SELECT DISTINCT p.pack_id, p.size_bytes
                    FROM packs p
                    JOIN volume_packs vp ON vp.pack_id = p.pack_id
                    WHERE vp.volume_id IN ({placeholders})
                )""",
            volume_ids,
        ).fetchone()
        return AbortSummary(
            volumes_deleted=len(volume_ids),
            packs_reclaimed=int(row[0]),
            bytes_reclaimed=int(row[1]),
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _gather_packs_for_staging(
        self,
        for_location: str | None = None,
        repo_ids: list[str] | None = None,
    ) -> list[Pack]:
        """Gather packs that need to be staged.

        If for_location is set, returns packs missing at that location.
        Otherwise, returns globally unarchived packs.
        """
        if for_location:
            packs = get_unarchived_or_missing_at_location(
                self._conn, for_location,
            )
        else:
            all_packs: list[Pack] = []
            if repo_ids:
                for rid in repo_ids:
                    all_packs.extend(get_unarchived_packs(self._conn, rid))
            else:
                all_packs = get_unarchived_packs(self._conn)
            packs = all_packs

        # Apply repo filter if both for_location and repo_ids specified
        if for_location and repo_ids:
            packs = [p for p in packs if p.repo_id in repo_ids]

        return packs

    def _multi_bin_pack(
        self,
        packs: list[Pack],
        media_type: MediaType,
    ) -> list[tuple[list[Pack], int]]:
        """Bin-pack packs into multiple volumes.

        Returns list of (selected_packs, total_bytes) tuples,
        one per volume.
        """
        remaining_packs = list(packs)
        volume_plans: list[tuple[list[Pack], int]] = []

        while remaining_packs:
            items = [(p.sha256, p.size_bytes) for p in remaining_packs]
            selected_items, leftover_items = first_fit_decreasing(
                items,
                capacity=media_type.usable_bytes,
                reserved=self._config.metadata_reserve_bytes,
            )

            if not selected_items:
                # None of the remaining packs fit — they're all too large.
                usable = media_type.usable_bytes - self._config.metadata_reserve_bytes
                oversized = [
                    p for p in remaining_packs if p.size_bytes > usable
                ]
                if oversized:
                    _logger.error(
                        "%d pack(s) exceed %s usable capacity (%d bytes) "
                        "and can never be archived on this media type: %s",
                        len(oversized),
                        media_type.name,
                        usable,
                        ", ".join(
                            f"{p.sha256[:12]} ({p.size_bytes:,} B)"
                            for p in oversized[:10]
                        ),
                    )
                    details = ", ".join(
                        f"{p.sha256[:12]} ({p.size_bytes:,} bytes)" for p in oversized
                    )
                    raise ValueError(
                        f"{len(oversized)} pack(s) exceed {media_type.name} usable "
                        f"capacity ({usable:,} bytes) and can never be archived on "
                        f"this media type: {details}. "
                        f"Consider using a larger media type (e.g. BDXL100)."
                    )
                raise ValueError(
                    f"Cannot fit remaining packs into {media_type.name} "
                    f"(usable={media_type.usable_bytes}, "
                    f"reserved={self._config.metadata_reserve_bytes})"
                )

            selected_hashes = {sha for sha, _size in selected_items}
            selected_packs = [
                p for p in remaining_packs if p.sha256 in selected_hashes
            ]
            total_bytes = sum(s for _, s in selected_items)
            volume_plans.append((selected_packs, total_bytes))

            remaining_packs = [
                p for p in remaining_packs if p.sha256 not in selected_hashes
            ]

        return volume_plans

    def _write_session_manifest(
        self,
        session_id: str,
        session_dir: Path,
        manifests: list[BurnManifest],
    ) -> Path:
        """Write session.json manifest to the session directory."""
        manifest_data = {
            "session_id": session_id,
            "created_at": datetime.now(UTC).isoformat(),
            "media_type": manifests[0].media_type.name if manifests else "",
            "status": "STAGED",
            "volumes": [
                {
                    "volume_id": m.volume_id,
                    "label": m.volume_label,
                    "uuid": m.volume_uuid,
                    "iso_path": str(m.iso_path) if m.iso_path else "",
                    "staging_path": str(m.staging_path),
                    "total_data_bytes": m.total_data_bytes,
                    "pack_count": len(m.selected_packs),
                    "pack_ids": [p.pack_id for p in m.selected_packs],
                }
                for m in manifests
            ],
        }
        manifest_path = session_dir / "session.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return manifest_path

    def _write_receipts(
        self,
        receipts: list[BurnReceipt],
        session_dir: Path,
        location: str,
    ) -> list[Path]:
        """Write burn receipt JSON files."""
        receipts_dir = session_dir / "receipts"
        ensure_dir(receipts_dir)

        paths: list[Path] = []
        for receipt in receipts:
            receipt_path = receipts_dir / (
                f"{receipt.volume_label}_{location}.json"
            )
            with open(receipt_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "volume_label": receipt.volume_label,
                        "volume_id": receipt.volume_id,
                        "session_id": receipt.session_id,
                        "location": receipt.location,
                        "device": receipt.device,
                        "burn_date": receipt.burn_date,
                        "iso_sha256": receipt.iso_sha256,
                        "iso_size_bytes": receipt.iso_size_bytes,
                        "verify_passed": receipt.verify_passed,
                        "pack_count": receipt.pack_count,
                        "pack_ids": receipt.pack_ids,
                    },
                    f,
                    indent=2,
                )
                f.flush()
                os.fsync(f.fileno())
            paths.append(receipt_path)

        return paths
