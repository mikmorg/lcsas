"""Tests for the ``lcsas verify`` command (``lcsas.cli.commands.verify``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.schema import create_all
from lcsas.db.volumes import create_volume


class TestCmdVerify:
    def test_verify_unknown_volume(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--db", str(db), "verify", "NONEXIST"])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_verify_disc_calls_runner(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "u1", "BD25", 25e9, "Home", "VERIFIED")
        conn.close()

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True) as mock_verify,
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
                  return_value="VOL_001"),
        ):
            result = main(["--db", str(db), "verify", "VOL_001", "--disc"])

        assert result == 0
        mock_verify.assert_called_once()

    def test_verify_mark_verified(self, tmp_path, capsys):
        """--mark-verified records VERIFY_PASS event and promotes BURNED."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_MV", "u1", "BD25", 25e9, "Home", "BURNED")
        conn.close()

        # #673: the volume has no disc recorded, so --location is needed.
        result = main(["--db", str(db), "verify", "VOL_MV",
                        "--mark-verified", "--detail", "Verified on remote machine",
                        "--location", "Home"])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_MV")
        assert vol.status == "VERIFIED"
        assert vol.closed_at is not None

        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")
        assert len(events) == 1
        assert "remote" in events[0].detail.lower()
        conn.close()

    def test_verify_mark_failed(self, tmp_path, capsys):
        """--mark-failed records VERIFY_FAIL event."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_MF", "u1", "BD25", 25e9, "Home", "VERIFIED")
        conn.close()

        result = main(["--db", str(db), "verify", "VOL_MF",
                        "--mark-failed", "--detail", "Sector errors at offset 0x400"])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_MF")
        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_FAIL")
        assert len(events) == 1
        assert "Sector errors" in events[0].detail
        conn.close()

    def test_verify_no_label_no_all_returns_error(self, tmp_path, capsys):
        """Omitting volume label without --all returns error."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--db", str(db), "verify"])
        assert result == 1

    def test_verify_all_skipped_returns_error(self, tmp_path, capsys):
        """When --all runs but all ISOs are cleaned, return 1 (not success)."""
        from lcsas.db.sessions import add_session_volume, create_session

        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        vol = create_volume(conn, "VOL_SKIP", "u1", "BD25", 25e9,
                            "Home", "VERIFIED")
        # Register session volume pointing to a nonexistent ISO
        create_session(conn, media_type="BD25", staging_dir="/tmp",
                       session_id="sess1")
        add_session_volume(conn, "sess1", vol.volume_id,
                           iso_path="/nonexistent/path.iso",
                           iso_sha256="")
        conn.close()

        result = main(["--db", str(db), "verify", "--all"])
        assert result == 1
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "No volumes" in out

    def test_verify_all_location_filter_only_verifies_matching(
        self, tmp_path, capsys,
    ):
        """`verify --all --location <name>` only verifies volumes with a
        copy at the named location.

        Issue #40: ``_verify_all`` filters candidates by checking each
        volume's ``volume_copies`` rows for a matching ``location``. This
        test seeds two volumes — one with a copy at location ``A``, one
        with a copy at location ``B`` — then invokes ``verify --all
        --location A`` and asserts:

          * the ECC verifier is called for the ``A`` volume only,
          * a ``VERIFY_PASS`` event lands on the ``A`` volume only,
          * the ``B`` volume receives no verify event.
        """
        from lcsas.db.locations import ensure_location
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volume_events import get_events_for_volume

        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)

        # Two ISO files that exist so _verify_all doesn't short-circuit.
        iso_a = tmp_path / "vol_a.iso"
        iso_b = tmp_path / "vol_b.iso"
        iso_a.write_bytes(b"\0" * 4096)
        iso_b.write_bytes(b"\0" * 4096)

        # FK from volume_copies.location → locations.name requires the
        # locations rows to exist first.
        ensure_location(conn, "A")
        ensure_location(conn, "B")

        vol_a = create_volume(conn, "VOL_A", "uuid-a", "BD25", 25e9,
                              "A", "BURNED")
        vol_b = create_volume(conn, "VOL_B", "uuid-b", "BD25", 25e9,
                              "B", "BURNED")
        # Volume copies pinning each volume to its location. The
        # `--location` filter consults volume_copies, not the volume's
        # own `location` field.
        add_volume_copy(conn, vol_a.volume_id, "A")
        add_volume_copy(conn, vol_b.volume_id, "B")

        create_session(conn, media_type="BD25", staging_dir="/tmp",
                       session_id="sess1")
        add_session_volume(conn, "sess1", vol_a.volume_id,
                           iso_path=str(iso_a), iso_sha256="")
        add_session_volume(conn, "sess1", vol_b.volume_id,
                           iso_path=str(iso_b), iso_sha256="")
        conn.close()

        # Track every ISO the verifier is invoked against.
        verified_paths: list[Path] = []

        def fake_verify_iso(self, iso_path, *args, **kwargs):
            verified_paths.append(Path(iso_path))
            return True

        with patch(
            "lcsas.ecc.dvdisaster.SubprocessDVDisasterRunner.verify_iso",
            fake_verify_iso,
        ):
            result = main(
                ["--db", str(db), "verify", "--all", "--location", "A"],
            )

        assert result == 0
        # Only the A-located volume's ISO was verified.
        assert verified_paths == [iso_a]

        # And only the A volume got a VERIFY_PASS event.
        conn = get_connection(db)
        a_events = get_events_for_volume(
            conn, vol_a.volume_id, "VERIFY_PASS",
        )
        b_events_pass = get_events_for_volume(
            conn, vol_b.volume_id, "VERIFY_PASS",
        )
        b_events_fail = get_events_for_volume(
            conn, vol_b.volume_id, "VERIFY_FAIL",
        )
        conn.close()

        assert len(a_events) == 1
        assert "Batch ISO verify" in a_events[0].detail
        assert b_events_pass == []
        assert b_events_fail == []

    def test_verify_not_implemented(self, capsys):
        result = main(["verify", "SOME_LABEL"])
        assert result == 1


