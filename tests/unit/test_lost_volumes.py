"""Lost volumes are a display, not a schema state (Q33/Q38, ADR 0003).

A lost volume is a burned volume with no disc left in service.  The
catalog records it as DEPRECATED plus the auto-deprecation NOTE event;
owner displays show it as LOST, while a volume deprecated by
consolidation still shows DEPRECATED.  ``lcsas copy destroy --lost``
records a lost disc (disc status DESTROYED, event note "lost").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import mark_pruned, register_pack
from lcsas.db.queries import (
    get_archive_status_summary,
    get_lost_volume_ids,
    get_lost_volume_packs,
    get_unarchived_packs,
    volume_display_status,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy, deprecate_copy, destroy_copy
from lcsas.db.volume_events import add_event, get_events_for_volume
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_id, update_status
from lcsas.utils.labels import generate_uuid


def _vol(conn, label, status="VERIFIED"):
    return create_volume(
        conn, label=label, uuid=generate_uuid(), media_type="BD25",
        capacity_bytes=25_000_000_000, status=status,
    )


@pytest.fixture
def cat(conn):
    """The unit ``conn`` fixture plus locations and one repository."""
    create_location(conn, "Home_Shelf")
    create_location(conn, "Offsite_Safe")
    register_repo(conn, "repo-photos", "photos", "/mnt/mirror/photos")
    return conn


def _lost(conn, vol):
    return vol.volume_id in get_lost_volume_ids(conn)


# ────────────────────────────────────────────────────────────────────
#  The predicate
# ────────────────────────────────────────────────────────────────────


class TestLostPredicate:
    def test_in_service_disc_is_not_lost(self, cat):
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        assert not _lost(cat, vol)

    @pytest.mark.parametrize("record", ["deprecate", "destroy", "lost"])
    def test_last_disc_gone_makes_volume_lost(self, cat, record):
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        if record == "deprecate":
            deprecate_copy(cat, vol.volume_id, "Home_Shelf")
        else:
            destroy_copy(cat, vol.volume_id, "Home_Shelf", lost=record == "lost")
        # Stored status is unchanged from before Q33: DEPRECATED.
        assert get_volume_by_id(cat, vol.volume_id).status == "DEPRECATED"
        assert _lost(cat, vol)

    def test_one_of_two_discs_gone_is_not_lost(self, cat):
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        add_volume_copy(cat, vol.volume_id, "Offsite_Safe")
        destroy_copy(cat, vol.volume_id, "Home_Shelf", lost=True)
        assert not _lost(cat, vol)

    def test_consolidated_volume_stays_deprecated(self, cat):
        """Deprecated by consolidation, then its discs taken out of service
        as the consolidation workflow says: still DEPRECATED, not LOST."""
        vol = _vol(cat, "OLD")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        add_volume_copy(cat, vol.volume_id, "Offsite_Safe")
        update_status(cat, vol.volume_id, "CONSOLIDATING")
        update_status(cat, vol.volume_id, "DEPRECATED")
        assert not _lost(cat, vol)

        deprecate_copy(cat, vol.volume_id, "Home_Shelf")
        destroy_copy(cat, vol.volume_id, "Offsite_Safe")
        assert not _lost(cat, vol)
        assert volume_display_status(
            get_volume_by_id(cat, vol.volume_id), get_lost_volume_ids(cat)
        ) == "DEPRECATED"

    @pytest.mark.parametrize("status", ["BURNED", "VERIFIED", "CONSOLIDATING"])
    def test_durable_status_with_no_disc_in_service_is_lost(self, cat, status):
        """Status/disc drift (or a catalog from before BURN-10): the
        recorded status claims a disc, but no disc is in service."""
        vol = _vol(cat, "DRIFT", status)
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        cat.execute(
            "UPDATE volume_copies SET status = 'DESTROYED' WHERE volume_id = ?",
            (vol.volume_id,),
        )
        cat.commit()
        assert _lost(cat, vol)

    @pytest.mark.parametrize(
        "status", ["STAGING", "BURNING", "BURNED", "VERIFIED", "DEPRECATED"],
    )
    def test_volume_with_no_disc_records_is_never_lost(self, cat, status):
        """Zero disc records = a legacy volume that counts by status alone,
        or one never burned."""
        vol = _vol(cat, "LEGACY", status)
        assert not _lost(cat, vol)

    def test_destroyed_volume_is_not_lost(self, cat):
        vol = _vol(cat, "GONE")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        update_status(cat, vol.volume_id, "DESTROYED", force=True)
        destroy_copy(cat, vol.volume_id, "Home_Shelf")
        assert not _lost(cat, vol)

    def test_old_event_wording_still_counts(self, cat):
        """Catalogs written before the glossary pass carry "last physical
        copy"; only the "auto-deprecated:" prefix is matched."""
        vol = _vol(cat, "OLD_WORDING", "DEPRECATED")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        cat.execute(
            "UPDATE volume_copies SET status = 'DESTROYED' WHERE volume_id = ?",
            (vol.volume_id,),
        )
        add_event(
            cat, vol.volume_id, "NOTE", location="Home_Shelf",
            detail="auto-deprecated: last physical copy destroyed at Home_Shelf",
        )
        assert _lost(cat, vol)

    def test_regained_disc_is_not_lost(self, cat):
        """A disc recorded again in service (re-burn at the location) ends
        the lost state, even though the volume stays DEPRECATED."""
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        destroy_copy(cat, vol.volume_id, "Home_Shelf", lost=True)
        assert _lost(cat, vol)
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        assert not _lost(cat, vol)

    def test_display_status(self, cat):
        lost = _vol(cat, "LOST_VOL")
        kept = _vol(cat, "KEPT_VOL")
        add_volume_copy(cat, lost.volume_id, "Home_Shelf")
        add_volume_copy(cat, kept.volume_id, "Home_Shelf")
        destroy_copy(cat, lost.volume_id, "Home_Shelf")
        ids = get_lost_volume_ids(cat)
        assert ids == {lost.volume_id}
        assert volume_display_status(get_volume_by_id(cat, lost.volume_id), ids) == "LOST"
        assert volume_display_status(kept, ids) == "VERIFIED"


