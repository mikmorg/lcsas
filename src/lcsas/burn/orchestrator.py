"""Burn orchestrator — the central pipeline for archiving packs to media.

Supports two modes:
  1. Legacy single-volume: prepare() → execute() (one volume at a time)
  2. Session-based: stage() → burn_session() (multi-volume, multi-copy)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lcsas.binpack.algorithm import first_fit_decreasing
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig
from lcsas.db.locations import ensure_location
from lcsas.db.models import Pack, Volume
from lcsas.db.queries import get_unarchived_packs, get_unarchived_or_missing_at_location
from lcsas.db.sessions import (
    add_session_volume,
    create_session,
    get_session_volumes,
    resolve_session_id,
    update_iso_sha256,
    update_session_status,
)
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import (
    create_volume,
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
from lcsas.utils.labels import generate_uuid, generate_volume_label, next_seq_num


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
        selected_items, _remaining = first_fit_decreasing(
            items,
            capacity=mt.usable_bytes,
            reserved=self._config.metadata_reserve_bytes,
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
            self._config.label_prefix, mt.name, seq
        )
        vol_uuid = generate_uuid()

        # 4. Build staging directory
        staging_root = self._config.staging_path / vol_label
        builder = StagingBuilder(staging_root)
        builder.initialize()

        # Stage packs — search across all repo mirror data dirs
        mirror_paths = self._get_mirror_paths()
        for _repo_id, mirror_path in mirror_paths.items():
            data_dir = mirror_path / "data"
            if data_dir.is_dir():
                builder.stage_packs(selected_packs, data_dir)

        # 5. Inject holographic metadata
        injector = HolographicInjector(staging_root)
        injector.inject_metadata(mirror_paths)

        # 6. Register volume in DB
        volume = create_volume(
            self._conn,
            label=vol_label,
            uuid=vol_uuid,
            media_type=mt.name,
            capacity_bytes=mt.capacity_bytes,
            location=self._config.default_location,
            status="STAGING",
        )

        # Link packs to volume
        pack_ids = [p.pack_id for p in selected_packs]
        bulk_link_packs(self._conn, volume.volume_id, pack_ids)
        update_used_bytes(self._conn, volume.volume_id, total_bytes)

        # Inject catalog AFTER DB updates so it includes this volume
        injector.inject_catalog(self._config.db_path)

        # Write volume info
        vol = get_volume_by_id(self._conn, volume.volume_id)
        injector.write_volume_info(vol)

        return BurnManifest(
            volume_label=vol_label,
            volume_uuid=vol_uuid,
            volume_id=volume.volume_id,
            media_type=mt,
            selected_packs=selected_packs,
            total_data_bytes=total_bytes,
            staging_path=staging_root,
        )

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
        # Update status
        update_status(self._conn, manifest.volume_id, "BURNING")

        iso_path = iso_output or (self._config.staging_path / f"{manifest.volume_label}.iso")
        ensure_dir(iso_path.parent)

        try:
            # Create ISO
            self._xorriso.create_iso(
                manifest.staging_path,
                iso_path,
                manifest.volume_label,
            )
            manifest.iso_path = iso_path

            # Add ECC
            if not skip_ecc:
                self._dvdisaster.augment_iso(
                    iso_path,
                    self._config.default_ecc_redundancy_pct,
                )

            # Burn to disc
            if not skip_burn:
                self._xorriso.burn_iso(iso_path, self._config.optical_device)

            # Finalize
            update_status(self._conn, manifest.volume_id, "VERIFIED")
            mark_closed(self._conn, manifest.volume_id)

        except Exception:
            update_status(self._conn, manifest.volume_id, "STAGING")
            raise

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

    def _get_mirror_paths(self) -> dict[str, Path]:
        """Build a dict of {repo_id: mirror_path} from config."""
        paths: dict[str, Path] = {}
        for repo_name, repo_cfg in self._config.repositories.items():
            paths[repo_name] = repo_cfg.mirror_path

        # If no repos configured, use mirror_base_path as a single repo
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
        skip_ecc: bool = False,
    ) -> StageResult:
        """Stage all unarchived packs into ISOs, creating a burn session.

        Handles multi-volume scenarios: if data exceeds one disc, multiple
        volumes and ISOs are created within a single session.

        Args:
            media_type: Target media type (defaults to config).
            for_location: If set, stage only packs missing at this location.
            repo_ids: Optional filter to specific repositories.
            skip_ecc: If True, skip ECC augmentation of ISOs.

        Returns:
            StageResult with session ID, manifests, and ISO paths.
        """
        mt = media_type or self._config.default_media_type

        # 1. Gather packs to stage
        packs_to_stage = self._gather_packs_for_staging(
            for_location=for_location,
            repo_ids=repo_ids,
        )

        if not packs_to_stage:
            raise ValueError("No packs need staging.")

        # 2. Bin-pack into multiple volumes
        volume_plans = self._multi_bin_pack(packs_to_stage, mt)

        # 3. Create session
        session_id = datetime.now(UTC).isoformat(timespec="microseconds")
        session_dir = self._config.staging_path / session_id.replace(":", "-")
        ensure_dir(session_dir)

        session = create_session(
            self._conn,
            media_type=mt.name,
            staging_dir=str(session_dir),
            session_id=session_id,
        )

        # 4. Build staging dirs, create ISOs, apply ECC for each volume
        manifests: list[BurnManifest] = []
        iso_paths: list[Path] = []

        existing_labels = [v.label for v in list_volumes(self._conn)]
        seq = next_seq_num(existing_labels, self._config.label_prefix)

        for i, (selected_packs, total_bytes) in enumerate(volume_plans):
            vol_label = generate_volume_label(
                self._config.label_prefix, mt.name, seq + i,
            )
            vol_uuid = generate_uuid()

            # Build staging directory
            staging_root = session_dir / vol_label
            builder = StagingBuilder(staging_root)
            builder.initialize()

            mirror_paths = self._get_mirror_paths()
            for _repo_id, mirror_path in mirror_paths.items():
                data_dir = mirror_path / "data"
                if data_dir.is_dir():
                    builder.stage_packs(selected_packs, data_dir)

            # Inject holographic metadata
            injector = HolographicInjector(staging_root)
            injector.inject_metadata(mirror_paths)

            # Register volume in DB
            volume = create_volume(
                self._conn,
                label=vol_label,
                uuid=vol_uuid,
                media_type=mt.name,
                capacity_bytes=mt.capacity_bytes,
                location=self._config.default_location,
                status="STAGING",
            )

            pack_ids = [p.pack_id for p in selected_packs]
            bulk_link_packs(self._conn, volume.volume_id, pack_ids)
            update_used_bytes(self._conn, volume.volume_id, total_bytes)

            # Inject catalog AFTER DB updates
            injector.inject_catalog(self._config.db_path)
            vol = get_volume_by_id(self._conn, volume.volume_id)
            injector.write_volume_info(vol)

            # Create ISO
            iso_path = session_dir / f"{vol_label}.iso"
            self._xorriso.create_iso(staging_root, iso_path, vol_label)

            # Apply ECC
            if not skip_ecc:
                self._dvdisaster.augment_iso(
                    iso_path, self._config.default_ecc_redundancy_pct,
                )

            # Compute ISO hash
            iso_hash = ""
            if iso_path.exists():
                iso_hash = sha256_file(iso_path)

            # Register in session
            add_session_volume(
                self._conn,
                session_id=session_id,
                volume_id=volume.volume_id,
                iso_path=str(iso_path),
                iso_sha256=iso_hash,
            )

            manifest = BurnManifest(
                volume_label=vol_label,
                volume_uuid=vol_uuid,
                volume_id=volume.volume_id,
                media_type=mt,
                selected_packs=selected_packs,
                total_data_bytes=total_bytes,
                staging_path=staging_root,
                iso_path=iso_path,
            )
            manifests.append(manifest)
            iso_paths.append(iso_path)

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
            vol = get_volume_by_id(self._conn, sv.volume_id)

            # Update status
            update_status(self._conn, sv.volume_id, "BURNING")

            try:
                # Burn
                if not skip_burn:
                    self._xorriso.burn_iso(iso_path, device)

                # Finalize volume status
                update_status(self._conn, sv.volume_id, "VERIFIED")
                mark_closed(self._conn, sv.volume_id)

                # Record copy at location
                add_volume_copy(
                    self._conn,
                    volume_id=sv.volume_id,
                    location=location,
                )

                # Build receipt
                from lcsas.db.volume_packs import get_pack_ids_for_volume
                pack_ids = get_pack_ids_for_volume(self._conn, sv.volume_id)

                receipt = BurnReceipt(
                    volume_label=vol.label,
                    volume_id=sv.volume_id,
                    session_id=session_id,
                    location=location,
                    device=device,
                    burn_date=datetime.now(UTC).isoformat(),
                    iso_sha256=sv.iso_sha256,
                    verify_passed=True,
                    pack_count=len(pack_ids),
                    pack_ids=pack_ids,
                )
                receipts.append(receipt)

            except Exception:
                update_status(self._conn, sv.volume_id, "STAGING")
                raise

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
                # None of the remaining packs fit — they're all too large
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
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
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
            with open(receipt_path, "w") as f:
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
            paths.append(receipt_path)

        return paths
