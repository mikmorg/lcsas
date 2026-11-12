"""``lcsas burn`` and ``lcsas burn-iso``."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from lcsas.cli.common import (
    _load_config_opt,
    _open_catalog,
    _resolve_device,
    _validate_config_or_exit,
)
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.iso.xorriso import XorrisoRunner

logger = get_logger()


def cmd_burn_session(args: argparse.Namespace) -> int:
    """Burn a staged session to disc."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for burn.")
        return 1
    if not _validate_config_or_exit(config):
        return 1

    with _open_catalog(args, config) as conn:
        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        location = args.location or config.default_location
        # Issue #19: reject unknown --location names rather than silently
        # auto-creating a phantom row. Owners opt into creation with
        # --create-location (or by pre-registering via `location add`).
        from lcsas.db.locations import UnknownLocationError, resolve_location
        try:
            resolve_location(
                conn, location,
                create=getattr(args, "create_location", False),
            )
        except UnknownLocationError as exc:
            logger.error(str(exc))
            return 1

        if getattr(args, "dry_run", False):
            from lcsas.db.queries import get_lost_volume_ids, volume_display_status
            from lcsas.db.sessions import get_session_volumes, resolve_session_id
            from lcsas.db.volumes import get_volume_by_id
            sid = resolve_session_id(conn, args.session or "latest")
            vols = get_session_volumes(conn, sid)
            lost_ids = get_lost_volume_ids(conn)
            logger.info(f"[DRY RUN] Session {sid}: {len(vols)} volume(s)")
            for sv in vols:
                vol = get_volume_by_id(conn, sv.volume_id)
                shown = volume_display_status(vol, lost_ids)
                logger.info(f"  {vol.label}  status={shown}")
            return 0

        device = _resolve_device(args, config)
        if not getattr(args, "skip_burn", False):
            import os
            if not os.path.exists(device):
                logger.error(
                    "Optical device '%s' not found.\n"
                    "Insert a disc or specify the correct device with --device.",
                    device,
                )
                return 1

        receipts = orch.burn_session(
            session_ref=args.session,
            location=location,
            device=device,
            interactive=not getattr(args, "no_prompt", False),
        )

    logger.info(f"Burned {len(receipts)} volume(s) to {location}:")
    for r in receipts:
        if r.verify_passed:
            logger.info(f"  {r.volume_label} → {r.pack_count} packs")
        else:
            logger.error(
                f"  {r.volume_label} → VERIFY FAILED — no disc recorded; "
                f"needs re-burn at {location}"
            )
    failed = [r for r in receipts if not r.verify_passed]
    if failed:
        # BURN-05: the session stays PARTIAL; re-burning at the same
        # location records the disc normally once verification passes.
        logger.error(
            "%d volume(s) failed verification — session %s is PARTIAL. "
            "Inspect the disc/drive and re-run: "
            "lcsas burn --session %s --location %s",
            len(failed), failed[0].session_id,
            failed[0].session_id, location,
        )
        return 1
    # BURN-06: ISOs persist so the same session can be burned at more
    # locations; cleanup is the explicit `stage --clean` step.
    logger.info(
        "ISOs retained for additional copies. After burning all "
        "locations, free the staging area with: lcsas stage --clean"
    )
    return 0