# ────────────────────────────────────────────────────────────────────
#  Disc fate events (Q38)
# ────────────────────────────────────────────────────────────────────


class TestDiscFateEvents:
    def _notes(self, conn, vol):
        return [e.detail for e in get_events_for_volume(conn, vol.volume_id, "NOTE")]

    def test_lost_disc_is_distinguishable_from_destroyed(self, cat):
        a = _vol(cat, "A")
        b = _vol(cat, "B")
        for v in (a, b):
            add_volume_copy(cat, v.volume_id, "Home_Shelf")
            add_volume_copy(cat, v.volume_id, "Offsite_Safe")
        destroy_copy(cat, a.volume_id, "Offsite_Safe", lost=True)
        destroy_copy(cat, b.volume_id, "Offsite_Safe")

        # Same disc status ...
        for v in (a, b):
            row = cat.execute(
                "SELECT status FROM volume_copies "
                "WHERE volume_id = ? AND location = 'Offsite_Safe'",
                (v.volume_id,),
            ).fetchone()
            assert row["status"] == "DESTROYED"
        # ... but the note says which, even though it was not the last disc.
        assert self._notes(cat, a) == [
            "disc at Offsite_Safe recorded as lost (whereabouts unknown)"
        ]
        assert self._notes(cat, b) == ["disc at Offsite_Safe recorded as destroyed"]

    def test_last_disc_lost_says_lost_in_auto_note(self, cat):
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        destroy_copy(cat, vol.volume_id, "Home_Shelf", lost=True)
        notes = self._notes(cat, vol)
        assert "auto-deprecated: last in-service disc lost at Home_Shelf" in notes
        assert "disc at Home_Shelf recorded as lost (whereabouts unknown)" in notes

    def test_deprecate_records_a_note(self, cat):
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        add_volume_copy(cat, vol.volume_id, "Offsite_Safe")
        deprecate_copy(cat, vol.volume_id, "Home_Shelf")
        assert self._notes(cat, vol) == [
            "disc at Home_Shelf recorded as deprecated (out of service)"
        ]


