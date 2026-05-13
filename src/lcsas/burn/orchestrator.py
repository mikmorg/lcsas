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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lcsas.binpack.algorithm import first_fit_decreasing
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig
from lcsas.db.locations import ensure_location
from lcsas.db.models import Pack, Volume
from lcsas.db.queries import get_unarchived_or_missing_at_location, get_unarchived_packs
from lcsas.db.repos import list_repos
from lcsas.db.sessions import (
    add_session_volume,
    create_session,
    get_session_volumes,
    resolve_session_id,
    update_session_status,
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
from lcsas.staging.builder import StagingBuilder
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
    ) -> None:
        self._config = config
        self._conn = conn
        self._xorriso = xorriso
        self._dvdisaster = dvdisaster

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
        skip_ecc: bool = False,
    ) -> Volume:
        """Execute the burn: create ISO, add ECC, burn to disc.

        Args:
            manifest: A BurnManifest from prepare().
            iso_output: Override path for the ISO file.
            skip_burn: If True, create ISO but don't burn to physical media.
            skip_ecc: If True, skip DVDisaster ECC augmentation.

        Returns:
            The finalized Volume object.
        """
        # Preflight: verify required binaries exist and meet minimum versions.
        from lcsas.utils.subprocess import SubprocessRunnerBase, check_binary_version
        if isinstance(self._xorriso, SubprocessRunnerBase):
            # xorriso 1.4.0+ required for reliable ISO-9660 level 3 support.
            check_binary_version(self._xorriso._binary, min_version=(1, 4, 0))
        if not skip_ecc and isinstance(self._dvdisaster, SubprocessRunnerBase):
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
            if not skip_ecc:
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
                self._xorriso.burn_iso(iso_path, self._config.optical_device)
                # Verify disc before marking VERIFIED
                verify_ok = self._xorriso.verify_disc(self._config.optical_device)
                if not verify_ok:
                    raise ValueError("Post-burn verification failed")

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
        skip_ecc: bool = False,
    ) -> BurnManifest:
        """Build staging dir, register volume, inject metadata, optionally create ISO.

        This is the shared core of :meth:`prepare` (iso_output=None) and
        :meth:`stage` (iso_output set).  Returns a :class:`BurnManifest`
        describing the result.

        Args:
            selected_packs: Packs to include on this volume.
            total_bytes: Sum of pack sizes.
            media_type: Target media.
            vol_label: Generated volume label.
            vol_uuid: Generated volume UUID.
            staging_root: Directory to stage files into.
            iso_output: If set, create an ISO at this path (+ optional ECC).
            skip_ecc: Skip DVDisaster ECC augmentation.

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
            if mirror_path is None:
                continue
            data_dir = mirror_path / "data"
            if data_dir.is_dir():
                builder.stage_packs(repo_packs, data_dir)

        # 2. Inject holographic metadata
        injector = HolographicInjector(staging_root)
        injector.inject_metadata(mirror_paths)

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
        self._conn.commit()

        # 4. Inject catalog AFTER DB commit.
        #    Checkpoint WAL so all committed data is in the main .db file,
        #    then copy it to staging.  If this fails, roll back the volume
        #    registration so DB and disc catalog remain in sync.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            injector.inject_catalog(self._config.db_path)
        except Exception as exc:
            _logger.error(
                "Catalog injection into staging failed; rolling back volume "
                "registration for %s: %s",
                vol_label, exc,
            )
            delete_volume(self._conn, volume.volume_id)
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

            if not skip_ecc:
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
        skip_ecc: bool = False,
        dry_run: bool = False,
    ) -> StageResult:
        """Stage all unarchived packs into ISOs, creating a burn session.

        Handles multi-volume scenarios: if data exceeds one disc, multiple
        volumes and ISOs are created within a single session.

        Args:
            media_type: Target media type (defaults to config).
            for_location: If set, stage only packs missing at this location.
            repo_ids: Optional filter to specific repositories.
            pack_sha256s: If set, stage only packs with these SHA-256 hashes.
            skip_ecc: If True, skip ECC augmentation of ISOs.
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

            # Stage, register, inject metadata, create ISO + ECC
            manifest = self._stage_single_volume(
                selected_packs=selected_packs,
                total_bytes=total_bytes,
                media_type=mt,
                vol_label=vol_label,
                vol_uuid=vol_uuid,
                staging_root=staging_root,
                iso_output=iso_path,
                skip_ecc=skip_ecc,
            )

            # Compute ISO hash
            iso_hash = ""
            if manifest.iso_path and manifest.iso_path.exists():
                iso_hash = sha256_file(manifest.iso_path)

            # Register in session
            add_session_volume(
                self._conn,
                session_id=session_id,
                volume_id=manifest.volume_id,
                iso_path=str(manifest.iso_path or iso_path),
                iso_sha256=iso_hash,
                commit=False,
            )
            self._conn.commit()

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

                # Post-burn verification  [S1]
                verify_passed = True
                if not skip_burn:
                    verify_ok = self._xorriso.verify_disc(device)
                    if verify_ok:
                        add_event(
                            self._conn, sv.volume_id, "VERIFY_PASS",
                            location=location, detail="Post-burn read-back",
                            commit=False,
                        )
                    else:
                        add_event(
                            self._conn, sv.volume_id, "VERIFY_FAIL",
                            location=location,
                            detail="Post-burn read-back failed",
                            commit=False,
                        )
                        verify_passed = False

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

                # Record copy at location
                add_volume_copy(
                    self._conn,
                    volume_id=sv.volume_id,
                    location=location,
                    commit=False,
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

        # Update session status
        update_session_status(self._conn, session_id, "COMPLETE")

        # Write receipts JSON
        session_vols_info = get_session_volumes(self._conn, session_id)
        if session_vols_info:
            session_dir = Path(session_vols_info[0].iso_path).parent
            self._write_receipts(receipts, session_dir, location)

        return receipts

    def clean_session(self, session_ref: str = "latest") -> None:
        """Remove staged ISOs and staging directories for a session."""
        session_id = resolve_session_id(self._conn, session_ref)
        session_vols = get_session_volumes(self._conn, session_id)

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

        update_session_status(self._conn, session_id, "CLEANED")

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
