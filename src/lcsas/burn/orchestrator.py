"""Burn orchestrator — the central pipeline for archiving packs to media."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lcsas.binpack.algorithm import first_fit_decreasing
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig
from lcsas.db.models import Pack, Volume
from lcsas.db.queries import get_unarchived_packs
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
from lcsas.utils.labels import generate_uuid, generate_volume_label, next_seq_num


@dataclass
class BurnManifest:
    """Describes a prepared burn operation."""

    volume_label: str
    volume_uuid: str
    volume_id: int
    media_type: MediaType
    selected_packs: list[Pack]
    total_data_bytes: int
    staging_path: Path
    iso_path: Path | None = None


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
