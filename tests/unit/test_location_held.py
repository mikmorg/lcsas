"""One "pack held at location L" rule for every per-location view (#680, #679).

``location list`` (get_location_summary), ``location status``
(get_packs_at_location / get_packs_missing_at_location) and
``stage --for-location`` (get_unarchived_or_missing_at_location) used to
answer "what does L hold" three different ways, so after a consolidation
they gave different numbers, and a location with no disc read "all current".
The redundancy report counted a never-burned volume as a disc (#679).
"""

from __future__ import annotations

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import mark_pruned, register_pack
from lcsas.db.queries import (
    get_live_volumes_for_packs,
    get_location_summary,
    get_packs_at_location,
    get_packs_missing_at_location,
    get_redundancy_report,
    get_unarchived_or_missing_at_location,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid

HOME, OFFSITE, VAULT = "Home_Shelf", "Offsite_Safe", "Vault"


def _volume(conn, label, packs, status, copies=()):
    vol = create_volume(conn, label, generate_uuid(), "TEST_TINY", 1_000_000,
                        status=status)
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    for loc in copies:
        add_volume_copy(conn, vol.volume_id, loc)
    return vol


def _consolidated(conn):
    """Two locations, then a consolidation that re-burned at HOME only.

    A (now DEPRECATED, its disc records still ACTIVE at both places) held
    p1, p2 and the since-pruned p3.  C (VERIFIED) holds p1, p2, at HOME.
    VAULT is registered but has never held a disc.
    """
    for loc in (HOME, OFFSITE, VAULT):
        create_location(conn, loc)
    p1 = register_pack(conn, "a" * 64, 100, "family")
    p2 = register_pack(conn, "b" * 64, 100, "family")
    p3 = register_pack(conn, "c" * 64, 100, "family")
    mark_pruned(conn, p3.pack_id)
    _volume(conn, "A", [p1, p2, p3], "DEPRECATED", copies=(HOME, OFFSITE))
    _volume(conn, "C", [p1, p2], "VERIFIED", copies=(HOME,))
    return p1, p2, p3


@pytest.fixture
def conn():
    c = get_memory_connection()
    create_all(c)
    register_repo(c, "family", "family", "/mnt/mirror/family")
    yield c
    c.close()


def _views(conn, location):
    """(held, missing) as each of the three views sees *location*."""
    summary = {s["location"]: s for s in get_location_summary(conn)}[location]
    held = get_packs_at_location(conn, location)
    missing = {p.pack_id for p in get_packs_missing_at_location(conn, location)}
    for_location = {
        p.pack_id for p in get_unarchived_or_missing_at_location(conn, location)
    }
    return summary, held, missing, for_location


@pytest.mark.parametrize(
    ("location", "held_count", "missing_count"),
    [(HOME, 2, 0), (OFFSITE, 0, 2), (VAULT, 0, 2)],
)
def test_three_views_agree_after_consolidation(conn, location, held_count, missing_count):
    _consolidated(conn)

    summary, held, missing, for_location = _views(conn, location)

    assert summary["packs"] == len(held) == held_count
    assert summary["missing"] == len(missing) == len(for_location) == missing_count
    assert missing == for_location


def test_location_with_no_disc_shows_every_archived_pack_missing(conn):
    p1, p2, _ = _consolidated(conn)

    vault = {s["location"]: s for s in get_location_summary(conn)}[VAULT]

    assert vault == {"location": VAULT, "volumes": 0, "packs": 0, "missing": 2}
    assert {p.pack_id for p in get_packs_missing_at_location(conn, VAULT)} == {
        p1.pack_id, p2.pack_id,
    }


def test_deprecated_volume_disc_holds_nothing(conn):
    _consolidated(conn)
    offsite = {s["location"]: s for s in get_location_summary(conn)}[OFFSITE]
    assert offsite["volumes"] == 0
    assert get_packs_at_location(conn, OFFSITE) == set()


def test_pruned_and_staging_only_packs_are_neither_held_nor_missing(conn):
    _, _, p3 = _consolidated(conn)
    p4 = register_pack(conn, "d" * 64, 100, "family")
    _volume(conn, "S", [p4], "STAGING")

    for loc in (HOME, OFFSITE, VAULT):
        held = get_packs_at_location(conn, loc)
        missing = {p.pack_id for p in get_packs_missing_at_location(conn, loc)}
        assert p3.pack_id not in held | missing
        assert p4.pack_id not in held | missing
        summary = {s["location"]: s for s in get_location_summary(conn)}[loc]
        assert summary["packs"] + summary["missing"] == 2

    # stage --for-location still picks up the never-burned pack (it is
    # unarchived), never the pruned one.
    picked = {p.pack_id for p in get_unarchived_or_missing_at_location(conn, OFFSITE)}
    assert p4.pack_id in picked
    assert p3.pack_id not in picked


def test_location_list_does_not_call_an_empty_location_current(tmp_path, capsys):
    db_path = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{tmp_path}"\nstaging = "{tmp_path}"\n'
        f'database = "{db_path}"\n'
    )
    main(["init", "--db-path", str(db_path)])
    c = get_connection(db_path)
    create_all(c)
    register_repo(c, "family", "family", "/mnt/mirror/family")
    _consolidated(c)
    c.close()
    capsys.readouterr()

    assert main(["--config", str(cfg), "location", "list"]) == 0

    lines = {
        ln.split()[0]: ln for ln in capsys.readouterr().out.splitlines()
        if ln.split()
    }
    assert "all current" in lines[HOME]
    assert "2 packs behind" in lines[OFFSITE]
    assert "2 packs behind" in lines[VAULT]


# ---------------------------------------------------------------------------
# #679: the redundancy report counts discs of durable volumes only
# ---------------------------------------------------------------------------


def test_redundancy_ignores_a_staging_volume(conn):
    create_location(conn, HOME)
    both = register_pack(conn, "e" * 64, 100, "family")
    staged_only = register_pack(conn, "f" * 64, 100, "family")
    _volume(conn, "V", [both], "VERIFIED", copies=(HOME,))
    staging = _volume(conn, "S", [both, staged_only], "STAGING")

    under_two = {p.pack_id for p in get_redundancy_report(conn, min_copies=2)}
    under_one = {p.pack_id for p in get_redundancy_report(conn, min_copies=1)}

    # One copy: under 2, not under 1.
    assert both.pack_id in under_two
    assert both.pack_id not in under_one
    # Zero copies.
    assert staged_only.pack_id in under_one

    live = get_live_volumes_for_packs(conn, [both.pack_id, staged_only.pack_id])
    assert [v.label for v in live.get(both.pack_id, [])] == ["V"]
    assert staging.volume_id not in {
        v.volume_id for vols in live.values() for v in vols
    }
