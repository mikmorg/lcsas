"""The backup set marker, ``metadata/<repo_id>/LCSAS_REPO.txt`` (rc6 C1).

Drill #476 found that the restore scripts' set menu shows each set's
repo_id, a UUID (F4), and that nothing on a disc says, in a form a script
can read, whether a set's password is split into share cards (F6).  The
marker carries both.  Checks:

- the exact bytes: LF only, no BOM, a ``#`` line first, ``format=1``,
  then id, name, name_cmd, split, split_id;
- sanitising: a name with spaces, quotes, ``& % ! ^ | < >``, non-ASCII,
  control characters, line breaks or 200 characters can never add a line
  or put a cmd metacharacter into ``name_cmd``;
- name precedence (config key, catalog name, id) and the split source
  (the recorded split wins over the config);
- the tolerant parser;
- the writers: data discs (``inject_metadata``, and a whole ``prepare()``
  of a mixed archive with UUID repo ids), and the meta disc with and
  without a catalog;
- the tier-3 Python reader and the standalone script ignore the extra
  file at the repository root.

The real rustic, restic and tier-1 binaries are checked in
``tests/recovery_hardening/test_repo_marker_tolerance.py``.
"""

from __future__ import annotations

import codecs
import dataclasses
import hashlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.key_escrow import config_names_by_repo_id, record_split
from lcsas.db.models import KeyEscrow
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.restore.restic_fallback import PurePythonRestorer
from lcsas.staging.metadata import HolographicInjector
from lcsas.staging.repo_marker import (
    MARKER_FILENAME,
    MARKER_HEADER,
    RepoMarker,
    cmd_name,
    display_name,
    make_repo_marker,
    parse_repo_marker,
    read_repo_marker,
    render_repo_marker,
    repo_markers,
    write_repo_marker,
)

PHOTOS = "3f2b8c1e-9a4d-4e7b-b6c2-0d5e7f1a2b3c"
DOCUMENTS = "a9c4e2d7-1b6f-4a83-9e5d-7c2b0f4e8a61"
CMD_SAFE = re.compile(r"[A-Za-z0-9 ._+,-]{1,64}")


def _record(repo_id: str, k: int = 2, n: int = 3, slip39_id: int = 42) -> KeyEscrow:
    return KeyEscrow(
        repo_id=repo_id, threshold=k, shares=n, slip39_id=slip39_id,
        split_at="2026-11-03T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def test_split_marker_text_is_exact() -> None:
    marker = make_repo_marker(PHOTOS, config_name="photos", record=_record(PHOTOS))
    assert render_repo_marker(marker) == (
        "# LCSAS backup set marker. Restore works without this file.\n"
        "format=1\n"
        f"id={PHOTOS}\n"
        "name=photos\n"
        "name_cmd=photos\n"
        "split=2-of-3\n"
        "split_id=00042\n"
    )


def test_plain_marker_file_bytes(tmp_path: Path) -> None:
    folder = tmp_path / DOCUMENTS
    folder.mkdir()
    path = write_repo_marker(
        folder, make_repo_marker(DOCUMENTS, catalog_name="documents")
    )
    assert path == folder / MARKER_FILENAME
    raw = path.read_bytes()
    assert raw == (
        f"{MARKER_HEADER}\nformat=1\nid={DOCUMENTS}\nname=documents\n"
        "name_cmd=documents\nsplit=none\nsplit_id=\n"
    ).encode("ascii")
    assert b"\r" not in raw  # LF only (C1), on every platform
    assert not raw.startswith(codecs.BOM_UTF8)
    assert raw.startswith(b"#")


def test_split_id_is_zero_padded_to_five_digits() -> None:
    for slip39_id, text in ((0, "00000"), (7, "00007"), (32767, "32767")):
        marker = make_repo_marker(PHOTOS, record=_record(PHOTOS, slip39_id=slip39_id))
        assert f"\nsplit_id={text}\n" in render_repo_marker(marker)


def test_non_ascii_name_is_written_as_utf8(tmp_path: Path) -> None:
    folder = tmp_path / PHOTOS
    folder.mkdir()
    write_repo_marker(folder, make_repo_marker(PHOTOS, config_name="Fotos été 写真"))
    raw = (folder / MARKER_FILENAME).read_bytes()
    assert "name=Fotos été 写真\n".encode() in raw
    assert b"name_cmd=Fotos _t_ __\n" in raw


# ---------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "name", "name_cmd"),
    [
        ("My Photos", "My Photos", "My Photos"),
        ('Mom\'s "best" photos', 'Mom\'s "best" photos', "Mom_s _best_ photos"),
        ("a&b%c!d^e|f<g>h", "a&b%c!d^e|f<g>h", "a_b_c_d_e_f_g_h"),
        ("(x)=y;z`$HOME*?", "(x)=y;z`$HOME*?", "_x__y_z__HOME__"),
        ("C:\\Users\\me/x", "C:\\Users\\me/x", "C__Users_me_x"),
        ("v1.2_final+copy, -old", "v1.2_final+copy, -old", "v1.2_final+copy, -old"),
        ("Fotos été 写真", "Fotos été 写真", "Fotos _t_ __"),
        ("line1\nline2\r\n\tend", "line1 line2   end", "line1 line2   end"),
        (" \x00 padded \x1b[31m ", "padded  [31m", "padded  _31m"),
        ("x\u2028y\u0085z\x9b\x7f", "x y z", "x y z"),
        ("%%%", "%%%", "___"),
    ],
)
def test_names_are_sanitised(raw: str, name: str, name_cmd: str) -> None:
    marker = make_repo_marker(PHOTOS, config_name=raw)
    assert marker.name == name
    assert marker.name_cmd == name_cmd
    assert CMD_SAFE.fullmatch(marker.name_cmd)
    fields = parse_repo_marker(render_repo_marker(marker))
    assert fields["name"] == name
    assert fields["name_cmd"] == name_cmd


