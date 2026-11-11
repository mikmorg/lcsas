"""Error-path tests for ``lcsas catalog import-receipts`` (issue #517).

``docs/workflows/burn-iso-portable.md`` promises that a bad receipt is
skipped with a warning and the rest of the batch still imports.  Nothing
pinned that.  These tests cover:

* malformed / unreadable receipt files (truncated, empty, garbage, missing,
  a directory);
* receipts missing ``volume_label`` / ``location``;
* an orphan receipt (volume label not in the catalog);
* mixed batches -- the bad receipt is skipped, the good one still imports,
  whichever order they arrive in.

They assert exit codes and, via a before/after dump of the catalog tables,
that a skipped receipt writes nothing at all.  Since #669 every skipped or
refused receipt makes the exit code non-zero, while the good receipts of
the batch are still imported.  Nothing here depends on ``verify_passed``
semantics: good receipts target an already-VERIFIED volume (the "re-burn to
a new location" path), so only the copy row and the
``BURN_RECEIPT_IMPORTED`` event are asserted.

The #666 / #669 / #670 / #671 cases are at the end of the file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import get_copies_for_volume
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import create_volume, get_volume_by_label

_TABLES = ("volumes", "volume_copies", "volume_events", "locations")


def _write_config(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{tmp_path / "archive.db"}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    return cfg


# #669: a receipt must name a registered location.
_LOCATIONS = ("Home", "Offsite", "Vault", "Vault_B", "Vault_C", "Bank_Box",
              "Home_Shelf")


def _make_catalog(tmp_path: Path, labels: dict[str, str],
                  staged_for: str = "Home") -> Path:
    from lcsas.db.locations import ensure_location

    db = tmp_path / "archive.db"
    conn = get_connection(db)
    create_all(conn)
    for name in _LOCATIONS:
        ensure_location(conn, name)
    for i, (label, status) in enumerate(labels.items()):
        create_volume(conn, label, f"uuid-{i}", "TEST_TINY", 1000, staged_for, status)
    conn.close()
    return db


def _dump(db: Path) -> dict[str, list[tuple]]:
    """Every row of the tables an import could touch, for before/after equality."""
    conn = get_connection(db)
    try:
        return {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()]
            for t in _TABLES
        }
    finally:
        conn.close()


def _receipt(tmp_path: Path, name: str, body: dict | str | bytes) -> Path:
    p = tmp_path / name
    if isinstance(body, bytes):
        p.write_bytes(body)
    elif isinstance(body, str):
        p.write_text(body)
    else:
        p.write_text(json.dumps(body))
    return p


def _good(label: str, location: str = "Offsite") -> dict:
    return {
        "volume_label": label,
        "location": location,
        "session_id": "s1",
        "device": "/dev/sr0",
        "burn_date": "2026-04-14T12:00:00+00:00",
        "iso_sha256": "a" * 64,
        "iso_size_bytes": 2048,
        "verify_passed": True,
    }


def _run(cfg: Path, *files: Path, flags: tuple[str, ...] = ()) -> int:
    return main(["--config", str(cfg), "catalog", "import-receipts",
                 *flags, *[str(f) for f in files]])


def _assert_imported(db: Path, label: str, location: str, expected_events: int = 1) -> None:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        copies = get_copies_for_volume(conn, vol.volume_id)
        assert [c.location for c in copies if c.location == location], (
            f"{label}: no copy recorded at {location}"
        )
        events = get_events_for_volume(conn, vol.volume_id, "BURN_RECEIPT_IMPORTED")
        assert len(events) == expected_events
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    """Config + catalog holding one VERIFIED volume, VOL_GOOD."""
    cfg = _write_config(tmp_path)
    db = _make_catalog(tmp_path, {"VOL_GOOD": "VERIFIED", "VOL_OTHER": "VERIFIED"})
    return tmp_path, cfg, db


# ---------------------------------------------------------------------------
# Single bad receipt: skipped, warned, nothing written
# ---------------------------------------------------------------------------

_MALFORMED = {
    "truncated": '{"volume_label": "VOL_GOOD", "location": "Off',
    "empty-file": "",
    "not-json": "this is not a receipt",
    "trailing-comma": '{"volume_label": "VOL_GOOD", "location": "Offsite",}',
}


class TestMalformedReceiptFile:
    @pytest.mark.parametrize("kind", sorted(_MALFORMED))
    def test_malformed_json_is_skipped_with_warning(self, env, capsys, kind):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "bad.json", _MALFORMED[kind])
        before = _dump(db)

        rc = _run(cfg, bad)

        out = capsys.readouterr().out
        assert rc == 1  # #669: a skipped receipt is a non-zero exit
        assert "Failed to read receipt" in out
        assert str(bad) in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_nonexistent_file_is_skipped_with_warning(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, tmp_path / "never-written.json")

        out = capsys.readouterr().out
        assert rc == 1
        assert "Failed to read receipt" in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_a_directory_in_place_of_a_receipt_is_skipped(self, env, capsys):
        tmp_path, cfg, db = env
        (tmp_path / "adir").mkdir()
        before = _dump(db)

        rc = _run(cfg, tmp_path / "adir")

        assert rc == 1
        assert "Failed to read receipt" in capsys.readouterr().out
        assert _dump(db) == before

    @pytest.mark.parametrize(
        ("body", "message"),
        [("[]", "not a JSON object"), ('"just a string"', "not a JSON object"),
         ("{}", "missing keys")],
    )
    def test_json_that_is_not_a_receipt_object_is_skipped(
        self, env, capsys, body, message
    ):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "weird.json", body))

        out = capsys.readouterr().out
        assert rc == 1
        assert message in out
        assert _dump(db) == before

    @pytest.mark.parametrize(
        "raw",
        [b"\xff\xfe\x00\x01\x80garbage", '{"volume_label": "VOL_GOOD"}'.encode("utf-16")],
        ids=["binary-garbage", "utf16"],
    )
    def test_undecodable_file_is_skipped_and_later_receipts_still_import(
        self, env, capsys, raw
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "undecodable.json", raw)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    @pytest.mark.parametrize("body", ["null", "42", "true"])
    def test_scalar_json_is_skipped_and_later_receipts_still_import(
        self, env, capsys, body
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "scalar.json", body)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    @pytest.mark.parametrize("size", ["abc", -5, True, [2048], {"n": 1}, "2.5"])
    def test_invalid_size_is_skipped_and_writes_nothing(self, env, capsys, size):
        """#522: the bad receipt writes nothing -- not even its location row."""
        tmp_path, cfg, db = env
        before = _dump(db)
        bad = _receipt(tmp_path, "badsize.json",
                       {**_good("VOL_OTHER", "Vault_Q"), "iso_size_bytes": size})
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        assert _run(cfg, bad, good) == 1

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert "invalid iso_size_bytes" in out
        assert not [row for row in _dump(db)["locations"] if "Vault_Q" in row]
        assert _dump(db)["volume_copies"] != before["volume_copies"]
        _assert_imported(db, "VOL_GOOD", "Offsite")
        conn = get_connection(db)
        try:
            other = get_volume_by_label(conn, "VOL_OTHER")
            assert other is not None
            assert get_copies_for_volume(conn, other.volume_id) == []
        finally:
            conn.close()

    def test_digit_string_size_is_recorded_as_an_integer(self, env, capsys):
        tmp_path, cfg, db = env
        rc = _run(cfg, _receipt(tmp_path, "r.json",
                                {**_good("VOL_GOOD"), "iso_size_bytes": "4096"}))

        assert rc == 0
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_GOOD")
            assert vol is not None
            sizes = [c.iso_size_bytes for c in get_copies_for_volume(conn, vol.volume_id)]
        finally:
            conn.close()
        assert sizes == [4096]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("volume_label", 42), ("volume_label", ""), ("location", ["Offsite"]),
         ("location", "   "), ("location", None)],
    )
    def test_non_string_or_blank_label_or_location_is_skipped(
        self, env, capsys, field, value
    ):
        tmp_path, cfg, db = env
        before = _dump(db)
        bad = _receipt(tmp_path, "bad.json", {**_good("VOL_OTHER"), field: value})

        assert _run(cfg, bad) == 1

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert f"invalid {field}" in out
        assert _dump(db) == before


