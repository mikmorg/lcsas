"""``lcsas verify``."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from lcsas.cli.common import (
    _load_config_opt,
    _open_catalog,
    _resolve_db_path,
    _resolve_device,
    _unknown_location_msg,
)
from lcsas.log import get_logger

logger = get_logger()


def _verify_disc_against_recorded_hash(
    conn: sqlite3.Connection,
    volume_id: int,
    label: str,
    device: str,
) -> bool:
    """Device read-back SHA-256 compare for ``verify --disc`` (BURN-04).

    Returns True when the disc's bytes match the ISO hash recorded at
    stage time, or when no hash was ever recorded (pre-Phase-13 volume:
    readability is all the evidence available).  Returns False on
    mismatch, on read errors, and when a hash is recorded but the ISO
    byte length cannot be determined (-check_media alone is not enough
    evidence for a volume that carries a hash).

    Hash + length come from ``session_volumes`` (written at stage time),
    falling back to ``volume_copies`` — the disc rows survive receipt
    import and catalog rebuild from disc, where session_volumes does
    not (FMA-03).
    """
    from lcsas.burn.device_verify import read_device_sha256

    expected: str | None = None
    iso_size: int | None = None

    row = conn.execute(
        "SELECT iso_path, iso_sha256, iso_size_bytes FROM session_volumes "
        "WHERE volume_id = ? AND iso_sha256 IS NOT NULL AND iso_sha256 != '' "
        "ORDER BY rowid DESC LIMIT 1",
        (volume_id,),
    ).fetchone()
    if row is not None:
        expected = str(row["iso_sha256"])
        iso_size = row["iso_size_bytes"]
        if iso_size is None and row["iso_path"]:
            iso_file = Path(row["iso_path"])
            if iso_file.exists():
                iso_size = iso_file.stat().st_size
    else:
        # Rebuilt / receipt-imported catalog: session_volumes is empty
        # but the volume_copies rows carry the evidence (schema v8, FMA-03).
        copy_row = conn.execute(
            "SELECT iso_sha256, iso_size_bytes FROM volume_copies "
            "WHERE volume_id = ? AND iso_sha256 IS NOT NULL AND iso_sha256 != '' "
            "ORDER BY (iso_size_bytes IS NULL), id DESC LIMIT 1",
            (volume_id,),
        ).fetchone()
        if copy_row is not None:
            expected = str(copy_row["iso_sha256"])
            iso_size = copy_row["iso_size_bytes"]

    if expected is None:
        logger.warning(
            "  No recorded ISO SHA-256 for %s — device hash check skipped "
            "(pre-Phase-13 volume); check_media proved readability only.",
            label,
        )
        return True

    if iso_size is None:
        logger.warning(
            "  cannot device-verify %s: no recorded ISO size (pre-upgrade "
            "session) and the ISO file is gone — NOT passing on "
            "check_media alone.",
            label,
        )
        return False

    try:
        device_hash = read_device_sha256(device, int(iso_size))
    except OSError as exc:
        logger.error(f"  Device hash verify: FAIL (read error: {exc})")
        return False

    ok = device_hash == expected
    logger.info(f"  Device hash verify: {'PASS' if ok else 'FAIL'}")
    if not ok:
        logger.error(
            f"  device hash mismatch: expected {expected[:8]}.., "
            f"got {device_hash[:8]}.."
        )
    return ok


def _check_disc_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
    label: str,
    device: str,
) -> str:
    """FMA-03 disc check chain: identity gate → readability → device hash.

    Returns ``"PASS"``, ``"FAIL"``, or ``"WRONG_DISC"``.  WRONG_DISC
    means the disc in the drive is not (provably) this volume — that is
    the owner's mistake, not evidence about the volume's burned disc, so
    callers must record NOTHING for it.
    """
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    runner = SubprocessXorrisoRunner()
    disc_id = runner.read_disc_volume_id(device=device)
    if disc_id != label:
        if disc_id:
            logger.error(
                f"Disc in {device} identifies as '{disc_id}' — "
                f"expected '{label}'. Wrong disc? "
                f"Nothing was recorded."
            )
        else:
            logger.error(
                f"Could not read a Volume ID from the disc in "
                f"{device} — cannot confirm it is "
                f"'{label}'. Nothing was recorded."
            )
        return "WRONG_DISC"
    ok = runner.verify_disc(device=device)
    logger.info(f"  Disc verify (check_media): {'PASS' if ok else 'FAIL'}")
    if not ok:
        return "FAIL"
    # BURN-04: -check_media proves only readability — any readable disc
    # in the tray passes it.  Read back the recorded ISO byte length
    # from the device and compare against the SHA-256 recorded at
    # stage time.
    if _verify_disc_against_recorded_hash(conn, volume_id, label, device):
        return "PASS"
    return "FAIL"


def _mark_verified(
    conn: sqlite3.Connection,
    label: str,
    location: str | None,
    detail: str,
) -> int:
    """``lcsas verify <label> --mark-verified``: the owner states that a
    disc of the volume was checked elsewhere.

    A volume with no ACTIVE disc on record also needs ``--location``:
    VERIFIED with no disc anywhere would leave its packs looking archived
    on nothing (#673).  The disc is then recorded there, with the staged
    ISO's hash, and the volume is closed, as an imported receipt does.
    ``last_verified_at`` stays NULL: nothing here read the disc.
    """
    from lcsas.db.locations import ensure_location
    from lcsas.db.queries import get_lost_volume_ids, volume_display_status
    from lcsas.db.sessions import get_staged_iso
    from lcsas.db.volume_copies import add_volume_copy, get_copies_for_volume
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import get_volume_by_label, mark_closed, update_status

    vol = get_volume_by_label(conn, label)
    if vol is None:
        logger.error(f"Volume '{label}' not found.")
        return 1
    shown = volume_display_status(vol, get_lost_volume_ids(conn))
    if shown not in ("BURNED", "STAGING"):
        logger.error(
            f"Cannot mark volume {vol.label} VERIFIED: status is {shown} "
            f"(only BURNED or STAGING allowed)"
        )
        return 1

    active = get_copies_for_volume(conn, vol.volume_id)
    if not active and not location:
        logger.error(
            f"Volume {vol.label} has no disc recorded.  Give --location "
            f"to say where its disc is kept, and that disc is recorded.  "
            f"To check the disc itself, use 'lcsas burn-iso --verify-only' "
            f"and import its receipt (docs/workflows/burn-iso-portable.md, "
            f"\"Missing receipt\")."
        )
        return 1
    if active and location and location not in {c.location for c in active}:
        held = ", ".join(sorted(c.location for c in active))
        logger.error(
            f"Volume {vol.label} has discs at {held}, not at {location}.  "
            f"--mark-verified records a disc only for a volume with none; "
            f"record another disc by importing its burn receipt."
        )
        return 1

    if vol.status == "STAGING":
        update_status(conn, vol.volume_id, "BURNING", commit=False)
    update_status(conn, vol.volume_id, "VERIFIED", commit=False)
    logger.info(f"Volume {vol.label}: status {vol.status} → VERIFIED")
    if not active and location:
        ensure_location(conn, location, commit=False)
        staged = get_staged_iso(conn, vol.volume_id)
        add_volume_copy(
            conn, vol.volume_id, location,
            notes="Recorded by verify --mark-verified",
            iso_sha256=staged[0] if staged else None,
            iso_size_bytes=staged[1] if staged else None,
            commit=False,
        )
        logger.info(f"Volume {vol.label}: disc recorded at {location}")
    if vol.closed_at is None:
        mark_closed(conn, vol.volume_id, commit=False)
    add_event(
        conn, vol.volume_id, "VERIFY_PASS",
        location=location,
        detail=detail or "Manual verification (remote)",
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a volume's ISO image or burned disc.

    Supports three modes:
    - Physical/ISO verification: check media integrity (default)
    - Manual marking: --mark-verified / --mark-failed for remote workflows
    - All volumes: --all to verify every BURNED/VERIFIED volume
    """
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import get_volume_by_label, update_status

    config = _load_config_opt(args)
    db_path = _resolve_db_path(args, config)
    device = _resolve_device(args, config)
    with _open_catalog(args, db_path=db_path) as conn:
        # --- All volumes: verify --all ---
        if args.verify_all:
            if args.disc:
                return _verify_all_disc(conn, args, device)
            return _verify_all(conn, args, config)

        # --- Single volume mode ---
        if not args.volume_label:
            logger.error("Volume label required (or use --all to verify every volume).")
            return 1

        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1

        # --- Manual marking (remote verification workflow) ---
        if args.mark_verified:
            return _mark_verified(conn, vol.label, args.location, args.detail)

        if args.mark_failed:
            add_event(
                conn, vol.volume_id, "VERIFY_FAIL",
                detail=args.detail or "Manual failure report",
            )
            logger.info(f"Volume {vol.label}: VERIFY_FAIL event recorded")
            return 0

        # --- Physical verification ---
        # Find ISO path from session_volumes if not explicitly provided
        iso_path = args.iso
        if iso_path is None and not args.disc:
            row = conn.execute(
                "SELECT iso_path FROM session_volumes WHERE volume_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (vol.volume_id,),
            ).fetchone()
            if row and row["iso_path"]:
                iso_path = Path(row["iso_path"])
            else:
                logger.error("No ISO path found for this volume. "
                             "Use --iso to specify one, or --disc to verify a burned disc.")
                return 1

        passed = True
        recorded_disc = False

        if args.disc:
            from datetime import UTC, datetime

            from lcsas.db.volume_copies import (
                add_volume_copy,
                get_copies_for_volume,
                touch_last_verified,
            )

            # FMA-05: resolve which disc this IS before touching
            # the device — with several ACTIVE discs the owner must
            # say which one is in the drive, or the stamp would assert
            # freshness for a disc that was never read.
            stamp_location = args.location
            # #678: --location names a place where no disc of this volume
            # is recorded (a catalog rebuilt from discs has no record for
            # the newest burn session).  A passing check records the disc
            # there.  The location must already exist, so a typo cannot
            # create a phantom one (#19).
            record_location = False
            if stamp_location is not None and conn.execute(
                "SELECT 1 FROM volume_copies WHERE volume_id = ? "
                "AND location = ?",
                (vol.volume_id, stamp_location),
            ).fetchone() is None:
                from lcsas.db.locations import (
                    UnknownLocationError,
                    resolve_location,
                )
                try:
                    resolve_location(conn, stamp_location)
                except UnknownLocationError as exc:
                    logger.error(_unknown_location_msg(exc))
                    return 1
                record_location = True
            if stamp_location is None:
                active = get_copies_for_volume(conn, vol.volume_id)
                if len(active) == 1:
                    stamp_location = active[0].location
                elif len(active) > 1:
                    locs = ", ".join(sorted(c.location for c in active))
                    logger.error(
                        f"Volume {vol.label} has ACTIVE discs at {locs} — "
                        f"pass --location to record which disc is being "
                        f"verified."
                    )
                    return 1
            logger.info(f"Verifying disc on {device} ...")
            # FMA-03: identity gate first.  If the disc in the drive is
            # not this volume (or carries no readable Volume ID), record
            # NOTHING — a wrong disc is owner error, not evidence
            # about this volume's burned discs.
            outcome = _check_disc_for_volume(
                conn, vol.volume_id, vol.label, device,
            )
            if outcome == "WRONG_DISC":
                return 1
            passed = outcome == "PASS"
            if passed:
                if stamp_location is None:
                    logger.warning(
                        f"  No ACTIVE disc of {vol.label} recorded — "
                        f"last_verified_at not stamped"
                    )
                elif record_location:
                    # Uncommitted: the add_event() below commits the
                    # disc record atomically with the VERIFY_PASS event.
                    add_volume_copy(
                        conn, vol.volume_id, stamp_location,
                        notes=(
                            "recorded by 'lcsas verify --disc'; the burn "
                            "date is the date it was recorded"
                        ),
                        last_verified_at=datetime.now(UTC).isoformat(),
                        commit=False,
                    )
                    recorded_disc = True
                    logger.info(
                        f"  Recorded the disc of {vol.label} at {stamp_location}"
                    )
                # Stamp uncommitted: the add_event() below commits it
                # atomically with the VERIFY_PASS event.
                elif touch_last_verified(
                    conn, vol.volume_id, stamp_location, commit=False,
                ):
                    logger.info(
                        f"  Stamped last_verified_at on the disc at {stamp_location}"
                    )
                else:
                    logger.warning(
                        f"  No ACTIVE disc of {vol.label} at {stamp_location} — "
                        f"last_verified_at not updated"
                    )
        else:
            if not iso_path.exists():
                logger.error(f"ISO file not found: {iso_path}")
                return 1

            # Phase 21.3 / FMT-01: try an RS03 ECC verify first (can detect
            # AND repair).  Prefer the real dvdisaster when installed; else
            # fall back to the bundled in-house lcsas-ecc (decode-only) so a
            # host without dvdisaster still spends the burned parity.  If
            # NEITHER ECC tool is present, fall back to a portable SHA-256
            # compare against the hash recorded at burn time (detect-only).
            # See docs/history/CROSS_PLATFORM_META_RFC.md §6 Q2 for the rationale.
            from lcsas.db.volume_copies import get_iso_sha256_for_label
            from lcsas.ecc.dvdisaster import select_ecc_runner
            from lcsas.restore.executor import verify_iso_sha256

            logger.info(f"Verifying ISO: {iso_path}")
            ecc_runner = select_ecc_runner()
            try:
                if ecc_runner is None:
                    raise FileNotFoundError(
                        "no dvdisaster or lcsas-ecc on PATH"
                    )
                ok = ecc_runner.verify_iso(iso_path)
                logger.info(f"  ECC verify: {'PASS' if ok else 'FAIL'}")
            except (FileNotFoundError, RuntimeError) as e:
                # No usable ECC tool — fall back to SHA-256.
                logger.info(
                    "  ECC tool unavailable (%s); falling back to "
                    "portable SHA-256 verify", e.__class__.__name__,
                )
                expected = get_iso_sha256_for_label(conn, args.volume_label)
                if not expected:
                    logger.error(
                        "  No recorded ISO SHA-256 for %s — cannot verify "
                        "without dvdisaster.  (Catalog rows pre-dating "
                        "Phase 13 don't carry the hash.)",
                        args.volume_label,
                    )
                    return 1
                ok = verify_iso_sha256(iso_path, expected)
                logger.info(f"  SHA-256 verify: {'PASS' if ok else 'FAIL'}")
            if not ok:
                passed = False

        # Record event
        event_type = "VERIFY_PASS" if passed else "VERIFY_FAIL"
        detail = args.detail or ("Disc verify" if args.disc else f"ISO verify: {iso_path}")
        add_event(conn, vol.volume_id, event_type, detail=detail)

        # If verification passed on a BURNED volume, promote to VERIFIED
        if passed and vol.status == "BURNED":
            update_status(conn, vol.volume_id, "VERIFIED")
            logger.info(f"Volume {vol.label}: promoted BURNED → VERIFIED")
        elif passed and recorded_disc and vol.status in ("STAGING", "BURNING"):
            # #678: the disc was identified, read, and is now recorded —
            # the same evidence a burn's own check gives.
            update_status(conn, vol.volume_id, "VERIFIED", force=True)
            logger.info(
                f"Volume {vol.label}: promoted {vol.status} → VERIFIED"
            )

        return 0 if passed else 1


def _verify_all(conn: sqlite3.Connection, args: argparse.Namespace, config: Any) -> int:
    """Verify all BURNED/VERIFIED volumes in one run, optionally at a location."""
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import list_volumes, update_status

    vols_burned = list_volumes(conn, status_filter="BURNED")
    vols_verified = list_volumes(conn, status_filter="VERIFIED")
    candidates = vols_burned + vols_verified

    if args.location:
        # Filter to volumes with a disc at the given location
        filtered = []
        for vol in candidates:
            copies = get_copies_for_volume(conn, vol.volume_id)
            if any(c.location == args.location for c in copies):
                filtered.append(vol)
        candidates = filtered

    if not candidates:
        logger.info("No volumes to verify.")
        return 0

    logger.info(f"Verifying {len(candidates)} volume(s)...")
    passed_count = 0
    failed_count = 0

    # Phase 21.5.a / FMT-01: probe the ECC tool ONCE up front, not
    # per-volume.  Prefer real dvdisaster, else the in-house lcsas-ecc
    # (decode-only) so a host without dvdisaster still spends the burned
    # parity; if neither is present, fall back to SHA-256 verification
    # against the catalog-recorded hash (Phase 21.3 pattern).
    from lcsas.db.volume_copies import get_iso_sha256_for_label
    from lcsas.ecc.dvdisaster import select_ecc_runner
    from lcsas.restore.executor import verify_iso_sha256

    ecc_runner = select_ecc_runner()
    ecc_available = ecc_runner is not None
    if not ecc_available:
        logger.info(
            "  no ECC tool (dvdisaster / lcsas-ecc) available; falling "
            "back to portable SHA-256 verify for all volumes "
            "(Phase 21.3 / 21.5 / FMT-01)."
        )

    for vol in candidates:
        # Try to find the ISO path
        row = conn.execute(
            "SELECT iso_path FROM session_volumes WHERE volume_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (vol.volume_id,),
        ).fetchone()

        if not row or not row["iso_path"]:
            # FMA-05: burn_session() deletes the ISO after a verified
            # burn — for a burned volume this is the NORMAL state, so
            # point at the disc mode (--all --disc) instead of leaving
            # a bare skip.
            logger.info(
                f"  {vol.label}: ISO deleted after burn — use "
                f"'verify --all --disc' to verify the physical disc"
            )
            continue

        iso_path = Path(row["iso_path"])
        if not iso_path.exists():
            logger.info(
                f"  {vol.label}: ISO deleted after burn ({iso_path}) — use "
                f"'verify --all --disc' to verify the physical disc"
            )
            continue

        if ecc_available:
            assert ecc_runner is not None  # mypy hint
            ok = ecc_runner.verify_iso(iso_path)
            verify_kind = "Batch ISO verify"
        else:
            # SHA-256 fallback path.  No recorded hash → skip (don't
            # silently pass: integrity is unknown).
            expected = get_iso_sha256_for_label(conn, vol.label)
            if not expected:
                logger.info(
                    f"  {vol.label}: no recorded SHA-256 and no ECC tool "
                    f"available — skipped"
                )
                continue
            ok = verify_iso_sha256(iso_path, expected)
            verify_kind = "Batch SHA-256 verify"
        event_type = "VERIFY_PASS" if ok else "VERIFY_FAIL"
        add_event(conn, vol.volume_id, event_type, detail=f"{verify_kind}: {iso_path}")

        if ok:
            passed_count += 1
            if vol.status == "BURNED":
                update_status(conn, vol.volume_id, "VERIFIED")
            logger.info(f"  {vol.label}: PASS")
        else:
            failed_count += 1
            logger.info(f"  {vol.label}: FAIL")

    logger.info(f"Verification complete: {passed_count} passed, {failed_count} failed, "
                f"{len(candidates) - passed_count - failed_count} skipped")
    if failed_count > 0:
        return 1
    if passed_count == 0:
        logger.warning("No volumes were actually verified (all skipped).")
        return 1
    return 0


def _verify_all_disc(
    conn: sqlite3.Connection, args: argparse.Namespace, device: str,
) -> int:
    """Re-verify every in-service disc, one at a time (FMA-05).

    Iterates ACTIVE discs of BURNED/VERIFIED volumes (optionally
    filtered to --location), prompting the owner to insert each
    disc.  A PASS stamps ``last_verified_at`` on the disc's row (committed
    atomically with its VERIFY_PASS event); a FAIL records VERIFY_FAIL
    and leaves the stamp untouched; a wrong disc in the drive records
    nothing (FMA-03: owner error is not evidence).
    """
    from lcsas.db.volume_copies import touch_last_verified
    from lcsas.db.volume_events import add_event
    from lcsas.db.volumes import get_volume_by_id, update_status

    params: tuple[Any, ...] = ()
    where = "vc.status = 'ACTIVE' AND v.status IN ('BURNED', 'VERIFIED')"
    if args.location:
        where += " AND vc.location = ?"
        params = (args.location,)
    rows = conn.execute(
        f"SELECT vc.volume_id, vc.location, v.label "
        f"FROM volume_copies vc JOIN volumes v USING (volume_id) "
        f"WHERE {where} ORDER BY v.label, vc.location",
        params,
    ).fetchall()
    if not rows:
        logger.info("No ACTIVE discs to verify.")
        return 0

    logger.info(f"Re-verifying {len(rows)} disc(s) on {device}")
    results: list[tuple[str, str, str]] = []
    quit_early = False
    for row in rows:
        label, location = row["label"], row["location"]
        if quit_early:
            results.append((label, location, "SKIPPED"))
            continue
        try:
            answer = input(
                f"Insert disc {label} and press Enter (s = skip, q = quit): "
            ).strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            quit_early = True
            results.append((label, location, "SKIPPED"))
            continue
        if answer == "s":
            results.append((label, location, "SKIPPED"))
            continue
        outcome = _check_disc_for_volume(
            conn, row["volume_id"], label, device,
        )
        if outcome == "PASS":
            # Uncommitted stamp + event commit together (one transaction).
            touch_last_verified(conn, row["volume_id"], location, commit=False)
            add_event(conn, row["volume_id"], "VERIFY_PASS",
                      location=location, detail="Batch disc verify")
            logger.info(f"  Stamped last_verified_at on the disc at {location}")
            # Re-read status: an earlier disc of the same volume may
            # already have promoted it in this run.
            if get_volume_by_id(conn, row["volume_id"]).status == "BURNED":
                update_status(conn, row["volume_id"], "VERIFIED")
                logger.info(f"Volume {label}: promoted BURNED → VERIFIED")
            results.append((label, location, "PASS"))
        elif outcome == "FAIL":
            add_event(conn, row["volume_id"], "VERIFY_FAIL",
                      location=location, detail="Batch disc verify")
            results.append((label, location, "FAIL"))
        else:
            # WRONG_DISC: nothing recorded; the disc stays unverified.
            results.append((label, location, "SKIPPED"))

    logger.info("Batch disc verification results:")
    for label, location, outcome in results:
        logger.info(f"  {label:<25} {location:<15} {outcome}")
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_fail = sum(1 for r in results if r[2] == "FAIL")
    logger.info(
        f"Verification complete: {n_pass} passed, {n_fail} failed, "
        f"{len(results) - n_pass - n_fail} skipped"
    )
    if n_fail > 0:
        return 1
    if n_pass == 0:
        logger.warning("No discs were actually verified (all skipped).")
        return 1
    return 0