# ────────────────────────────────────────────────────────────────────
#  What a lost volume leaves on no disc
# ────────────────────────────────────────────────────────────────────


def _pack(conn, sha_byte, size=100):
    return register_pack(conn, sha256=sha_byte * 32, size_bytes=size,
                         repo_id="repo-photos")


class TestLostVolumePacks:
    def test_splits_mirror_only_and_gone(self, cat):
        lost = _vol(cat, "LOST_VOL")
        other = _vol(cat, "OTHER_VOL")
        add_volume_copy(cat, lost.volume_id, "Home_Shelf")
        add_volume_copy(cat, other.volume_id, "Home_Shelf")
        only_here = _pack(cat, "a1")
        pruned_here = _pack(cat, "b2")
        mark_pruned(cat, pruned_here.pack_id)
        held_elsewhere = _pack(cat, "c3")
        pruned_elsewhere = _pack(cat, "d4")
        mark_pruned(cat, pruned_elsewhere.pack_id)
        bulk_link_packs(cat, lost.volume_id, [
            only_here.pack_id, pruned_here.pack_id,
            held_elsewhere.pack_id, pruned_elsewhere.pack_id,
        ])
        bulk_link_packs(cat, other.volume_id,
                        [held_elsewhere.pack_id, pruned_elsewhere.pack_id])

        destroy_copy(cat, lost.volume_id, "Home_Shelf", lost=True)
        left = get_lost_volume_packs(cat, lost.volume_id)
        assert [p.sha256 for p in left.mirror_only] == ["a1" * 32]
        assert [p.sha256 for p in left.gone] == ["b2" * 32]
        # mirror_only is exactly what the next `lcsas stage` picks up.
        assert {p.sha256 for p in get_unarchived_packs(cat)} == {"a1" * 32}

    def test_pack_on_another_lost_volume_still_needs_burning(self, cat):
        first = _vol(cat, "FIRST")
        second = _vol(cat, "SECOND")
        add_volume_copy(cat, first.volume_id, "Home_Shelf")
        add_volume_copy(cat, second.volume_id, "Home_Shelf")
        shared = _pack(cat, "e5")
        bulk_link_packs(cat, first.volume_id, [shared.pack_id])
        bulk_link_packs(cat, second.volume_id, [shared.pack_id])

        destroy_copy(cat, first.volume_id, "Home_Shelf")
        assert get_lost_volume_packs(cat, first.volume_id).mirror_only == []
        destroy_copy(cat, second.volume_id, "Home_Shelf")
        left = get_lost_volume_packs(cat, second.volume_id)
        assert [p.sha256 for p in left.mirror_only] == ["e5" * 32]

    def test_lost_packs_count_as_unarchived_not_staged(self, cat):
        """A lost volume's packs are on no disc and re-enter the stage pool;
        `lcsas status` must not call them staged on a never-burned volume."""
        vol = _vol(cat, "V")
        add_volume_copy(cat, vol.volume_id, "Home_Shelf")
        p = _pack(cat, "f6")
        bulk_link_packs(cat, vol.volume_id, [p.pack_id])
        assert get_archive_status_summary(cat)["archived"] == 1

        destroy_copy(cat, vol.volume_id, "Home_Shelf")
        summary = get_archive_status_summary(cat)
        assert summary["archived"] == 0
        assert summary["staged"] == 0
        assert summary["unarchived"] == 1


# ────────────────────────────────────────────────────────────────────
#  CLI: the warning when the last disc goes
# ────────────────────────────────────────────────────────────────────


