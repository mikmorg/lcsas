"""Tests for the ``lcsas status`` command (``lcsas.cli.commands.status``)."""

from __future__ import annotations

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.schema import create_all
from lcsas.db.volumes import create_volume


class TestCmdStatus:
    def test_status_with_data(self, tmp_path, capsys):
        """Status with repos shows counts."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "fam", "/mnt/fam"])
        capsys.readouterr()

        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Packs:" in out


class TestCmdStatusEdges:
    def test_status_with_volumes(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "uuid1", "TEST_TINY", 1000000, "Home", "VERIFIED")
        conn.close()

        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Volumes:" in out

    def test_status_fails_and_creates_nothing_at_unknown_path(
        self, tmp_path, capsys
    ):
        """`status` against a fresh DB path must FAIL, not auto-create.

        FUP-02 mitigation 4: read-only commands are genuinely read-only — they
        no longer call ``create_all`` as a side effect (which used to write
        outside the catalog lock and masked a mistyped path).  A missing
        catalog yields a non-zero exit and the "Run `lcsas init`" hint.
        """
        db = tmp_path / "fresh-subdir" / "archive.db"
        assert not db.exists()
        assert not db.parent.exists()

        result = main(["--db", str(db), "status"])
        assert result != 0
        out = capsys.readouterr()
        assert "lcsas init" in (out.out + out.err)
        # The read-only command must NOT have created the DB or its parent.
        assert not db.exists(), "status created a catalog as a side effect"

    def test_status_fails_without_catalog(self, tmp_path, capsys):
        db = tmp_path / "nope.db"
        result = main(["--db", str(db), "status"])
        assert result != 0
        combined = capsys.readouterr()
        text = combined.out + combined.err
        assert "lcsas init" in text
        # The read-only command must NOT have created the catalog.
        assert not db.exists(), "status created a catalog as a side effect"


def _seed_ghost_volume(db: str, *, label: str = "GHOST_VOL",
                       age_hours: int = 48) -> None:
    """Seed a stale never-burned STAGING volume with two linked packs."""
    from lcsas.db.connection import get_connection
    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    conn = get_connection(db)
    try:
        register_repo(conn, "r1", "R1", "/mnt/r1")
        p1 = register_pack(conn, sha256="a" * 64, size_bytes=100,
                           repo_id="r1")
        p2 = register_pack(conn, sha256="b" * 64, size_bytes=200,
                           repo_id="r1")
        vol = create_volume(
            conn, label=label, uuid=f"uuid-{label}",
            media_type="TEST_TINY", capacity_bytes=2_097_152,
            status="STAGING",
        )
        bulk_link_packs(conn, vol.volume_id, [p1.pack_id, p2.pack_id])
        conn.execute(
            "UPDATE volumes SET created_at = datetime('now', ?) "
            "WHERE volume_id = ?",
            (f"-{int(age_hours)} hours", vol.volume_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_status_reports_staged_unburned_bucket(tmp_path, capsys):
    """`lcsas status` shows the staged bucket + never-burned warning.
    [FMA-01]"""
    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    capsys.readouterr()

    result = main(["--db", str(db), "status"])
    assert result == 0
    out = capsys.readouterr().out
    assert "2 staged (NOT yet on disc)" in out
    assert "never" in out and "burned" in out
    assert "catalog reconcile" in out


def _give_staged_iso(db: str, tmp_path, label: str = "GHOST_VOL") -> object:
    """Record a burn session for ``label`` whose ISO exists on disk."""
    from lcsas.db.connection import get_connection
    from lcsas.db.sessions import add_session_volume, create_session
    from lcsas.db.volumes import get_volume_by_label

    session_dir = tmp_path / "staging" / f"sess-{label}"
    session_dir.mkdir(parents=True)
    iso = session_dir / f"{label}.iso"
    iso.write_bytes(b"\x00" * 64)
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        create_session(conn, media_type="TEST_TINY",
                       staging_dir=str(session_dir), session_id=f"sess-{label}")
        add_session_volume(conn, f"sess-{label}", vol.volume_id, str(iso),
                           iso_sha256="c" * 64, iso_size_bytes=64)
    finally:
        conn.close()
    return iso


def test_status_says_waiting_not_reconcile_while_the_iso_exists(tmp_path, capsys):
    """#647: a staged volume whose ISO is still in the staging area is
    waiting to be burned; status must not point at `catalog reconcile`."""
    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    _give_staged_iso(str(db), tmp_path)
    capsys.readouterr()

    assert main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "2 staged (NOT yet on disc)" in out
    assert "waiting to be burned" in out and "GHOST_VOL" in out
    assert "import-receipts" in out
    assert "reconcile" not in out


def test_status_names_what_reconcile_fix_deletes_once_the_iso_is_gone(
    tmp_path, capsys,
):
    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    _seed_ghost_volume(str(db))
    _give_staged_iso(str(db), tmp_path).unlink()
    capsys.readouterr()

    assert main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "ISOs are gone: GHOST_VOL" in out
    assert "'lcsas catalog reconcile --fix' deletes them" in out
    assert "waiting to be burned" not in out


def _fma05_catalog(db_path, volumes):
    """Create a catalog of BURNED volumes, one ACTIVE copy each.

    *volumes* maps label -> (location, disc_bytes); each copy row
    carries the SHA-256 + size of *disc_bytes* as its evidence (the
    rebuilt-catalog shape: no session_volumes rows).  Returns
    label -> volume_id.
    """
    import hashlib

    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.schema import create_all
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import create_volume

    conn = get_connection(db_path)
    create_all(conn)
    ids = {}
    for label, (location, disc_bytes) in volumes.items():
        vol = create_volume(conn, label, f"uuid-{label}", "TEST_TINY",
                            2_097_152, "Home", "BURNED")
        ensure_location(conn, location)
        add_volume_copy(
            conn, volume_id=vol.volume_id, location=location,
            iso_sha256=hashlib.sha256(disc_bytes).hexdigest(),
            iso_size_bytes=len(disc_bytes),
        )
        ids[label] = vol.volume_id
    conn.commit()
    conn.close()
    return ids


def test_status_stale_copies_report(tmp_path, capsys):
    """FMA-05: ``status --stale-copies`` lists never-verified and
    overdue copies with ages; fresh copies are absent; plain ``status``
    carries a one-line summary warning."""
    from datetime import UTC, datetime, timedelta

    from lcsas.db.connection import get_connection

    disc_bytes = b"fma05-stale"
    db = tmp_path / "test.db"
    _fma05_catalog(db, {
        "VOL_NEVER": ("Vault", disc_bytes),
        "VOL_OLD": ("Vault", disc_bytes),
        "VOL_FRESH": ("Vault", disc_bytes),
    })
    conn = get_connection(db)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    conn.execute(
        "UPDATE volume_copies SET last_verified_at = ? WHERE volume_id = "
        "(SELECT volume_id FROM volumes WHERE label = 'VOL_OLD')", (old,),
    )
    conn.execute(
        "UPDATE volume_copies SET last_verified_at = ? WHERE volume_id = "
        "(SELECT volume_id FROM volumes WHERE label = 'VOL_FRESH')",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()
    conn.close()

    assert main(["--db", str(db), "status", "--stale-copies"]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "VOL_NEVER" in out and "never verified" in out
    assert "VOL_OLD" in out and "400 days ago" in out
    assert "VOL_FRESH" not in out, "freshly verified copies are not stale"
    assert "lcsas verify --all --disc" in out

    # Plain status: one summary line when anything is overdue.
    assert main(["--db", str(db), "status"]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "2 disc(s) not verified in the last 365 days" in out

    # Tighter threshold pulls the fresh copy in as well.
    assert main(["--db", str(db), "status", "--stale-copies",
                 "--older-than-days", "0"]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "VOL_FRESH" in out


def _blast_radius_catalog(db_path):
    """Catalog with one single-copy disc and one doubly-copied disc.

    VOL_SINGLE (VERIFIED, 1 ACTIVE copy)  : sole holder of packs s1, s2
    VOL_DOUBLE (VERIFIED, 2 ACTIVE copies): holds pack d1 (2 physical
        discs → already redundant) plus pack shared1, which also lives
        on VOL_SINGLE.
    Returns ``{label: volume_id}``.
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import create_location
    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume
    from lcsas.utils.labels import generate_uuid

    conn = get_connection(str(db_path))
    try:
        create_all(conn)
        create_location(conn, "Home_Shelf")
        create_location(conn, "Offsite_Safe")
        register_repo(conn, "repo-photos", "photos", "/mnt/mirror/photos")
        register_repo(conn, "repo-docs", "docs", "/mnt/mirror/docs")

        vols = {}
        for label in ("VOL_SINGLE", "VOL_DOUBLE"):
            vols[label] = create_volume(
                conn, label=label, uuid=generate_uuid(),
                media_type="BD25", capacity_bytes=25_000_000_000,
                status="VERIFIED",
            ).volume_id
        add_volume_copy(conn, vols["VOL_SINGLE"], "Home_Shelf")
        add_volume_copy(conn, vols["VOL_DOUBLE"], "Home_Shelf")
        add_volume_copy(conn, vols["VOL_DOUBLE"], "Offsite_Safe")

        packs = {
            "s1": register_pack(conn, sha256="51" * 32,
                                size_bytes=1_000_000_000,
                                repo_id="repo-photos"),
            "s2": register_pack(conn, sha256="52" * 32,
                                size_bytes=500_000_000, repo_id="repo-docs"),
            "d1": register_pack(conn, sha256="d1" * 32,
                                size_bytes=2_000_000_000,
                                repo_id="repo-photos"),
            "shared1": register_pack(conn, sha256="ee" * 32,
                                     size_bytes=250_000_000,
                                     repo_id="repo-photos"),
        }
        bulk_link_packs(conn, vols["VOL_SINGLE"],
                        [packs["s1"].pack_id, packs["s2"].pack_id,
                         packs["shared1"].pack_id])
        bulk_link_packs(conn, vols["VOL_DOUBLE"],
                        [packs["d1"].pack_id, packs["shared1"].pack_id])
        return vols
    finally:
        conn.close()


