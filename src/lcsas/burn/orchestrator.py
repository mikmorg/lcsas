"""Burn orchestrator — the central pipeline for archiving packs to media.

A burn session: stage() masters every volume's ISO, then burn_session()
burns them (multi-volume, multi-copy).  The single-volume
prepare()/execute()/abort() path was removed in #699.
"""

from __future__ import annotations

import contextlib
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
from lcsas.burn.operator import (
    ConsoleOperatorPrompt,
    NullOperatorPrompt,
    OperatorPrompt,
    eject_tray,
)
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.key_escrow import (
    EscrowDriftError,
    detect_escrow_drift,
    get_split_for_repo,
    recorded_splits,
)
from lcsas.db.label_mark import label_seq, next_label_seq, raise_label_high_water
from lcsas.db.lifecycle import Refusal, VolumeLifecycle
from lcsas.db.locations import ensure_location
from lcsas.db.models import Pack, SessionVolume
from lcsas.db.queries import (
    get_unarchived_or_missing_at_location,
    get_unarchived_packs,
)
from lcsas.db.repos import list_repos
from lcsas.db.sessions import (
    add_session_volume,
    create_session,
    delete_empty_session,
    get_session_volumes,
    resolve_session_id,
    update_session_status,
    update_session_volume_iso,
)
from lcsas.db.volume_events import add_event
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import (
    create_volume,
    get_volume_by_id,
    update_used_bytes,
)
from lcsas.ecc.dvdisaster import (
    MIN_EFFECTIVE_REDUNDANCY_PCT,
    DVDisasterRunner,
    rs03_pad_target_bytes,
    rs03_redundancy_pct,
    rs03_staged_image_bytes,
    smallest_fitting_medium_bytes,
)
from lcsas.exceptions import BurnError, StagingError
from lcsas.iso.xorriso import (
    XORRISO_DEFAULT_PADDING_BYTES,
    MediaStatus,
    XorrisoRunner,
)
from lcsas.packs.snapshot_coverage import LeftOutSnapshot, snapshot_coverage
from lcsas.staging.builder import MirrorUnavailableError, StagingBuilder
from lcsas.staging.metadata import (
    DiscMetadataSize,
    HolographicInjector,
    measure_disc_metadata,
    pack_disc_bytes,
)
from lcsas.staging.repo_marker import repo_markers
from lcsas.utils.fs import dir_size_bytes, ensure_dir, safe_remove_tree
from lcsas.utils.hashing import sha256_file
from lcsas.utils.labels import (
    generate_session_id,
    generate_uuid,
    generate_volume_label,
)
from lcsas.utils.timestamps import utc_now

_logger = logging.getLogger(__name__)

# #731: the oldest tool versions stage() accepts (docs/workflows/
# stage-and-burn.md).  xorriso 1.4.0 for reliable ISO 9660 level 3;
# dvdisaster 0.79 for RS03 augmented images (only on ECC media).
XORRISO_MIN_VERSION: tuple[int, ...] = (1, 4, 0)
DVDISASTER_MIN_VERSION: tuple[int, ...] = (0, 79, 0)


class LowRedundancyError(StagingError):
    """A volume would carry too little RS03 parity (#674)."""


class NothingToStageError(StagingError, ValueError):
    """Every unarchived pack already waits on a staged volume (#672)."""


class DiscMetadataTooLargeError(StagingError):
    """One disc cannot hold the disc metadata and a pack (#811)."""


class WaitingSessionGapError(StagingError):
    """Staging now would leave snapshots off the new discs for good (#946).

    A snapshot whose packs sit only on a volume waiting to be burned
    cannot go on this session's discs.  Once both sessions are burned,
    the highest-labelled disc would lack it, and nothing would ever stage
    it again; the heir restores from that disc.
    """


def _describe_waiting(held: dict[str, list[str]]) -> str:
    """``LABEL, LABEL in session S; ...`` for the waiting-volume messages."""
    return "; ".join(
        f"{', '.join(labels)} in session {sid}"
        for sid, labels in sorted(held.items())
    )


def _unrecorded_burn_refusal(
    what: str, volumes: list[tuple[str, str]],
) -> str:
    """The clean/abort refusal for burned volumes with no disc record.

    *volumes* is (label, why) per volume, as from
    ``BurnOrchestrator._unrecorded_burned_session_volumes``.
    """
    listed = "; ".join(f"{label} is {why}" for label, why in volumes)
    one = len(volumes) == 1
    label = volumes[0][0] if one else "<label>"
    return (
        f"Refusing to clean or abort {what}: {listed}. A disc of "
        f"{'it' if one else 'each'} may exist (a catalog rebuilt from "
        f"discs records the newest discs like this), so "
        f"{'it is' if one else 'they are'} never deleted, even with "
        f"--force. Nothing was deleted. For each disc you hold, put it "
        f"in the drive and run 'lcsas verify {label} --disc --location "
        f"<where it is kept>', or import its burn receipt with 'lcsas "
        f"catalog import-receipts'. That records the disc; then run this "
        f"command again."
    )


def _burn_unknown_refusal(
    what: str, labels: list[str], abort_cmd: str,
) -> str:
    """The clean/abort refusal for volumes a catalog rebuild brought in
    with no record of whether they were burned (#758)."""
    one = len(labels) == 1
    return (
        f"Refusing to clean or abort {what}: {', '.join(labels)} "
        f"{'comes' if one else 'come'} from a catalog rebuilt from discs, "
        f"and no disc it read shows whether {'it was' if one else 'they were'}"
        f" burned. A disc of {'it' if one else 'each'} may exist. Nothing "
        f"was deleted. For each disc you hold, put it in the drive and run "
        f"'lcsas verify <label> --disc --location <where it is kept>', or "
        f"import its burn receipt with 'lcsas catalog import-receipts'. If "
        f"you are sure no disc of {'it' if one else 'them'} was ever "
        f"burned, run '{abort_cmd} --never-burned'."
    )


def _abort_volume_refusal(label: str, refusal: Refusal) -> str:
    """Why ``session abort --volume`` refuses *label*
    (:meth:`~lcsas.db.lifecycle.VolumeLifecycle.may_delete`)."""
    if refusal.kind == "status":
        return (
            f"Volume {label} has status {refusal.status} — only never-burned "
            f"(STAGING/BURNING) volumes can be aborted."
        )
    if refusal.kind == "disc":
        return (
            f"Volume {label} has a recorded disc — a burned disc "
            f"exists, refusing to abort it."
        )
    if refusal.kind == "burned":
        return _unrecorded_burn_refusal(
            f"volume {label}",
            [(label, f"{refusal.status}, but {refusal.evidence}")],
        )
    return _burn_unknown_refusal(
        f"volume {label}", [label], f"lcsas session abort --volume {label}",
    )


def _complete_snapshots(
    snapshot_needs: dict[str, dict[str, frozenset[str]]], held: set[str],
) -> dict[str, set[str]]:
    """Per repo_id, the snapshots whose needed packs are all in *held*."""
    return {
        repo_id: {sid for sid, need in needs.items() if need <= held}
        for repo_id, needs in snapshot_needs.items()
    }


def _snapshot_files(staging_root: Path, repo_id: str) -> list[Path]:
    """The snapshot files of a repository in a volume's staging tree."""
    snaps = staging_root / "metadata" / repo_id / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(p for p in snaps.iterdir() if p.is_file())


def unfinished_iso_path(iso_path: Path) -> Path:
    """Working name of an ISO that is still being mastered or augmented (#665).

    ``<label>.iso`` becomes ``<label>.unfinished``.  Nothing looks for this
    name: it is renamed to ``<label>.iso`` only once the ISO is finished.
    """
    return iso_path.with_suffix(".unfinished")


#: Written into a burn session's staging folder when ``lcsas stage``
#: finished it: every volume staged, or the kept volumes of a stage that
#: stopped part-way made consistent (#813).  Its absence marks a stage
#: that was killed (#862).
SESSION_MANIFEST = "session.json"


def unfinished_stage_refusal(
    session_dir: Path, session_id: str | None,
) -> str | None:
    """Why a burn session's staging folder must not be burned, or None.

    A stage killed hard (SIGKILL, power loss) runs no clean-up.  Killed
    while it hard-linked the packs of a volume, it leaves the volumes
    before it with snapshot files whose packs are on no disc, so
    "latest" on those discs could not be restored (#862).  Such a folder
    has no :data:`SESSION_MANIFEST`: the stage writes it last.  A folder
    that does not exist is not refused here (its ISOs are missing, which
    the burn reports).  *session_id* None (``burn-iso``, which reads no
    catalog) names the session by its folder.
    """
    if not session_dir.is_dir() or (session_dir / SESSION_MANIFEST).is_file():
        return None
    if session_id is None:
        which = f"the burn session in {session_dir}"
        ref = "<session-id>"
        find = " ('lcsas session list' shows its id)"
    else:
        which = f"burn session {session_id}"
        ref = session_id
        find = ""
    return (
        f"The stage of {which} did not finish: its staging folder has no "
        f"{SESSION_MANIFEST}. Its discs could name a snapshot whose packs "
        f"are on no disc. Nothing was burned. Remove the session with "
        f"'lcsas session abort {ref}' (or 'lcsas stage --clean --session "
        f"{ref} --force'){find}, then run 'lcsas stage' again."
    )


def unfinished_stage_refusal_for_iso(iso_path: Path) -> str | None:
    """:func:`unfinished_stage_refusal` for an ISO burned on its own
    (``lcsas burn-iso``).

    Only an ISO still in its staging folder can be checked: that folder
    holds the volume's tree (``<label>/volume_info.json``) beside
    ``<label>.iso``.  An ISO copied elsewhere (the portable workflow
    ships ISOs alone) is not refused.
    """
    session_dir = iso_path.resolve().parent
    if not (session_dir / iso_path.stem / "volume_info.json").is_file():
        return None
    return unfinished_stage_refusal(session_dir, None)


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
    # SHA-256 and byte length of the finished (post-ECC) ISO, recorded on
    # the session volume before the ISO gets its final name (#665).
    iso_sha256: str = ""
    iso_size_bytes: int | None = None


@dataclass
class StageResult:
    """Result of a multi-volume staging operation."""

    session_id: str
    media_type: MediaType
    staging_dir: Path
    manifests: list[BurnManifest] = field(default_factory=list)
    iso_paths: list[Path] = field(default_factory=list)
    # #657: per repository name, the snapshots left off these discs
    # because their packs are not all on discs yet.
    snapshots_left_out: dict[str, list[LeftOutSnapshot]] = field(
        default_factory=dict)
    # #657: repositories whose snapshots could not be checked (no
    # password_file, or the walk failed); every snapshot was copied.
    snapshots_unchecked: dict[str, str] = field(default_factory=dict)
    # #701: the last planned volume, left out because it filled less of
    # its pack space than the minimum fill.  When it was the only
    # volume, nothing is staged: session_id is "" and manifests is empty.
    underfilled: UnderfilledVolume | None = None


@dataclass(frozen=True)
class UnderfilledVolume:
    """A planned volume below the minimum fill, not staged (#701)."""

    pack_count: int
    data_bytes: int
    pack_space_bytes: int
    fill_pct: float
    min_fill_pct: float

    def describe(self, media_name: str) -> str:
        return (
            f"{self.pack_count} pack(s), {self.data_bytes:,} bytes: "
            f"{self.fill_pct:.1f}% of a {media_name} volume's pack space "
            f"({self.pack_space_bytes:,} bytes), below the minimum fill of "
            f"{self.min_fill_pct:g}%"
        )


@dataclass
class AbortSummary:
    """What an abort returned to the unarchived pool."""

    volumes_deleted: int
    packs_reclaimed: int
    bytes_reclaimed: int
    labels: list[str] = field(default_factory=list)
    # Consolidation sources returned to VERIFIED (#677, #817).
    sources_released: list[str] = field(default_factory=list)