def _cli_catalog(db_path: Path, *, discs=("Home_Shelf",)) -> dict[str, int]:
    """LOST_ME (VERIFIED, one disc per *discs*) holding pack aa (only here),
    pack bb (pruned, only here) and pack cc (also on KEEP, which has a
    disc at Home_Shelf).  Returns ``{label: volume_id}``."""
    conn = get_connection(str(db_path))
    try:
        create_all(conn)
        create_location(conn, "Home_Shelf")
        create_location(conn, "Offsite_Safe")
        register_repo(conn, "repo-photos", "photos", "/mnt/mirror/photos")
        lost_me = _vol(conn, "LOST_ME")
        keep = _vol(conn, "KEEP")
        for loc in discs:
            add_volume_copy(conn, lost_me.volume_id, loc)
        add_volume_copy(conn, keep.volume_id, "Home_Shelf")
        aa = _pack(conn, "aa", size=1_000_000_000)
        bb = _pack(conn, "bb")
        mark_pruned(conn, bb.pack_id)
        cc = _pack(conn, "cc")
        bulk_link_packs(conn, lost_me.volume_id, [aa.pack_id, bb.pack_id, cc.pack_id])
        bulk_link_packs(conn, keep.volume_id, [cc.pack_id])
        return {"LOST_ME": lost_me.volume_id, "KEEP": keep.volume_id}
    finally:
        conn.close()