class TestStatusRedundancy:
    def test_status_redundancy_lists_single_copy_packs(self, tmp_path, capsys):
        """`status --redundancy` groups under-replicated packs by holding
        disc; a volume with >=2 ACTIVE copies is real redundancy and must
        be absent (copies are counted, not volume rows)."""
        db = tmp_path / "test.db"
        _blast_radius_catalog(db)

        assert main(["--db", str(db), "status", "--redundancy"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err

        assert "VOL_SINGLE" in out
        # s1 + s2 are sole-holder packs on VOL_SINGLE (1.50 GB).
        assert "only durable holder of 2 pack(s) (1.50 GB)" in out
        # d1 has two ACTIVE physical copies and shared1 lives on both
        # volumes — neither is under-replicated, so VOL_DOUBLE never
        # appears as a single point of failure.
        assert "VOL_DOUBLE" not in out

    def test_status_redundancy_all_replicated(self, tmp_path, capsys):
        """No under-replicated packs → a clean all-clear message."""
        from lcsas.db.connection import get_connection

        db = tmp_path / "test.db"
        vols = _blast_radius_catalog(db)
        conn = get_connection(str(db))
        try:
            # Give VOL_SINGLE a second physical copy → everything >= 2.
            from lcsas.db.volume_copies import add_volume_copy
            add_volume_copy(conn, vols["VOL_SINGLE"], "Offsite_Safe")
        finally:
            conn.close()

        assert main(["--db", str(db), "status", "--redundancy"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "All packs have at least 2 live cop(ies)" in out
        assert "VOL_SINGLE" not in out

    def test_status_warns_on_under_replication(self, tmp_path, capsys):
        """Default `status` prints the one-line warning iff some pack
        exists on only one disc."""
        from lcsas.db.connection import get_connection

        db = tmp_path / "test.db"
        vols = _blast_radius_catalog(db)

        assert main(["--db", str(db), "status"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "exist on only one disc" in out
        assert "lcsas status --redundancy" in out

        # Fully replicate VOL_SINGLE → the warning must disappear.
        conn = get_connection(str(db))
        try:
            from lcsas.db.volume_copies import add_volume_copy
            add_volume_copy(conn, vols["VOL_SINGLE"], "Offsite_Safe")
        finally:
            conn.close()

        assert main(["--db", str(db), "status"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "exist on only one disc" not in out


def test_status_shows_where_the_discs_are(tmp_path, capsys):
    """#643: the volume list shows the locations of a volume's ACTIVE discs,
    "(for X)" for one still being staged for X, never just volumes.location."""
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import create_volume

    db = tmp_path / "test.db"
    main(["init", "--db-path", str(db)])
    conn = get_connection(str(db))
    try:
        for loc in ("Home_Shelf", "Bank_Box"):
            ensure_location(conn, loc)
        burned = create_volume(conn, "BURNED_VOL", "u-1", "TEST_TINY", 1000,
                               location="Home_Shelf", status="VERIFIED")
        add_volume_copy(conn, burned.volume_id, "Bank_Box")
        create_volume(conn, "WAITING_VOL", "u-2", "TEST_TINY", 1000,
                      location="Bank_Box", status="STAGING")
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["--db", str(db), "status"]) == 0
    lines = {ln.split()[0]: ln for ln in capsys.readouterr().out.splitlines()
             if ln.strip().startswith(("BURNED_VOL", "WAITING_VOL"))}
    assert lines["BURNED_VOL"].rstrip().endswith("Bank_Box")
    assert "Home_Shelf" not in lines["BURNED_VOL"]
    assert lines["WAITING_VOL"].rstrip().endswith("(for Bank_Box)")