def cmd_burn_iso(args: argparse.Namespace) -> int:
    """Burn a single ISO file to optical media (standalone).

    With ``--verify-only`` nothing is burned: the disc already in the drive
    is checked against the ISO and a receipt is written for it, which
    replaces a receipt that was lost (#673).
    """
    from datetime import UTC, datetime

    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    iso_path = args.iso_path
    if not iso_path.exists():
        logger.error(f"ISO file not found: {iso_path}")
        return 1

    emit_receipt = getattr(args, "emit_receipt", None)
    verify_only = bool(getattr(args, "verify_only", False))
    if emit_receipt is not None and not args.location:
        logger.error("--location is required when --emit-receipt is given.")
        return 1
    if verify_only and not args.verify:
        logger.error("--verify-only checks the disc; it cannot be combined "
                     "with --no-verify.")
        return 1

    runner = SubprocessXorrisoRunner()
    # burn-iso is the portable half of the split workflow: it needs no
    # CATALOG, so it never opens one.  It does still read config — via
    # _resolve_device, which falls back to `--config`'s optical_device so
    # an owner who recorded the drive there need not repeat it as
    # --device on every burn.  That read happens only when --device was
    # not given and only when --config was: --device short-circuits
    # first, and _load_config_opt returns None without a --config.
    device = _resolve_device(args)

    # Hash before burn — cheap insurance against the file changing under
    # us, and the hash the disc is read back against (#664).
    iso_sha256 = ""
    iso_size_bytes: int | None = None
    if emit_receipt is not None or args.verify:
        from lcsas.utils.hashing import sha256_file
        iso_sha256 = sha256_file(iso_path)
        iso_size_bytes = iso_path.stat().st_size

    # #514: the parent directory was the default label, but `lcsas stage`
    # writes <session>/<LABEL>.iso, so that gave the session name and the
    # identity check below then failed.  The ISO's PVD volume ID is the
    # label it was mastered with, wherever the file sits.
    label = args.label or _iso_volume_id(iso_path) or iso_path.parent.name

    # #675: settle where the receipt goes, and refuse a clash, BEFORE the
    # burn: a refusal after it would leave a disc with no receipt.
    out_path: Path | None = None
    if emit_receipt is not None:
        out_path = _receipt_out_path(str(emit_receipt), label, args.location)
        refusal = _receipt_overwrite_refusal(out_path, label, args.location)
        if refusal is not None:
            logger.error(refusal)
            return 1

    if verify_only:
        logger.info(
            f"--verify-only: checking the disc in {device} against "
            f"{iso_path}; nothing is burned."
        )
    else:
        # FUP-01 mitigation 2: blank-media pre-check (behind --verify). A
        # disc that already carries data is refused before the burn, not
        # after.
        if args.verify:
            from lcsas.iso.xorriso import MediaStatus
            status = runner.media_status(device=device)
            if status is MediaStatus.CLOSED:
                logger.error(
                    "Disc in %s is not blank (it already contains data). "
                    "Remove it and insert a blank disc.", device,
                )
                return 1
            if status is MediaStatus.UNKNOWN:
                logger.warning(
                    "Could not determine the media status of %s — "
                    "proceeding anyway.", device,
                )

        logger.info(f"Burning {iso_path} to {device} ...")
        runner.burn_iso(iso_path, device=device)
        logger.info("Burn complete.")

    # None = not verified (--no-verify).  It used to default to True, so a
    # disc never read back imported as VERIFIED (#514).
    verify_passed: bool | None = None
    device_sha256: str | None = None
    if args.verify:
        logger.info(f"Verifying disc on {device} ...")
        verify_passed, device_sha256 = _burn_iso_readback(
            runner, device, label, iso_sha256, iso_size_bytes,
        )
        logger.info(f"  Verify: {'PASS' if verify_passed else 'FAIL'}")

    if out_path is not None:
        receipt = {
            "volume_label": label,
            "session_id": args.session,
            "location": args.location,
            "device": device,
            "burn_date": datetime.now(UTC).isoformat(),
            "iso_sha256": iso_sha256,
            "iso_size_bytes": iso_size_bytes,
            "verify_passed": verify_passed,
            # #664: SHA-256 of the bytes read back from the disc (null when
            # nothing was read back).
            "device_sha256": device_sha256,
            # #673: true when this run checked a disc burned earlier instead
            # of burning one (a receipt that replaces a lost one).
            "verify_only": verify_only,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        logger.info(f"Receipt written: {out_path}")

    if verify_passed is False:
        return 1

    return 0


def _burn_iso_readback(
    runner: XorrisoRunner,
    device: str,
    label: str,
    iso_sha256: str,
    iso_size_bytes: int | None,
) -> tuple[bool, str | None]:
    """burn-iso's check of the disc in *device* against the ISO (#664).

    The same three steps as the direct burn path (``_verify_burned_disc``):
    ``xorriso -check_media`` (readable), the PVD volume ID equals *label*
    (FUP-01: the right disc), then exactly *iso_size_bytes* read back from
    the device must have the ISO's SHA-256.  Returns ``(passed, hash read
    back)``; the hash is None when the read-back did not run or failed.
    """
    from lcsas.burn.device_verify import read_device_sha256

    if not runner.verify_disc(device=device):
        logger.error(f"Disc in {device} failed the readability check "
                     "(xorriso -check_media).")
        return False, None
    disc_id = runner.read_disc_volume_id(device=device)
    if disc_id != label:
        logger.error(
            "Disc in %s identifies as '%s' — expected '%s'. "
            "Wrong disc?", device, disc_id, label,
        )
        return False, None
    if not iso_sha256 or not iso_size_bytes:
        logger.error("The ISO is empty; there is nothing to compare the "
                     "disc with.")
        return False, None
    try:
        device_sha256 = read_device_sha256(device, iso_size_bytes)
    except (OSError, ValueError) as exc:
        logger.error(f"Reading the disc back from {device} failed: {exc}")
        return False, None
    if device_sha256 != iso_sha256:
        logger.error(
            f"The disc in {device} does not match the ISO: read back "
            f"SHA-256 {device_sha256}, ISO SHA-256 {iso_sha256}."
        )
        return False, device_sha256
    logger.info(f"  Read back {iso_size_bytes:,} bytes; SHA-256 matches "
                f"the ISO ({device_sha256[:16]}..).")
    return True, device_sha256


def _receipt_out_path(target: str, label: str, location: str) -> Path:
    """Where ``burn-iso --emit-receipt TARGET`` writes the receipt (#675).

    TARGET is a folder when it is one, when it ends in a path separator,
    or when it does not exist and has no file extension: the receipt is
    then ``<label>_<location>.json`` inside it (the folder is created when
    the receipt is written).  Any other TARGET is the receipt file itself.
    """
    path = Path(target)
    seps = tuple(s for s in (os.sep, os.altsep) if s)
    if path.is_dir() or target.endswith(seps) or (
        not path.exists() and not path.suffix
    ):
        return path / f"{label}_{location}.json"
    return path


def _receipt_overwrite_refusal(
    out_path: Path, label: str, location: str,
) -> str | None:
    """Why the receipt may not be written to *out_path*, or None (#675).

    A file already there may only be replaced by a receipt for the same
    volume and location (a re-run); anything else is another disc's
    receipt, or not a receipt at all, and is kept.  The folder the receipt
    goes in must be one, or be creatable: a file in its place (e.g. a
    'receipts' file written before #675) is refused too.
    """
    ancestor = out_path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists() and not ancestor.is_dir():
        return (
            f"{ancestor} is a file, not a folder, so the receipt cannot be "
            f"written in it.  Move that file aside or give --emit-receipt "
            f"another folder.  Nothing was burned or checked."
        )
    if not out_path.exists():
        return None
    existing: object = None
    if out_path.is_file():
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            existing = None
    if isinstance(existing, dict) and (
        existing.get("volume_label") == label
        and existing.get("location") == location
    ):
        return None
    if isinstance(existing, dict) and "volume_label" in existing:
        owner = (f"the receipt of volume '{existing.get('volume_label')}' "
                 f"at '{existing.get('location')}'")
    else:
        owner = "not a receipt of this volume"
    return (
        f"{out_path} already exists and is {owner}; refusing to overwrite "
        f"it.  Give --emit-receipt a folder (for example 'receipts/') or "
        f"another file name.  Nothing was burned or checked."
    )


def _iso_volume_id(iso_path: Path) -> str | None:
    """The ISO 9660 primary volume descriptor's volume identifier, or None.

    lcsas masters every image with its volume label as the PVD volume ID
    (and burn-iso's identity check compares the burned disc against it),
    so this names the volume whatever the file is called (#514).
    """
    try:
        with open(iso_path, "rb") as f:
            for sector in range(16, 48):          # descriptors from LBA 16
                f.seek(sector * 2048)
                desc = f.read(2048)
                if len(desc) < 72 or desc[1:6] != b"CD001":
                    return None
                if desc[0] == 1:                  # primary volume descriptor
                    vid = desc[40:72].decode("ascii", "replace").strip(" \x00")
                    return vid or None
                if desc[0] == 255:                # set terminator
                    return None
    except OSError:
        return None
    return None