def test_long_names_are_cut() -> None:
    marker = make_repo_marker(PHOTOS, config_name="A" * 200)
    assert marker.name == "A" * 128
    assert marker.name_cmd == "A" * 64
    wide = make_repo_marker(PHOTOS, config_name="é" * 200)
    assert wide.name == "é" * 128
    assert wide.name_cmd == "_" * 64
    # A cut that ends on a space does not leave a trailing space.
    spaced = make_repo_marker(PHOTOS, config_name="a" * 63 + " " + "b" * 136)
    assert spaced.name_cmd == "a" * 63
    assert len(spaced.name) == 128


def test_a_hostile_name_cannot_add_a_line() -> None:
    evil = "evil\nsplit=1-of-1\r\nname_cmd=&calc\nid=other"
    marker = make_repo_marker(PHOTOS, config_name=evil, record=_record(PHOTOS))
    text = render_repo_marker(marker)
    assert text.count("\n") == 7
    fields = parse_repo_marker(text)
    assert fields["id"] == PHOTOS
    assert fields["split"] == "2-of-3"
    assert CMD_SAFE.fullmatch(fields["name_cmd"])
    assert fields["name"] == "evil split=1-of-1  name_cmd=&calc id=other"


def test_name_cmd_is_never_empty() -> None:
    assert cmd_name("", PHOTOS) == PHOTOS
    assert cmd_name("   ", "family") == "family"
    assert display_name("\n\t \x00") == ""


def test_bad_ids_and_names_are_refused(tmp_path: Path) -> None:
    for bad in ("", "  ", "a\nb", "a\rb", "a\x00b", "a\u2028b"):
        with pytest.raises(ValueError):
            make_repo_marker(bad, config_name="x")
    with pytest.raises(ValueError):
        RepoMarker(repo_id=PHOTOS, name="a\nb")
    with pytest.raises(ValueError):
        RepoMarker(repo_id=PHOTOS, name=" padded")
    with pytest.raises(ValueError):
        RepoMarker(repo_id=PHOTOS, name="x", split=(3, 2))
    with pytest.raises(ValueError):
        RepoMarker(repo_id=PHOTOS, name="x", split_id=5)
    wrong = tmp_path / "not-the-id"
    wrong.mkdir()
    with pytest.raises(ValueError):
        write_repo_marker(wrong, make_repo_marker(PHOTOS))


# ---------------------------------------------------------------------------
# Name precedence and split source
# ---------------------------------------------------------------------------