class TestVerifyAllShaFallback:
    """Phase 21.5.a — `verify --all` falls back to SHA-256 when dvdisaster
    isn't on PATH (same pattern as cmd_verify's ISO mode in Phase 21.3)."""

    def _seed(self, db_path, *, iso_path, iso_sha256, label):
        """Helper: build a one-volume catalog where _verify_all will
        find ``iso_path`` and (optionally) a recorded ``iso_sha256``."""
        from lcsas.db.locations import ensure_location
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volume_copies import add_volume_copy

        conn = get_connection(db_path)
        try:
            create_all(conn)
            ensure_location(conn, "Home")
            vol = create_volume(
                conn, label, "u-" + label, "BD25", 25_000_000_000,
                "Home", "BURNED",
            )
            create_session(
                conn, media_type="BD25", staging_dir="/tmp",
                session_id="sess-" + label,
            )
            add_session_volume(
                conn, "sess-" + label, vol.volume_id,
                iso_path=str(iso_path), iso_sha256=iso_sha256,
            )
            add_volume_copy(
                conn, vol.volume_id, "Home", iso_sha256=iso_sha256,
            )
            return vol
        finally:
            conn.close()

    def test_verify_all_falls_back_when_dvdisaster_missing(
        self, tmp_path, capsys,
    ):
        """`verify --all` on a host without dvdisaster verifies each
        volume's ISO via SHA-256 instead.  No FileNotFoundError leak."""
        from lcsas.utils.hashing import sha256_file

        db = tmp_path / "test.db"
        iso = tmp_path / "v.iso"
        iso.write_bytes(b"some iso content for batch verify" * 100)
        sha = sha256_file(iso)
        self._seed(db, iso_path=iso, iso_sha256=sha, label="VOL_BATCH")

        with patch(
            "shutil.which",
            side_effect=lambda name: (
                None if name in ("dvdisaster", "lcsas-ecc")
                else "/usr/bin/" + name
            ),
        ):
            result = main(["--db", str(db), "verify", "--all"])

        # SHA matched → exit 0 with PASS.
        assert result == 0
        out = capsys.readouterr().out
        assert "VOL_BATCH: PASS" in out
        # And the "falling back" line must appear at least once.
        assert "falling back" in out.lower() or "SHA-256" in out

    def test_verify_all_skips_volumes_with_no_recorded_sha(
        self, tmp_path, capsys,
    ):
        """If dvdisaster is gone AND the catalog has no hash for a
        volume, that volume is skipped (not silently passed and not
        crashing the rest of the batch).
        """
        db = tmp_path / "test.db"
        iso = tmp_path / "v.iso"
        iso.write_bytes(b"x" * 4096)
        # Seed with empty hash — no SHA available for verification.
        self._seed(db, iso_path=iso, iso_sha256="", label="VOL_NOHASH")

        with patch(
            "shutil.which",
            side_effect=lambda name: (
                None if name in ("dvdisaster", "lcsas-ecc")
                else "/usr/bin/" + name
            ),
        ):
            result = main(["--db", str(db), "verify", "--all"])

        # No verifications performed → "all skipped" exit path (rc=1
        # so operators notice).
        assert result == 1
        out = capsys.readouterr().out
        assert "skipped" in out.lower()
        assert "no recorded SHA-256" in out or "VOL_NOHASH" in out