def _out(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


class TestCopyCommandWarnings:
    def test_last_disc_lost_warns_mirror_only_and_pruned(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _cli_catalog(db)
        assert main(["--db", str(db), "copy", "destroy", "--lost",
                     "LOST_ME", "Home_Shelf"]) == 0
        out = _out(capsys)

        assert "as lost (disc status DESTROYED)" in out
        # Lost disc: confidentiality warning.
        assert "stranger's hands" in out
        assert "guess your passwords offline" in out
        assert "DISC_CONFIDENTIALITY.md" in out
        # Lost volume warning.
        assert "LOST_ME is now a lost volume" in out
        assert "records it as DEPRECATED" in out
        # Mirror-only pack aa, with the next-stage promise.
        assert ("1 pack(s) (1.00 GB) from LOST_ME are now on no disc in "
                "service, only in the mirror") in out
        assert "next 'lcsas stage' will burn them again" in out
        assert "aa" * 32 in out
        # Pruned pack bb, loudly, as gone for good.
        assert "DATA LOSS: 1 pack(s) from LOST_ME were pruned" in out
        assert "bb" * 32 in out
        # Pack cc is on KEEP too: never listed.
        assert "cc" * 32 not in out
        assert "stage --for-location Home_Shelf" in out

    def test_destroy_without_lost_has_no_confidentiality_warning(
        self, tmp_path, capsys,
    ):
        db = tmp_path / "test.db"
        _cli_catalog(db)
        assert main(["--db", str(db), "copy", "destroy",
                     "LOST_ME", "Home_Shelf"]) == 0
        out = _out(capsys)
        assert "as DESTROYED." in out
        assert "stranger's hands" not in out
        assert "LOST_ME is now a lost volume" in out

    def test_lost_disc_with_another_disc_in_service(self, tmp_path, capsys):
        """Not the last disc: the lost-disc warning still prints (the disc
        may be in a stranger's hands) but the volume is not lost."""
        db = tmp_path / "test.db"
        _cli_catalog(db, discs=("Home_Shelf", "Offsite_Safe"))
        assert main(["--db", str(db), "copy", "destroy", "--lost",
                     "LOST_ME", "Offsite_Safe"]) == 0
        out = _out(capsys)
        assert "stranger's hands" in out
        assert "now a lost volume" not in out
        assert "DATA LOSS" not in out

    def test_lost_volume_with_every_pack_elsewhere(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(str(db))
        try:
            create_all(conn)
            create_location(conn, "Home_Shelf")
            register_repo(conn, "repo-photos", "photos", "/mnt/mirror/photos")
            a = _vol(conn, "A")
            b = _vol(conn, "B")
            add_volume_copy(conn, a.volume_id, "Home_Shelf")
            add_volume_copy(conn, b.volume_id, "Home_Shelf")
            p = _pack(conn, "ab")
            bulk_link_packs(conn, a.volume_id, [p.pack_id])
            bulk_link_packs(conn, b.volume_id, [p.pack_id])
        finally:
            conn.close()
        assert main(["--db", str(db), "copy", "deprecate", "A", "Home_Shelf"]) == 0
        out = _out(capsys)
        assert "A is now a lost volume" in out
        assert "nothing needs burning again" in out
        assert "DATA LOSS" not in out

    def test_lost_flag_only_on_destroy(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _cli_catalog(db)
        with pytest.raises(SystemExit):
            main(["--db", str(db), "copy", "deprecate", "--lost",
                  "LOST_ME", "Home_Shelf"])


# ────────────────────────────────────────────────────────────────────
#  CLI: displays say LOST
# ────────────────────────────────────────────────────────────────────


def _display_catalog(db_path: Path) -> None:
    """LOST_ME lost (last disc destroyed); OLD deprecated by consolidation
    with its disc then deprecated; KEEP in service."""
    vols = _cli_catalog(db_path)
    conn = get_connection(str(db_path))
    try:
        destroy_copy(conn, vols["LOST_ME"], "Home_Shelf", lost=True)
        old = _vol(conn, "OLD")
        add_volume_copy(conn, old.volume_id, "Home_Shelf")
        update_status(conn, old.volume_id, "DEPRECATED")
        deprecate_copy(conn, old.volume_id, "Home_Shelf")
    finally:
        conn.close()


def _row(out: str, label: str) -> str:
    return next(line for line in out.splitlines() if f" {label} " in f"{line} ")


class TestDisplays:
    def test_status_shows_lost(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _display_catalog(db)
        assert main(["--db", str(db), "status"]) == 0
        out = _out(capsys)
        assert "Volumes: 3 total (1 lost)" in out
        assert "LOST" in _row(out, "LOST_ME")
        assert "DEPRECATED" in _row(out, "OLD")
        assert "LOST" not in _row(out, "OLD")
        assert "VERIFIED" in _row(out, "KEEP")
        # Lost packs are unarchived, not "staged on a never-burned volume".
        assert "never burned" not in out
        assert "1 unarchived" in out

    def test_status_redundancy_never_lists_lost_volume(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _display_catalog(db)
        assert main(["--db", str(db), "status", "--redundancy"]) == 0
        out = _out(capsys)
        assert "LOST_ME" not in out
        assert "on NO disc at all" in out

    def test_volume_impact_on_lost_volume(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _display_catalog(db)
        assert main(["--db", str(db), "volume", "impact", "LOST_ME"]) == 0
        out = _out(capsys)
        assert "Volume LOST_ME  [LOST]" in out
        assert "it is a lost volume (recorded as DEPRECATED)" in out
        assert "only in the mirror" in out
        assert "aa" * 32 in out
        assert "DATA LOSS" in out
        assert "Blast radius" not in out

    def test_volume_impact_on_consolidated_volume(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _display_catalog(db)
        assert main(["--db", str(db), "volume", "impact", "OLD"]) == 0
        out = _out(capsys)
        assert "Volume OLD  [DEPRECATED]" in out
        assert "No disc of this volume is left in service." in out
        assert "lost volume" not in out

    def test_reconcile_drift_shows_lost(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(str(db))
        try:
            create_all(conn)
            create_location(conn, "Home_Shelf")
            drift = _vol(conn, "DRIFT", "BURNED")
            add_volume_copy(conn, drift.volume_id, "Home_Shelf")
            conn.execute(
                "UPDATE volume_copies SET status = 'DESTROYED' WHERE volume_id = ?",
                (drift.volume_id,),
            )
            conn.commit()
        finally:
            conn.close()
        assert main(["--db", str(db), "catalog", "reconcile"]) == 0
        out = _out(capsys)
        assert "DRIFT  status=LOST (recorded as BURNED)" in out

    def test_mark_verified_refusal_says_lost(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _display_catalog(db)
        assert main(["--db", str(db), "verify", "LOST_ME", "--mark-verified"]) == 1
        assert "status is LOST" in _out(capsys)

    def test_estate_card_skips_lost_volumes(self, tmp_path):
        db = tmp_path / "archive.db"
        _display_catalog(db)
        conn = get_connection(str(db))
        try:
            mid = _vol(conn, "MID")
            add_volume_copy(conn, mid.volume_id, "Home_Shelf")
            update_status(conn, mid.volume_id, "CONSOLIDATING")
        finally:
            conn.close()
        cfg = tmp_path / "lcsas.toml"
        mirror = tmp_path / "mirror-alpha"
        mirror.mkdir()
        (tmp_path / "alpha.pw").write_bytes(b"pw\n")
        cfg.write_text("\n".join([
            "[paths]",
            f'mirror_base = "{tmp_path / "mirror_base"}"',
            f'staging = "{tmp_path / "staging"}"',
            f'database = "{db}"',
            "[defaults]",
            'label_prefix = "SMITH"',
            "[repos.alpha]",
            f'mirror_path = "{mirror}"',
            f'password_file = "{tmp_path / "alpha.pw"}"',
        ]) + "\n")
        card = tmp_path / "card.txt"
        assert main(["--config", str(cfg), "estate", "card",
                     "--output", str(card)]) == 0
        # KEEP (VERIFIED) + OLD (deprecated by consolidation) + MID
        # (CONSOLIDATING); LOST_ME has no disc left, so it is not counted.
        assert "3 disc(s), labeled SMITH_*" in card.read_text(encoding="utf-8")

    def test_burn_dry_run_shows_lost(self, tmp_path, capsys):
        from lcsas.db.sessions import add_session_volume, create_session

        db = tmp_path / "archive.db"
        _display_catalog(db)
        conn = get_connection(str(db))
        try:
            sess = create_session(conn, "BD25", str(tmp_path / "staging"))
            for label in ("LOST_ME", "KEEP"):
                vid = conn.execute(
                    "SELECT volume_id FROM volumes WHERE label = ?", (label,),
                ).fetchone()[0]
                add_session_volume(conn, sess.session_id, vid, f"/x/{label}.iso")
        finally:
            conn.close()
        (tmp_path / "mirror_base").mkdir()
        (tmp_path / "staging").mkdir()
        cfg = tmp_path / "lcsas.toml"
        cfg.write_text("\n".join([
            "[paths]",
            f'mirror_base = "{tmp_path / "mirror_base"}"',
            f'staging = "{tmp_path / "staging"}"',
            f'database = "{db}"',
        ]) + "\n")
        assert main(["--config", str(cfg), "burn", "--session",
                     sess.session_id, "--dry-run"]) == 0
        out = _out(capsys)
        assert "LOST_ME  status=LOST" in out
        assert "KEEP  status=VERIFIED" in out


class TestRegainedThenLostAgain:
    def test_losing_a_regained_disc_warns_again(self, tmp_path, capsys):
        """A lost volume that regained a disc stays DEPRECATED, so losing
        that disc demotes nothing; the warning must still print."""
        db = tmp_path / "test.db"
        vols = _cli_catalog(db)
        conn = get_connection(str(db))
        try:
            destroy_copy(conn, vols["LOST_ME"], "Home_Shelf", lost=True)
            add_volume_copy(conn, vols["LOST_ME"], "Offsite_Safe")
            assert vols["LOST_ME"] not in get_lost_volume_ids(conn)
        finally:
            conn.close()
        assert main(["--db", str(db), "copy", "destroy",
                     "LOST_ME", "Offsite_Safe"]) == 0
        out = _out(capsys)
        assert "LOST_ME is now a lost volume" in out
        assert "aa" * 32 in out
        assert "DATA LOSS" in out

    def test_consolidated_volume_last_disc_stays_silent(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(str(db))
        try:
            create_all(conn)
            create_location(conn, "Home_Shelf")
            old = _vol(conn, "OLD")
            add_volume_copy(conn, old.volume_id, "Home_Shelf")
            update_status(conn, old.volume_id, "DEPRECATED")
        finally:
            conn.close()
        assert main(["--db", str(db), "copy", "destroy", "OLD", "Home_Shelf"]) == 0
        assert "now a lost volume" not in _out(capsys)