def test_config_key_wins_over_catalog_name_over_id() -> None:
    both = make_repo_marker(PHOTOS, config_name="photos", catalog_name="Family Photos")
    assert both.name == "photos"
    catalog_only = make_repo_marker(PHOTOS, catalog_name="Family Photos")
    assert catalog_only.name == "Family Photos"
    neither = make_repo_marker(PHOTOS)
    assert neither.name == PHOTOS
    assert neither.name_cmd == PHOTOS
    # A name that is empty once sanitised falls through to the next.
    blank = make_repo_marker(PHOTOS, config_name="\n\t", catalog_name="Family")
    assert blank.name == "Family"
    assert make_repo_marker(PHOTOS, config_name=" ", catalog_name="\x00").name == PHOTOS


def test_recorded_split_wins_over_config() -> None:
    record = _record(PHOTOS, 2, 3, 4242)
    for config_split in (None, (True, 3, 5), (False, 2, 5)):
        marker = make_repo_marker(PHOTOS, record=record, config_split=config_split)
        assert (marker.split, marker.split_id) == ((2, 3), 4242)


def test_config_split_without_a_record_has_no_split_id() -> None:
    marker = make_repo_marker(PHOTOS, config_split=(True, 3, 5))
    assert (marker.split, marker.split_id) == ((3, 5), None)
    text = render_repo_marker(marker)
    assert "\nsplit=3-of-5\nsplit_id=\n" in text


def test_no_split() -> None:
    for config_split in (None, (False, 2, 3)):
        marker = make_repo_marker(PHOTOS, config_split=config_split)
        assert marker.split is None and marker.split_id is None
        assert "\nsplit=none\nsplit_id=\n" in render_repo_marker(marker)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_is_tolerant() -> None:
    text = (
        "\ufeff# comment\r\n"
        "format=1\r\n"
        "\r\n"
        "# another=comment\n"
        f"id={PHOTOS}\r\n"
        "name=a=b\n"
        "no equals sign here\n"
        "future_key=whatever\n"
        "split=2-of-3"  # no final newline
    )
    assert parse_repo_marker(text) == {
        "format": "1",
        "id": PHOTOS,
        "name": "a=b",
        "future_key": "whatever",
        "split": "2-of-3",
    }


def test_crlf_copy_parses_like_the_original() -> None:
    text = render_repo_marker(make_repo_marker(PHOTOS, record=_record(PHOTOS)))
    assert parse_repo_marker(text.replace("\n", "\r\n")) == parse_repo_marker(text)


def test_read_repo_marker_checks_the_folder_name(tmp_path: Path) -> None:
    folder = tmp_path / PHOTOS
    folder.mkdir()
    assert read_repo_marker(folder) is None  # no file
    write_repo_marker(folder, make_repo_marker(PHOTOS, config_name="photos"))
    fields = read_repo_marker(folder)
    assert fields is not None and fields["name"] == "photos"
    # The same file copied into another set's folder is ignored.
    other = tmp_path / DOCUMENTS
    other.mkdir()
    (other / MARKER_FILENAME).write_bytes((folder / MARKER_FILENAME).read_bytes())
    assert read_repo_marker(other) is None


# ---------------------------------------------------------------------------
# From the catalog: repo_markers
# ---------------------------------------------------------------------------


def _mixed_config(tmp_path: Path, mirror: Path) -> LCSASConfig:
    """photos: split 2-of-3 in its own table.  documents: [defaults], plain."""
    return LCSASConfig(
        mirror_base_path=mirror,
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "archive.db",
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=1000,
        label_prefix="TEST",
        key_split=False,
        key_threshold=2,
        key_shares=5,
        repositories={
            "photos": RepositoryConfig(
                name="photos", mirror_path=mirror / "photos",
                key_split=True, key_threshold=2, key_shares=3,
            ),
            "documents": RepositoryConfig(
                name="documents", mirror_path=mirror / "documents",
            ),
        },
        allow_unverified_repo_format=True,
    )


def _mixed_catalog(conn: sqlite3.Connection, mirror: Path) -> None:
    """UUID repo_ids, catalog names unlike the config keys (repo add takes any)."""
    create_all(conn)
    register_repo(conn, PHOTOS, "Family Photos", str(mirror / "photos"))
    register_repo(conn, DOCUMENTS, "Scans & Papers", str(mirror / "documents"))
    record_split(conn, PHOTOS, 2, 3, 4242)