class TestCmdVerifyShaFallback:
    """Phase 21.3 — cmd_verify falls back to SHA-256 when dvdisaster
    isn't installed on the host."""

    def _seed_volume_with_iso(self, db_path, *, iso_path, iso_sha256):
        """Helper: create a volume + session_volumes row pointing at
        iso_path so cmd_verify's ISO mode picks it up.  Also adds a
        volume_copies row carrying ``iso_sha256`` for the lookup
        path Phase 21.3 wires.
        """
        from lcsas.db.locations import ensure_location
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volume_copies import add_volume_copy

        conn = get_connection(db_path)
        try:
            create_all(conn)
            ensure_location(conn, "Home")
            vol = create_volume(
                conn, "VOL_SHA", "u-sha", "BD25", 25_000_000_000,
                "Home", "BURNED",
            )
            create_session(conn, media_type="BD25", staging_dir="/tmp",
                           session_id="sess-sha")
            add_session_volume(
                conn, "sess-sha", vol.volume_id,
                iso_path=str(iso_path), iso_sha256=iso_sha256,
            )
            add_volume_copy(
                conn, vol.volume_id, "Home", iso_sha256=iso_sha256,
            )
            return vol
        finally:
            conn.close()

    def test_dvdisaster_missing_falls_back_to_sha(self, tmp_path, capsys):
        """When dvdisaster raises FileNotFoundError, cmd_verify resorts
        to ``verify_iso_sha256`` against the catalog-recorded hash."""
        from lcsas.utils.hashing import sha256_file

        db = tmp_path / "test.db"
        iso = tmp_path / "vol.iso"
        iso.write_bytes(b"some fake iso bytes for sha test" * 50)
        sha = sha256_file(iso)
        self._seed_volume_with_iso(db, iso_path=iso, iso_sha256=sha)

        # Make the dvdisaster runner pretend it's not installed.
        with patch(
            "lcsas.ecc.dvdisaster.SubprocessDVDisasterRunner.verify_iso",
            side_effect=FileNotFoundError("dvdisaster not installed"),
        ):
            result = main(["--db", str(db), "verify", "VOL_SHA"])

        # SHA matched the catalog → verify passes overall.
        assert result == 0
        out = capsys.readouterr().out
        # The "falling back" log line must appear.
        assert "falling back" in out.lower() or "SHA-256" in out

    def test_dvdisaster_missing_with_bad_sha_fails(self, tmp_path, capsys):
        """SHA fallback that catches actual corruption → verify fails
        with rc=1, not silently passes."""
        db = tmp_path / "test.db"
        iso = tmp_path / "vol.iso"
        iso.write_bytes(b"good bytes" * 1000)
        # Seed a DIFFERENT hash so the SHA compare fails.
        self._seed_volume_with_iso(db, iso_path=iso, iso_sha256="9" * 64)

        with patch(
            "lcsas.ecc.dvdisaster.SubprocessDVDisasterRunner.verify_iso",
            side_effect=FileNotFoundError("dvdisaster not installed"),
        ):
            result = main(["--db", str(db), "verify", "VOL_SHA"])

        assert result == 1
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_dvdisaster_missing_no_recorded_sha_returns_error(
        self, tmp_path, capsys,
    ):
        """When dvdisaster is gone AND the catalog has no hash for this
        volume, cmd_verify exits non-zero with an explanatory message
        (rather than silently passing or hanging)."""
        db = tmp_path / "test.db"
        iso = tmp_path / "vol.iso"
        iso.write_bytes(b"fake" * 100)
        # Seed WITHOUT any hash — both session_volumes and volume_copies
        # carry an empty string.
        self._seed_volume_with_iso(db, iso_path=iso, iso_sha256="")

        with patch(
            "lcsas.ecc.dvdisaster.SubprocessDVDisasterRunner.verify_iso",
            side_effect=FileNotFoundError("dvdisaster not installed"),
        ):
            result = main(["--db", str(db), "verify", "VOL_SHA"])

        assert result == 1
        out = capsys.readouterr().out
        assert (
            "No recorded ISO SHA-256" in out
            or "cannot verify without dvdisaster" in out.lower()
        )