class TestMissingKeys:
    @pytest.mark.parametrize(
        ("body", "named"),
        [
            ({"location": "Offsite"}, ["volume_label"]),
            ({"volume_label": "VOL_GOOD"}, ["location"]),
            ({"session_id": "s1", "verify_passed": True}, ["volume_label", "location"]),
        ],
        ids=["no-volume_label", "no-location", "neither"],
    )
    def test_missing_required_key_is_skipped_and_named(self, env, capsys, body, named):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "incomplete.json", body))

        out = capsys.readouterr().out
        assert rc == 1
        assert "missing keys" in out
        for key in named:
            assert key in out
        # The key that IS present must not be reported as missing.
        present = {"volume_label", "location"} - set(named)
        line = next(ln for ln in out.splitlines() if "missing keys" in ln)
        for key in present:
            assert key not in line.split("missing keys:")[1]
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_optional_keys_are_not_required(self, env, capsys):
        """Only volume_label, location and (#669) verify_passed are needed;
        everything else defaults."""
        tmp_path, cfg, db = env
        minimal = _receipt(tmp_path, "min.json",
                           {"volume_label": "VOL_GOOD", "location": "Offsite",
                            "verify_passed": True})

        assert _run(cfg, minimal) == 0

        assert "Imported 1 receipt(s)." in capsys.readouterr().out
        _assert_imported(db, "VOL_GOOD", "Offsite")


