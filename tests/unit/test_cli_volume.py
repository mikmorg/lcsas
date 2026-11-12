"""Tests for the ``lcsas volume`` command (``lcsas.cli.commands.volume``)."""

from __future__ import annotations

from lcsas.cli.main import main


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


class TestVolumeImpact:
    def test_volume_impact_lists_at_risk_packs(self, tmp_path, capsys):
        """`volume impact` reports only the packs whose sole live home is
        the named volume, grouped by repo with byte totals."""
        db = tmp_path / "test.db"
        _blast_radius_catalog(db)

        assert main(["--db", str(db), "volume", "impact", "VOL_SINGLE"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err

        # s1 (photos, 1 GB) + s2 (docs, 0.5 GB) at risk; shared1 lives
        # on VOL_DOUBLE too, so it must NOT count.
        assert "2 pack(s) (1.50 GB) become unrestorable" in out
        assert "repo photos" in out and "1 pack(s) (1.00 GB)" in out
        assert "repo docs" in out and "1 pack(s) (0.50 GB)" in out
        # Copy inventory with verification age (FMA-05 tie-in).
        assert "Home_Shelf" in out and "never verified" in out

    def test_volume_impact_no_unique_data(self, tmp_path, capsys):
        """A disc whose packs all have other live homes reports NONE."""
        from lcsas.db.connection import get_connection
        from lcsas.db.volume_packs import bulk_link_packs

        db = tmp_path / "test.db"
        vols = _blast_radius_catalog(db)
        conn = get_connection(str(db))
        try:
            # Move d1's uniqueness away: link it onto VOL_SINGLE as well.
            row = conn.execute(
                "SELECT pack_id FROM packs WHERE sha256 = ?", ("d1" * 32,)
            ).fetchone()
            bulk_link_packs(conn, vols["VOL_SINGLE"], [row["pack_id"]])
        finally:
            conn.close()

        assert main(["--db", str(db), "volume", "impact", "VOL_DOUBLE"]) == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "Blast radius: NONE" in out

    def test_volume_impact_unknown_volume_errors(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        _blast_radius_catalog(db)
        assert main(["--db", str(db), "volume", "impact", "NO_SUCH"]) == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out + captured.err


def _write_impact_config(tmp_path, *, with_mirror: bool) -> str:
    """TOML config wiring repo 'photos' to a mirror + password file."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror" / "photos"
    if with_mirror:
        mirror.mkdir(parents=True, exist_ok=True)
    pw = tmp_path / "photos.key"
    pw.write_text("secret\n")
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{tmp_path / 'mirror'}"
staging = "{staging}"
database = "{tmp_path / 'test.db'}"

[repos.photos]
mirror_path = "{mirror}"
password_file = "{pw}"
""")
    return str(cfg)


class TestVolumeImpactSnapshots:
    def _seed_snapshots(self, db):
        from lcsas.db.connection import get_connection
        from lcsas.db.snapshots import upsert_snapshot

        conn = get_connection(str(db))
        try:
            upsert_snapshot(conn, "snapS", "repo-photos",
                            hostname="nas", timestamp="2026-01-01T00:00:00Z",
                            paths='["/home/pics"]')
            upsert_snapshot(conn, "snapT", "repo-photos",
                            hostname="nas", timestamp="2026-02-01T00:00:00Z",
                            paths='["/home/other"]')
        finally:
            conn.close()

    def test_volume_impact_snapshots_at_risk(self, tmp_path, capsys):
        """--snapshots lists exactly the snapshots whose required packs
        intersect the at-risk set (snapshot→pack mapping via the same
        restore_dry_run machinery the restore planner uses)."""
        from unittest.mock import MagicMock, patch

        from lcsas.rustic.types import RestorePlan

        db = tmp_path / "test.db"
        _blast_radius_catalog(db)
        self._seed_snapshots(db)
        cfg = _write_impact_config(tmp_path, with_mirror=True)

        def _dry_run(snapshot_id, repo_path, password_file):
            if snapshot_id == "snapS":
                # snapS needs s1 — the at-risk pack on VOL_SINGLE.
                return RestorePlan(snapshot_id=snapshot_id,
                                   required_pack_hashes=["51" * 32])
            # snapT only needs the doubly-held pack.
            return RestorePlan(snapshot_id=snapshot_id,
                               required_pack_hashes=["d1" * 32])

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.side_effect = _dry_run

        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner",
                  return_value=mock_runner),
            patch("lcsas.utils.subprocess.check_binary_version",
                  return_value="0.9.0"),
        ):
            result = main(["--config", cfg, "--db", str(db),
                           "volume", "impact", "VOL_SINGLE", "--snapshots"])

        assert result == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "snapS" in out
        assert "/home/pics" in out
        assert "would become unrestorable" in out
        # snapT references no at-risk pack — it must not be listed.
        assert "snapT" not in out
        # The docs repo has no at-risk... (docs HAS s2 at risk but no
        # config entry) → degrade message for it, never an exception.
        assert "catalog-only" in out

    def test_volume_impact_snapshots_degrades_without_mirror(
        self, tmp_path, capsys
    ):
        """Mirror not mounted → clear degrade message, exit 0, and the
        pack-level report still prints."""
        from unittest.mock import MagicMock, patch

        db = tmp_path / "test.db"
        _blast_radius_catalog(db)
        self._seed_snapshots(db)
        cfg = _write_impact_config(tmp_path, with_mirror=False)

        mock_runner = MagicMock()
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner",
                  return_value=mock_runner),
            patch("lcsas.utils.subprocess.check_binary_version",
                  return_value="0.9.0"),
        ):
            result = main(["--config", cfg, "--db", str(db),
                           "volume", "impact", "VOL_SINGLE", "--snapshots"])

        assert result == 0
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert ("snapshot impact needs the live mirror — pack-level "
                "report above is catalog-only") in out
        assert "2 pack(s) (1.50 GB) become unrestorable" in out
        mock_runner.restore_dry_run.assert_not_called()
