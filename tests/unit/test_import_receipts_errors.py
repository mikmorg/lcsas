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
that a skipped receipt writes nothing at all.  Nothing here depends on
``verify_passed`` semantics: good receipts target an already-VERIFIED volume
(the "re-burn to a new location" path), so only the copy row and the
``BURN_RECEIPT_IMPORTED`` event are asserted.
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


def _make_catalog(tmp_path: Path, labels: dict[str, str]) -> Path:
    db = tmp_path / "archive.db"
    conn = get_connection(db)
    create_all(conn)
    for i, (label, status) in enumerate(labels.items()):
        create_volume(conn, label, f"uuid-{i}", "TEST_TINY", 1000, "Home", status)
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


def _run(cfg: Path, *files: Path) -> int:
    return main(["--config", str(cfg), "catalog", "import-receipts",
                 *[str(f) for f in files]])


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
        assert rc == 0  # documented: skipped receipts are not an error
        assert "Failed to read receipt" in out
        assert str(bad) in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_nonexistent_file_is_skipped_with_warning(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, tmp_path / "never-written.json")

        out = capsys.readouterr().out
        assert rc == 0
        assert "Failed to read receipt" in out
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_a_directory_in_place_of_a_receipt_is_skipped(self, env, capsys):
        tmp_path, cfg, db = env
        (tmp_path / "adir").mkdir()
        before = _dump(db)

        rc = _run(cfg, tmp_path / "adir")

        assert rc == 0
        assert "Failed to read receipt" in capsys.readouterr().out
        assert _dump(db) == before

    @pytest.mark.parametrize("body", ["[]", '"just a string"', "{}"])
    def test_json_that_is_not_a_receipt_object_is_skipped(self, env, capsys, body):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, _receipt(tmp_path, "weird.json", body))

        out = capsys.readouterr().out
        assert rc == 0
        assert "missing keys" in out
        assert _dump(db) == before

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: cmd_catalog_import only catches JSONDecodeError/OSError. A "
            "receipt that is not valid UTF-8 (UTF-16 from PowerShell '>', binary "
            "garbage) raises UnicodeDecodeError, which escapes as 'Unexpected "
            "error' (rc 1) and aborts the whole batch; burn-iso-portable.md lists "
            "'encoding error' among the skipped-with-a-warning cases. See #522."
        ),
    )
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: the required-key check does `k not in receipt` on whatever "
            "json.load returned, so a receipt file whose JSON is null / a number "
            "/ true raises TypeError -> 'Unexpected error' (rc 1) and aborts the "
            "batch, instead of being skipped like '[]' and '\"str\"' are. "
            "See #522."
        ),
    )
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: int(receipt['iso_size_bytes']) raises ValueError on a "
            "non-numeric size -> 'Unexpected error' (rc 1), aborting the batch; "
            "ensure_location() has already committed, leaving a stray locations "
            "row. See #522."
        ),
    )
    def test_non_numeric_size_is_skipped_and_writes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)
        bad = _receipt(tmp_path, "badsize.json",
                       {**_good("VOL_OTHER", "Vault_Q"), "iso_size_bytes": "abc"})
        good = _receipt(tmp_path, "good.json", _good("VOL_GOOD"))

        _run(cfg, bad, good)

        out = capsys.readouterr().out
        assert "Unexpected error" not in out
        assert not [row for row in _dump(db)["locations"] if "Vault_Q" in row]
        assert _dump(db)["volume_copies"] != before["volume_copies"]
        _assert_imported(db, "VOL_GOOD", "Offsite")


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
        assert rc == 0
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
        """Only volume_label + location are mandatory; everything else defaults."""
        tmp_path, cfg, db = env
        minimal = _receipt(tmp_path, "min.json",
                           {"volume_label": "VOL_GOOD", "location": "Offsite"})

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
        assert rc == 0
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
        assert rc == 0
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

        assert rc == 0
        assert "Imported 2 receipt(s)." in capsys.readouterr().out
        _assert_imported(db, "VOL_GOOD", "Offsite")
        _assert_imported(db, "VOL_OTHER", "Vault")

    def test_all_bad_batch_exits_zero_and_changes_nothing(self, env, capsys):
        tmp_path, cfg, db = env
        before = _dump(db)

        rc = _run(cfg, *[make(tmp_path) for make in _BAD_FILES.values()])

        out = capsys.readouterr().out
        assert rc == 0  # documented: only an iso_sha256 mismatch is a non-zero exit
        assert "Imported 0 receipt(s)." in out
        assert _dump(db) == before

    def test_hash_mismatch_exits_nonzero_but_does_not_stop_the_batch(self, env, capsys):
        """The one rc != 0 case: a rejected receipt flips the exit code, yet the
        good receipt after it is still committed and the rejected one wrote nothing."""
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
