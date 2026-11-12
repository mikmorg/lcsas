"""``lcsas catalog``: import-receipts, validate, reconcile, rebuild."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from lcsas.cli.common import (
    _confirm,
    _load_config_opt,
    _open_catalog,
    _reburn_hint,
    _resolve_db_path,
)
from lcsas.log import get_logger

logger = get_logger()


def _receipt_verified_at(burn_date: object) -> str:
    """The time a verified receipt's disc was read back, as the UTC ISO-8601
    string ``volume_copies.last_verified_at`` is compared in (#646): the
    receipt's ``burn_date`` (burn-iso verifies straight after burning),
    normalised to UTC, or the import time when it is missing or unreadable.
    A time in the future is clamped to now."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    if isinstance(burn_date, str) and burn_date.strip():
        try:
            when = datetime.fromisoformat(burn_date.strip())
        except ValueError:
            return now.isoformat()
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return min(when.astimezone(UTC), now).isoformat()
    return now.isoformat()


def _receipt_timestamp(value: object) -> float | None:
    """A receipt or catalog time as POSIX seconds, or None when it is
    missing or unreadable.  A time without a zone is read as UTC."""
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _receipt_already_imported(
    conn: sqlite3.Connection,
    volume_id: int,
    location: str,
    burn_date: str,
    receipt_hash: str,
) -> bool:
    """True when a receipt with this location, burn date and ISO hash was
    imported for the volume before (#666), or was written by a direct
    ``lcsas burn`` on this catalog (#727).

    The BURN_RECEIPT_IMPORTED event records the burn date since #666; for
    receipts imported earlier the disc record's burn date stands in.  A
    direct burn records the disc with the receipt's burn date, and dates
    its verify events (pass or fail) with it, so its receipt matches the
    disc record, or the verify event when the disc failed or a later
    burn at the location replaced the record.  An import dates its own
    events with the import time, so only the burn matches them.
    """
    from lcsas.db.volume_copies import get_copy_at_location
    from lcsas.db.volume_events import get_events_for_volume

    for prior in get_events_for_volume(conn, volume_id):
        if (
            prior.event_type in ("VERIFY_PASS", "VERIFY_FAIL",
                                 "VERIFY_FAIL_REBURN")
            and prior.location == location
            and burn_date
            and prior.event_date == burn_date
        ):
            return True
    for prior in get_events_for_volume(conn, volume_id, "BURN_RECEIPT_IMPORTED"):
        if prior.location != location:
            continue
        try:
            detail = json.loads(prior.detail) if prior.detail else {}
        except json.JSONDecodeError:
            continue
        if (
            isinstance(detail, dict)
            and detail.get("burn_date") == burn_date
            and (detail.get("iso_sha256") or "") == receipt_hash
        ):
            return True
    copy = get_copy_at_location(conn, volume_id, location)
    return (
        copy is not None
        and copy.burn_date == burn_date
        and (copy.iso_sha256 or "") == receipt_hash
    )


def cmd_catalog_import(args: argparse.Namespace) -> int:
    """Import burn receipts from remote burns.

    A receipt for a volume with no verified disc yet (STAGING, BURNING or
    BURNED; #670) moves the volume on:
      - verify_passed=true  → VERIFIED (+ mark_closed, VERIFY_PASS).  The
        receipt's iso_sha256 must match the ISO recorded at staging, unless
        --allow-unhashed (#669).
      - verify_passed=false → back to STAGING, VERIFY_FAIL event (#483, as
        burn_session does: the packs go back to the unarchived pool, and
        ``lcsas stage --for-location`` stages them again, #671)
      - verify_passed=null  → BURNED (burn-iso --no-verify: a disc exists
        but was never read back, so it is not VERIFIED)
    Already-VERIFIED volumes (re-burn to a new location) keep their status.

    A disc is recorded for every receipt except verify_passed=false: a disc
    that failed verification is not recorded (BURN-05, as burn_session does),
    so that location still needs the volume.  A re-burn that failed gets a
    VERIFY_FAIL_REBURN event instead (#514).

    Every receipt that cannot be imported is refused with a message, the
    others are still imported, and the exit code is non-zero (#669).  A
    receipt imported before is skipped as "already imported" (exit 0), and
    an old receipt never brings back a disc recorded lost or destroyed
    (#666).
    """
    from lcsas.db.locations import (
        UnknownLocationError,
        list_locations,
        resolve_location,
    )
    from lcsas.db.sessions import get_staged_iso, staging_incomplete
    from lcsas.db.volume_copies import (
        add_volume_copy,
        get_copies_for_volume,
        get_copy_at_location,
        get_disc_retired_at,
    )
    from lcsas.db.volume_events import add_event, get_events_for_volume
    from lcsas.db.volumes import get_volume_by_label, mark_closed, update_status

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for catalog.")
        return 1
    allow_unhashed = bool(getattr(args, "allow_unhashed", False))
    allow_other_location = bool(getattr(args, "allow_other_location", False))
    allow_incomplete = bool(getattr(args, "allow_incomplete_staging", False))
    absent = object()

    refused = 0
    already = 0
    imported = 0
    superseded = 0
    with _open_catalog(args, config) as conn:
        for receipt_file in args.receipt_files:
            # A receipt that cannot be imported is refused with a message
            # and the import goes on (#522); the exit code says so (#669).
            # ValueError covers JSONDecodeError and the UnicodeDecodeError of
            # a non-UTF-8 file (e.g. UTF-16 from a PowerShell `>` redirect).
            try:
                with open(receipt_file, encoding="utf-8") as f:
                    receipt = json.load(f)
            except (ValueError, OSError) as e:
                logger.warning(
                    f"Failed to read receipt '{receipt_file}': {e} "
                    "(a receipt must be a UTF-8 JSON file), skipping."
                )
                refused += 1
                continue
            if not isinstance(receipt, dict):
                logger.warning(
                    f"Receipt '{receipt_file}' is not a JSON object "
                    f"(got {type(receipt).__name__}), skipping."
                )
                refused += 1
                continue

            # Validate required receipt fields
            missing = [k for k in ("volume_label", "location") if k not in receipt]
            if missing:
                logger.warning(f"Receipt '{receipt_file}' missing keys: "
                               f"{', '.join(missing)}, skipping.")
                refused += 1
                continue
            bad_fields = [
                k for k in ("volume_label", "location")
                if not isinstance(receipt[k], str) or not receipt[k].strip()
            ]
            raw_size = receipt.get("iso_size_bytes")
            receipt_size: int | None = None
            if raw_size is not None:
                try:
                    if isinstance(raw_size, bool):
                        raise ValueError(raw_size)
                    receipt_size = int(raw_size)
                    if receipt_size < 0:
                        raise ValueError(raw_size)
                except (TypeError, ValueError):
                    bad_fields.append("iso_size_bytes")
            receipt_hash = receipt.get("iso_sha256") or ""
            if not isinstance(receipt_hash, str):
                bad_fields.append("iso_sha256")
            burn_date = receipt.get("burn_date") or ""
            if not isinstance(burn_date, str):
                bad_fields.append("burn_date")
            # #669: verify_passed must be JSON true, false or null.  It used
            # to go through bool(), so the string "false" counted as passed.
            raw_verify = receipt.get("verify_passed", absent)
            if raw_verify is not None and not isinstance(raw_verify, bool):
                bad_fields.append("verify_passed")
            if bad_fields:
                why = ""
                if "verify_passed" in bad_fields:
                    got = ("nothing" if raw_verify is absent
                           else json.dumps(raw_verify))
                    why = (" (verify_passed must be JSON true, false or "
                           f"null; got {got})")
                logger.warning(
                    f"Receipt '{receipt_file}' has invalid "
                    f"{', '.join(bad_fields)}{why}, skipping."
                )
                refused += 1
                continue
            verify_passed = raw_verify is True
            verify_failed = raw_verify is False
            label: str = receipt["volume_label"]
            location: str = receipt["location"]
            name = Path(receipt_file).name

            vol = get_volume_by_label(conn, label)
            if vol is None:
                logger.warning(f"Volume '{label}' not found, skipping.")
                refused += 1
                continue

            # #666: a receipt imported before changes nothing.  In particular
            # it never brings back a disc recorded lost or destroyed since.
            if _receipt_already_imported(
                conn, vol.volume_id, location, burn_date, receipt_hash,
            ):
                copy = get_copy_at_location(conn, vol.volume_id, location)
                if copy is not None and copy.status != "ACTIVE":
                    logger.warning(
                        f"Receipt '{receipt_file}': already imported.  The "
                        f"disc of '{label}' at {location} has since been "
                        f"recorded as {copy.status} (lost, destroyed or out "
                        "of service); it stays so."
                    )
                else:
                    logger.info(f"Receipt '{receipt_file}': already imported.")
                already += 1
                continue

            # #669: the location must be a known one, as for `lcsas burn`;
            # a mistyped name must not become a new location.
            try:
                resolve_location(conn, location)
            except UnknownLocationError as exc:
                known = [loc.name for loc in list_locations(conn)]
                hint = (f" Did you mean: {', '.join(exc.suggestions)}?"
                        if exc.suggestions else "")
                logger.error(
                    f"Receipt '{receipt_file}': unknown location "
                    f"'{location}'.{hint} Known locations: "
                    f"{', '.join(known) if known else '(none)'}.  If the "
                    "disc really is kept there, register it with 'lcsas "
                    f"location add {location}' and import the receipt "
                    "again; skipping."
                )
                refused += 1
                continue

            # #670: STAGING, BURNING and BURNED all have no verified disc yet.
            first_disc = vol.status in ("STAGING", "BURNING", "BURNED")

            # #669: the first disc belongs where the volume was staged for.
            # A VERIFIED volume taking a disc elsewhere is the multi-copy
            # path and is not checked.  Older volumes may have no location.
            if (
                first_disc and vol.location and vol.location != location
                and not allow_other_location
            ):
                logger.error(
                    f"Receipt '{receipt_file}': '{label}' was staged for "
                    f"location {vol.location}, but the receipt says "
                    f"{location}; skipping.  If the disc really is kept at "
                    f"{location}, import it again with --allow-other-location."
                )
                refused += 1
                continue

            # #665: a volume whose staging never finished (no ISO hash was
            # recorded, so mastering or ECC was cut short) may have been
            # burned from an ISO without RS03 data.  Never record such a
            # disc as good on the receipt's word alone.
            if (
                first_disc and staging_incomplete(conn, vol.volume_id)
                and not allow_incomplete
            ):
                logger.error(
                    f"Receipt '{receipt_file}': the staging of '{label}' did "
                    "not finish (no ISO hash was recorded), so its ISO may "
                    "lack RS03 error correction; skipping.  Abort that burn "
                    "session ('lcsas session abort <session>') and stage "
                    "again.  If you have checked the disc and want it "
                    "recorded anyway, import again with "
                    "--allow-incomplete-staging."
                )
                refused += 1
                continue

            # #644: the receipt must name the ISO that was staged.  burn-iso
            # hashes whatever file it is given on the burning machine, so an
            # ISO damaged or swapped on its way there would otherwise burn,
            # pass its own verify, and import as VERIFIED.
            staged = get_staged_iso(conn, vol.volume_id)
            hash_mismatch = False
            unchecked = ""
            if staged is None:
                unchecked = f"no ISO hash was recorded when '{label}' was staged"
            elif not receipt_hash:
                unchecked = "the receipt carries no iso_sha256"
            else:
                staged_hash, staged_size = staged
                if receipt_hash != staged_hash:
                    logger.error(
                        f"Receipt '{receipt_file}' iso_sha256 {receipt_hash} "
                        f"does not match the ISO staged for volume "
                        f"'{label}' ({staged_hash}): the "
                        "disc was burned from a different or damaged copy "
                        "of the ISO; rejecting.  Copy the ISO again, check "
                        "it with sha256sum, and re-burn."
                    )
                    hash_mismatch = True
                elif (
                    receipt_size is not None and staged_size is not None
                    and receipt_size != staged_size
                ):
                    logger.error(
                        f"Receipt '{receipt_file}' iso_size_bytes "
                        f"{receipt_size} does not match the ISO staged for "
                        f"volume '{label}' ({staged_size} bytes); rejecting."
                    )
                    hash_mismatch = True
            # Issue #18: receipt-vs-prior-receipt hash check. If a previous
            # BURN_RECEIPT_IMPORTED event for this volume recorded an
            # iso_sha256, the new receipt's iso_sha256 must match. Mismatch
            # means the receipt is for a different physical ISO (swapped /
            # tampered / re-mastered) — reject without writing anything.
            if receipt_hash and not hash_mismatch:
                for prior in get_events_for_volume(
                    conn, vol.volume_id, "BURN_RECEIPT_IMPORTED"
                ):
                    try:
                        prior_detail = (
                            json.loads(prior.detail) if prior.detail else {}
                        )
                    except json.JSONDecodeError:
                        continue
                    prior_hash = prior_detail.get("iso_sha256") or ""
                    if prior_hash and prior_hash != receipt_hash:
                        logger.error(
                            f"Receipt '{receipt_file}' iso_sha256 "
                            f"{receipt_hash} does not match previously "
                            f"recorded hash {prior_hash} for volume "
                            f"'{label}'; rejecting."
                        )
                        hash_mismatch = True
                        break
            if hash_mismatch:
                refused += 1
                continue
            if unchecked:
                # #669: a volume becomes VERIFIED from a receipt only when
                # the receipt proves its disc holds the staged ISO.
                if first_disc and verify_passed and not allow_unhashed:
                    logger.error(
                        f"Receipt '{receipt_file}': {unchecked}, so the disc "
                        f"cannot be matched to the ISO staged for '{label}'; "
                        "skipping.  Check the disc against the staged ISO "
                        "(sha256sum), then import the receipt again with "
                        "--allow-unhashed."
                    )
                    refused += 1
                    continue
                logger.warning(
                    f"Receipt '{receipt_file}': {unchecked}; the check "
                    f"against the ISO staged for '{label}' was SKIPPED."
                )

            # #666: never turn a disc recorded lost, destroyed or out of
            # service back into an in-service one, unless this receipt is
            # for a disc burned after that was recorded (a real re-burn).
            old_copy = get_copy_at_location(conn, vol.volume_id, location)
            if (
                old_copy is not None and old_copy.status != "ACTIVE"
                and not verify_failed
            ):
                retired = (get_disc_retired_at(conn, vol.volume_id, location)
                           or old_copy.burn_date)
                retired_ts = _receipt_timestamp(retired)
                burn_ts = _receipt_timestamp(burn_date)
                if retired_ts is None or burn_ts is None or burn_ts <= retired_ts:
                    logger.warning(
                        f"Receipt '{receipt_file}': the disc of '{label}' at "
                        f"{location} was recorded as {old_copy.status} "
                        f"({retired or 'date unknown'}), and this receipt's "
                        f"burn date ({burn_date or 'none'}) is not later.  "
                        f"It stays {old_copy.status}; skipping."
                    )
                    refused += 1
                    continue

            # Issue #18: persist receipt provenance (iso_sha256, session_id,
            # device, pack_ids) on the master catalog as a
            # BURN_RECEIPT_IMPORTED audit event, so later "which device
            # burned this?" and "does the disc hash match the recorded one?"
            # queries can be answered from the catalog alone.  burn_date
            # lets a later import see that the receipt was imported (#666).
            provenance = {
                "iso_sha256": receipt_hash,
                "burn_date": burn_date,
                "session_id": receipt.get("session_id") or "",
                "device": receipt.get("device") or "",
                # pack_ids is emitted by the session-based burn path but NOT
                # by standalone `burn-iso` (documented limitation — see
                # docs/workflows/burn-iso-portable.md). Persist verbatim so
                # whichever path produced the receipt is preserved.
                "pack_ids": list(receipt.get("pack_ids") or []),
                "receipt_file": name,
                "verify_passed": raw_verify,
            }

            # #754: the disc in service at this location was burned after
            # this receipt's disc (a re-burn there), so the receipt is for
            # a disc it replaced.  The disc record keeps its newer burn
            # date and verify time; a passing receipt only fills in an ISO
            # hash or size the record lacks (a disc recorded by 'verify
            # --disc --location' after a rebuild has none).  A failing one
            # records no VERIFY_FAIL_REBURN: the newer disc is what is
            # there.  The import event makes a re-run "already imported".
            if old_copy is not None and old_copy.status == "ACTIVE":
                recorded_ts = _receipt_timestamp(old_copy.burn_date)
                burn_ts = _receipt_timestamp(burn_date)
                if (recorded_ts is not None and burn_ts is not None
                        and burn_ts < recorded_ts):
                    filled = ""
                    if not verify_failed and (
                        (receipt_hash and not old_copy.iso_sha256)
                        or (receipt_size is not None
                            and old_copy.iso_size_bytes is None)
                    ):
                        conn.execute(
                            "UPDATE volume_copies SET "
                            "iso_sha256 = CASE WHEN COALESCE(iso_sha256, '') "
                            "= '' THEN ? ELSE iso_sha256 END, "
                            "iso_size_bytes = COALESCE(iso_size_bytes, ?) "
                            "WHERE volume_id = ? AND location = ?",
                            (receipt_hash or None, receipt_size,
                             vol.volume_id, location),
                        )
                        filled = (" Its ISO hash and size were added to the "
                                  "disc record, which lacked them.")
                    logger.warning(
                        f"Receipt '{receipt_file}': the disc of '{label}' "
                        f"at {location} on record was burned "
                        f"{old_copy.burn_date}, after this receipt's disc "
                        f"({burn_date}).  The newer disc record is kept."
                        f"{filled}"
                    )
                    add_event(
                        conn, vol.volume_id, "BURN_RECEIPT_IMPORTED",
                        location=location,
                        detail=json.dumps(provenance),
                        commit=False,
                    )
                    conn.commit()
                    superseded += 1
                    continue

            event_detail = f"Offline burn receipt: {name}"
            if verify_failed:
                if first_disc and not get_copies_for_volume(conn, vol.volume_id):
                    # #483: a disc that failed verification returns the
                    # volume to STAGING, as burn_session does, so its packs
                    # go back to the unarchived pool.  The event records the
                    # attempt.
                    if vol.status != "STAGING":
                        update_status(conn, vol.volume_id, "STAGING",
                                      commit=False)
                    add_event(conn, vol.volume_id, "VERIFY_FAIL",
                              location=location, detail=event_detail,
                              commit=False)
                else:
                    add_event(conn, vol.volume_id, "VERIFY_FAIL_REBURN",
                              location=location, detail=event_detail,
                              commit=False)
                logger.warning(
                    f"Receipt '{receipt_file}': the disc for '{label}' "
                    f"FAILED verification at {location}.  No disc was "
                    "recorded; that location still needs this volume.  "
                    + _reburn_hint(conn, vol.volume_id, location)
                )
            else:
                if verify_passed:
                    if first_disc:
                        if vol.status == "STAGING":
                            update_status(conn, vol.volume_id, "BURNING",
                                          commit=False)
                        update_status(conn, vol.volume_id, "VERIFIED",
                                      commit=False)
                        mark_closed(conn, vol.volume_id, commit=False)
                    # One VERIFY_PASS per passing disc, as the direct burn
                    # writes: `lcsas status` reads the newest verify result
                    # at each location (#671).
                    add_event(conn, vol.volume_id, "VERIFY_PASS",
                              location=location, detail=event_detail,
                              commit=False)
                else:
                    if vol.status == "STAGING":
                        update_status(conn, vol.volume_id, "BURNING",
                                      commit=False)
                    if vol.status in ("STAGING", "BURNING"):
                        update_status(conn, vol.volume_id, "BURNED",
                                      commit=False)
                    logger.warning(
                        f"Receipt '{receipt_file}': '{label}' was burned at "
                        f"{location} without verification (burn-iso "
                        "--no-verify).  Recorded, but not as VERIFIED: check "
                        f"it with `lcsas verify {label} --disc`."
                    )
                add_volume_copy(
                    conn,
                    volume_id=vol.volume_id,
                    location=location,
                    burn_date=burn_date,
                    iso_sha256=receipt_hash or None,
                    iso_size_bytes=receipt_size,
                    # #646: burn-iso read the disc back right after the
                    # burn and compared its SHA-256 with the ISO (#664),
                    # as the direct burn path does, so the disc was
                    # verified then.  --no-verify (null) leaves it NULL.
                    last_verified_at=(
                        _receipt_verified_at(burn_date)
                        if verify_passed else None
                    ),
                    commit=False,
                )

            add_event(
                conn, vol.volume_id, "BURN_RECEIPT_IMPORTED",
                location=location,
                detail=json.dumps(provenance),
                commit=False,
            )
            conn.commit()
            imported += 1

    logger.info(f"Imported {imported} receipt(s).")
    if already:
        logger.info(f"Skipped {already} receipt(s) that were already imported.")
    if superseded:
        logger.info(
            f"Kept the newer disc record for {superseded} receipt(s) of a "
            "disc that was burned again at the same location since."
        )
    if refused:
        logger.error(
            f"Rejected {refused} receipt(s); see the messages above.  The "
            "other receipts were imported."
        )
        return 1
    return 0


def cmd_catalog_validate(args: argparse.Namespace) -> int:
    """Cross-check a mounted disc's data files against its disc catalog."""
    from lcsas.db.verify import validate_disc

    disc_path = args.disc
    if not disc_path.is_dir():
        logger.error("Disc path does not exist or is not a directory: %s", disc_path)
        return 1

    logger.info("Validating disc at: %s", disc_path)
    try:
        result = validate_disc(disc_path, content=getattr(args, "content", False))
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Volume label   : %s", result.volume_label or "(unknown)")
    logger.info("Catalog packs  : %d", result.catalog_pack_count)
    logger.info("Disc packs     : %d", result.disc_pack_count)

    if result.missing_from_disc:
        logger.error(
            "%d pack(s) in catalog but MISSING from disc:",
            len(result.missing_from_disc),
        )
        for sha in result.missing_from_disc:
            logger.error("  MISSING: %s", sha)

    if result.orphaned_on_disc:
        logger.warning(
            "%d pack file(s) on disc but NOT in catalog (orphaned):",
            len(result.orphaned_on_disc),
        )
        for sha in result.orphaned_on_disc:
            logger.warning("  ORPHAN : %s", sha)

    if result.corrupt_on_disc:
        logger.error(
            "%d pack file(s) on disc are CORRUPT "
            "(content does not match the pack ID in the filename):",
            len(result.corrupt_on_disc),
        )
        for sha in result.corrupt_on_disc:
            logger.error("  CORRUPT: %s", sha)

    if result.ok:
        logger.info("Catalog validation PASSED — disc and catalog are in sync.")
        return 0
    else:
        logger.error(
            "Catalog validation FAILED — %d missing, %d orphaned, %d corrupt.",
            len(result.missing_from_disc),
            len(result.orphaned_on_disc),
            len(result.corrupt_on_disc),
        )
        return 1


def cmd_catalog_reconcile(args: argparse.Namespace) -> int:
    """Report and optionally repair catalog/physical-state disagreements.

    Two checks (FMA-01):

    1. Ghost volumes — STAGING/BURNING with zero ``volume_copies`` rows,
       older than the cutoff.  These claim packs but correspond to no
       disc at all; restore pick lists offer them by label and an heir
       would hunt for a disc that never existed.  ``--fix`` deletes them
       (the migration path for catalogs predating the FMA-01 semantics).
    2. Durable volumes without an ACTIVE disc — status says a disc exists
       but no disc record backs it.  Report-only.
    """
    from lcsas.db.models import Volume
    from lcsas.db.queries import (
        get_durable_volumes_without_active_copies,
        get_ghost_volumes,
        get_lost_volume_ids,
        get_volume_pack_stats_by_repo,
        volume_display_status,
    )
    from lcsas.db.sessions import staged_iso_present
    from lcsas.db.volume_events import burn_evidence, rebuild_burn_unknown
    from lcsas.db.volumes import delete_volume

    config = _load_config_opt(args)
    db_path = _resolve_db_path(args, config)
    include_waiting = getattr(args, "include_waiting", False)

    with _open_catalog(args, db_path=db_path) as conn:
        candidates = get_ghost_volumes(conn, args.older_than_hours)
        # #746: a volume with no disc record whose events show a good disc
        # of it was burned (the #737 check session abort and stage --clean
        # use) is a burned volume, not a ghost.  Never deleted, even with
        # --include-waiting; its disc just needs recording.
        burned_unrecorded: list[tuple[Volume, str]] = []
        for v in candidates:
            evidence = burn_evidence(conn, v.volume_id)
            if evidence is not None:
                burned_unrecorded.append((v, evidence))
        if burned_unrecorded:
            kept_ids = {v.volume_id for v, _why in burned_unrecorded}
            candidates = [v for v in candidates
                          if v.volume_id not in kept_ids]
            logger.warning(
                "%d volume(s) have no disc recorded, but a disc of each was "
                "burned; they are not ghosts and --fix never deletes them:",
                len(burned_unrecorded),
            )
            for v, why in burned_unrecorded:
                logger.warning("  %s  (status=%s, but %s)", v.label, v.status,
                               why)
            logger.warning(
                "  For each disc you hold, put it in the drive and run "
                "'lcsas verify <label> --disc --location <where it is "
                "kept>', or import its burn receipt with 'lcsas catalog "
                "import-receipts'."
            )

        # #758: a volume a catalog rebuild brought in with no record of
        # whether it was burned may have a disc.  Never deleted here, even
        # with --include-waiting; 'session abort --never-burned' is the
        # owner's way to say it never was.
        unknown = [v for v in candidates
                   if rebuild_burn_unknown(conn, v.volume_id)]
        if unknown:
            unknown_ids = {v.volume_id for v in unknown}
            candidates = [v for v in candidates
                          if v.volume_id not in unknown_ids]
            logger.warning(
                "%d volume(s) come from a catalog rebuilt from discs, and no "
                "disc it read shows whether they were burned; they are not "
                "ghosts and --fix never deletes them:", len(unknown),
            )
            for v in unknown:
                logger.warning("  %s  (status=%s)", v.label, v.status)
            logger.warning(
                "  For each disc you hold, put it in the drive and run "
                "'lcsas verify <label> --disc --location <where it is "
                "kept>', or import its burn receipt with 'lcsas catalog "
                "import-receipts'. If you are sure a volume was never "
                "burned, 'lcsas session abort --volume <label> "
                "--never-burned' deletes it."
            )

        # #647: a never-burned volume whose staged ISO still exists is
        # waiting to be burned (possibly on another machine, with its
        # receipt still to come), not a ghost.  --fix leaves it alone
        # unless --include-waiting.
        waiting = [v for v in candidates if staged_iso_present(conn, v.volume_id)]
        if waiting:
            logger.info(
                "%d volume(s) older than %dh are waiting to be burned "
                "(their staged ISOs still exist); not treated as ghosts%s:",
                len(waiting), args.older_than_hours,
                "" if include_waiting else " (--include-waiting to include them)",
            )
            for v in waiting:
                logger.info("  %s  (status=%s, created %s)",
                            v.label, v.status, v.created_at)
        ghosts = candidates if include_waiting else [
            v for v in candidates if v not in waiting
        ]
        drifted = get_durable_volumes_without_active_copies(conn)

        if drifted:
            logger.warning(
                "%d volume(s) have a durable status but no ACTIVE disc "
                "recorded (status/disc drift — investigate, not auto-fixed):",
                len(drifted),
            )
            lost_ids = get_lost_volume_ids(conn)
            for v in drifted:
                # Q33: one with disc records, none in service, is lost.
                shown = volume_display_status(v, lost_ids)
                recorded = f" (recorded as {v.status})" if shown != v.status else ""
                logger.warning("  %s  status=%s%s  location=%s",
                               v.label, shown, recorded, v.location)

        if not ghosts:
            logger.info(
                "Catalog reconcile: no ghost volumes found "
                "(STAGING/BURNING, zero copies, older than %dh).",
                args.older_than_hours,
            )
            return 0

        logger.warning(
            "%d ghost volume(s) — staged but with no record of ever being "
            "burned. Their packs are NOT on any disc:",
            len(ghosts),
        )
        ghost_pack_total = 0
        ghost_byte_total = 0
        for v in ghosts:
            stats = get_volume_pack_stats_by_repo(conn, v.volume_id)
            n_packs = sum(c for _r, c, _b in stats)
            n_bytes = sum(b for _r, _c, b in stats)
            ghost_pack_total += n_packs
            ghost_byte_total += n_bytes
            per_repo = "; ".join(
                f"{repo}: {c} pack(s), {b:,} bytes" for repo, c, b in stats
            ) or "no packs"
            logger.warning(
                "  %s  (status=%s, created %s) — %s",
                v.label, v.status, v.created_at, per_repo,
            )

        if not args.fix:
            logger.warning(
                "Run 'lcsas catalog reconcile --fix' to delete these "
                "volumes and return %d pack(s) (%s bytes) to the "
                "unarchived pool.",
                ghost_pack_total, f"{ghost_byte_total:,}",
            )
            return 1

        if not args.yes:
            try:
                confirmed = _confirm(
                    f"Delete {len(ghosts)} ghost volume(s) and return "
                    f"their packs to the unarchived pool? A burn receipt "
                    f"for a deleted volume can no longer be imported, and "
                    f"any of their ISOs left in the staging area are "
                    f"orphaned. Type 'yes' to confirm: "
                )
            except EOFError:
                logger.error(
                    "No terminal available for confirmation — re-run with "
                    "--yes to confirm non-interactively."
                )
                return 1
            if not confirmed:
                logger.info("Reconcile canceled — no changes made.")
                return 1

        ghost_ids = [v.volume_id for v in ghosts]
        placeholders = ",".join("?" for _ in ghost_ids)
        reclaimed = conn.execute(
            f"SELECT COUNT(DISTINCT pack_id) FROM volume_packs "
            f"WHERE volume_id IN ({placeholders})",
            ghost_ids,
        ).fetchone()[0]
        for v in ghosts:
            logger.info("Deleting ghost volume %s", v.label)
            delete_volume(conn, v.volume_id)
        logger.info(
            "Deleted %d ghost volume(s); %d pack(s) returned to the "
            "unarchived pool (re-run 'lcsas stage' to re-select them).",
            len(ghosts), int(reclaimed),
        )
    return 0


def cmd_catalog_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the master catalog by merging disc catalogs."""
    from lcsas.db.rebuild import rebuild_catalog

    disc_dirs = [Path(d) for d in args.disc_dirs]
    output_db = args.output

    # Basic sanity checks
    bad = [str(d) for d in disc_dirs if not d.is_dir()]
    if bad:
        for b in bad:
            logger.error("Not a directory (disc not mounted?): %s", b)
        return 1

    logger.info(
        "Rebuilding catalog from %d disc(s) → %s", len(disc_dirs), output_db
    )
    result = rebuild_catalog(disc_dirs, output_db)

    logger.info("Discs processed  : %d", result.discs_processed)
    if result.discs_skipped:
        logger.warning("Discs skipped    : %d (see errors above)", result.discs_skipped)
    logger.info("Repositories     : %d new", result.repositories_merged)
    logger.info("Volumes          : %d new", result.volumes_merged)
    logger.info("Packs            : %d new", result.packs_merged)
    logger.info("Snapshots        : %d new", result.snapshots_merged)
    logger.info("Password splits  : %d recorded", result.splits_merged)

    # FMA-06: mixed-age disc boxes carry conflicting catalog views; the
    # merge keeps the freshest one and explains what it overrode here.
    if result.warnings:
        logger.warning("Rebuild produced %d warning(s):", len(result.warnings))
        for warning in result.warnings:
            logger.warning("  %s", warning)

    # #678: a disc cannot record its own burn, so the newest burn
    # session's volumes come back with no disc record.  Their discs were
    # read, so the rebuild marked them BURNED; where each disc is kept
    # and its verify must be entered again.
    if result.needs_verify:
        logger.warning(
            "%d volume(s) were marked BURNED because their own disc was "
            "read, but no disc records where they are kept or that they "
            "were verified: %s",
            len(result.needs_verify), ", ".join(result.needs_verify),
        )
        logger.warning(
            "For each disc of them you hold, put it in the drive and run "
            "(against the rebuilt catalog, --db %s):", output_db,
        )
        for label in result.needs_verify:
            logger.warning(
                "  lcsas verify %s --disc --location <where it is kept>",
                label,
            )
        logger.warning(
            "That records the disc at its location and marks the volume "
            "VERIFIED. Until then, 'lcsas session abort' and 'lcsas stage "
            "--clean' refuse their burn session, so it keeps its ISOs."
        )

    # #758: volumes staged before a disc that was read, whose own disc
    # was not read: nothing shows whether they were burned.
    if result.burn_unknown:
        logger.warning(
            "%d volume(s) are STAGING with no disc recorded, and no disc "
            "that was read shows whether they were burned: %s",
            len(result.burn_unknown), ", ".join(result.burn_unknown),
        )
        logger.warning(
            "For each disc of them you hold, run 'lcsas verify <label> "
            "--disc --location <where it is kept>' (or import its burn "
            "receipt). 'lcsas session abort', 'lcsas stage --clean' and "
            "'lcsas catalog reconcile --fix' never delete them; if you are "
            "sure one was never burned, 'lcsas session abort --volume "
            "<label> --never-burned' deletes it."
        )

    if result.errors:
        logger.error("Rebuild completed with %d error(s):", len(result.errors))
        for err in result.errors:
            logger.error("  %s", err)
        return 1

    logger.info("Catalog rebuild complete: %s", output_db)
    return 0