class TestOrphanReceipt:
    def test_unknown_volume_label_is_skipped_and_writes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)
        orphan = _receipt(tmp_path, "orphan.json", _good("VOL_NOT_IN_CATALOG", "Vault_Z"))

        rc = _run(cfg, orphan)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Volume 'VOL_NOT_IN_CATALOG' not found" in out
        assert "Imported 0 receipt(s)." in out
        after = _dump(db)
        assert after == before
        # In particular the receipt's location was NOT created as a side effect.
        assert not [row for row in after["locations"] if "Vault_Z" in row]
        # ... and no volume was invented for the orphan label.
        conn = get_connection(db)
        try:
            assert get_volume_by_label(conn, "VOL_NOT_IN_CATALOG") is None
        finally:
            conn.close()

    def test_label_match_is_exact_not_prefix(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        # VOL_GOOD exists; VOL_GOO and VOL_GOOD2 do not.
        _run(cfg, _receipt(tmp_path, "a.json", _good("VOL_GOO")),
             _receipt(tmp_path, "b.json", _good("VOL_GOOD2")))

        out = capsys.readouterr().out
        assert out.count("not found, skipping") == 2
        assert _dump(db) == before


# ---------------------------------------------------------------------------
# Mixed batches
# ---------------------------------------------------------------------------

_BAD_FILES = {
    "malformed": lambda tp: _receipt(tp, "bad_malformed.json", '{"volume_label": '),
    "missing-keys": lambda tp: _receipt(tp, "bad_keys.json", {"volume_label": "VOL_OTHER"}),
    "orphan": lambda tp: _receipt(tp, "bad_orphan.json", _good("VOL_GHOST", "Nowhere")),
    "nonexistent": lambda tp: tp / "bad_missing_file.json",
}


class TestMixedBatch:
    @pytest.mark.parametrize("kind", sorted(_BAD_FILES))
    @pytest.mark.parametrize("bad_first", [True, False], ids=["bad-first", "bad-last"])
    def test_bad_receipt_is_skipped_good_one_still_imports(self, env, capsys, kind, bad_first):
        tmp_path, cfg, db = env
        bad = _BAD_FILES[kind](tmp_path)
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD", "Offsite"))
        files = (bad, good) if bad_first else (good, bad)

        rc = _run(cfg, *files)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_GOOD", "Offsite")
        # The bad receipt left no trace: no event on the volume it named, and
        # nothing for the other seeded volume either.
        conn = get_connection(db)
        try:
            other = get_volume_by_label(conn, "VOL_OTHER")
            assert get_copies_for_volume(conn, other.volume_id) == []
            assert get_events_for_volume(conn, other.volume_id, "BURN_RECEIPT_IMPORTED") == []
            locs = {row[0] for row in conn.execute("SELECT name FROM locations").fetchall()}
            assert "Nowhere" not in locs
        finally:
            conn.close()

    def test_bad_receipts_sandwiching_two_good_ones(self, env, capsys):
        tmp_path, cfg, db = env
        files = [
            _BAD_FILES["malformed"](tmp_path),
            _receipt(tmp_path, "g1.json", _good("VOL_GOOD", "Offsite")),
            _BAD_FILES["orphan"](tmp_path),
            _receipt(tmp_path, "g2.json", _good("VOL_OTHER", "Vault")),
            _BAD_FILES["missing-keys"](tmp_path),
        ]

        rc = _run(cfg, *files)

        assert rc == 1
        assert "Imported 2 receipt(s)." in capsys.readouterr().out
        _assert_imported(db, "VOL_GOOD", "Offsite")
        _assert_imported(db, "VOL_OTHER", "Vault")

    def test_all_bad_batch_exits_nonzero_and_changes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, *[make(tmp_path) for make in _BAD_FILES.values()])

        out = capsys.readouterr().out
        assert rc == 1  # #669: any refused receipt is a non-zero exit
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_hash_mismatch_exits_nonzero_but_does_not_stop_the_batch(self, env, capsys):
        """A rejected receipt flips the exit code, yet the good receipt after
        it is still committed and the rejected one wrote nothing."""
        tmp_path, cfg, db = env
        first = _receipt(tmp_path, "first.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, first) == 0
        capsys.readouterr()

        tampered = dict(_good("VOL_GOOD", "Vault_B"), iso_sha256="b" * 64)
        rejected = _receipt(tmp_path, "tampered.json", tampered)
        fine = _receipt(tmp_path, "fine.json", _good("VOL_OTHER", "Vault_C"))

        rc = _run(cfg, rejected, fine)

        out = capsys.readouterr().out
        assert rc == 1
        assert "does not match previously recorded hash" in out
        assert "Rejected 1 receipt(s)" in out
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_OTHER", "Vault_C")
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_GOOD")
            # Still exactly the one event from the first, honest import.
            assert len(get_events_for_volume(conn, vol.volume_id, "BURN_RECEIPT_IMPORTED")) == 1
            assert [c.location for c in get_copies_for_volume(conn, vol.volume_id)] == ["Offsite"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# #646: a verified receipt records the disc as verified
# ---------------------------------------------------------------------------


class TestReceiptLastVerified:
    def _last_verified(self, db: Path, label: str, location: str) -> str | None:
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, label)
            assert vol is not None
            (copy,) = [c for c in get_copies_for_volume(conn, vol.volume_id)
                       if c.location == location]
            return copy.last_verified_at
        finally:
            conn.close()

    def test_verified_receipt_sets_last_verified_and_status_is_quiet(
        self, env, capsys,
    ):
        from datetime import UTC, datetime

        from lcsas.db.volume_copies import find_stale_copies

        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"),
                    burn_date=datetime.now(UTC).isoformat())
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") == body["burn_date"]
        conn = get_connection(db)
        try:
            assert find_stale_copies(conn) == []
        finally:
            conn.close()
        capsys.readouterr()
        assert main(["--config", str(cfg), "status"]) == 0
        assert "not verified" not in capsys.readouterr().out

    def test_unverified_receipt_leaves_it_null(self, env):
        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"), verify_passed=None)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is None

    def test_reburn_without_verify_clears_the_old_verification(self, env):
        """The row now describes a new, unread disc.  (#666: the re-burn has
        a later burn date; the same burn date and hash would be the same
        receipt imported again.)"""
        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "a.json", _good("VOL_GOOD", "Bank_Box"))) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is not None
        again = dict(_good("VOL_GOOD", "Bank_Box"), verify_passed=None,
                     burn_date="2026-05-01T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "b.json", again)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") is None

    @pytest.mark.parametrize("burn_date, expected", [
        ("2026-04-14T14:00:00+02:00", "2026-04-14T12:00:00+00:00"),
        ("2026-04-14T12:00:00", "2026-04-14T12:00:00+00:00"),
    ])
    def test_burn_date_is_normalised_to_utc(self, env, burn_date, expected):
        tmp_path, cfg, db = env
        body = dict(_good("VOL_GOOD", "Bank_Box"), burn_date=burn_date)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert self._last_verified(db, "VOL_GOOD", "Bank_Box") == expected

    @pytest.mark.parametrize("burn_date", ["", "not a date", "2999-01-01T00:00:00+00:00"])
    def test_missing_bad_or_future_burn_date_uses_the_import_time(
        self, env, burn_date,
    ):
        from datetime import UTC, datetime

        tmp_path, cfg, db = env
        before = datetime.now(UTC).isoformat()
        body = dict(_good("VOL_GOOD", "Bank_Box"), burn_date=burn_date)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        got = self._last_verified(db, "VOL_GOOD", "Bank_Box")
        assert got is not None and before <= got <= datetime.now(UTC).isoformat()