def test_repo_markers_from_a_mixed_catalog(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    config = _mixed_config(tmp_path, mirror)
    conn = get_memory_connection()
    _mixed_catalog(conn, mirror)
    names = config_names_by_repo_id(conn, {
        k: r.mirror_path for k, r in config.repositories.items()
    })

    markers = repo_markers(
        conn, [PHOTOS, DOCUMENTS], config=config, config_names=names
    )
    assert markers[PHOTOS] == RepoMarker(PHOTOS, "photos", (2, 3), 4242)
    assert markers[DOCUMENTS] == RepoMarker(DOCUMENTS, "documents", None, None)

    # Without a config: the catalog names, and the record alone.
    bare = repo_markers(conn, [PHOTOS, DOCUMENTS])
    assert bare[PHOTOS] == RepoMarker(PHOTOS, "Family Photos", (2, 3), 4242)
    assert bare[DOCUMENTS] == RepoMarker(DOCUMENTS, "Scans & Papers", None, None)
    assert bare[DOCUMENTS].name_cmd == "Scans _ Papers"


def test_no_record_on_a_catalog_that_records_splits_means_no_split(
    tmp_path: Path,
) -> None:
    """The config says split, but the catalog (v9+) records none.

    No record means no share cards were made: the drift guard's rule.
    The guard checks only the sets with packs on the volume, so the
    marker must not take the config's word for the others.
    """
    mirror = tmp_path / "mirror"
    config = _mixed_config(tmp_path, mirror)
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, PHOTOS, "Family Photos", str(mirror / "photos"))
    names = config_names_by_repo_id(conn, {
        k: r.mirror_path for k, r in config.repositories.items()
    })
    assert config.repo_split(names[PHOTOS]) == (True, 2, 3)

    markers = repo_markers(conn, [PHOTOS], config=config, config_names=names)
    assert markers[PHOTOS] == RepoMarker(PHOTOS, "photos", None, None)
    assert "\nsplit=none\nsplit_id=\n" in render_repo_marker(markers[PHOTOS])