@dataclass
class BurnReceipt:
    """Burn receipt for one disc, emitted after burning it."""

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
    # Post-ECC ISO byte length — travels with the hash so an imported burn
    # receipt (or a catalog rebuilt from the discs) can still device-
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
        allow_escrow_drift: bool = False,
        prompt: OperatorPrompt | None = None,
        allow_low_redundancy: bool = False,
        allow_no_password_hint: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        # #657: set by stage() for the session being staged.
        self._snapshot_allow: dict[str, set[str]] | None = None
        self._xorriso = xorriso
        self._dvdisaster = dvdisaster
        # FUP-01: injectable owner-interaction seam.  Defaults to the
        # TTY-backed console prompt; unit tests pass a recording fake and
        # `burn_session(interactive=False)` swaps in a no-op.
        self._prompt = prompt or ConsoleOperatorPrompt()
        # Injected for testability (BURN-04), matching the protocol-
        # injection pattern used for xorriso/dvdisaster: reads N bytes
        # from a device and returns the hex SHA-256.
        self._device_reader = device_reader
        # KEY-08 escape hatch: skip the split drift abort (logged as a
        # volume event on each staged volume).  For multi-config edge cases.
        self._allow_escrow_drift = allow_escrow_drift
        # #674 escape hatch: stage a volume whose RS03 redundancy is below
        # MIN_EFFECTIVE_REDUNDANCY_PCT (recorded as a volume event).
        self._allow_low_redundancy = allow_low_redundancy
        # #960: the owner staged with no password hint on purpose; each
        # volume records it (the CLI refuses otherwise).
        self._allow_no_password_hint = allow_no_password_hint

    def _stage_single_volume(
        self,
        selected_packs: list[Pack],
        total_bytes: int,
        media_type: MediaType,
        vol_label: str,
        vol_uuid: str,
        staging_root: Path,
        iso_output: Path,
        session_id: str,
        burn_session_position: tuple[int, int],
        location: str | None = None,
        reserve_through: int = 0,
    ) -> BurnManifest:
        """Build staging dir, register volume, inject metadata, create the ISO.

        One volume of :meth:`stage`.  Returns a :class:`BurnManifest`
        describing the result.

        ECC is applied automatically whenever the media type carries
        non-zero ECC overhead (i.e. all production media).
        Test media (``TEST_TINY``, 0% overhead) is implicitly skipped — see
        ``MediaType.ecc_overhead_pct`` in ``src/lcsas/config/media.py``.

        Args:
            selected_packs: Packs to include on this volume.
            total_bytes: Sum of pack sizes.
            media_type: Target media.
            vol_label: Generated volume label.
            vol_uuid: Generated volume UUID.
            staging_root: Directory to stage files into.
            iso_output: Create the ISO at this path (+ ECC for production
                media).
            session_id: Link the volume into session_volumes in the SAME
                transaction that creates it, so no crash can leave a
                volume row invisible to ``burn_session``.
            burn_session_position: ``(N, M)``: this is volume N of the M
                volumes ``stage`` builds in one burn session; printed in
                the disc's VOLUME_INFO.txt.
            reserve_through: The last label number of the burn session
                (#957); the label mark is raised to it with this volume.

        Returns:
            BurnManifest describing the staged volume.
        """
        # 1. Build staging directory
        builder = StagingBuilder(staging_root)
        builder.initialize()

        mirror_paths = self._get_mirror_paths()
        # Group packs by repository so we look for each pack only in its own
        # mirror.  This avoids MissingPacksError when multiple repositories
        # share a volume.
        packs_by_repo: dict[str, list[Pack]] = {}
        for p in selected_packs:
            packs_by_repo.setdefault(p.repo_id, []).append(p)

        # KEY-08: refuse to stage if disc share-instructions would drift from
        # the recorded split.  Runs BEFORE any catalog write or card render so
        # an abort leaves no wrong-instruction disc behind.
        drift_msg = self._escrow_preflight(list(packs_by_repo))

        done = 0
        for repo_id, repo_packs in packs_by_repo.items():
            mirror_path = mirror_paths.get(repo_id)
            data_dir = mirror_path / "data" if mirror_path is not None else None
            if data_dir is None or not data_dir.is_dir():
                # Fail loud BEFORE any catalog write: a silent skip here
                # would link packs to a volume that physically lacks them.
                raise MirrorUnavailableError(
                    repo_id, mirror_path, [p.sha256[:12] for p in repo_packs],
                )
            builder.stage_packs(repo_packs, data_dir, done_before=done,
                                volume_total=len(selected_packs))
            done += len(repo_packs)

        # Post-stage invariant: every selected pack must be present in the
        # staging tree before anything is written to the catalog.
        builder.assert_staged_complete(selected_packs)

        # KEY-08: the disc text renders share K/N from the recorded split
        # when present, so it reflects the split that was actually
        # performed.  #587: each repository's own recorded split, by name,
        # so a mixed archive (split + single passwords) prints both right.
        # The backup set markers (rc6 C1) read the same catalog: each
        # repository's name and its split, the record winning.
        config_names = self._config_names()
        escrow_kn = self._effective_kn(list(packs_by_repo))
        recorded = recorded_splits(self._conn, config_names)
        markers = repo_markers(
            self._conn, mirror_paths,
            config=self._config, config_names=config_names,
        )

        # 2. Inject repository metadata — strict for repositories with packs
        #    on this volume (the disc must stay self-describing for them) —
        #    and each repository's metadata/<repo_id>/LCSAS_BACKUP_SET.txt.
        injector = HolographicInjector(staging_root)
        injector.inject_metadata(
            mirror_paths, required_repos=set(packs_by_repo), markers=markers,
            snapshot_allow=self._snapshot_allow,
        )

        # 3. Register volume in the catalog (atomic transaction)
        volume = create_volume(
            self._conn,
            label=vol_label,
            uuid=vol_uuid,
            media_type=media_type.name,
            capacity_bytes=media_type.capacity_bytes,
            # #642/#643: the location the volume is staged FOR (stage
            # --for-location), else the configured default.  It is the
            # volume's intended first location; where its discs really
            # are lives in volume_copies.
            location=location or self._config.default_location,
            status="STAGING",
            commit=False,
        )
        # #676: remember the label number in the same transaction, so it
        # is never issued again even if this volume is later deleted.
        # #957: with the session's last number, before this volume's disc
        # catalog is copied.
        issued = label_seq(vol_label)
        if issued is not None:
            raise_label_high_water(self._conn, max(issued, reserve_through))

        pack_ids = [p.pack_id for p in selected_packs]
        bulk_link_packs(self._conn, volume.volume_id, pack_ids, commit=False)
        update_used_bytes(self._conn, volume.volume_id, total_bytes, commit=False)
        if drift_msg is not None:
            # Escape hatch was used: audit that this disc was staged despite
            # split drift, in the same transaction as the volume row.
            add_event(
                self._conn,
                volume.volume_id,
                "NOTE",
                detail=f"KEY-08 split drift override: {drift_msg}",
                commit=False,
            )
        if self._allow_no_password_hint:
            from lcsas.config.settings import usable_password_hint
            if not usable_password_hint(self._config):
                add_event(
                    self._conn,
                    volume.volume_id,
                    "NOTE",
                    detail=("#960 no password hint: staged with "
                            "--allow-no-password-hint; the disc says no "
                            "hint was given"),
                    commit=False,
                )
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
            # 4. Inject the disc catalog AFTER the catalog commit: an
            #    online backup of this connection's catalog (#812), so it
            #    holds this volume even while another process reads the
            #    master catalog.
            try:
                injector.inject_catalog(self._conn)
            except Exception as exc:
                _logger.error(
                    "Catalog injection into the staging area failed; rolling "
                    "back volume registration for %s: %s",
                    vol_label, exc,
                )
                raise RuntimeError(
                    f"Failed to inject catalog into the staging area for volume "
                    f"{vol_label}. Volume registration has been rolled back."
                ) from exc

            # 5. Write volume info files
            vol = get_volume_by_id(self._conn, volume.volume_id)
            # #959: name each retired backup set and its last disc.
            from lcsas.db.repos import retired_backup_sets
            retired = retired_backup_sets(self._conn)
            injector.write_volume_info(
                vol, packs=selected_packs,
                burn_session_position=burn_session_position,
                repo_names={rid: m.name for rid, m in markers.items()},
                retired=retired,
            )
            injector.write_restore_instructions()
            injector.write_standalone_restorer()
            if not media_type.is_test:
                injector.write_lcsas_source()
            injector.write_start_here(
                self._config, escrow_override=escrow_kn, recorded=recorded,
                retired=retired,
            )
            injector.write_key_info(
                self._config, escrow_override=escrow_kn, recorded=recorded
            )
            injector.write_config_summary(self._config)
            injector.write_disc_care(self._config)

            # 6-8. Master the ISO, apply ECC, record its hash, give it
            # its final name.
            iso_sha256, iso_size_bytes = self._master_volume(
                staging_root, iso_output, vol_label, volume.volume_id,
                media_type, session_id,
            )
        except BaseException:
            self._conn.rollback()  # drop any uncommitted partial state
            # Removes volume_packs links and the session_volumes row too,
            # so the packs reappear in get_unarchived_packs().
            VolumeLifecycle(self._conn).stage_failed(volume.volume_id)
            # The unfinished ISO is never burnable; free its space.
            with contextlib.suppress(OSError):
                unfinished_iso_path(iso_output).unlink(missing_ok=True)
            raise

        return BurnManifest(
            volume_label=vol_label,
            volume_uuid=vol_uuid,
            volume_id=volume.volume_id,
            media_type=media_type,
            selected_packs=selected_packs,
            total_data_bytes=total_bytes,
            staging_path=staging_root,
            iso_path=iso_output,
            iso_sha256=iso_sha256,
            iso_size_bytes=iso_size_bytes,
        )

    def _master_volume(
        self,
        staging_root: Path,
        iso_output: Path,
        vol_label: str,
        volume_id: int,
        media_type: MediaType,
        session_id: str,
    ) -> tuple[str, int]:
        """Master a volume's staging tree into its ISO (steps 6-8 of
        :meth:`_stage_single_volume`; also :meth:`_refinalise_kept`).

        Checks the tree's size, masters it under the working name, pads a
        thin part-filled ISO up an RS03 rung (#732) and applies RS03 on ECC
        media, checks the ISO's size, records its hash and
        length on the session volume, then gives it its final name.
        Returns ``(sha256, size)``.  The caller cleans up on failure.
        """
        # 6. Create ISO + ECC
        # Pre-flight: verify staging dir fits in the target media.
        estimated_bytes = dir_size_bytes(staging_root)
        # #674: the content must leave the medium's ECC share free.
        # Packs are planned against usable_bytes minus the measured
        # metadata reserve (#811), so this is a last check.  Checked
        # before mastering.
        if (estimated_bytes > media_type.usable_bytes
                and not self._allow_low_redundancy):
            raise LowRedundancyError(
                f"Staging directory for {vol_label} is "
                f"{estimated_bytes:,} bytes, more than the "
                f"{media_type.usable_bytes:,} usable bytes of "
                f"{media_type.name} (its capacity less the "
                f"{media_type.ecc_overhead_pct}% kept for error "
                f"correction). The disc metadata is larger than "
                f"measured.",
                recovery_hint=(
                    "Raise metadata_reserve_mb in lcsas.toml and "
                    "stage again, or pass --allow-low-redundancy "
                    "to stage it anyway (NOT recommended)."
                ),
            )
        if estimated_bytes > media_type.capacity_bytes:
            raise ValueError(
                f"Staging directory for {vol_label} is too large: "
                f"{estimated_bytes:,} bytes > {media_type.capacity_bytes:,} bytes "
                f"capacity ({media_type.name}). Reduce pack count or use larger media."
            )
        # #665: master and augment under a working name.  The ISO
        # gets its final name <label>.iso only once ECC and the
        # size check have passed and its hash is recorded, so a
        # stage killed part-way (SIGTERM, power loss) never leaves
        # a finished-looking ISO without RS03 data.
        work_iso = unfinished_iso_path(iso_output)
        self._xorriso.create_iso(
            staging_root, work_iso, vol_label,
            expected_bytes=estimated_bytes,
        )

        if media_type.ecc_overhead_pct > 0:
            self._pad_for_rs03(
                staging_root, work_iso, vol_label, volume_id, media_type,
            )
            self._check_redundancy(
                work_iso, vol_label, volume_id,
            )
            self._dvdisaster.augment_iso(
                work_iso, self._config.default_ecc_redundancy_pct,
            )

        # 7. Validate ISO size against media capacity  [O4]
        if not work_iso.exists():
            raise FileNotFoundError(
                f"ISO not created by xorriso: {work_iso}. "
                f"Check xorriso output for errors."
            )
        iso_size = work_iso.stat().st_size
        if iso_size > media_type.capacity_bytes:
            raise ValueError(
                f"ISO {iso_output.name} is {iso_size:,} bytes, exceeds "
                f"{media_type.name} capacity of "
                f"{media_type.capacity_bytes:,} bytes"
            )

        # 8. Record the hash and length, THEN give the ISO its
        # final name: a <label>.iso on disk always has its hash
        # on record (an empty hash means staging never finished).
        # The length is what device read-back verification hashes
        # against once the ISO itself is cleaned up (BURN-04).
        iso_sha256 = sha256_file(work_iso)
        update_session_volume_iso(
            self._conn,
            session_id=session_id,
            volume_id=volume_id,
            iso_path=str(iso_output),
            iso_sha256=iso_sha256,
            iso_size_bytes=iso_size,
        )
        os.replace(work_iso, iso_output)
        return iso_sha256, iso_size

    def _pad_for_rs03(
        self, staging_root: Path, iso: Path, vol_label: str,
        volume_id: int, media_type: MediaType,
    ) -> None:
        """Pad a part-filled ISO so its RS03 parity clears the floor (#732).

        An ISO just under a lower rung of dvdisaster's ladder (say 4.5 GB
        on a BD25 volume) would get only a few percent of parity.  Master
        it again with enough zero padding inside the ISO volume that
        dvdisaster moves it up a rung, where the parity is large.  Nothing
        changes when the ISO already clears the floor, or when no rung
        that fits the medium would (then _check_redundancy decides).
        """
        iso_size = iso.stat().st_size
        if iso_size <= 0:
            return
        try:
            target = rs03_pad_target_bytes(iso_size, media_type.capacity_bytes)
        except ValueError:
            return  # too large for any medium: augment_iso reports it
        if target is None or target <= iso_size:
            return
        self._xorriso.create_iso(
            staging_root, iso, vol_label, expected_bytes=target,
            padding_bytes=target - iso_size + XORRISO_DEFAULT_PADDING_BYTES,
        )
        padded = iso.stat().st_size
        if padded < target:
            raise RuntimeError(
                f"Padding the ISO of {vol_label} for RS03 left it at "
                f"{padded:,} bytes, short of the {target:,} needed."
            )
        detail = (
            f"ISO padded from {iso_size:,} to {padded:,} bytes so RS03 "
            f"parity is {rs03_redundancy_pct(padded):.0f}%, not "
            f"{rs03_redundancy_pct(iso_size):.1f}% (#732)"
        )
        _logger.info("Volume %s: %s.", vol_label, detail)
        add_event(self._conn, volume_id, "NOTE", detail=detail)

    def _check_redundancy(
        self, iso: Path, vol_label: str, volume_id: int,
    ) -> None:
        """Refuse an ISO whose RS03 redundancy would be thin (#674).

        RS03 pads the image to the smallest fitting medium and the padding
        is the parity, so the redundancy is known before dvdisaster runs.
        Below MIN_EFFECTIVE_REDUNDANCY_PCT the stage fails, unless
        ``allow_low_redundancy`` is set: then the percentage is recorded
        as a NOTE event on the volume.
        """
        iso_size = iso.stat().st_size
        if iso_size <= 0:
            return
        try:
            padded = smallest_fitting_medium_bytes(iso_size)
        except ValueError:
            return  # too large for any medium: augment_iso reports it
        pct = (padded - iso_size) / iso_size * 100
        if pct >= MIN_EFFECTIVE_REDUNDANCY_PCT:
            return
        detail = (
            f"RS03 effective redundancy {pct:.1f}% is below the "
            f"{MIN_EFFECTIVE_REDUNDANCY_PCT:.0f}% floor (ISO {iso_size:,} "
            f"bytes, padded to {padded:,})"
        )
        if not self._allow_low_redundancy:
            raise LowRedundancyError(
                f"Volume {vol_label}: {detail}. This disc would have thin "
                f"protection against bit-rot.",
                recovery_hint=(
                    "Stage fewer packs per volume or use a larger medium. "
                    "Pass --allow-low-redundancy to stage it anyway (NOT "
                    "recommended); the percentage is recorded on the volume."
                ),
            )
        _logger.warning("Volume %s: %s. Staging anyway "
                        "(--allow-low-redundancy).", vol_label, detail)
        add_event(
            self._conn, volume_id, "NOTE",
            detail=f"{detail}; staged with --allow-low-redundancy",
        )

    def _format_preflight(self, repo_ids: list[str]) -> None:
        """Prove every participating repository is readable by the pinned readers.

        FMT-03: before any ISO is mastered, decode-prove each repository with
        the tier-3 Python reader, :class:`PurePythonRestorer` (config version
        1-2 + index + one blob).  A repository whose live rustic has outrun
        the pinned recovery readers (e.g. a future v3 format) is refused here,
        so no disc that no shipped reader can open is ever burned.

        A repository with no ``password_file`` configured cannot be
        decode-proven; it is refused unless ``allow_unverified_repo_format``
        is set.  The catalog's repository is matched to its
        ``RepositoryConfig`` by mirror_path (the authoritative join: the
        catalog ``name`` may be title-cased / differ from the config key),
        falling back to name.
        """
        from lcsas.burn.format_preflight import (
            FormatDriftError,
            check_repo_recoverable,
        )
        from lcsas.db.repos import get_repo

        for repo_id in sorted(set(repo_ids)):
            repo = get_repo(self._conn, repo_id)
            repo_cfg = self._match_repo_config(repo.name, repo.mirror_path)
            password_file = repo_cfg.password_file if repo_cfg else None

            if password_file is None:
                msg = (
                    f"repo '{repo.name}': no password_file configured — cannot "
                    f"prove discs will be restorable by the pinned recovery "
                    f"readers."
                )
                if self._config.allow_unverified_repo_format:
                    _logger.warning(
                        "%s Burning anyway (allow_unverified_repo_format=true) "
                        "— NOT recommended.", msg,
                    )
                    continue
                raise FormatDriftError(
                    f"{msg} Set password_file or set "
                    f"allow_unverified_repo_format = true in lcsas.toml to "
                    f"burn anyway (NOT recommended)."
                )

            from lcsas.config.mirrors import mirror_path
            mirror = mirror_path(self._config, repo)
            if mirror is None:
                raise MirrorUnavailableError(
                    repo_id, None, [],
                    detail="neither the config nor the catalog places it",
                )
            check_repo_recoverable(mirror, password_file)

    def _match_repo_config(
        self, db_name: str, db_mirror_path: str,
    ) -> RepositoryConfig | None:
        """Find the RepositoryConfig for a catalog repository
        (:func:`lcsas.config.mirrors.config_for_repo`): mirror path, then
        name; a blank catalog path joins on the name alone (#958).
        """
        from lcsas.config.mirrors import config_for_repo

        return config_for_repo(self._config, db_name, db_mirror_path)

    def _config_names(self) -> dict[str, str]:
        """Catalog repo_id -> the repository's ``[repos.<name>]`` key (#587).

        Joined with :meth:`_match_repo_config`, so a catalog name that
        differs from the config key still finds the repository's own split
        setting and its recorded split.
        """
        names: dict[str, str] = {}
        for repo in list_repos(self._conn):
            cfg = self._match_repo_config(repo.name, repo.mirror_path)
            if cfg is not None:
                names[repo.repo_id] = cfg.name
        return names

    def _get_mirror_paths(self) -> dict[str, Path]:
        """Every catalog repository's mirror, by repo_id (#958): the
        config's ``mirror_path`` first, then the catalog's
        (:func:`lcsas.config.mirrors.mirror_paths`)."""
        from lcsas.config.mirrors import mirror_paths

        paths = mirror_paths(self._conn, self._config)

        # Fallback: if no repositories in the catalog, use mirror_base_path
        # as "default"
        if not paths and not list_repos(self._conn):
            paths["default"] = self._config.mirror_base_path

        return paths

    def _escrow_preflight(self, repo_ids: list[str]) -> str | None:
        """Abort the burn if config split/K/N drifts from the record.

        Returns the drift message when ``allow_escrow_drift`` suppresses the
        abort (so callers can log it as a volume event), else None.  Raises
        :class:`EscrowDriftError` on drift unless the escape hatch is set.
        """
        message = detect_escrow_drift(
            self._conn,
            repo_ids,
            config_split=self._config.key_split,
            config_threshold=self._config.key_threshold,
            config_shares=self._config.key_shares,
            split_for=self._config.repo_split,
            config_names=self._config_names(),
        )
        if message is None:
            return None
        if self._allow_escrow_drift:
            _logger.warning("%s (proceeding: --allow-split-drift)", message)
            return message
        raise EscrowDriftError(message)

    def _effective_kn(self, repo_ids: list[str]) -> tuple[int, int] | None:
        """K/N to render on this volume's disc text, from the record when present.

        A recorded split is the authoritative source for the heir-facing K/N
        (KEY-08); falls back to None (caller uses config) when no repository
        on the volume has a record.  If the staged repositories carry different
        recorded splits, the first by repo_id wins.  It is only the fallback
        K/N: the disc text takes each configured repository's own record
        first (``recorded_splits_by_name``, #587).
        """
        for repo_id in sorted(set(repo_ids)):
            record = get_split_for_repo(self._conn, repo_id)
            if record is not None:
                return (record.threshold, record.shares)
        return None

    # =================================================================
    # Burn sessions: staging and burning (multi-volume, multi-copy)
    # =================================================================

    def stage(
        self,
        media_type: MediaType | None = None,
        for_location: str | None = None,
        repo_ids: list[str] | None = None,
        pack_sha256s: list[str] | None = None,
        dry_run: bool = False,
        min_fill_pct: float = 0.0,
        partial: bool = False,
    ) -> StageResult:
        """Stage all unarchived packs into ISOs, creating a burn session.

        Handles multi-volume scenarios: if data exceeds one disc, multiple
        volumes and ISOs are created within a single burn session.

        ECC is applied automatically for production media (any ``MediaType``
        with ``ecc_overhead_pct > 0``). Test media (``TEST_TINY``, 0%
        overhead) is implicitly skipped because RS03 has a minimum image
        size — see ``MediaType.ecc_overhead_pct`` in
        ``src/lcsas/config/media.py``.

        Args:
            media_type: Target media type (defaults to config).
            for_location: If set, stage only packs missing at this location.
                It is recorded as the new volumes' location.
            repo_ids: Optional filter to specific repositories.
            pack_sha256s: If set, stage exactly the packs with these
                SHA-256s (consolidation), whether or not they are already
                on a disc; ``for_location`` and ``repo_ids`` then do not
                select packs.
            dry_run: If True, compute the plan but skip all side effects.

            min_fill_pct: If above 0, the last planned volume is not staged
                when its packs fill less than this percent of its pack
                space (#701); the volumes before it are.  Its packs wait
                for a later stage.
            partial: Stage the last volume even below ``min_fill_pct``.

        Returns:
            StageResult with burn session ID, manifests, and ISO paths.
        """
        from lcsas.log import get_logger
        logger = get_logger()

        mt = media_type or self._config.default_media_type

        # 1. Gather packs to stage
        if pack_sha256s is not None:
            # consolidate --execute: exactly these packs, archived or not
            # (they sit on VERIFIED volumes, so the unarchived pool never
            # holds them — #677).  for_location is then only recorded as
            # the new volumes' location.
            packs_to_stage = self._packs_by_sha256(pack_sha256s)
        else:
            packs_to_stage = self._gather_packs_for_staging(
                for_location=for_location,
                repo_ids=repo_ids,
            )

        # #672: plain stage leaves out packs whose disc is on its way: a
        # never-burned volume whose finished ISO waits in the staging area.
        held: dict[str, list[str]] = {}
        killed: dict[str, list[str]] = {}
        if for_location is None:
            packs_to_stage, held = self._hold_back_waiting(packs_to_stage)
            # #968: a session whose stage was killed hard is not waiting
            # to be burned: 'status' says do not burn it, and so does this.
            killed = self._killed_stage_sessions(held)
            if killed:
                logger.warning(
                    "Not staging packs that are on volumes of a burn session "
                    "whose stage did not finish: %s. Do not burn %s. Remove "
                    "%s with %s, then run 'lcsas stage' again.",
                    _describe_waiting(killed),
                    "it" if len(killed) == 1 else "them",
                    "it" if len(killed) == 1 else "each",
                    " and ".join(f"'lcsas session abort {sid}'"
                                 for sid in sorted(killed)),
                )
                held = {s: v for s, v in held.items() if s not in killed}
            if held and packs_to_stage:
                logger.warning(
                    "Not staging packs that are already on volumes waiting "
                    "to be burned: %s. Burn them with 'lcsas burn --session "
                    "<id>', or 'lcsas session abort <id>' to stage their "
                    "packs again.", _describe_waiting(held),
                )

        if not packs_to_stage:
            if killed and not held:
                raise NothingToStageError(
                    "No packs need staging: every unarchived pack is on a "
                    "volume of a burn session whose stage did not finish "
                    f"({_describe_waiting(killed)}).",
                )
            if held:
                raise NothingToStageError(
                    "No packs need staging: every unarchived pack is on a "
                    f"volume waiting to be burned ({_describe_waiting(held)}).",
                    recovery_hint=(
                        "lcsas burn --session <id> --location <name>, or "
                        "lcsas session abort <id> to discard the waiting ISOs."
                    ),
                )
            # A StagingError, so the CLI prints it as it is, not as an
            # "Unexpected error" (#701: a scheduled collection stage
            # meets this whenever nothing new came in).
            raise NothingToStageError("No packs need staging.")

        # 2. Measure the metadata every disc carries (#811), then
        #    bin-pack into multiple volumes.  Both before any side effect.
        reserve, measured = self._metadata_reserve(packs_to_stage, mt)
        logger.info(
            "%sDisc metadata: about %s bytes measured (%s); reserving "
            "%s bytes on each %s disc (metadata_reserve_mb: %s bytes).",
            "[DRY RUN] " if dry_run else "", f"{measured.total:,}",
            measured.describe(), f"{reserve:,}", mt.name,
            f"{self._config.metadata_reserve_bytes:,}",
        )
        self._check_metadata_fits(packs_to_stage, mt, reserve, measured)
        volume_plans = self._multi_bin_pack(packs_to_stage, mt, reserve)

        # #701: a last volume below the minimum fill waits for more data.
        # Decided on the plan, before any side effect.
        volume_plans, underfilled = self._trim_underfilled(
            volume_plans, mt, min_fill_pct, partial=partial, reserve=reserve,
        )
        if not volume_plans:
            logger.info(
                "%sNothing staged: the only volume would hold %s. Its "
                "packs wait for a later stage; pass --partial to stage "
                "it now.", "[DRY RUN] " if dry_run else "",
                underfilled.describe(mt.name) if underfilled else "",
            )
            return StageResult(
                session_id="", media_type=mt, staging_dir=Path("/dev/null"),
                underfilled=underfilled,
            )
        if underfilled is not None:
            logger.warning(
                "%sNot staging the last volume: it would hold %s. Its "
                "packs wait for a later stage; pass --partial to stage "
                "it now.", "[DRY RUN] " if dry_run else "",
                underfilled.describe(mt.name),
            )

        participating_repos = {p.repo_id for _packs, _b in volume_plans
                               for p in _packs}

        # KEY-08: refuse password split drift for every repository of the
        # plan BEFORE any side effect, so a refused stage leaves no burn
        # session, staging directory or half-built session behind (#747).
        # With --allow-split-drift a real stage warns in
        # _stage_single_volume, which also records the override NOTE on
        # each volume; a dry run warns here.
        if dry_run or not self._allow_escrow_drift:
            self._escrow_preflight(sorted(participating_repos))

        # --- Dry-run: report the plan without side effects ---
        if dry_run:
            total_bytes = sum(b for _, b in volume_plans)
            logger.info(f"[DRY RUN] {len(volume_plans)} volume(s) planned "
                        f"on {mt.name}")
            pack_space = self._pack_space_bytes(mt, reserve)
            for i, (packs, vol_bytes) in enumerate(volume_plans, 1):
                fill_pct = (vol_bytes / mt.capacity_bytes) * 100
                space_pct = vol_bytes / pack_space * 100
                logger.info(f"  Volume {i}: {len(packs)} packs, "
                            f"{vol_bytes:,} bytes ({fill_pct:.1f}% fill, "
                            f"{space_pct:.1f}% of its pack space)")
            logger.info(f"  Total data: {total_bytes:,} bytes")
            return StageResult(
                session_id="dry-run", media_type=mt,
                staging_dir=Path("/dev/null"),
            )

        # #731: the tools must be new enough, checked before any side
        # effect (a dry run never runs them, so it does not need them).
        self._tool_version_preflight(apply_ecc=mt.ecc_overhead_pct > 0)

        # FMT-03: prove every participating repository decodes with the pinned
        # recovery readers BEFORE any burn-session/staging/ISO side effect.  A
        # repository whose live rustic has outrun the readers (e.g. a v3
        # format) is refused here, never burned.
        self._format_preflight(sorted(participating_repos))

        # #657: only snapshots whose packs are all on discs (durable
        # volumes, or this session's volumes) go onto this session's
        # discs.  Walked once, before any side effect; each volume then
        # takes the snapshots that the packs staged up to it complete
        # (#813).
        session_packs = {p.sha256 for _packs, _b in volume_plans
                         for p in _packs}
        snapshot_needs, left_out, unchecked = self._snapshot_coverage(
            session_packs)

        # #946: refuse a stage whose discs would lack, for good, a
        # snapshot that only a waiting volume's packs keep off them.
        self._refuse_waiting_gap(snapshot_needs, session_packs)

        # #753: every repository of the plan has its mirror data, checked
        # before the burn session exists (a missing mirror used to fail
        # inside the first volume, leaving the session behind).
        self._mirror_preflight(volume_plans)

        # 3. Disk space pre-flight check [BURN-07]
        # Per volume we hold the staging tree (packs + disc catalog +
        # repository metadata) AND its ISO — ISOs persist for the whole
        # burn session (BURN-06), so the padded ISO sizes are SUMMED, not
        # maxed.  On ECC media (ecc_overhead_pct > 0) dvdisaster's RS03
        # augmented mode pads each ISO up to a full medium (a 1 GB ISO
        # becomes a 4.7 GB DVD image; #732 padding may move a thin one up
        # a rung, which rs03_staged_image_bytes counts), and augment_iso
        # additionally duplicates the ISO while augmenting — budget one
        # extra copy of the largest padded ISO.
        apply_ecc = mt.ecc_overhead_pct > 0
        metadata_reserve = reserve
        required_bytes = 0
        max_padded_iso = 0
        for _, vol_bytes in volume_plans:
            # ~5% ISO 9660 filesystem overhead on top of the staged tree.
            est_iso = int(vol_bytes * 1.05) + metadata_reserve
            padded_iso = (
                rs03_staged_image_bytes(est_iso, mt.capacity_bytes)
                if apply_ecc else est_iso
            )
            required_bytes += vol_bytes + metadata_reserve + padded_iso
            max_padded_iso = max(max_padded_iso, padded_iso)
        if apply_ecc:
            required_bytes += max_padded_iso  # augment_iso temp copy
        staging_usage = shutil.disk_usage(self._config.staging_path)
        if staging_usage.free < required_bytes:
            avail_gb = staging_usage.free / 1e9
            need_gb = required_bytes / 1e9
            raise OSError(
                f"Insufficient disk space in the staging area: "
                f"{avail_gb:.1f} GB available, ~{need_gb:.1f} GB needed "
                f"including RS03 medium padding "
                f"(at {self._config.staging_path})"
            )

        # #947: after the space check, which tests drive with packs that
        # are not in the mirror; still before any side effect.
        self._lost_pack_preflight(volume_plans)

        # 4. Create the burn session.  FUP-02: commit the burn_sessions row
        # BEFORE the directory exists on disk.  Invariant: any session-named dir on disk
        # has a non-CLEANED row (never flagged orphan by staging-clean); the
        # only other window is a row with no dir (a crash between commit and
        # mkdir), which detect_orphaned_staging already ignores and
        # clean_session tolerates.  This closes the in-flight-dir-orphaned race.
        session_id = generate_session_id()
        session_dir = self._config.staging_path / session_id.replace(":", "-")

        create_session(
            self._conn,
            media_type=mt.name,
            staging_dir=str(session_dir),
            session_id=session_id,
        )

        # 5. Build staging dirs, create ISOs, apply ECC for each volume
        manifests: list[BurnManifest] = []
        iso_paths: list[Path] = []

        # #753: a failure from here on (a refusal, xorriso, dvdisaster,
        # Ctrl-C) removes the session when no volume of it was finished,
        # and names the kept volumes otherwise.  _stage_single_volume has
        # already removed the volume that failed.
        try:
            ensure_dir(session_dir)
            self._stage_volumes(
                volume_plans, mt, session_id, session_dir, for_location,
                snapshot_needs, manifests, iso_paths,
            )
        except BaseException:
            self._snapshot_allow = None
            self._after_failed_stage(
                session_id, session_dir, manifests, len(volume_plans),
                snapshot_needs, mt,
            )
            raise

        # Write session manifest JSON
        self._write_session_manifest(session_id, session_dir, manifests)

        return StageResult(
            session_id=session_id,
            media_type=mt,
            staging_dir=session_dir,
            manifests=manifests,
            iso_paths=iso_paths,
            snapshots_left_out=left_out,
            snapshots_unchecked=unchecked,
        )

    def _stage_volumes(
        self,
        volume_plans: list[tuple[list[Pack], int]],
        mt: MediaType,
        session_id: str,
        session_dir: Path,
        for_location: str | None,
        snapshot_needs: dict[str, dict[str, frozenset[str]]],
        manifests: list[BurnManifest],
        iso_paths: list[Path],
    ) -> None:
        """Stage each planned volume of a burn session (one :meth:`stage`).

        Appends each finished volume to *manifests* and *iso_paths* as it
        is done, so a caller that catches a failure knows which volumes
        were kept.

        *snapshot_needs* is, per repo_id, each snapshot's packs that are
        on no durable volume (:meth:`_snapshot_coverage`).  Every volume
        carries the snapshots whose needed packs are all in this session
        (#657), so any disc of the session is a starting point.  When the
        stage stops part-way, :meth:`_after_failed_stage` takes off the
        kept volumes the snapshots that need a volume that was never
        staged (#813).
        """
        # #676: above every label number ever issued, not only those of
        # the volumes still in the catalog.
        seq = next_label_seq(self._conn, self._config.label_prefix)
        # #957: the session's numbers are reserved with its first volume,
        # so every disc of the session carries the session's last number
        # and a catalog rebuilt from any one of them continues after it.
        # Numbers of an interrupted session are not reused (as for an
        # aborted one, #676): a gap never means a missing disc.
        reserve_through = seq + len(volume_plans) - 1

        # #657: _stage_single_volume reads this for the whole session.
        session_packs = {p.sha256 for packs, _b in volume_plans for p in packs}
        self._snapshot_allow = _complete_snapshots(snapshot_needs,
                                                   session_packs)

        for i, (selected_packs, total_bytes) in enumerate(volume_plans):
            vol_label = generate_volume_label(
                self._config.label_prefix, mt.label_name, seq + i,
            )
            vol_uuid = generate_uuid()

            staging_root = session_dir / vol_label
            iso_path = session_dir / f"{vol_label}.iso"

            # Stage, register, inject metadata, create ISO + ECC.  The
            # session_volumes row is committed atomically with the volume
            # row inside _stage_single_volume (iso_sha256='' until the
            # finished ISO is hashed, just before it gets its final name).
            manifest = self._stage_single_volume(
                selected_packs=selected_packs,
                total_bytes=total_bytes,
                media_type=mt,
                vol_label=vol_label,
                vol_uuid=vol_uuid,
                staging_root=staging_root,
                iso_output=iso_path,
                session_id=session_id,
                burn_session_position=(i + 1, len(volume_plans)),
                location=for_location,
                reserve_through=reserve_through,
            )

            # The ISO hash + byte length are already on the session
            # volume: _stage_single_volume records them before the ISO
            # gets its final name (#665).
            manifests.append(manifest)
            if manifest.iso_path:
                iso_paths.append(manifest.iso_path)
        self._snapshot_allow = None

    def _tool_version_preflight(self, *, apply_ecc: bool) -> None:
        """Refuse a stage whose xorriso or dvdisaster is too old (#731).

        Raises :class:`~lcsas.exceptions.BinaryError` (missing, or below
        :data:`XORRISO_MIN_VERSION` / :data:`DVDISASTER_MIN_VERSION`).
        dvdisaster is checked only when this stage applies ECC.  Only real
        subprocess runners are checked: an injected fake runs no binary.
        """
        from lcsas.utils.subprocess import (
            SubprocessRunnerBase,
            check_binary_version,
        )

        if isinstance(self._xorriso, SubprocessRunnerBase):
            check_binary_version(self._xorriso._binary, XORRISO_MIN_VERSION)
        if apply_ecc and isinstance(self._dvdisaster, SubprocessRunnerBase):
            check_binary_version(
                self._dvdisaster._binary, DVDISASTER_MIN_VERSION,
            )

    def _mirror_preflight(
        self, volume_plans: list[tuple[list[Pack], int]],
    ) -> None:
        """Refuse the stage when a repository of the plan has no mirror
        data directory, before any side effect (#753).

        :meth:`_stage_single_volume` checks again for each volume.
        """
        mirror_paths = self._get_mirror_paths()
        by_repo: dict[str, list[str]] = {}
        for packs, _bytes in volume_plans:
            for p in packs:
                by_repo.setdefault(p.repo_id, []).append(p.sha256[:12])
        for repo_id, shas in sorted(by_repo.items()):
            mirror_path = mirror_paths.get(repo_id)
            if mirror_path is None or not (mirror_path / "data").is_dir():
                raise MirrorUnavailableError(repo_id, mirror_path, shas)

    def _lost_pack_preflight(
        self, volume_plans: list[tuple[list[Pack], int]],
    ) -> None:
        """#947: refuse when a pack to stage has no file in the mirror.

        A consolidation of a volume whose pack the mirror lost, or an
        unarchived pack lost after scan.  Named with the discs that hold
        it, before the burn session exists.
        """
        from lcsas.db.repos import get_repo
        from lcsas.packs.mirror_damage import (
            MirrorDamageError,
            damage_message,
            discs_holding,
        )

        mirror_paths = self._get_mirror_paths()
        lost: dict[str, list[str]] = {}
        for packs, _bytes in volume_plans:
            for p in packs:
                data = mirror_paths[p.repo_id] / "data"
                if not ((data / p.sha256[:2] / p.sha256).is_file()
                        or (data / p.sha256).is_file()):
                    lost.setdefault(p.repo_id, []).append(p.sha256)
        if lost:
            raise MirrorDamageError("\n".join(
                damage_message(get_repo(self._conn, rid).name,
                               discs_holding(self._conn, shas),
                               to_stage=True)
                for rid, shas in sorted(lost.items())
            ))

    def _after_failed_stage(
        self,
        session_id: str,
        session_dir: Path,
        manifests: list[BurnManifest],
        planned: int,
        snapshot_needs: dict[str, dict[str, frozenset[str]]],
        media_type: MediaType,
    ) -> None:
        """Clean up after :meth:`stage` failed inside its session (#753).

        Each kept volume carries the snapshots the whole planned session
        would complete (#657).  It is re-finalised first (#813,
        :meth:`_refinalise_kept`) so it carries only those its kept
        volumes and the durable ones complete.

        With no finished volume the session is removed: its staging
        directory first, then its row (a row with no directory is the
        harmless order, FUP-02).  Otherwise the finished volumes stay
        staged, as before, and the log names them.  The catalog decides
        which volumes were finished (the failed one is already deleted),
        not *manifests*, which an interrupt can catch one volume behind.
        Never raises: the stage's own error is what the caller sees.
        """
        try:
            self._conn.rollback()
            self._refinalise_kept(session_id, session_dir, snapshot_needs,
                                  media_type)
            kept = [
                str(r[0]) for r in self._conn.execute(
                    "SELECT v.label FROM session_volumes sv "
                    "JOIN volumes v USING (volume_id) "
                    "WHERE sv.session_id = ? ORDER BY v.label",
                    (session_id,),
                )
            ]
            if not kept:
                if session_dir.exists():
                    safe_remove_tree(session_dir)
                delete_empty_session(self._conn, session_id)
                _logger.error(
                    "Staging failed; no volume was staged, so burn session "
                    "%s and its staging directory were removed.", session_id,
                )
                return
            with contextlib.suppress(OSError):
                self._write_session_manifest(
                    session_id, session_dir,
                    [m for m in manifests if m.volume_label in kept],
                )
            _logger.error(
                "Staging stopped after %d of %d volume(s). Burn session %s "
                "keeps the volume(s) staged before the failure: %s. Burn "
                "them with 'lcsas burn --session %s --location <name>', or "
                "remove them with 'lcsas session abort %s'.",
                len(kept), planned, session_id, ", ".join(kept), session_id,
                session_id,
            )
        except Exception as exc:  # never hide the stage's own error
            _logger.error(
                "Could not clean up burn session %s after the failed stage: "
                "%s. See 'lcsas session list'.", session_id, exc,
            )

    def _refinalise_kept(
        self,
        session_id: str,
        session_dir: Path,
        snapshot_needs: dict[str, dict[str, frozenset[str]]],
        media_type: MediaType,
    ) -> None:
        """Take off the kept volumes of a stopped stage the snapshots they
        cannot restore (#813).

        The volumes were staged with every snapshot the whole planned
        session completes (#657).  When the stage stops part-way, a
        snapshot that needs a volume that was never staged would be
        "latest" on those discs and could not be restored.  For each kept
        volume carrying such a snapshot file: its recorded ISO hash is
        blanked first (an interrupted re-master is then refused by the
        burn, #665), the snapshot files are removed from its staging
        tree, and the ISO is mastered again.  A volume that cannot be
        mastered again is deleted, as a failed volume is.  A repository
        that was not filtered (no entry in *snapshot_needs*) is left as
        it is.
        """
        if not snapshot_needs:
            return
        rows = self._conn.execute(
            "SELECT sv.volume_id, sv.iso_path, v.label FROM session_volumes sv "
            "JOIN volumes v USING (volume_id) "
            "WHERE sv.session_id = ? ORDER BY v.label",
            (session_id,),
        ).fetchall()
        if not rows:
            return
        placeholders = ",".join("?" * len(rows))
        kept_packs = {
            str(r[0]) for r in self._conn.execute(
                f"SELECT p.sha256 FROM volume_packs vp JOIN packs p "
                f"USING (pack_id) WHERE vp.volume_id IN ({placeholders})",
                [r[0] for r in rows],
            )
        }
        allow = _complete_snapshots(snapshot_needs, kept_packs)
        for volume_id, iso_path, label in rows:
            staging_root = session_dir / str(label)
            extra = [
                snap
                for repo_id, keep in allow.items()
                for snap in _snapshot_files(staging_root, repo_id)
                if snap.name not in keep
            ]
            if not extra:
                continue
            iso_output = Path(iso_path)
            update_session_volume_iso(
                self._conn, session_id=session_id, volume_id=volume_id,
                iso_path=str(iso_output), iso_sha256="", iso_size_bytes=None,
            )
            try:
                iso_output.unlink(missing_ok=True)
                for snap in extra:
                    snap.unlink()
                self._master_volume(
                    staging_root, iso_output, str(label), volume_id,
                    media_type, session_id,
                )
            except Exception as exc:
                self._conn.rollback()
                VolumeLifecycle(self._conn).stage_failed(volume_id)
                with contextlib.suppress(OSError):
                    unfinished_iso_path(iso_output).unlink(missing_ok=True)
                _logger.error(
                    "Volume %s carried %d snapshot(s) whose packs are not "
                    "all on the kept volumes, and could not be mastered "
                    "again without them (%s); it was removed.",
                    label, len(extra), exc,
                )
                continue
            _logger.warning(
                "Volume %s was mastered again without %d snapshot(s) whose "
                "packs are not all on the kept volumes.", label, len(extra),
            )

    def _snapshot_coverage(
        self, session_packs: set[str],
    ) -> tuple[dict[str, dict[str, frozenset[str]]],
               dict[str, list[LeftOutSnapshot]], dict[str, str]]:
        """#657: which snapshot files may go on this session's discs.

        For every repository with a mirror, walk its snapshots once with
        the tier-3 reader and find, per snapshot, the packs it needs that
        are on no durable volume.  Returns ``(needs by repo_id: snapshot
        id -> those packs, left out by repo name, unchecked by repo name
        with the reason)``.  A snapshot is left out when *session_packs*
        do not hold all its needed packs; :meth:`_stage_volumes` decides
        per volume (#813).  A repository that cannot be checked (no
        password_file, or the walk fails) is not filtered — every
        snapshot is copied, as before — and is reported as unchecked.
        """
        from lcsas.db.queries import get_archived_pack_ids
        from lcsas.db.repos import get_repo
        from lcsas.restore.restic_fallback import IntegrityError

        needs: dict[str, dict[str, frozenset[str]]] = {}
        left_out: dict[str, list[LeftOutSnapshot]] = {}
        unchecked: dict[str, str] = {}
        for repo_id, mirror in sorted(self._get_mirror_paths().items()):
            repo = get_repo(self._conn, repo_id)
            if not (mirror / "snapshots").is_dir():
                continue
            repo_cfg = self._match_repo_config(repo.name, repo.mirror_path)
            password_file = repo_cfg.password_file if repo_cfg else None
            if password_file is None or not password_file.is_file():
                unchecked[repo.name] = "no password_file configured"
                continue
            held = get_archived_pack_ids(self._conn, repo_id)
            try:
                cov = snapshot_coverage(mirror, password_file, held)
            except (IntegrityError, OSError, ValueError, KeyError) as exc:
                unchecked[repo.name] = f"snapshot check failed: {exc}"
                _logger.warning(
                    "repo '%s': could not check which snapshots are fully "
                    "on discs (%s); every snapshot file is copied.",
                    repo.name, exc,
                )
                continue
            # #947: a tree that cannot be read although its pack is on a
            # burned disc: the mirror lost (or broke) that pack.  Never
            # report it as "packs not on any disc yet".
            damaged = cov.unreadable_tree_packs & held
            if damaged:
                from lcsas.packs.mirror_damage import (
                    MirrorDamageError,
                    damage_message,
                    discs_holding,
                )
                raise MirrorDamageError(damage_message(
                    repo.name, discs_holding(self._conn, damaged),
                    unreadable=True,
                ))
            needs[repo_id] = dict(cov.missing)
            rest = [
                LeftOutSnapshot(snapshot_id=sid, time=cov.times.get(sid, ""),
                                missing_packs=len(need - session_packs))
                for sid, need in cov.missing.items()
                if not need <= session_packs
            ]
            if rest:
                left_out[repo.name] = rest
        return needs, left_out, unchecked

    def _refuse_waiting_gap(
        self,
        snapshot_needs: dict[str, dict[str, frozenset[str]]],
        session_packs: set[str],
    ) -> None:
        """#946: refuse the stage when a waiting session makes a gap.

        A snapshot is left off this session's discs when it needs packs
        that are neither on a durable volume nor in this session.  When
        every such pack sits on a volume that waits to be burned (never
        burned, its disc not failed), burning both sessions would leave
        the highest-labelled disc without that snapshot, and no later
        ``stage`` would carry it: every pack is then archived.  So stop
        before anything is created and name the waiting session.

        Checked on every path (plain, ``--for-location``, consolidation).
        A snapshot that is also missing a pack held back by ``--min-fill``
        (or unreadable, ``?tree:``) is not refused here: a later stage
        carries it once those packs are staged.
        """
        waiting = self._waiting_pack_holders()
        if not waiting:
            return
        from lcsas.db.repos import get_repo

        # waiting session -> repository name -> snapshot IDs it keeps off
        gaps: dict[str, dict[str, set[str]]] = {}
        for repo_id, needs in sorted(snapshot_needs.items()):
            for snap_id, need in needs.items():
                missing = need - session_packs
                if not missing or not all(m in waiting for m in missing):
                    continue
                name = get_repo(self._conn, repo_id).name
                for m in missing:
                    session_id = waiting[m][1]
                    gaps.setdefault(session_id, {}).setdefault(
                        name, set()).add(snap_id)
        if not gaps:
            return
        labels: dict[str, set[str]] = {}
        locations: dict[str, set[str]] = {}
        for label, session_id, location in waiting.values():
            labels.setdefault(session_id, set()).add(label)
            locations.setdefault(session_id, set()).add(location)
        parts: list[str] = []
        for session_id, per_repo in sorted(gaps.items()):
            sets = ", ".join(f"{len(snaps)} snapshot(s) of '{name}'"
                             for name, snaps in sorted(per_repo.items()))
            location = (sorted(locations[session_id])[0]
                        if len(locations[session_id]) == 1 else "<location>")
            if self._killed_stage_sessions({session_id: []}):
                # #968: as 'status' says, a session whose stage was killed
                # hard must not be burned.
                parts.append(
                    f"The stage of burn session {session_id} "
                    f"({', '.join(sorted(labels[session_id]))}) did not "
                    f"finish, so it must not be burned. Staging now would "
                    f"leave {sets} off the new discs for good. Remove it "
                    f"(lcsas session abort {session_id}) and stage again."
                )
                continue
            parts.append(
                f"Session {session_id} "
                f"({', '.join(sorted(labels[session_id]))}) is staged and "
                f"waits to be burned. Staging now would leave {sets} off "
                f"the new discs for good. Burn it first (lcsas burn "
                f"--session {session_id} --location {location}), or "
                f"discard it (lcsas session abort {session_id}) and stage "
                f"again."
            )
        raise WaitingSessionGapError(" ".join(parts))

    def _waiting_pack_holders(self) -> dict[str, tuple[str, str, str]]:
        """#946: packs on volumes that wait to be burned.

        Pack SHA-256 -> (label, burn session, location) for every pack
        on a never-burned volume (STAGING/BURNING) of a burn session
        whose disc did not fail verification.  Broader than
        :meth:`_hold_back_waiting` on purpose: a pack this stage does not
        take only matters when it is on such a volume, whichever path
        left it out (plain stage holds back the finished ones,
        ``--for-location`` every one staged for that location).
        """
        rows = self._conn.execute(
            """SELECT p.sha256, v.label, v.location,
                      (SELECT sv.session_id FROM session_volumes sv
                       JOIN burn_sessions bs USING (session_id)
                       WHERE sv.volume_id = v.volume_id
                       ORDER BY julianday(bs.created_at) DESC, sv.rowid DESC
                       LIMIT 1) AS session_id
               FROM volumes v
               JOIN volume_packs vp ON vp.volume_id = v.volume_id
               JOIN packs p ON p.pack_id = vp.pack_id
               WHERE v.status IN ('STAGING', 'BURNING')
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_events e
                     WHERE e.volume_id = v.volume_id
                       AND e.event_type = 'VERIFY_FAIL'
                 )"""
        ).fetchall()
        return {
            str(r["sha256"]): (str(r["label"]), str(r["session_id"]),
                               str(r["location"] or ""))
            for r in rows if r["session_id"] is not None
        }

    def burn_session(
        self,
        session_ref: str = "latest",
        location: str = "Home_Shelf",
        device: str | None = None,
        skip_burn: bool = False,
        interactive: bool = True,
        reburn: bool = False,
    ) -> list[BurnReceipt]:
        """Burn the ISOs of a burn session to disc, tagged with a location.

        A volume that already has a verified disc in service at *location*
        is skipped (#814): re-running a PARTIAL burn session burns only
        the volumes that still need a disc there.  *reburn* burns those
        volumes again too (a new disc replaces the one recorded there).

        Args:
            session_ref: Burn session ID or 'latest'.
            location: The location where the discs burned now will be kept.
            device: Optical device (overrides config).
            skip_burn: If True, skip physical burn (for testing).
            interactive: If True (default), pause before each physical
                burn so the owner can load a blank disc, and after each
                verified burn eject the tray and print the label to write.
                Forced off when ``skip_burn`` is set (no disc is touched).

        Returns:
            List of BurnReceipt objects.
        """
        session_id = resolve_session_id(self._conn, session_ref)
        session_vols = get_session_volumes(self._conn, session_id)
        device = device or self._config.optical_device

        # skip_burn never touches a disc, so it implies non-interactive
        # (protects cdemu/CI flows that pass skip_burn=True).  [FUP-01]
        prompt: OperatorPrompt = (
            self._prompt if interactive and not skip_burn
            else NullOperatorPrompt()
        )

        # Ensure location exists
        ensure_location(self._conn, location)

        # Check every volume BEFORE the first prompt for a blank disc.
        self._burn_preflight(session_id, session_vols)

        # #814: a volume with a verified disc at this location is done.
        if not reburn:
            from lcsas.db.volume_copies import has_verified_disc_at

            done = [
                sv for sv in session_vols
                if has_verified_disc_at(self._conn, sv.volume_id, location)
            ]
            for sv in done:
                _logger.info(
                    "Volume %s already has a verified disc at %s; not "
                    "burning it again (use --reburn to replace that disc).",
                    get_volume_by_id(self._conn, sv.volume_id).label,
                    location,
                )
            session_vols = [sv for sv in session_vols if sv not in done]
            if not session_vols:
                _logger.info(
                    "Every volume of burn session %s already has a verified "
                    "disc at %s. Nothing to burn.", session_id, location,
                )
                return []

        # #905: a volume no disc of which may be burned (CONSOLIDATING,
        # DEPRECATED, DESTROYED) is refused with its reason, before the
        # first disc prompt.  Volumes skipped above are not asked.
        lifecycle = VolumeLifecycle(self._conn)
        for sv in session_vols:
            refusal = lifecycle.may_burn(sv.volume_id)
            if refusal is not None:
                raise BurnError(f"{refusal} Nothing was burned.")

        # #958: a blank ISO path (a catalog rebuilt from discs, which
        # carry no paths) means the ISO is not on this computer.  Said
        # before the first disc prompt.
        if not skip_burn:
            from lcsas.db.sessions import recorded_path

            not_here = [
                get_volume_by_id(self._conn, sv.volume_id).label
                for sv in session_vols if recorded_path(sv.iso_path) is None
            ]
            if not_here:
                raise BurnError(
                    f"The ISO of {', '.join(not_here)} is not on this "
                    f"computer: the catalog records no ISO path for it (a "
                    f"catalog rebuilt from discs has none). Nothing was "
                    f"burned.",
                    recovery_hint=(
                        f"To burn another disc of these volumes, stage them "
                        f"again: lcsas stage --for-location {location}, then "
                        f"lcsas burn --session latest --location {location}."
                    ),
                )

        receipts: list[BurnReceipt] = []
        total = len(session_vols)

        for idx, sv in enumerate(session_vols, start=1):
            # Not blank unless skip_burn, which does not read it (above).
            iso_path = Path(sv.iso_path)
            vol = get_volume_by_id(self._conn, sv.volume_id)

            # FUP-01: owner checkpoint + blank-media pre-check BEFORE
            # the physical burn.  Both are no-ops under skip_burn.
            if not skip_burn:
                prompt.checkpoint(
                    f"=== Disc {idx} of {total} — volume {vol.label} ===\n"
                    f"Insert a BLANK disc into {device}."
                )
                self._assert_blank_media(device, vol.label)

            if not skip_burn and not iso_path.exists():
                raise FileNotFoundError(
                    f"ISO file missing for volume {vol.label}: {iso_path}. "
                    f"The session's staging area was cleaned "
                    f"('lcsas stage --clean') or the file was removed "
                    f"manually. To burn another disc, re-stage for the "
                    f"target location: "
                    f"lcsas stage --for-location {location} && "
                    f"lcsas burn --session latest --location {location}"
                )

            # A never-burned (STAGING) volume enters BURNING, committed,
            # and only it goes back to STAGING when the burn fails.  A
            # VERIFIED or lost volume gets another disc (#726); a BURNED
            # one stays BURNED for the whole burn (#810).  The lifecycle
            # decides; its Burn record carries that to the outcome.
            lifecycle = VolumeLifecycle(self._conn)
            burn = lifecycle.burn_started(sv.volume_id)

            try:
                # Burn
                if not skip_burn:
                    self._xorriso.burn_iso(iso_path, device)
                # #727: one burn date for this disc.  The disc record, the
                # verify events and the burn receipt all carry it, so
                # 'catalog import-receipts' of this receipt on this
                # catalog sees it was already recorded.
                burn_date = utc_now()

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
                        event_date=burn_date,
                    )
                    if verify_passed:
                        verified_at = utc_now()

                if verify_passed:
                    # VERIFIED (a re-burn already is) in the same
                    # transaction as the disc row, with the verification
                    # evidence (BURN-04).
                    lifecycle.burn_succeeded(
                        burn, location=location, burn_date=burn_date,
                        iso_sha256=sv.iso_sha256 or None,
                        iso_size_bytes=sv.iso_size_bytes,
                        verified_at=verified_at,
                    )
                else:
                    # A never-burned volume goes back to STAGING (#483):
                    # BURNED would count its packs as archived (FMA-01's
                    # DURABLE_VOLUME_STATUSES) with no verified disc behind
                    # them.  A re-burned volume keeps its status, a BURNED
                    # one stays BURNED (#810).
                    lifecycle.burn_failed(
                        burn, location=location, burn_date=burn_date,
                    )
                    # BURN-05: a failed disc is not recorded. Recording one
                    # would satisfy the ACTIVE-disc location queries and
                    # `stage --for-location` would never re-stage these
                    # packs — a phantom disc in the catalog forever. The
                    # VERIFY_FAIL event carries the location for the
                    # audit trail.
                    _logger.error(
                        "Burn at %s FAILED verification for %s. NO disc "
                        "was recorded — this location still needs this "
                        "volume. Inspect the disc/drive and re-run: "
                        "lcsas burn --session %s --location %s",
                        location, vol.label, session_id, location,
                    )
                self._conn.commit()

                # Build the burn receipt
                from lcsas.db.volume_packs import get_pack_ids_for_volume
                pack_ids = get_pack_ids_for_volume(self._conn, sv.volume_id)

                receipt = BurnReceipt(
                    volume_label=vol.label,
                    volume_id=sv.volume_id,
                    session_id=session_id,
                    location=location,
                    device=device,
                    burn_date=burn_date,
                    iso_sha256=sv.iso_sha256 or "",
                    verify_passed=verify_passed,
                    pack_count=len(pack_ids),
                    pack_ids=pack_ids,
                    iso_size_bytes=sv.iso_size_bytes,
                )
                receipts.append(receipt)

                # FUP-01: persist this disc's burn receipt NOW (durable,
                # beside the master catalog), not only in the end-of-session
                # sweep — a mid-session crash then still leaves a burn receipt
                # for every disc actually burned.  The end-of-session writes below
                # are idempotent re-writes of the same files.
                self._write_durable_receipts([receipt], location)

            except BaseException as original_exc:
                # BaseException: Ctrl-C during a burn must also return the
                # volume to STAGING, or the next run finds it BURNING (#668).
                # Only a volume this run moved to BURNING goes back; a
                # BURNED one never left BURNED (#810).
                try:
                    self._conn.rollback()
                    lifecycle.burn_stopped(burn)
                except Exception as cleanup_exc:
                    _logger.error(
                        "Error during exception cleanup: %s",
                        cleanup_exc,
                        exc_info=True,
                    )
                    raise original_exc from cleanup_exc
                # If at least one volume was burned, mark the burn session PARTIAL
                if receipts:
                    update_session_status(self._conn, session_id, "PARTIAL")
                raise original_exc

            # FUP-01: after a verified burn, eject the tray (best-effort)
            # and tell the owner exactly what to write on the disc — the
            # hand-written label is the only link from a physical disc back
            # to the catalog decades later.  Skipped on a failed verify
            # (the disc is suspect) and under skip_burn (no disc).
            if not skip_burn and receipt.verify_passed:
                if interactive and not eject_tray(device):
                    _logger.warning(
                        "Could not eject %s — remove the disc by hand.",
                        device,
                    )
                self._prompt_labeling(prompt, vol.label, location)

            # BURN-06: the ISO is deliberately NOT deleted here. The
            # documented multi-copy flow re-burns the same burn session at a
            # second location, which needs the ISO intact. Deletion is
            # the explicit `clean_session` / `abort_session` step.

        # Update burn session status — any failed verify leaves the burn
        # session PARTIAL so it can never masquerade as a completed set of
        # discs; re-running the burn at the same location is the way to
        # retry.  [BURN-05]
        any_failed = any(not r.verify_passed for r in receipts)
        update_session_status(
            self._conn, session_id, "PARTIAL" if any_failed else "COMPLETE",
        )

        # Write burn receipts JSON — once into the session staging tree (a
        # convenience duplicate that dies with `stage --clean`) and once
        # durably beside the master catalog.  The disc catalog of this
        # burn session was mastered BEFORE this burn, so the durable file is
        # the only record of where this burn session's discs went that
        # survives both clean_session and the loss of the master catalog.
        # [BURN-08]
        session_vols_info = get_session_volumes(self._conn, session_id)
        if session_vols_info:
            from lcsas.db.sessions import recorded_path

            # A blank ISO path (#958): no staging tree here to copy into.
            first_iso = recorded_path(session_vols_info[0].iso_path)
            if first_iso is not None:
                self._write_receipts(receipts, first_iso.parent, location)
        self._write_durable_receipts(receipts, location)

        return receipts

    def _burn_preflight(
        self, session_id: str, session_vols: list[SessionVolume],
    ) -> None:
        """Refuse a session that cannot be burned, before any disc prompt.

        #665: a never-burned volume whose ISO hash is empty was staged by
        a ``lcsas stage`` that did not finish (killed during mastering or
        ECC).  Its ISO may lack RS03 data, so it is never burned.

        #862: a session whose staging folder has no session.json was
        staged by a ``lcsas stage`` that was killed
        (:func:`unfinished_stage_refusal`).
        """
        from lcsas.db.sessions import get_session, recorded_path

        vols = [(sv, get_volume_by_id(self._conn, sv.volume_id))
                for sv in session_vols]
        incomplete = [
            vol.label for sv, vol in vols
            if vol.status in ("STAGING", "BURNING") and not sv.iso_sha256
        ]
        if incomplete:
            raise BurnError(
                f"Staging of {', '.join(incomplete)} did not finish: the "
                f"ISO has no recorded hash and may lack its error-correction "
                f"data. Nothing was burned.",
                recovery_hint=(
                    f"lcsas session abort {session_id}, then lcsas stage "
                    f"again."
                ),
            )
        # #958: a blank staging folder is not on this computer; the burn
        # says its ISOs are not here.
        session_dir = recorded_path(get_session(self._conn, session_id).staging_dir)
        refusal = (unfinished_stage_refusal(session_dir, session_id)
                   if session_dir is not None else None)
        if refusal is not None:
            raise BurnError(refusal)

        # #668: a burn interrupted hard (killed, power loss) leaves its
        # volume in BURNING.  With no disc recorded it goes back to the
        # status it had before that burn, so this run burns it: STAGING,
        # or BURNED when an older LCSAS moved a BURNED volume to BURNING
        # (#810).  BURNING with a disc recorded is not a state the burn
        # writes; refuse it rather than guess.
        from lcsas.db.volume_copies import get_copies_for_volume

        for sv, vol in vols:
            if vol.status != "BURNING":
                continue
            if get_copies_for_volume(
                self._conn, sv.volume_id, active_only=False,
            ):
                raise BurnError(
                    f"Volume {vol.label} is BURNING but already has a disc "
                    f"recorded. Nothing was burned.",
                    recovery_hint=(
                        f"Check it with 'lcsas catalog reconcile' and "
                        f"'lcsas verify {vol.label} --disc'."
                    ),
                )
            recovered = VolumeLifecycle(self._conn).burn_interrupted_recovered(
                sv.volume_id,
            )
            _logger.warning(
                "Volume %s was left BURNING by an interrupted burn; "
                "resetting it to %s and burning it again.", vol.label,
                recovered.status,
            )

    def _assert_blank_media(self, device: str, label: str) -> None:
        """Refuse to burn onto a disc that already carries data (FUP-01).

        Converts the "forgot to swap the just-burned disc" mistake from a
        raw CalledProcessError + PARTIAL burn session into a clean, retryable
        error BEFORE any burn or status transition.  ``unknown`` (an odd
        drive we cannot classify) warns and proceeds rather than bricking
        the burn; ``no_media`` is left to ``burn_iso``'s own diagnostics.
        """
        status = self._xorriso.media_status(device)
        if status is MediaStatus.CLOSED:
            raise ValueError(
                f"Disc in {device} is not blank (it already contains data) "
                f"— cannot burn {label}. Remove it and insert a blank disc."
            )
        if status is MediaStatus.UNKNOWN:
            _logger.warning(
                "Could not determine the media status of %s before burning "
                "%s — proceeding anyway.", device, label,
            )

    def _prompt_labeling(
        self, prompt: OperatorPrompt, label: str, location: str,
    ) -> None:
        """Tell the owner what to hand-write on the disc just burned."""
        prompt.checkpoint(
            "Burn VERIFIED. Remove the disc and write on it now, with a "
            "soft felt-tip marker:\n"
            f"    {label}        (location: {location})"
        )

    def _verify_burned_disc(
        self,
        sv: SessionVolume,
        label: str,
        device: str,
        location: str,
        iso_path: Path,
        event_date: str | None = None,
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
        (the caller owns the transaction), dated *event_date* when given:
        the burn passes the disc's burn date, the ``burn_date`` of its
        receipt, so a replayed receipt is seen as imported (#727).  Returns True only when every
        applicable step passed.
        """
        def fail(detail: str) -> bool:
            add_event(
                self._conn, sv.volume_id, "VERIFY_FAIL",
                location=location, event_date=event_date, detail=detail,
                commit=False,
            )
            return False

        if not self._xorriso.verify_disc(device):
            return fail("Post-burn read-back failed")

        # Disc identity: the PVD Volume ID was written from the volume
        # label at mastering time, so a mismatch means the disc in the
        # drive is not this volume at all (wrong disc never swapped, or
        # a silently failed burn left an old disc behind).  [FMA-03]
        disc_id = self._xorriso.read_disc_volume_id(device)
        if disc_id != label:
            return fail(
                f"Disc in {device} identifies as '{disc_id}' — "
                f"expected '{label}'. Wrong disc?"
            )

        expected_hash = sv.iso_sha256
        if not expected_hash:
            if iso_path.exists():
                # Only a re-burn reaches this: _burn_preflight refuses a
                # never-burned volume with no hash (#665), so the #370
                # crash case never burns.  A VERIFIED volume from a
                # pre-Phase-13 session has no hash but may still have its
                # ISO: re-hash it for a full read-back comparison instead
                # of downgrading to readability-only (#699 kept this).
                expected_hash = sha256_file(iso_path)
            else:
                # Genuine pre-Phase-13 row (no hash ever recorded) AND the
                # ISO is gone: readability is all the evidence available —
                # preserve old semantics.
                add_event(
                    self._conn, sv.volume_id, "VERIFY_PASS",
                    location=location,
                    event_date=event_date,
                    detail="Post-burn read-back (no hash on record)",
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
            return fail(
                "device hash check impossible: no recorded ISO size "
                "(pre-upgrade session); -check_media alone is "
                "insufficient evidence"
            )

        try:
            device_hash = self._device_reader(device, iso_size)
        except OSError as exc:
            return fail(f"device read-back failed: {exc}")

        if device_hash != expected_hash:
            return fail(
                f"device hash mismatch: expected {expected_hash[:8]}.., "
                f"got {device_hash[:8]}.."
            )

        add_event(
            self._conn, sv.volume_id, "VERIFY_PASS",
            location=location,
            event_date=event_date,
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
        never_burned: bool = False,
    ) -> list[str]:
        """Remove staged ISOs and staging directories for a burn session.

        Refuses to clean a burn session whose volumes were never burned: the
        ISOs/staging tree are the only burnable artifacts, while the catalog
        would keep claiming the packs "archived" on phantom STAGING volumes.
        With ``force=True`` those volumes are deleted first, returning their
        packs to the unarchived pool, then cleaning proceeds.

        A volume that was burned but has no disc record (BURNED with no
        ``volume_copies`` row, as a catalog rebuilt from discs records the
        newest burn session, or STAGING/BURNING with an event that records
        a good disc) is never deleted: the whole clean is refused, with or
        without ``force``, before anything is removed.

        A volume a catalog rebuild brought in with no record of whether it
        was burned (:meth:`lcsas.db.lifecycle.VolumeLifecycle.burn_unknown`,
        #758) is refused the same way, unless *never_burned*: the owner
        confirms no disc of it was burned, and it is then deleted like a
        never-burned volume.

        With ``force=True`` the burn session is aborted, so when it is a
        consolidation session its CONSOLIDATING sources go back to
        VERIFIED, as ``session abort`` always did (#677, #817).  Returns
        their labels (empty for any other session, and without *force*:
        a plain clean after the burn keeps them CONSOLIDATING for
        ``consolidate --deprecate``).

        The burn receipts inside the staging tree die here too — that is
        fine: they are duplicates of the durable files under
        ``<db dir>/receipts/`` (:meth:`_write_durable_receipts`), which
        this method never touches.  [BURN-08]
        """
        session_id = resolve_session_id(self._conn, session_ref)
        session_vols = get_session_volumes(self._conn, session_id)

        # A volume that was burned but whose disc is not recorded yet
        # (a catalog rebuilt from discs) is never deleted, --force or
        # not.  Checked first: nothing is deleted when any is found.
        unrecorded = self._unrecorded_burned_session_volumes(session_id)
        if unrecorded:
            raise ValueError(
                _unrecorded_burn_refusal(f"session {session_id}", unrecorded)
            )
        # #758: checked next, also before anything is deleted.
        unknown = self._burn_unknown_session_volumes(session_id)
        if unknown and not never_burned:
            raise ValueError(_burn_unknown_refusal(
                f"session {session_id}", unknown,
                f"lcsas session abort {session_id}",
            ))

        strandable = self._strandable_session_volumes(
            session_id, never_burned=never_burned,
        )
        if strandable and not force:
            labels = ", ".join(label for _vid, label in strandable)
            raise ValueError(
                f"Session {session_id} has {len(strandable)} volume(s) with "
                f"no verified disc ({labels}) — never burned, or burned "
                f"but post-burn verification failed. Cleaning now would "
                f"permanently strand their packs as falsely 'archived'. "
                f"Re-burn the session ('lcsas burn --session {session_id}'), "
                f"or abort it ('lcsas session abort {session_id}') to return "
                f"the packs to the unarchived pool. "
                f"Use --force to clean AND abort in one step."
            )

        lifecycle = VolumeLifecycle(self._conn)
        for vid, label in strandable:
            reclaim = self._reclaim_summary([vid])
            _logger.info(
                "Volume %s has no verified disc — deleting; %d pack(s) "
                "return to the unarchived pool",
                label, reclaim.packs_reclaimed,
            )
            lifecycle.never_burned_deleted(vid, never_burned=never_burned)

        # #958: a blank path (a catalog rebuilt from discs) is not on
        # this computer: nothing to remove.  Never Path(''), the cwd.
        from lcsas.db.sessions import get_session, recorded_path

        for sv in session_vols:
            iso_path = recorded_path(sv.iso_path)
            if iso_path is not None and iso_path.is_file():
                iso_path.unlink()

        session = get_session(self._conn, session_id)
        staging_dir = recorded_path(session.staging_dir)
        if staging_dir is None:
            _logger.info(
                "The catalog records no staging folder for session %s (a "
                "catalog rebuilt from discs): nothing to remove. 'lcsas "
                "staging clean' removes a folder of it left in the staging "
                "area.", session_id,
            )
        elif staging_dir.exists():
            safe_remove_tree(staging_dir)

        # Aborted sessions reuse CLEANED: the sessions.status CHECK allows
        # only STAGED/PARTIAL/COMPLETE/CLEANED (no table rebuild for an
        # ABORTED value); the log carries the distinction.
        update_session_status(self._conn, session_id, "CLEANED")

        if not force:
            return []
        # #817: 'stage --clean --force' aborts the session as 'session
        # abort' does, so a consolidation's sources go back to VERIFIED
        # in both; nothing is left to finish them.
        from lcsas.consolidate.merger import release_consolidation_sources

        released = release_consolidation_sources(self._conn, session_id)
        if released:
            _logger.info(
                "Returned %d consolidation source volume(s) to VERIFIED: %s",
                len(released), ", ".join(released),
            )
        return released

    def abort_session(
        self, session_ref: str = "latest", *, never_burned: bool = False,
    ) -> AbortSummary:
        """Abort a never-burned burn session: reclaim its packs, then clean it.

        Deletes the burn session's STAGING/BURNING volumes (returning their packs
        to the unarchived pool so the next `lcsas stage` picks them up),
        removes the ISOs and staging tree, and marks the burn session CLEANED.
        Refused, before anything is deleted, when the burn session has a
        volume that was burned but has no disc record, or one a catalog
        rebuild left with no record of whether it was burned unless
        *never_burned* (see :meth:`clean_session`).
        """
        from lcsas.log import get_logger
        logger = get_logger()

        session_id = resolve_session_id(self._conn, session_ref)
        strandable = self._strandable_session_volumes(
            session_id, never_burned=never_burned,
        )
        summary = self._reclaim_summary([vid for vid, _label in strandable])
        summary.labels = [label for _vid, label in strandable]

        summary.sources_released = self.clean_session(
            session_id, force=True, never_burned=never_burned,
        )

        if not strandable:
            # #968: no volume of it was left unburned, so the abort
            # deleted nothing from the catalog; it only cleaned the session.
            logger.info(
                "Cleaned session %s: it has no volume that was never burned, "
                "so nothing was deleted from the catalog and no pack "
                "returned to the unarchived pool. Its ISOs and staging "
                "folder were removed ('lcsas stage --clean' does the same).",
                session_id,
            )
            return summary

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

    def abort_volume(
        self, label: str, *, never_burned: bool = False,
    ) -> AbortSummary:
        """Delete a single stranded never-burned volume by label.

        Covers volumes with no burn session (crash-window strandings from old
        catalogs).  Refuses anything not in STAGING/BURNING, a volume
        with burn evidence, and one a catalog rebuild left with no record
        of whether it was burned unless *never_burned* (#758).
        """
        from lcsas.db.volumes import get_volume_by_label
        from lcsas.log import get_logger
        logger = get_logger()

        vol = get_volume_by_label(self._conn, label)
        if vol is None:
            raise ValueError(f"Volume '{label}' not found in catalog.")
        lifecycle = VolumeLifecycle(self._conn)
        refusal = lifecycle.may_delete(vol.volume_id,
                                       never_burned=never_burned)
        if refusal is not None:
            raise ValueError(_abort_volume_refusal(label, refusal))

        summary = self._reclaim_summary([vol.volume_id])
        summary.labels = [label]
        lifecycle.never_burned_deleted(vol.volume_id,
                                       never_burned=never_burned)

        logger.info(
            "Aborted volume %s: reclaimed %d pack(s), %s bytes returned "
            "to the unarchived pool.",
            label, summary.packs_reclaimed, f"{summary.bytes_reclaimed:,}",
        )
        return summary

    def _strandable_session_volumes(
        self, session_id: str, *, never_burned: bool = False,
    ) -> list[tuple[int, str]]:
        """Return (volume_id, label) for a burn session's never-burned
        volumes — cleaning them would strand their packs as falsely
        'archived', so clean refuses them and abort deletes them.

        That is a STAGING/BURNING volume (never burned, or burned but
        failed its post-burn verification, #483) with no ``volume_copies``
        row and no event that records a good disc
        (:meth:`~lcsas.db.lifecycle.VolumeLifecycle.burn_evidence`).
        A volume with any ``volume_copies`` row corresponds to a disc
        (however stale its status) and is never deleted by clean/abort
        (FMA-01); nor is a BURNED volume with no disc row, or a
        STAGING/BURNING one with burn evidence: those are
        :meth:`_unrecorded_burned_session_volumes`.  Nor, unless
        *never_burned*, is one a catalog rebuild left with no record of
        whether it was burned (:meth:`_burn_unknown_session_volumes`).
        """
        lifecycle = VolumeLifecycle(self._conn)
        return [
            (vid, label)
            for vid, label, _status in self._session_volumes_without_disc(
                session_id)
            if lifecycle.may_delete(vid, never_burned=never_burned) is None
        ]

    def _burn_unknown_session_volumes(self, session_id: str) -> list[str]:
        """Labels of a burn session's volumes that a catalog rebuild left
        with no record of whether they were burned (#758): the ones
        :meth:`~lcsas.db.lifecycle.VolumeLifecycle.may_delete` refuses for
        that reason."""
        lifecycle = VolumeLifecycle(self._conn)
        return [
            label
            for vid, label, _status in self._session_volumes_without_disc(
                session_id)
            if (refusal := lifecycle.may_delete(vid)) is not None
            and refusal.kind == "burn unknown"
        ]

    def _unrecorded_burned_session_volumes(
        self, session_id: str
    ) -> list[tuple[str, str]]:
        """Return (label, why) for a burn session's volumes that were burned
        but have no ``volume_copies`` row, which clean/abort never delete.

        A catalog rebuilt from discs records the newest burn session's
        volumes like this: BURNED, with no disc row (#678).  A BURNED
        volume with no disc row from before #483 (a failed post-burn
        verification) looks the same, so it is kept too.  A STAGING/BURNING
        volume counts when an event records a good disc of it.  Both are
        what :meth:`~lcsas.db.lifecycle.VolumeLifecycle.may_delete` refuses
        for their status or their burn evidence.
        """
        lifecycle = VolumeLifecycle(self._conn)
        found: list[tuple[str, str]] = []
        for vid, label, _status in self._session_volumes_without_disc(
                session_id):
            refusal = lifecycle.may_delete(vid)
            if refusal is None:
                continue
            if refusal.kind == "status":
                found.append((label, f"{refusal.status}, but no disc of it "
                                     f"is recorded"))
            elif refusal.kind == "burned":
                found.append((label, f"{refusal.status}, but "
                                     f"{refusal.evidence}"))
        return found

    def _session_volumes_without_disc(
        self, session_id: str
    ) -> list[tuple[int, str, str]]:
        """(volume_id, label, status) of a burn session's STAGING, BURNING
        and BURNED volumes that have no ``volume_copies`` row."""
        rows = self._conn.execute(
            """SELECT v.volume_id, v.label, v.status FROM volumes v
               JOIN session_volumes sv ON sv.volume_id = v.volume_id
               WHERE sv.session_id = ?
                 AND v.status IN ('STAGING', 'BURNING', 'BURNED')
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_copies vc
                     WHERE vc.volume_id = v.volume_id
                 )
               ORDER BY v.label""",
            (session_id,),
        ).fetchall()
        return [(row["volume_id"], row["label"], row["status"])
                for row in rows]

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

    def _packs_by_sha256(self, pack_sha256s: list[str]) -> list[Pack]:
        """The catalog's packs with these SHA-256s, in the order given.

        Raises ValueError naming any SHA-256 the catalog does not know.
        """
        from lcsas.db.packs import get_pack_by_sha256

        packs: list[Pack] = []
        unknown: list[str] = []
        for sha in dict.fromkeys(pack_sha256s):
            pack = get_pack_by_sha256(self._conn, sha)
            if pack is None:
                unknown.append(sha)
            else:
                packs.append(pack)
        if unknown:
            raise ValueError(
                f"{len(unknown)} pack(s) are not in the catalog: "
                + ", ".join(unknown[:5])
                + (" …" if len(unknown) > 5 else "")
            )
        return packs

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
            if repo_ids:
                packs = [p for p in packs if p.repo_id in repo_ids]
            return packs
        if repo_ids:
            return [p for rid in repo_ids
                    for p in get_unarchived_packs(self._conn, rid)]
        return get_unarchived_packs(self._conn)

    def _killed_stage_sessions(
        self, held: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        """The sessions of *held* whose stage was killed hard (#862):
        :func:`unfinished_stage_refusal` refuses to burn them."""
        from lcsas.db.sessions import get_session, recorded_path

        killed: dict[str, list[str]] = {}
        for sid, labels in held.items():
            session_dir = recorded_path(get_session(self._conn, sid).staging_dir)
            if (session_dir is not None
                    and unfinished_stage_refusal(session_dir, sid) is not None):
                killed[sid] = labels
        return killed

    def _hold_back_waiting(
        self, packs: list[Pack],
    ) -> tuple[list[Pack], dict[str, list[str]]]:
        """Drop the packs that sit on a volume waiting to be burned (#672).

        A volume is waiting when it was never burned (STAGING/BURNING),
        its staging finished (the ISO hash is recorded, #665) and its ISO
        is still in the staging area.  As for ``stage --for-location``
        (#642), a volume whose disc failed verification does not hold its
        packs back.  Returns the remaining packs and the waiting volumes'
        labels per burn session.
        """
        from lcsas.db.sessions import staged_iso_present, staging_incomplete

        rows = self._conn.execute(
            """SELECT v.volume_id, v.label,
                      (SELECT sv.session_id FROM session_volumes sv
                       JOIN burn_sessions bs USING (session_id)
                       WHERE sv.volume_id = v.volume_id
                       ORDER BY julianday(bs.created_at) DESC, sv.rowid DESC
                       LIMIT 1) AS session_id
               FROM volumes v
               WHERE v.status IN ('STAGING', 'BURNING')
                 AND NOT EXISTS (
                     SELECT 1 FROM volume_events e
                     WHERE e.volume_id = v.volume_id
                       AND e.event_type = 'VERIFY_FAIL'
                 )
               ORDER BY v.label"""
        ).fetchall()
        waiting: dict[int, tuple[str, str]] = {}
        for r in rows:
            vid = int(r["volume_id"])
            if r["session_id"] is None:
                continue
            if staging_incomplete(self._conn, vid):
                continue
            if not staged_iso_present(self._conn, vid):
                continue
            waiting[vid] = (str(r["label"]), str(r["session_id"]))
        if not waiting:
            return packs, {}

        placeholders = ",".join("?" * len(waiting))
        held_rows = self._conn.execute(
            f"SELECT pack_id, volume_id FROM volume_packs "
            f"WHERE volume_id IN ({placeholders})",
            list(waiting),
        ).fetchall()
        holder = {int(r["pack_id"]): int(r["volume_id"]) for r in held_rows}
        remaining: list[Pack] = []
        held: dict[str, list[str]] = {}
        for p in packs:
            on_volume = holder.get(p.pack_id)
            if on_volume is None:
                remaining.append(p)
                continue
            label, session_id = waiting[on_volume]
            labels = held.setdefault(session_id, [])
            if label not in labels:
                labels.append(label)
        return remaining, held

    def plan_volumes(
        self, packs: list[Pack], media_type: MediaType,
    ) -> list[tuple[list[Pack], int]]:
        """The volumes :meth:`stage` makes of exactly *packs*, with no
        side effect: the measured reserve (#811) and the same bin-packing.

        ``lcsas consolidate`` previews with it, so the volume count it
        shows is the one ``consolidate --execute`` stages (#862).  Raises
        as :meth:`stage` does when the packs cannot fit.
        """
        reserve, measured = self._metadata_reserve(packs, media_type)
        self._check_metadata_fits(packs, media_type, reserve, measured)
        return self._multi_bin_pack(packs, media_type, reserve)

    def _multi_bin_pack(
        self,
        packs: list[Pack],
        media_type: MediaType,
        reserve: int | None = None,
    ) -> list[tuple[list[Pack], int]]:
        """Bin-pack packs into multiple volumes.

        *reserve* is the space kept for the disc metadata on each volume;
        None measures it (:meth:`_metadata_reserve`), as :meth:`stage`
        does.  Each pack counts with its ISO 9660 overhead
        (:func:`~lcsas.staging.metadata.pack_disc_bytes`).

        Returns list of (selected_packs, total_bytes) tuples, one per
        volume; total_bytes is the packs' own size.
        """
        if reserve is None:
            reserve = self._metadata_reserve(packs, media_type)[0]
        remaining_packs = list(packs)
        volume_plans: list[tuple[list[Pack], int]] = []

        while remaining_packs:
            items = [
                (p.sha256, pack_disc_bytes(p.size_bytes))
                for p in remaining_packs
            ]
            selected_items, leftover_items = first_fit_decreasing(
                items,
                capacity=media_type.usable_bytes,
                reserved=reserve,
            )

            if not selected_items:
                # None of the remaining packs fit — they're all too large.
                usable = media_type.usable_bytes - reserve
                oversized = [
                    p for p in remaining_packs
                    if pack_disc_bytes(p.size_bytes) > usable
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
                    f"reserved={reserve})"
                )

            selected_hashes = {sha for sha, _size in selected_items}
            selected_packs = [
                p for p in remaining_packs if p.sha256 in selected_hashes
            ]
            total_bytes = sum(p.size_bytes for p in selected_packs)
            volume_plans.append((selected_packs, total_bytes))

            remaining_packs = [
                p for p in remaining_packs if p.sha256 not in selected_hashes
            ]

        return volume_plans

    def _pack_space_bytes(self, media_type: MediaType, reserve: int) -> int:
        """Bytes of packs one volume can hold: what _multi_bin_pack fills.

        The medium's usable bytes (after the ECC share) less the metadata
        *reserve*.  A volume with this many bytes of packs is 100% full.
        """
        return max(1, media_type.usable_bytes - reserve)

    def _metadata_reserve(
        self, packs: list[Pack], media_type: MediaType,
    ) -> tuple[int, DiscMetadataSize]:
        """The space kept for the disc metadata on each volume (#811).

        ``max(metadata_reserve_mb, measured + margin)``: the configured
        reserve is a floor, and the measurement
        (:func:`~lcsas.staging.metadata.measure_disc_metadata`) covers an
        archive whose catalog and index files have outgrown it.  Returns
        the reserve and the measurement.
        """
        configured = self._config.metadata_reserve_bytes
        measured = measure_disc_metadata(
            self._conn, self._get_mirror_paths(),
            pack_count=len(packs), volume_count=1,
            with_source=not media_type.is_test,
        )
        # The catalog gains rows for each volume of the stage: estimate
        # how many volumes the packs need, then count them in.
        space = media_type.usable_bytes - max(configured, measured.reserve)
        if space > 0 and len(packs) > 1:
            pack_bytes = sum(pack_disc_bytes(p.size_bytes) for p in packs)
            extra = -(-pack_bytes // space)  # one more than needed
            measured = measure_disc_metadata(
                self._conn, self._get_mirror_paths(),
                pack_count=len(packs), volume_count=1 + extra,
                with_source=not media_type.is_test,
            )
        return max(configured, measured.reserve), measured

    def _check_metadata_fits(
        self,
        packs: list[Pack],
        media_type: MediaType,
        reserve: int,
        measured: DiscMetadataSize,
    ) -> None:
        """Refuse the stage when one disc cannot hold the disc metadata and
        the smallest pack (#811), before anything is staged.

        A pack too large for the medium even without metadata is left to
        :meth:`_multi_bin_pack`, which names it.
        """
        if not packs:
            return
        usable = media_type.usable_bytes
        smallest = min(pack_disc_bytes(p.size_bytes) for p in packs)
        if reserve + smallest <= usable:
            return
        if smallest > usable - self._config.metadata_reserve_bytes:
            return  # the pack itself is too large: _multi_bin_pack says so
        raise DiscMetadataTooLargeError(
            f"The metadata that goes on every disc needs {reserve:,} bytes "
            f"({measured.total:,} measured: {measured.describe()}; plus "
            f"a margin). A {media_type.name} disc holds {usable:,} bytes "
            f"besides its error correction, so it has no room left for "
            f"even the smallest pack ({smallest:,} bytes). Nothing was "
            f"staged.",
            recovery_hint=(
                "Use a larger media type: 'lcsas stage --media <type>', "
                "or media_type in lcsas.toml."
            ),
        )

    def _trim_underfilled(
        self,
        volume_plans: list[tuple[list[Pack], int]],
        media_type: MediaType,
        min_fill_pct: float,
        *,
        partial: bool,
        reserve: int,
    ) -> tuple[list[tuple[list[Pack], int]], UnderfilledVolume | None]:
        """Drop the last planned volume when it is below *min_fill_pct* (#701).

        Fill is measured against the volume's pack space
        (:meth:`_pack_space_bytes`), so a volume packed as full as it can
        be is at 100%, whatever the medium's ECC share.  Only the last
        volume is checked: _multi_bin_pack fills each volume as far as
        the packs allow before it starts the next, so the last one holds
        what is left over.  With *partial*, or no minimum, nothing is
        dropped.
        """
        if min_fill_pct <= 0 or partial or not volume_plans:
            return volume_plans, None
        packs, data_bytes = volume_plans[-1]
        space = self._pack_space_bytes(media_type, reserve)
        fill = data_bytes / space * 100
        if fill >= min_fill_pct:
            return volume_plans, None
        return volume_plans[:-1], UnderfilledVolume(
            pack_count=len(packs),
            data_bytes=data_bytes,
            pack_space_bytes=space,
            fill_pct=fill,
            min_fill_pct=min_fill_pct,
        )

    def _write_session_manifest(
        self,
        session_id: str,
        session_dir: Path,
        manifests: list[BurnManifest],
    ) -> Path:
        """Write session.json manifest to the burn session directory.

        Written last by :meth:`stage` (and by :meth:`_after_failed_stage`
        for the volumes it keeps), it marks the session's stage finished
        (:func:`unfinished_stage_refusal`).
        """
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
        # Written under another name and renamed: the file marks a
        # finished stage (#862), so a half-written one must never exist.
        manifest_path = session_dir / SESSION_MANIFEST
        work = manifest_path.with_suffix(".json.tmp")
        with open(work, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(work, manifest_path)
        return manifest_path

    @staticmethod
    def _dump_receipt(receipt: BurnReceipt, path: Path) -> None:
        """Serialize one burn receipt to *path* (fsynced)."""
        with open(path, "w", encoding="utf-8") as f:
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

    def _write_receipts(
        self,
        receipts: list[BurnReceipt],
        session_dir: Path,
        location: str,
    ) -> list[Path]:
        """Write burn receipt JSON files into the burn session's staging tree.

        These are convenience duplicates only: ``clean_session`` removes
        them along with the staging tree.  The permanent record is the
        file under ``<db dir>/receipts/`` written by
        :meth:`_write_durable_receipts`.  [BURN-08]
        """
        receipts_dir = session_dir / "receipts"
        ensure_dir(receipts_dir)

        paths: list[Path] = []
        for receipt in receipts:
            receipt_path = receipts_dir / (
                f"{receipt.volume_label}_{location}.json"
            )
            self._dump_receipt(receipt, receipt_path)
            paths.append(receipt_path)

        return paths

    def _write_durable_receipts(
        self,
        receipts: list[BurnReceipt],
        location: str,
    ) -> list[Path]:
        """Persist burn receipts beside the master catalog.  [BURN-08]

        Every disc of a burn session carries a disc catalog mastered BEFORE
        the burn session's burns, so the disc catalog can never say where
        the newest discs are kept.  These files — on the (backed-up)
        drive that holds the master catalog, outside the staging area — are
        the durable record of that provenance, and are what
        ``lcsas catalog import-receipts <db dir>/receipts/*.json``
        replays onto a rebuilt master catalog.
        """
        receipts_dir = self._config.db_path.parent / "receipts"
        ensure_dir(receipts_dir)

        paths: list[Path] = []
        for receipt in receipts:
            # Same ':' sanitization as the session staging dir name.
            session_part = receipt.session_id.replace(":", "-")
            receipt_path = receipts_dir / (
                f"{session_part}_{receipt.volume_label}_"
                f"{location}.json"
            )
            self._dump_receipt(receipt, receipt_path)
            _logger.info(
                "Burn receipt: %s — print it and file it with the disc.",
                receipt_path,
            )
            paths.append(receipt_path)

        return paths