class TestVerifyDiscIdentityAndEvidence:
    """FMA-03: ``lcsas verify <LABEL> --disc`` must prove the disc in
    the drive IS the requested volume (PVD Volume ID), source its hash
    evidence from session_volumes OR volume_copies, and downgrade
    loudly — never silently — on catalogs that predate the evidence."""

    def _make_burned_volume(
        self,
        db_path,
        label: str = "VOL_A",
        *,
        iso_sha256: str | None = None,
        iso_size_bytes: int | None = None,
        iso_path: str = "/gone/away.iso",
    ) -> int:
        """Create a catalog with one BURNED volume; optionally a
        session_volumes row carrying hash/size evidence."""
        from lcsas.db.connection import get_connection
        from lcsas.db.schema import create_all
        from lcsas.db.sessions import add_session_volume, create_session
        from lcsas.db.volumes import create_volume

        conn = get_connection(db_path)
        create_all(conn)
        vol = create_volume(conn, label, f"uuid-{label}", "TEST_TINY",
                            2_097_152, "Home", "BURNED")
        if iso_sha256 is not None:
            session = create_session(conn, media_type="TEST_TINY",
                                     staging_dir="/tmp")
            add_session_volume(
                conn, session_id=session.session_id,
                volume_id=vol.volume_id, iso_path=iso_path,
                iso_sha256=iso_sha256, iso_size_bytes=iso_size_bytes,
            )
        conn.commit()
        conn.close()
        return vol.volume_id

    def test_verify_disc_rejects_wrong_volume(self, tmp_path, caplog):
        """Wrong disc in the drive: exit 1, BOTH labels named, and
        NOTHING recorded — it is operator error, not evidence about
        this volume."""
        from unittest.mock import patch

        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_label

        db = tmp_path / "test.db"
        vol_id = self._make_burned_volume(
            db, "VOL_A", iso_sha256="a" * 64, iso_size_bytes=100,
        )

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value="VOL_B"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True) as mock_check,
        ):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", "/dev/null"])

        assert result == 1
        assert "VOL_B" in caplog.text and "VOL_A" in caplog.text
        assert "Wrong disc" in caplog.text
        # The identity gate fires before any other check runs.
        mock_check.assert_not_called()

        conn = get_connection(db)
        # Nothing recorded: no events, no promotion.
        events = conn.execute(
            "SELECT COUNT(*) FROM volume_events WHERE volume_id = ?",
            (vol_id,),
        ).fetchone()[0]
        assert events == 0
        assert get_volume_by_label(conn, "VOL_A").status == "BURNED"
        conn.close()

    def test_verify_disc_unreadable_volume_id_records_nothing(
        self, tmp_path, caplog,
    ):
        """No readable PVD (blank/absent disc): exit 1, nothing recorded."""
        from unittest.mock import patch

        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_label

        db = tmp_path / "test.db"
        vol_id = self._make_burned_volume(
            db, "VOL_A", iso_sha256="a" * 64, iso_size_bytes=100,
        )

        with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                   "read_disc_volume_id", return_value=""):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", "/dev/null"])

        assert result == 1
        assert "Could not read a Volume ID" in caplog.text

        conn = get_connection(db)
        events = conn.execute(
            "SELECT COUNT(*) FROM volume_events WHERE volume_id = ?",
            (vol_id,),
        ).fetchone()[0]
        assert events == 0
        assert get_volume_by_label(conn, "VOL_A").status == "BURNED"
        conn.close()

    def test_verify_disc_downgrade_warning_on_old_catalog(
        self, tmp_path, caplog,
    ):
        """Pre-v7 catalog row (hash recorded, size NULL, ISO gone):
        explicit downgrade warning, VERIFY_FAIL, no promotion — never
        a silent pass on -check_media alone."""
        from unittest.mock import patch

        from lcsas.db.connection import get_connection
        from lcsas.db.volume_events import get_events_for_volume
        from lcsas.db.volumes import get_volume_by_label

        db = tmp_path / "test.db"
        vol_id = self._make_burned_volume(
            db, "VOL_A", iso_sha256="a" * 64, iso_size_bytes=None,
        )

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value="VOL_A"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
        ):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", "/dev/null"])

        assert result == 1
        assert "cannot device-verify" in caplog.text

        conn = get_connection(db)
        assert get_volume_by_label(conn, "VOL_A").status == "BURNED"
        assert get_events_for_volume(conn, vol_id, "VERIFY_FAIL")
        conn.close()

    def test_verify_disc_no_hash_warns_readability_only(
        self, tmp_path, caplog,
    ):
        """Catalog rows that never carried a hash (pre-Phase-13):
        readability-only verification with an explicit warning."""
        from unittest.mock import patch

        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_label

        db = tmp_path / "test.db"
        self._make_burned_volume(db, "VOL_A", iso_sha256=None)

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value="VOL_A"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
        ):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", "/dev/null"])

        assert result == 0
        assert "No recorded ISO SHA-256" in caplog.text

        conn = get_connection(db)
        assert get_volume_by_label(conn, "VOL_A").status == "VERIFIED"
        conn.close()

    def test_verify_disc_uses_copy_evidence_on_rebuilt_catalog(
        self, tmp_path,
    ):
        """A catalog rebuilt from disc copies has no session_volumes
        rows — the hash + size on volume_copies (schema v8) must still
        enable the full device compare."""
        import hashlib
        from unittest.mock import patch

        from lcsas.db.connection import get_connection
        from lcsas.db.volume_copies import add_volume_copy
        from lcsas.db.volumes import get_volume_by_label

        db = tmp_path / "test.db"
        vol_id = self._make_burned_volume(db, "VOL_A", iso_sha256=None)

        # "Device" whose bytes ARE the recorded image.
        disc_bytes = b"image-bytes" * 64
        device = tmp_path / "fake_device"
        device.write_bytes(disc_bytes)

        conn = get_connection(db)
        from lcsas.db.locations import ensure_location
        ensure_location(conn, "Vault")
        add_volume_copy(
            conn, volume_id=vol_id, location="Vault",
            iso_sha256=hashlib.sha256(disc_bytes).hexdigest(),
            iso_size_bytes=len(disc_bytes),
        )
        conn.close()

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value="VOL_A"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
        ):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", str(device)])

        assert result == 0
        conn = get_connection(db)
        assert get_volume_by_label(conn, "VOL_A").status == "VERIFIED"
        conn.close()

        # And a tampered device fails against the same copy evidence.
        device.write_bytes(b"x" + disc_bytes[1:])
        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value="VOL_A"),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
        ):
            result = main(["--db", str(db), "verify", "VOL_A", "--disc",
                           "--device", str(device)])
        assert result == 1


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