# ---------------------------------------------------------------------------
# #644: the receipt must name the ISO that was staged
# ---------------------------------------------------------------------------


def _stage_record(db: Path, label: str, sha: str | None, size: int | None,
                  session_id: str = "sess-1") -> None:
    """Record a burn session for ``label`` as `lcsas stage` would."""
    from lcsas.db.sessions import add_session_volume, create_session

    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        create_session(conn, "TEST_TINY", "/staging", session_id=session_id)
        add_session_volume(conn, session_id, vol.volume_id, f"/staging/{label}.iso",
                           iso_sha256=sha, iso_size_bytes=size)
    finally:
        conn.close()


@pytest.fixture
def staged_env(tmp_path):
    """One STAGING volume whose ISO was recorded as ("a"*64, 2048 bytes)."""
    cfg = _write_config(tmp_path)
    db = _make_catalog(tmp_path, {"VOL_NEW": "STAGING"}, staged_for="Bank_Box")
    _stage_record(db, "VOL_NEW", "a" * 64, 2048)
    return tmp_path, cfg, db


def _status(db: Path, label: str) -> str:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        return vol.status
    finally:
        conn.close()


class TestStagedIsoCheck:
    def test_matching_hash_imports_and_verifies(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "SKIPPED" not in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_first_receipt_with_a_different_hash_is_rejected(self, staged_env, capsys):
        """The case #644 is about: no earlier receipt to compare with."""
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="c" * 64)
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad))
        out = capsys.readouterr().out
        assert rc == 1
        assert "does not match the ISO staged" in out
        assert "c" * 64 in out and "a" * 64 in out
        assert "Rejected 1 receipt(s)" in out
        assert _dump(db) == before
        assert _status(db, "VOL_NEW") == "STAGING"

    def test_different_size_is_rejected(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_size_bytes=4096)
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad))
        out = capsys.readouterr().out
        assert rc == 1
        assert "iso_size_bytes 4096" in out
        assert _dump(db) == before

    def test_mismatch_does_not_stop_the_batch(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        bad = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="c" * 64)
        good = _good("VOL_NEW", "Bank_Box")
        rc = _run(cfg, _receipt(tmp_path, "bad.json", bad),
                  _receipt(tmp_path, "good.json", good))
        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")

    def test_latest_session_wins(self, staged_env, capsys):
        """A volume re-mastered in a later session is checked against the newer ISO."""
        tmp_path, cfg, db = staged_env
        conn = get_connection(db)
        try:
            conn.execute("UPDATE burn_sessions SET created_at = '2000-01-01T00:00:00'")
            conn.commit()
        finally:
            conn.close()
        _stage_record(db, "VOL_NEW", "d" * 64, 2048, session_id="sess-2")
        rc = _run(cfg, _receipt(tmp_path, "old.json", _good("VOL_NEW", "Bank_Box")))
        assert rc == 1
        assert "does not match the ISO staged" in capsys.readouterr().out
        newer = dict(_good("VOL_NEW", "Bank_Box"), iso_sha256="d" * 64)
        assert _run(cfg, _receipt(tmp_path, "new.json", newer)) == 0

    def test_receipt_without_hash_warns_that_the_check_was_skipped(
        self, staged_env, capsys,
    ):
        tmp_path, cfg, db = staged_env
        body = _good("VOL_NEW", "Bank_Box")
        del body["iso_sha256"]
        nohash = _receipt(tmp_path, "nohash.json", body)
        # #669: without a hash the volume does not become VERIFIED ...
        before = _dump(db)
        assert _run(cfg, nohash) == 1
        out = capsys.readouterr().out
        assert "carries no iso_sha256" in out and "--allow-unhashed" in out
        assert _dump(db) == before
        # ... unless the owner says so.
        rc = _run(cfg, nohash, flags=("--allow-unhashed",))
        out = capsys.readouterr().out
        assert rc == 0
        assert "carries no iso_sha256" in out and "SKIPPED" in out
        _assert_imported(db, "VOL_NEW", "Bank_Box")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_volume_without_staged_hash_warns_that_the_check_was_skipped(
        self, env, capsys,
    ):
        """env's volumes have no burn session at all (e.g. an older catalog)."""
        tmp_path, cfg, db = env
        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Bank_Box")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "no ISO hash was recorded" in out and "SKIPPED" in out
        _assert_imported(db, "VOL_GOOD", "Bank_Box")

    def test_empty_staged_hash_counts_as_none(self, tmp_path, capsys):
        """session_volumes rows start with iso_sha256='' before the ISO exists."""
        cfg = _write_config(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": "STAGING"}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "", None)
        r = _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box"))
        assert _run(cfg, r) == 1
        assert "no ISO hash was recorded" in capsys.readouterr().out
        assert _run(cfg, r, flags=("--allow-unhashed",)) == 0
        assert "no ISO hash was recorded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #666: an old receipt imported again changes nothing
# ---------------------------------------------------------------------------


def _copy(db: Path, label: str, location: str):
    from lcsas.db.volume_copies import get_copy_at_location

    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        return get_copy_at_location(conn, vol.volume_id, location)
    finally:
        conn.close()


class TestReimport:
    def test_same_receipt_twice_changes_nothing_the_second_time(self, env, capsys):
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        # Something the owner wrote on the disc record since.
        conn = get_connection(db)
        try:
            conn.execute("UPDATE volume_copies SET notes = 'shelf 3'")
            conn.commit()
        finally:
            conn.close()
        capsys.readouterr()
        before = _dump(db)

        assert _run(cfg, r) == 0

        out = capsys.readouterr().out
        assert "already imported" in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before  # last_verified_at and notes untouched
        assert _copy(db, "VOL_GOOD", "Offsite").notes == "shelf 3"

    def test_receipt_imported_before_burn_date_was_recorded_is_seen(self, env, capsys):
        """A receipt imported before #666 has no burn_date in its event; the
        disc record's burn date stands in."""
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT event_id, detail FROM volume_events "
                "WHERE event_type = 'BURN_RECEIPT_IMPORTED'"
            ).fetchone()
            detail = json.loads(row["detail"])
            del detail["burn_date"]
            conn.execute("UPDATE volume_events SET detail = ? WHERE event_id = ?",
                         (json.dumps(detail), row["event_id"]))
            conn.commit()
        finally:
            conn.close()
        capsys.readouterr()
        before = _dump(db)
        assert _run(cfg, r) == 0
        assert "already imported" in capsys.readouterr().out
        assert _dump(db) == before

    def test_lost_disc_stays_destroyed_when_its_receipt_is_imported_again(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        r = _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))
        assert _run(cfg, r) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        capsys.readouterr()
        before = _dump(db)

        assert _run(cfg, r) == 0

        out = capsys.readouterr().out
        assert "already imported" in out and "stays so" in out
        assert _dump(db) == before
        assert _copy(db, "VOL_GOOD", "Offsite").status == "DESTROYED"

    def test_other_receipt_not_newer_than_the_loss_is_refused(self, env, capsys):
        """A receipt that was never imported, for a disc burned before the
        loss was recorded, cannot bring the lost disc back either."""
        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        capsys.readouterr()
        before = _dump(db)
        older = dict(_good("VOL_GOOD", "Offsite"),
                     burn_date="2026-04-15T12:00:00+00:00")

        assert _run(cfg, _receipt(tmp_path, "older.json", older)) == 1

        out = capsys.readouterr().out
        assert "is not later" in out and "It stays DESTROYED" in out
        assert _dump(db) == before

    def test_reburn_after_the_loss_puts_a_disc_back_in_service(self, env, capsys):
        from datetime import UTC, datetime

        tmp_path, cfg, db = env
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Offsite"))) == 0
        assert main(["--config", str(cfg), "copy", "destroy", "VOL_GOOD",
                     "Offsite", "--lost"]) == 0
        reburn = dict(_good("VOL_GOOD", "Offsite"),
                      burn_date=datetime.now(UTC).isoformat())

        assert _run(cfg, _receipt(tmp_path, "reburn.json", reburn)) == 0

        copy = _copy(db, "VOL_GOOD", "Offsite")
        assert copy.status == "ACTIVE"
        assert copy.burn_date == reburn["burn_date"]


