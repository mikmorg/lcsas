"""``lcsas status``."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import TYPE_CHECKING, Any

from lcsas.cli.common import _open_catalog, _reburn_hint
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.db.models import Pack

logger = get_logger()


def _print_stale_copies(conn: sqlite3.Connection, older_than_days: int) -> int:
    """Report ACTIVE discs overdue for re-verification (FMA-05)."""
    from datetime import UTC, datetime

    from lcsas.db.volume_copies import find_stale_copies
    from lcsas.utils.timestamps import parse_utc

    stale = find_stale_copies(conn, older_than_days)
    if not stale:
        logger.info(
            f"All ACTIVE discs verified within the last "
            f"{older_than_days} days."
        )
        return 0
    now = datetime.now(UTC)
    logger.info(
        f"{len(stale)} disc(s) not verified in the last "
        f"{older_than_days} days:"
    )
    for label, location, last_verified_at in stale:
        when = parse_utc(last_verified_at)
        if last_verified_at is None:
            age = "never verified"
        elif when is None:
            age = f"unreadable date ({last_verified_at})"
        else:
            days = (now - when).days
            age = f"{days} days ago ({when.date().isoformat()})"
        logger.info(f"  {label:<25} {location:<15} {age}")
    logger.info("Re-verify with: lcsas verify --all --disc")
    return 0


def _print_redundancy_report(conn: sqlite3.Connection, min_copies: int) -> int:
    """Blast-radius view: under-replicated packs grouped by volume (FMA-08).

    Answers "which discs are single points of failure, and for how much
    data?" from the catalog alone.  Per-volume detail (repositories, snapshots)
    lives in ``lcsas volume impact <LABEL>``.
    """
    from lcsas.db.models import Volume
    from lcsas.db.queries import (
        get_live_volumes_for_packs,
        get_lost_volume_ids,
        get_redundancy_report,
        volume_display_status,
    )
    from lcsas.db.volume_copies import get_copies_for_volume

    under = get_redundancy_report(conn, min_copies=min_copies)
    if not under:
        logger.info(
            f"All packs have at least {min_copies} live cop(ies) — no "
            f"single disc is a point of failure at this threshold."
        )
        return 0

    live_map = get_live_volumes_for_packs(conn, [p.pack_id for p in under])
    lost_ids = get_lost_volume_ids(conn)

    by_volume: dict[int, list[Pack]] = {}
    vol_info: dict[int, Volume] = {}
    on_no_disc: list[Pack] = []
    for p in under:
        holders = live_map.get(p.pack_id, [])
        if not holders:
            on_no_disc.append(p)
            continue
        for v in holders:
            vol_info[v.volume_id] = v
            by_volume.setdefault(v.volume_id, []).append(p)

    total_bytes = sum(p.size_bytes for p in under)
    logger.info(
        f"{len(under)} pack(s) ({total_bytes / 1e9:.2f} GB) have fewer "
        f"than {min_copies} live cop(ies):"
    )
    for vol_id in sorted(by_volume, key=lambda v: vol_info[v].label):
        v = vol_info[vol_id]
        packs = by_volume[vol_id]
        sole = [p for p in packs if len(live_map[p.pack_id]) == 1]
        locs = sorted({c.location for c in get_copies_for_volume(conn, vol_id)})
        loc_str = ", ".join(locs) if locs else "<no ACTIVE disc recorded>"
        logger.info("")
        shown = volume_display_status(v, lost_ids)
        logger.info(f"  {v.label}  [{shown}]  ACTIVE discs at: {loc_str}")
        if sole:
            sole_bytes = sum(p.size_bytes for p in sole)
            logger.info(
                f"    only durable holder of {len(sole)} pack(s) "
                f"({sole_bytes / 1e9:.2f} GB) — lose every disc of this "
                f"volume and they are gone"
            )
        if len(packs) > len(sole):
            logger.info(
                f"    plus {len(packs) - len(sole)} under-replicated "
                f"pack(s) shared with other discs"
            )
    if on_no_disc:
        logger.warning(
            "WARNING: %d pack(s) (%.2f GB) are on NO disc at all — see "
            "the unarchived/staged buckets in 'lcsas status'.",
            len(on_no_disc),
            sum(p.size_bytes for p in on_no_disc) / 1e9,
        )
    logger.info("")
    logger.info("Per-volume blast radius: lcsas volume impact <LABEL>")
    return 0


def _volumes_needing_reburn(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, bool]]:
    """(label, location, disc recorded there) for every location where a
    volume's newest verify result is a failure (BURN-05, #671).

    Only VERIFY_PASS, VERIFY_FAIL and VERIFY_FAIL_REBURN count, location
    by location: a later pass at the same location (a good re-burn)
    clears it, a pass at another location does not.  Once the remedy is
    done -- the volume's packs were staged again for that location
    (``stage --for-location``) or are on another disc there -- the
    location needs nothing more from it and it is not listed, unless a
    disc of it is still recorded there (a disc that failed a re-check).
    """
    from lcsas.db.queries import get_unarchived_or_missing_at_location

    newest: dict[tuple[int, str], tuple[str, str]] = {}
    for row in conn.execute(
        """SELECT e.volume_id, e.location, e.event_type, v.label
           FROM volume_events e JOIN volumes v USING (volume_id)
           WHERE e.event_type IN ('VERIFY_PASS', 'VERIFY_FAIL',
                                  'VERIFY_FAIL_REBURN')
           ORDER BY julianday(e.event_date), e.event_id"""
    ):
        key = (int(row["volume_id"]), row["location"] or "<unknown>")
        newest[key] = (row["event_type"], row["label"])
    active = {
        (int(r["volume_id"]), r["location"])
        for r in conn.execute(
            "SELECT volume_id, location FROM volume_copies "
            "WHERE status = 'ACTIVE'"
        )
    }
    missing_at: dict[str, set[int]] = {}
    result: list[tuple[str, str, bool]] = []
    for (vid, loc), (event_type, label) in newest.items():
        if event_type == "VERIFY_PASS":
            continue
        has_disc = (vid, loc) in active
        if not has_disc and loc != "<unknown>":
            packs = {int(r[0]) for r in conn.execute(
                "SELECT pack_id FROM volume_packs WHERE volume_id = ?", (vid,)
            )}
            if loc not in missing_at:
                missing_at[loc] = {
                    p.pack_id
                    for p in get_unarchived_or_missing_at_location(conn, loc)
                }
            if packs and not packs & missing_at[loc]:
                continue
        result.append((label, loc, has_disc))
    return sorted(result)


class _LogToStderr:
    """Send the ``lcsas`` log to stderr while active (``status --json``).

    The log normally goes to stdout; a JSON document on stdout must be
    the only thing there, so a script can parse it.
    """

    def __enter__(self) -> None:
        import logging

        lg = logging.getLogger("lcsas")
        self._saved = lg.handlers[:]
        handler = logging.StreamHandler(sys.stderr)
        if self._saved:
            handler.setLevel(self._saved[0].level)
            handler.setFormatter(self._saved[0].formatter)
        lg.handlers = [handler]

    def __exit__(self, *exc: object) -> None:
        import logging

        logging.getLogger("lcsas").handlers = self._saved


# Version of the `lcsas status --json` document.  Bump it when a field
# changes meaning or is removed; adding a field does not need a bump.
STATUS_JSON_FORMAT = 1


def _status_json(
    conn: sqlite3.Connection,
    *,
    summary: dict[str, Any],
    volumes: list[Any],
    shown_status: dict[int, str],
    disc_locations: dict[int, list[str]],
    waiting: list[Any],
    incomplete: list[str],
    incomplete_sessions: list[str],
    ghost_labels: list[str],
    burned_unrecorded: list[str],
    burn_unknown: list[str],
    partial_sessions: list[str],
    needs_reburn: list[tuple[str, str, bool]],
    single_disc_packs: int,
    stale_count: int,
    older_than_days: int,
) -> dict[str, Any]:
    """The ``lcsas status --json`` document (#701).

    The same facts as the text summary.  ``locations`` has one entry per
    registered location (and per location a waiting volume was staged
    for): its disc count, the volumes waiting to be burned for it and the
    volumes that need a re-burn there.

    ``ghosts`` lists only never-burned volumes whose ISO is gone (what
    ``catalog reconcile --fix`` deletes).  ``burned_unrecorded`` (#751)
    lists volumes that were burned but have no disc recorded, and
    ``burn_unknown`` (#758) those a catalog rebuild left with no record
    of whether they were burned; ``reconcile --fix`` deletes neither.
    """
    from lcsas.db.locations import list_locations

    locations: dict[str, dict[str, Any]] = {
        loc.name: {"discs": 0, "waiting": [], "needs_reburn": []}
        for loc in list_locations(conn)
    }

    def at(name: str) -> dict[str, Any]:
        return locations.setdefault(
            name, {"discs": 0, "waiting": [], "needs_reburn": []},
        )

    for locs in disc_locations.values():
        for name in locs:
            at(name)["discs"] += 1
    for v in waiting:
        row = conn.execute(
            "SELECT sv.session_id FROM session_volumes sv "
            "JOIN burn_sessions bs USING (session_id) "
            "WHERE sv.volume_id = ? "
            "ORDER BY julianday(bs.created_at) DESC, sv.rowid DESC LIMIT 1",
            (v.volume_id,),
        ).fetchone()
        at(v.location)["waiting"].append({
            "label": v.label,
            "media_type": v.media_type,
            "data_bytes": v.used_bytes,
            "session_id": None if row is None else str(row["session_id"]),
        })
    reburn = []
    for label, loc, has_disc in needs_reburn:
        known = loc != "<unknown>"
        if known:
            at(loc)["needs_reburn"].append(label)
        reburn.append({"label": label, "location": loc if known else None,
                       "disc_at_location": has_disc})
    return {
        "format": STATUS_JSON_FORMAT,
        "packs": dict(summary),
        "volumes": [
            {
                "label": v.label,
                "media_type": v.media_type,
                "status": shown_status[v.volume_id],
                "discs_at": disc_locations.get(v.volume_id, []),
                "staged_for": (v.location if v.status in ("STAGING", "BURNING")
                               else None),
            }
            for v in volumes
        ],
        "locations": locations,
        "waiting": [v.label for v in waiting],
        "incomplete": {"labels": incomplete, "sessions": incomplete_sessions},
        "ghosts": ghost_labels,
        "burned_unrecorded": burned_unrecorded,
        "burn_unknown": burn_unknown,
        "partial_sessions": partial_sessions,
        "needs_reburn": reburn,
        "single_disc_packs": single_disc_packs,
        "stale_discs": {"count": stale_count,
                        "older_than_days": older_than_days},
    }


def cmd_status(args: argparse.Namespace) -> int:
    """Show archive status summary (``--json``: one JSON document, #701)."""
    if not getattr(args, "json", False):
        return _cmd_status(args)
    if args.stale_copies or args.redundancy:
        logger.error(
            "--json gives the summary only; it cannot be combined with "
            "--stale-copies or --redundancy."
        )
        return 1
    with _LogToStderr():
        return _cmd_status(args)


def _cmd_status(args: argparse.Namespace) -> int:
    from lcsas.db.queries import (
        get_archive_status_summary,
        get_ghost_volumes,
        get_live_volumes_for_packs,
        get_lost_volume_ids,
        get_redundancy_report,
        volume_display_status,
    )
    from lcsas.db.sessions import (
        list_sessions,
        staged_iso_present,
        staging_incomplete,
    )
    from lcsas.db.volume_copies import find_stale_copies
    from lcsas.db.volume_events import burn_evidence, rebuild_burn_unknown
    from lcsas.db.volumes import list_volumes

    doc: dict[str, Any] = {}
    with _open_catalog(args, mode="read") as conn:
        if args.stale_copies:
            return _print_stale_copies(conn, args.older_than_days)
        if args.redundancy:
            return _print_redundancy_report(conn, args.min_copies)

        stale_count = len(find_stale_copies(conn, args.older_than_days))
        summary = get_archive_status_summary(conn)
        volumes = list_volumes(conn)
        # #643: show where each volume's discs are (its ACTIVE disc
        # records), not volumes.location, which is only the location it
        # was staged for.
        disc_locations: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT volume_id, location FROM volume_copies "
            "WHERE status = 'ACTIVE' ORDER BY location"
        ):
            disc_locations.setdefault(row["volume_id"], []).append(row["location"])
        lost_ids = get_lost_volume_ids(conn)
        partial_sessions = list_sessions(conn, status_filter="PARTIAL")
        # #647: never-burned volumes split by whether their ISO is still in
        # the staging area (waiting to be burned) or gone (a ghost).
        # BURN-05: a volume whose newest verify result at a location is a
        # failure needs a re-burn there (#671: other events, such as the
        # BURN_RECEIPT_IMPORTED written after a failed receipt, don't count).
        needs_reburn = _volumes_needing_reburn(conn)
        failed_labels = {label for label, _, _ in needs_reburn}
        # #728: one next step per failed burn.  The needs-re-burn line
        # names it (burn the session again while its ISO is kept, else
        # stage again), so a PARTIAL session holding one of those volumes
        # gets no second, different line.
        reburn_hints: dict[tuple[str, str], str] = {}
        covered_sessions: set[str] = set()
        for label, loc, has_disc in needs_reburn:
            if has_disc or loc == "<unknown>":
                continue
            vid = int(conn.execute(
                "SELECT volume_id FROM volumes WHERE label = ?", (label,)
            ).fetchone()[0])
            reburn_hints[(label, loc)] = _reburn_hint(conn, vid, loc)
            covered_sessions.update(
                str(r[0]) for r in conn.execute(
                    "SELECT session_id FROM session_volumes "
                    "WHERE volume_id = ?", (vid,),
                )
            )
        unburned = get_ghost_volumes(conn, older_than_hours=0)
        # #751: a volume with no disc record that was burned is not a
        # ghost: BURNED/VERIFIED with no disc row at all (a catalog rebuilt
        # from discs records the newest discs like this), or STAGING/
        # BURNING with burn evidence (the check reconcile --fix uses).
        burned_unrecorded: list[tuple[str, str]] = [
            (str(r["label"]), f"{r['status']}, but no disc of it is recorded")
            for r in conn.execute(
                "SELECT v.label, v.status FROM volumes v "
                "WHERE v.status IN ('BURNED', 'VERIFIED') AND NOT EXISTS "
                "(SELECT 1 FROM volume_copies vc "
                "WHERE vc.volume_id = v.volume_id) ORDER BY v.label"
            )
        ]
        # #758: one a catalog rebuild left with no record of whether it
        # was burned is not a ghost either: its disc may exist.
        burn_unknown: list[str] = []
        unread = []
        staged_but_burned = False
        for v in unburned:
            why = burn_evidence(conn, v.volume_id)
            if why is not None:
                burned_unrecorded.append((v.label, f"{v.status}, but {why}"))
                staged_but_burned = True
            elif rebuild_burn_unknown(conn, v.volume_id):
                burn_unknown.append(v.label)
            else:
                unread.append(v)
        burned_unrecorded.sort()
        unburned = unread
        # #665: a stage that did not finish (no ISO hash recorded) is
        # incomplete, whatever is in the staging area: never burn it.
        incomplete = [v.label for v in unburned
                      if staging_incomplete(conn, v.volume_id)]
        incomplete_sessions = sorted({
            str(r["session_id"]) for r in conn.execute(
                "SELECT sv.session_id FROM session_volumes sv "
                "JOIN volumes v USING (volume_id) WHERE v.label IN "
                f"({','.join('?' * len(incomplete))})",
                incomplete,
            )
        }) if incomplete else []
        unburned = [v for v in unburned if v.label not in incomplete]
        with_iso = [v.label for v in unburned if staged_iso_present(conn, v.volume_id)]
        # #671: a volume whose disc failed verification is listed under
        # "needs re-burn", not as waiting to be burned.
        waiting = [label for label in with_iso if label not in failed_labels]
        # #728: nor as a ghost once its ISO is gone: the needs-re-burn
        # line then names the one step (stage it again for the location).
        ghost_labels = [v.label for v in unburned
                        if v.label not in with_iso
                        and v.label not in failed_labels]
        # FMA-08: packs that live on exactly one disc.  Packs in
        # the <2-copies report with at least one live holder are on one
        # disc; zero-holder packs are the unarchived/staged buckets above.
        under = get_redundancy_report(conn, min_copies=2)
        live_map = get_live_volumes_for_packs(conn, [p.pack_id for p in under])
        single_disc_packs = [p for p in under if live_map.get(p.pack_id)]
        if getattr(args, "json", False):
            doc = _status_json(
                conn,
                summary=summary,
                volumes=volumes,
                shown_status={v.volume_id: volume_display_status(v, lost_ids)
                              for v in volumes},
                disc_locations=disc_locations,
                waiting=[v for v in unburned if v.label in waiting],
                incomplete=incomplete,
                incomplete_sessions=incomplete_sessions,
                ghost_labels=ghost_labels,
                burned_unrecorded=[label for label, _ in burned_unrecorded],
                burn_unknown=burn_unknown,
                partial_sessions=[s.session_id for s in partial_sessions],
                needs_reburn=needs_reburn,
                single_disc_packs=len(single_disc_packs),
                stale_count=stale_count,
                older_than_days=args.older_than_days,
            )

    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    logger.info(f"Packs: {summary['total']} total, "
               f"{summary['archived']} archived, "
               f"{summary['staged']} staged (NOT yet on disc), "
               f"{summary['unarchived']} unarchived, "
               f"{summary['pruned']} pruned")
    # Q33: a lost volume (no disc left in service) is recorded as
    # DEPRECATED but shown as LOST.
    lost_note = f" ({len(lost_ids)} lost)" if lost_ids else ""
    logger.info(f"Volumes: {len(volumes)} total{lost_note}")
    for v in volumes:
        shown = volume_display_status(v, lost_ids)
        where = ", ".join(disc_locations.get(v.volume_id, []))
        if not where:
            where = (f"(for {v.location})" if v.status in ("STAGING", "BURNING")
                     else "(no disc in service)")
        logger.info(f"  {v.label:<25} {v.media_type:<10} {shown:<10} {where}")
    if incomplete:
        logger.warning(
            "WARNING: %d volume(s) are INCOMPLETE: their staging did not "
            "finish, so their ISOs may lack error-correction data: %s. Do "
            "not burn them. Run 'lcsas session abort <id>' for session %s, "
            "then 'lcsas stage' again. Until then their packs are NOT on "
            "any disc.",
            len(incomplete), ", ".join(incomplete),
            ", ".join(incomplete_sessions),
        )
    if waiting:
        logger.warning(
            "WARNING: %d volume(s) are staged and waiting to be burned "
            "(their ISOs are in the staging area): %s. Burn them with "
            "'lcsas burn', or burn the ISOs elsewhere with 'lcsas burn-iso "
            "--emit-receipt' and import the receipts with 'lcsas catalog "
            "import-receipts'. Until then their packs are NOT on any disc.",
            len(waiting), ", ".join(waiting),
        )
    if ghost_labels:
        logger.warning(
            "WARNING: %d volume(s) were staged but never burned, and their "
            "ISOs are gone: %s. 'lcsas catalog reconcile --fix' deletes them "
            "from the catalog and returns their packs to the unarchived "
            "pool for the next 'lcsas stage'.",
            len(ghost_labels), ", ".join(ghost_labels),
        )
    if burned_unrecorded:
        logger.warning(
            "WARNING: %d volume(s) were burned, but no disc of them is "
            "recorded: %s. For each disc you hold, put it in the drive and "
            "run 'lcsas verify <label> --disc --location <where it is "
            "kept>', or import its burn receipt with 'lcsas catalog "
            "import-receipts'.",
            len(burned_unrecorded),
            "; ".join(f"{label} ({why})" for label, why in burned_unrecorded),
        )
    if burn_unknown:
        logger.warning(
            "WARNING: %d volume(s) come from a catalog rebuilt from discs, "
            "and no disc it read shows whether they were burned: %s. For "
            "each disc of them you hold, run 'lcsas verify <label> --disc "
            "--location <where it is kept>', or import its burn receipt. If "
            "you are sure one was never burned, 'lcsas session abort "
            "--volume <label> --never-burned' deletes it and returns its "
            "packs to the unarchived pool.",
            len(burn_unknown), ", ".join(burn_unknown),
        )
    # #728: nor for the packs of a failed burn, listed under "needs
    # re-burn" with their step.
    if summary["staged"] > 0 and not (
        with_iso or ghost_labels or incomplete or burn_unknown
        or staged_but_burned or reburn_hints
    ):
        logger.warning(
            "WARNING: %d pack(s) are staged on volumes that were never "
            "burned. See 'lcsas catalog reconcile'.",
            summary["staged"],
        )
    for s in partial_sessions:
        if s.session_id in covered_sessions:
            continue
        logger.warning(
            "WARNING: session %s is PARTIAL — at least one burn failed "
            "verification and recorded no disc. Re-run: "
            "lcsas burn --session %s --location <location>",
            s.session_id, s.session_id,
        )
    for label, loc, has_disc in needs_reburn:
        if loc == "<unknown>":
            logger.warning(
                "WARNING: %s failed verification (no location recorded). "
                "Check it with 'lcsas verify %s --disc'.",
                label, label,
            )
        elif has_disc:
            logger.warning(
                "WARNING: %s needs re-burn at %s: its disc there failed "
                "verification. Check it with 'lcsas verify %s --disc'; if it "
                "is bad, record it with 'lcsas copy deprecate %s %s', then "
                "stage it again with 'lcsas stage --for-location %s' and burn "
                "the new volume.",
                label, loc, label, label, loc, loc,
            )
        else:
            logger.warning(
                "WARNING: %s needs re-burn at %s: its disc failed "
                "verification and no disc of it is recorded there. %s",
                label, loc, reburn_hints[(label, loc)],
            )
    if single_disc_packs:
        logger.warning(
            "WARNING: %d pack(s) (%.1f GB) exist on only one disc — run "
            "'lcsas status --redundancy' for the blast-radius report.",
            len(single_disc_packs),
            sum(p.size_bytes for p in single_disc_packs) / 1e9,
        )
    if stale_count > 0:
        logger.warning(
            "WARNING: %d disc(s) not verified in the last %d "
            "days — run 'lcsas status --stale-copies' to list them and "
            "'lcsas verify --all --disc' to re-verify.",
            stale_count, args.older_than_days,
        )
    return 0