def test_verify_disc_stamps_last_verified_at(tmp_path):
    """FMA-05: a passing ``verify --disc`` stamps last_verified_at on
    the volume's single ACTIVE copy WITHOUT an explicit --location; a
    failing verify leaves the stamp NULL (and records VERIFY_FAIL)."""
    from unittest.mock import patch

    from lcsas.db.connection import get_connection
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volume_events import get_events_for_volume

    disc_bytes = b"fma05-payload" * 64
    device = tmp_path / "fake_device"
    device.write_bytes(disc_bytes)

    db = tmp_path / "pass.db"
    ids = _fma05_catalog(db, {"VOL_A": ("Vault", disc_bytes)})

    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value="VOL_A"),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
    ):
        assert main(["--db", str(db), "verify", "VOL_A", "--disc",
                     "--device", str(device)]) == 0

    conn = get_connection(db)
    (copy,) = get_copies_for_volume(conn, ids["VOL_A"])
    assert copy.location == "Vault"
    assert copy.last_verified_at is not None, (
        "a passing disc verify must stamp the copy"
    )
    conn.close()

    # FAIL: same catalog shape, tampered device — stamp must stay NULL.
    db_fail = tmp_path / "fail.db"
    ids = _fma05_catalog(db_fail, {"VOL_A": ("Vault", disc_bytes)})
    device.write_bytes(b"X" + disc_bytes[1:])

    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value="VOL_A"),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
    ):
        assert main(["--db", str(db_fail), "verify", "VOL_A", "--disc",
                     "--device", str(device)]) == 1

    conn = get_connection(db_fail)
    (copy,) = get_copies_for_volume(conn, ids["VOL_A"])
    assert copy.last_verified_at is None, (
        "a FAILED disc verify must never stamp the copy"
    )
    assert get_events_for_volume(conn, ids["VOL_A"], "VERIFY_FAIL")
    conn.close()