# ---------------------------------------------------------------------------
# #669: bad receipts are refused, and the exit code says so
# ---------------------------------------------------------------------------


class TestStrictReceipt:
    @pytest.mark.parametrize("value", ["false", "true", "", 0, 1, [], {}])
    def test_non_boolean_verify_passed_is_refused(self, staged_env, capsys, value):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=value)

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 1

        out = capsys.readouterr().out
        assert "verify_passed must be JSON true, false or null" in out
        assert _dump(db) == before
        assert _status(db, "VOL_NEW") == "STAGING"

    def test_missing_verify_passed_is_refused(self, staged_env, capsys):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        body = _good("VOL_NEW", "Bank_Box")
        del body["verify_passed"]

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 1

        assert "got nothing" in capsys.readouterr().out
        assert _dump(db) == before

    def test_unknown_location_is_refused_and_known_ones_are_listed(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_GOOD", "Ofsite")))

        out = capsys.readouterr().out
        assert rc == 1
        assert "unknown location 'Ofsite'" in out
        assert "Did you mean: Offsite?" in out
        assert "Known locations: " + ", ".join(sorted(_LOCATIONS)) in out
        assert "lcsas location add Ofsite" in out
        assert _dump(db) == before  # no location was created

    def test_refused_receipt_exits_nonzero_but_valid_ones_still_import(
        self, env, capsys,
    ):
        tmp_path, cfg, db = env
        bad = _receipt(tmp_path, "bad.json",
                       dict(_good("VOL_OTHER", "Vault"), verify_passed="false"))
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD", "Offsite"))

        rc = _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert rc == 1
        assert "Imported 1 receipt(s)." in out and "Rejected 1 receipt(s)" in out
        _assert_imported(db, "VOL_GOOD", "Offsite")

    def test_location_other_than_staged_for_is_refused_unless_allowed(
        self, staged_env, capsys,
    ):
        tmp_path, cfg, db = staged_env
        before = _dump(db)
        r = _receipt(tmp_path, "r.json", _good("VOL_NEW", "Vault"))

        assert _run(cfg, r) == 1

        out = capsys.readouterr().out
        assert "was staged for location Bank_Box" in out
        assert "--allow-other-location" in out
        assert _dump(db) == before

        assert _run(cfg, r, flags=("--allow-other-location",)) == 0
        _assert_imported(db, "VOL_NEW", "Vault")
        assert _status(db, "VOL_NEW") == "VERIFIED"

    def test_volume_without_a_staged_for_location_is_not_checked(self, tmp_path, capsys):
        """Older volumes may have no volumes.location: nothing to compare."""
        cfg = _write_config(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_OLD": "STAGING"}, staged_for="")
        _stage_record(db, "VOL_OLD", "a" * 64, 2048)
        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_OLD", "Vault"))) == 0
        assert _status(db, "VOL_OLD") == "VERIFIED"


# ---------------------------------------------------------------------------
# #670: STAGING, BURNING and BURNED are treated alike
# ---------------------------------------------------------------------------


class TestFirstDiscStates:
    @pytest.mark.parametrize("start", ["STAGING", "BURNING", "BURNED"])
    def test_passing_receipt_verifies_and_closes_the_volume(self, tmp_path, start):
        from lcsas.db.packs import register_pack
        from lcsas.db.queries import get_unarchived_packs
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        cfg = _write_config(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": start}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        conn = get_connection(db)
        try:
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        finally:
            conn.close()

        assert _run(cfg, _receipt(tmp_path, "r.json", _good("VOL_NEW", "Bank_Box"))) == 0

        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_NEW")
            assert vol.status == "VERIFIED"
            assert vol.closed_at is not None
            assert len(get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")) == 1
            # `lcsas stage` does not select its packs again.
            assert pack.pack_id not in {p.pack_id for p in get_unarchived_packs(conn)}
        finally:
            conn.close()

    @pytest.mark.parametrize("start", ["BURNING", "BURNED"])
    def test_failed_receipt_without_a_disc_returns_the_volume_to_staging(
        self, tmp_path, start,
    ):
        cfg = _write_config(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": start}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)

        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0

        assert _status(db, "VOL_NEW") == "STAGING"
        conn = get_connection(db)
        try:
            vol = get_volume_by_label(conn, "VOL_NEW")
            assert len(get_events_for_volume(conn, vol.volume_id, "VERIFY_FAIL")) == 1
        finally:
            conn.close()

    def test_unverified_receipt_for_a_burned_volume_keeps_it_burned(self, tmp_path):
        cfg = _write_config(tmp_path)
        db = _make_catalog(tmp_path, {"VOL_NEW": "BURNED"}, staged_for="Bank_Box")
        _stage_record(db, "VOL_NEW", "a" * 64, 2048)
        body = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=None)
        assert _run(cfg, _receipt(tmp_path, "r.json", body)) == 0
        assert _status(db, "VOL_NEW") == "BURNED"


# ---------------------------------------------------------------------------
# #671: a failed burn shows under "needs re-burn"; one next step everywhere
# ---------------------------------------------------------------------------


class TestFailedBurnNextStep:
    def test_fail_status_reburn_pass_status(self, staged_env, capsys):
        from lcsas.db.packs import register_pack
        from lcsas.db.queries import get_unarchived_or_missing_at_location
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        tmp_path, cfg, db = staged_env
        # The staged ISO is still in the staging area.
        iso = tmp_path / "VOL_NEW.iso"
        iso.write_bytes(b"\0" * 2048)
        conn = get_connection(db)
        try:
            conn.execute("UPDATE session_volumes SET iso_path = ?", (str(iso),))
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
            conn.commit()
        finally:
            conn.close()
        next_step = "lcsas stage --for-location Bank_Box"

        # 1. The laptop burn failed its verify.
        failed = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        out = capsys.readouterr().out
        assert "FAILED verification" in out and next_step in out

        # 2. status lists it under needs re-burn, with the same next step,
        #    and not as waiting to be burned.
        assert main(["--config", str(cfg), "status"]) == 0
        out = capsys.readouterr().out
        assert "VOL_NEW needs re-burn at Bank_Box" in out
        assert next_step in out
        assert "waiting to be burned" not in out
        assert "catalog reconcile" not in out  # no second, different step

        # ... and `stage --for-location` would stage its packs again.
        conn = get_connection(db)
        try:
            missing = get_unarchived_or_missing_at_location(conn, "Bank_Box")
            assert pack.pack_id in {p.pack_id for p in missing}
        finally:
            conn.close()

        # 3. The re-burn passes.
        passed = dict(_good("VOL_NEW", "Bank_Box"),
                      burn_date="2026-04-15T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "pass.json", passed)) == 0
        assert _status(db, "VOL_NEW") == "VERIFIED"
        capsys.readouterr()

        # 4. status no longer lists it.
        assert main(["--config", str(cfg), "status"]) == 0
        assert "needs re-burn" not in capsys.readouterr().out

    def test_doing_the_named_step_clears_the_warning(self, staged_env, capsys):
        """After `stage --for-location` put the packs on a new volume, the
        failed one is no longer listed: the location needs nothing from it."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo
        from lcsas.db.volume_packs import bulk_link_packs

        tmp_path, cfg, db = staged_env
        conn = get_connection(db)
        try:
            register_repo(conn, "repo1", "Repo 1", "/mirror/repo1")
            pack = register_pack(conn, "f" * 64, 100, "repo1")
            vol = get_volume_by_label(conn, "VOL_NEW")
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        finally:
            conn.close()
        failed = dict(_good("VOL_NEW", "Bank_Box"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        capsys.readouterr()
        assert main(["--config", str(cfg), "status"]) == 0
        assert "VOL_NEW needs re-burn at Bank_Box" in capsys.readouterr().out

        # What `lcsas stage --for-location Bank_Box` does: a new volume,
        # staged for Bank_Box, holding the same pack.
        conn = get_connection(db)
        try:
            new = create_volume(conn, "VOL_NEXT", "uuid-next", "TEST_TINY", 1000,
                                "Bank_Box", "STAGING")
            bulk_link_packs(conn, new.volume_id, [pack.pack_id])
        finally:
            conn.close()

        assert main(["--config", str(cfg), "status"]) == 0
        assert "needs re-burn" not in capsys.readouterr().out

    def test_pass_at_another_location_does_not_clear_a_failure(self, env, capsys):
        """The newest verify result counts location by location."""
        tmp_path, cfg, db = env
        failed = dict(_good("VOL_GOOD", "Vault"), verify_passed=False)
        assert _run(cfg, _receipt(tmp_path, "fail.json", failed)) == 0
        passed = dict(_good("VOL_GOOD", "Offsite"),
                      burn_date="2026-04-15T12:00:00+00:00")
        assert _run(cfg, _receipt(tmp_path, "pass.json", passed)) == 0
        capsys.readouterr()

        assert main(["--config", str(cfg), "status"]) == 0

        out = capsys.readouterr().out
        assert "VOL_GOOD needs re-burn at Vault" in out
        assert "needs re-burn at Offsite" not in out