def test_repo_markers_from_a_catalog_without_names_or_records(tmp_path: Path) -> None:
    """A catalog with no key_escrow table (before v9): the config decides."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE repositories (repo_id TEXT, mirror_path TEXT)")
    conn.execute("INSERT INTO repositories VALUES (?, ?)", (PHOTOS, "/m/p"))

    assert repo_markers(conn, [PHOTOS]) == {PHOTOS: RepoMarker(PHOTOS, PHOTOS)}
    # An archive-wide split in a config with no [repos.*] tables.
    config = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path, db_path=tmp_path / "a.db",
        key_split=True, key_threshold=3, key_shares=5,
    )
    assert repo_markers(conn, [PHOTOS], config=config) == {
        PHOTOS: RepoMarker(PHOTOS, PHOTOS, (3, 5), None)
    }


def test_repo_markers_skips_an_id_it_cannot_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = get_memory_connection()
    create_all(conn)
    assert repo_markers(conn, ["bad\nid", "good"]) == {
        "good": RepoMarker("good", "good")
    }
    assert "bad" in caplog.text


# ---------------------------------------------------------------------------
# Data discs
# ---------------------------------------------------------------------------


def _mirror_repo(root: Path) -> Path:
    for sub in ("index", "snapshots", "keys", "data"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "keys" / ("2" * 64)).write_bytes(b"k")
    (root / "config").write_bytes(b"c")
    return root


def test_inject_metadata_writes_the_marker_in_the_folder(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = _mirror_repo(tmp_path / "mirror" / "photos")
    marker = make_repo_marker(PHOTOS, config_name="photos", record=_record(PHOTOS))

    HolographicInjector(staging).inject_metadata(
        {PHOTOS: mirror}, required_repos={PHOTOS}, markers={PHOTOS: marker},
    )
    folder = staging / "metadata" / PHOTOS
    assert (folder / "keys").is_dir()
    assert (folder / MARKER_FILENAME).read_text(encoding="utf-8") == (
        render_repo_marker(marker)
    )
    # Not beside the folder: whole-folder copies must carry it.
    assert sorted(p.name for p in (staging / "metadata").iterdir()) == [PHOTOS]


def test_inject_metadata_without_markers_writes_none(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = _mirror_repo(tmp_path / "mirror" / "photos")
    HolographicInjector(staging).inject_metadata({PHOTOS: mirror})
    assert not (staging / "metadata" / PHOTOS / MARKER_FILENAME).exists()


def test_no_folder_and_no_marker_when_nothing_is_copied(tmp_path: Path) -> None:
    """An unreachable mirror with no packs here: no empty folder (NEW-7)."""
    staging = tmp_path / "staging"
    staging.mkdir()
    HolographicInjector(staging).inject_metadata(
        {PHOTOS: tmp_path / "no-such-mirror"},
        markers={PHOTOS: make_repo_marker(PHOTOS)},
    )
    assert not (staging / "metadata" / PHOTOS).exists()


def _seed_repo_packs(
    conn: sqlite3.Connection, repo_id: str, mirror: Path, base: int,
) -> None:
    _mirror_repo(mirror)
    for i in range(base, base + 2):
        content = f"pack-{i:04d}".encode() * 10
        sha = hashlib.sha256(content).hexdigest()
        register_pack(conn, sha256=sha, size_bytes=len(content), repo_id=repo_id)
        (mirror / "data" / sha).write_bytes(content)


def test_prepare_writes_a_marker_for_each_set_of_a_mixed_archive(
    tmp_path: Path,
) -> None:
    """The drill's shape: UUID ids, one split set, one plain set.

    Each set's marker gives its config key as the name (the name on the
    estate card, PASSWORD_INFO.txt and the share cards), and the split
    agrees with PASSWORD_INFO.txt.  VOLUME_INFO.txt names each set.
    """
    mirror = tmp_path / "mirror"
    config = _mixed_config(tmp_path, mirror)
    config.staging_path.mkdir(parents=True)
    config.db_path.write_bytes(b"sqlite_placeholder")
    conn = get_memory_connection()
    _mixed_catalog(conn, mirror)
    _seed_repo_packs(conn, PHOTOS, mirror / "photos", 1)
    _seed_repo_packs(conn, DOCUMENTS, mirror / "documents", 10)

    orch = BurnOrchestrator(
        config, conn, MagicMock(), MagicMock(), prompt=NullOperatorPrompt(),
    )
    root = orch.prepare(media_type=MediaType.TEST_TINY).staging_path

    photos = read_repo_marker(root / "metadata" / PHOTOS)
    documents = read_repo_marker(root / "metadata" / DOCUMENTS)
    assert photos == {
        "format": "1", "id": PHOTOS, "name": "photos", "name_cmd": "photos",
        "split": "2-of-3", "split_id": "04242",
    }
    assert documents == {
        "format": "1", "id": DOCUMENTS, "name": "documents",
        "name_cmd": "documents", "split": "none", "split_id": "",
    }

    info = (root / "PASSWORD_INFO.txt").read_text(encoding="utf-8")
    assert "photos : any 2 of 3 share cards" in info
    assert "Every other repository (documents) has an ordinary password." in info

    volume_info = (root / "VOLUME_INFO.txt").read_text(encoding="utf-8")
    assert f"Repositories:  photos ({PHOTOS})\n" in volume_info
    assert f"\n                 documents ({DOCUMENTS})\n" in volume_info
    assert "  2 packs, " in volume_info
    assert all(len(line) <= 72 for line in volume_info.splitlines())


def test_prepare_marks_a_set_with_no_recorded_split_as_not_split(
    tmp_path: Path,
) -> None:
    """A third set: key_split=true in the config, no record, no packs.

    The drift guard checks only the sets with packs on this volume, so
    the burn goes ahead without --allow-escrow-drift.  Its marker must
    say split=none: no record means no share cards were made.
    """
    videos = "5d1e7a3c-2f8b-4c69-a0e4-9b3d6f2c8e17"
    mirror = tmp_path / "mirror"
    base = _mixed_config(tmp_path, mirror)
    config = dataclasses.replace(base, repositories={
        **base.repositories,
        "videos": RepositoryConfig(
            name="videos", mirror_path=mirror / "videos",
            key_split=True, key_threshold=3, key_shares=5,
        ),
    })
    config.staging_path.mkdir(parents=True)
    config.db_path.write_bytes(b"sqlite_placeholder")
    conn = get_memory_connection()
    _mixed_catalog(conn, mirror)
    register_repo(conn, videos, "videos", str(mirror / "videos"))
    _mirror_repo(mirror / "videos")
    _seed_repo_packs(conn, PHOTOS, mirror / "photos", 1)
    _seed_repo_packs(conn, DOCUMENTS, mirror / "documents", 10)

    orch = BurnOrchestrator(
        config, conn, MagicMock(), MagicMock(), prompt=NullOperatorPrompt(),
    )
    root = orch.prepare(media_type=MediaType.TEST_TINY).staging_path

    fields = read_repo_marker(root / "metadata" / videos)
    assert fields is not None
    assert (fields["name"], fields["split"], fields["split_id"]) == (
        "videos", "none", "",
    )
    photos = read_repo_marker(root / "metadata" / PHOTOS)
    assert photos is not None
    assert (photos["split"], photos["split_id"]) == ("2-of-3", "04242")


def test_volume_info_keeps_the_old_row_when_the_name_is_the_id(
    tmp_path: Path,
) -> None:
    from tests.unit.test_staging import _packs, _volume

    root = tmp_path / "staging"
    root.mkdir()
    HolographicInjector(root).write_volume_info(
        _volume(), packs=_packs(), repo_names={"family": "family", "work": "Work"},
    )
    text = (root / "VOLUME_INFO.txt").read_text(encoding="utf-8")
    assert "Repositories:  family (2 packs, 2.5 GB)\n" in text
    assert "                 Work (work)\n                   1 pack, 1.5 MB\n" in text


# ---------------------------------------------------------------------------
# Meta disc
# ---------------------------------------------------------------------------


def _meta_catalog(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "mirror"
    _mirror_repo(mirror / "photos")
    _mirror_repo(mirror / "documents")
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    _mixed_catalog(conn, mirror)
    conn.close()
    return db, mirror


def _bundle(tmp_path: Path, db: Path | None, config: LCSASConfig | None) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    builder = MetaVolumeBuilder(
        out, project_root=tmp_path, catalog_db_path=db, config=config,
    )
    builder._bundle_metadata()
    for result in builder.metadata_results:
        assert MARKER_FILENAME not in result.parts
    return out / "metadata"


def test_meta_disc_with_catalog_and_config(tmp_path: Path) -> None:
    db, mirror = _meta_catalog(tmp_path)
    meta = _bundle(tmp_path, db, _mixed_config(tmp_path, mirror))
    photos = read_repo_marker(meta / PHOTOS)
    documents = read_repo_marker(meta / DOCUMENTS)
    assert photos is not None and documents is not None
    assert (photos["name"], photos["split"], photos["split_id"]) == (
        "photos", "2-of-3", "04242",
    )
    assert (documents["name"], documents["split"], documents["split_id"]) == (
        "documents", "none", "",
    )


def test_meta_disc_with_catalog_but_no_config(tmp_path: Path) -> None:
    db, _mirror = _meta_catalog(tmp_path)
    meta = _bundle(tmp_path, db, None)
    photos = read_repo_marker(meta / PHOTOS)
    documents = read_repo_marker(meta / DOCUMENTS)
    assert photos is not None and documents is not None
    assert (photos["name"], photos["split"]) == ("Family Photos", "2-of-3")
    assert (documents["name"], documents["name_cmd"], documents["split"]) == (
        "Scans & Papers", "Scans _ Papers", "none",
    )


def test_meta_disc_config_split_without_a_record_is_not_split(
    tmp_path: Path,
) -> None:
    """A v9+ catalog with no record for photos: the config's split is ignored.

    The meta disc has no drift guard, so this is the only check.
    """
    mirror = tmp_path / "mirror"
    _mirror_repo(mirror / "photos")
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, PHOTOS, "Family Photos", str(mirror / "photos"))
    conn.close()

    meta = _bundle(tmp_path, db, _mixed_config(tmp_path, mirror))
    fields = read_repo_marker(meta / PHOTOS)
    assert fields is not None
    assert (fields["name"], fields["split"], fields["split_id"]) == (
        "photos", "none", "",
    )


def test_meta_disc_catalog_without_records_uses_the_config_split(
    tmp_path: Path,
) -> None:
    """A catalog with no key_escrow table (before schema v9): the config decides."""
    mirror = tmp_path / "mirror"
    _mirror_repo(mirror / "photos")
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE repositories (repo_id TEXT, name TEXT, mirror_path TEXT)"
    )
    conn.execute(
        "INSERT INTO repositories VALUES (?, ?, ?)",
        (PHOTOS, "Family Photos", str(mirror / "photos")),
    )
    conn.commit()
    conn.close()

    with_config = _bundle(tmp_path, db, _mixed_config(tmp_path, mirror))
    fields = read_repo_marker(with_config / PHOTOS)
    assert fields is not None
    assert (fields["name"], fields["split"], fields["split_id"]) == (
        "photos", "2-of-3", "",
    )

    (tmp_path / "out").rename(tmp_path / "out-1")
    no_config = _bundle(tmp_path, db, None)
    fields = read_repo_marker(no_config / PHOTOS)
    assert fields is not None
    assert (fields["name"], fields["split"]) == ("Family Photos", "none")


def test_meta_disc_minimal_catalog_gets_an_id_only_marker(tmp_path: Path) -> None:
    """A hand-rolled catalog with only (repo_id, mirror_path)."""
    mirror = tmp_path / "mirror"
    _mirror_repo(mirror / "photos")
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE repositories (repo_id TEXT, mirror_path TEXT)")
    conn.execute(
        "INSERT INTO repositories VALUES (?, ?)", (PHOTOS, str(mirror / "photos"))
    )
    conn.commit()
    conn.close()

    fields = read_repo_marker(_bundle(tmp_path, db, None) / PHOTOS)
    assert fields is not None
    assert (fields["name"], fields["name_cmd"], fields["split"]) == (
        PHOTOS, PHOTOS, "none",
    )


def test_meta_disc_without_catalog_has_no_metadata_and_no_marker(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    meta = _bundle(tmp_path, None, _mixed_config(tmp_path, mirror))
    assert not meta.exists()


def test_meta_disc_empty_mirror_gets_no_marker(tmp_path: Path) -> None:
    """Nothing copied: no marker (the keys/ gate still flags the folder)."""
    mirror = tmp_path / "mirror" / "photos"
    mirror.mkdir(parents=True)  # reachable, but empty
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, PHOTOS, "photos", str(mirror))
    conn.close()
    meta = _bundle(tmp_path, db, None)
    assert (meta / PHOTOS).is_dir()
    assert not (meta / PHOTOS / MARKER_FILENAME).exists()


# ---------------------------------------------------------------------------
# Tier 3 (the Python reader) ignores the file at the repository root
# ---------------------------------------------------------------------------


def test_python_reader_ignores_the_marker(tmp_path: Path) -> None:
    from tests.unit.test_restic_fallback import PASSWORD, _build_test_repo

    repo = _build_test_repo(tmp_path).rename(tmp_path / PHOTOS)
    write_repo_marker(repo, make_repo_marker(PHOTOS, config_name="photos"))

    restorer = PurePythonRestorer(repo, password=PASSWORD)
    assert restorer.verify_key()
    assert len(restorer.list_snapshots()) == 1
    assert restorer.repo_info()["snapshots"] == 1
    target = tmp_path / "restored"
    restorer.restore(target=target)
    assert (target / "hello.txt").read_text() == "Hello, World!\n"
    assert (target / "subdir" / "nested.txt").read_text() == "Nested file content.\n"


def test_standalone_script_ignores_the_marker(tmp_path: Path) -> None:
    from tests.unit.test_standalone_subprocess import _build_repo_and_script

    repo, pw_file, script = _build_repo_and_script(tmp_path)
    repo = repo.rename(tmp_path / PHOTOS)
    write_repo_marker(
        repo, make_repo_marker(PHOTOS, config_name='a "hostile" & name\n')
    )
    target = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--repo", str(repo),
            "--password-file", str(pw_file),
            "--target", str(target),
            "--interactive", "off",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert (target / "standalone_test.txt").read_bytes() == (
        b"Standalone subprocess test data!\n"
    )