def test_verify_disc_ambiguous_copies_require_location(tmp_path, caplog):
    """FMA-05: with ACTIVE copies at several locations and no
    --location, verify --disc refuses up front (before any device
    read) — the stamp must never guess which disc was in the drive."""
    from unittest.mock import patch

    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.volume_copies import add_volume_copy

    disc_bytes = b"fma05-payload" * 64
    device = tmp_path / "fake_device"
    device.write_bytes(disc_bytes)
    db = tmp_path / "test.db"
    ids = _fma05_catalog(db, {"VOL_A": ("Vault", disc_bytes)})

    conn = get_connection(db)
    ensure_location(conn, "Offsite")
    add_volume_copy(conn, volume_id=ids["VOL_A"], location="Offsite")
    conn.close()

    with patch(
        "lcsas.iso.xorriso.SubprocessXorrisoRunner.read_disc_volume_id",
        return_value="VOL_A",
    ) as mock_id:
        assert main(["--db", str(db), "verify", "VOL_A", "--disc",
                     "--device", str(device)]) == 1
    assert "pass --location" in caplog.text
    mock_id.assert_not_called()


def test_verify_all_disc_iterates_copies(tmp_path, capsys):
    """FMA-05: ``verify --all --disc`` walks ACTIVE copies with one
    prompt per disc — PASS stamps the copy, FAIL records VERIFY_FAIL
    without stamping, 's' skips — and prints the result table."""
    from unittest.mock import patch

    from lcsas.db.connection import get_connection
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volume_events import get_events_for_volume
    from lcsas.db.volumes import get_volume_by_label

    disc_bytes = b"fma05-batch" * 64
    device = tmp_path / "fake_device"
    device.write_bytes(disc_bytes)

    db = tmp_path / "test.db"
    ids = _fma05_catalog(db, {
        "VOL_A": ("Vault", disc_bytes),         # device matches → PASS
        "VOL_B": ("Vault", b"other-image"),     # hash mismatch → FAIL
        "VOL_C": ("Vault", disc_bytes),         # operator skips
    })

    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", side_effect=["VOL_A", "VOL_B"]),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
        patch("builtins.input", side_effect=["", "", "s"]),
    ):
        rc = main(["--db", str(db), "verify", "--all", "--disc",
                   "--device", str(device)])
    assert rc == 1, "one FAIL must make the batch exit non-zero"

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Batch disc verification results:" in out
    assert "1 passed, 1 failed, 1 skipped" in out

    conn = get_connection(db)
    (copy_a,) = get_copies_for_volume(conn, ids["VOL_A"])
    assert copy_a.last_verified_at is not None
    assert get_volume_by_label(conn, "VOL_A").status == "VERIFIED"

    (copy_b,) = get_copies_for_volume(conn, ids["VOL_B"])
    assert copy_b.last_verified_at is None
    assert get_volume_by_label(conn, "VOL_B").status == "BURNED"
    assert get_events_for_volume(conn, ids["VOL_B"], "VERIFY_FAIL")

    (copy_c,) = get_copies_for_volume(conn, ids["VOL_C"])
    assert copy_c.last_verified_at is None
    assert not get_events_for_volume(conn, ids["VOL_C"], "VERIFY_PASS")
    assert not get_events_for_volume(conn, ids["VOL_C"], "VERIFY_FAIL")
    conn.close()


def test_verify_all_disc_wrong_disc_records_nothing(tmp_path, capsys):
    """FMA-05 + FMA-03: in batch mode a wrong disc in the drive is
    operator error — counted SKIPPED, no event, no stamp."""
    from unittest.mock import patch

    from lcsas.db.connection import get_connection
    from lcsas.db.volume_copies import get_copies_for_volume

    disc_bytes = b"fma05-wrong" * 64
    device = tmp_path / "fake_device"
    device.write_bytes(disc_bytes)
    db = tmp_path / "test.db"
    ids = _fma05_catalog(db, {"VOL_A": ("Vault", disc_bytes)})

    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value="VOL_OTHER"),
        patch("builtins.input", side_effect=[""]),
    ):
        rc = main(["--db", str(db), "verify", "--all", "--disc",
                   "--device", str(device)])
    assert rc == 1, "nothing verified → non-zero"

    captured = capsys.readouterr()
    assert "Wrong disc" in captured.out + captured.err

    conn = get_connection(db)
    (copy,) = get_copies_for_volume(conn, ids["VOL_A"])
    assert copy.last_verified_at is None
    n_events = conn.execute(
        "SELECT COUNT(*) FROM volume_events WHERE volume_id = ?",
        (ids["VOL_A"],),
    ).fetchone()[0]
    assert n_events == 0, "wrong disc is operator error — nothing recorded"
    conn.close()
