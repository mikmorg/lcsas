"""A password split per repository (#587).

One archive can hold repositories whose password is split into share cards
next to repositories with an ordinary password.  ``key_split`` /
``key_threshold`` / ``key_shares`` under ``[repos.<name>]`` override the
``[defaults]`` values for that repository.  Checks:

- the config parses and resolves the per-repository keys;
- the burn's drift guard compares each repository with its own setting;
- the disc text (data-disc START_HERE + PASSWORD_INFO, meta-disc START_HERE)
  and the estate card name each split repository with its K/N and the
  repositories with an ordinary password;
- an archive split alike throughout renders exactly the archive-wide text
  it always did, whether the split is set under [defaults] or per repository;
- the recorded split's K/N wins over the config's (KEY-08, #573).
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from lcsas.cli.main import _estate_card_text, main
from lcsas.config.settings import (
    LCSASConfig,
    RepositoryConfig,
    load_config,
    unknown_toml_keys,
)
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.key_escrow import (
    config_names_by_repo_id,
    detect_escrow_drift,
    record_split,
    recorded_splits_by_name,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all, ensure_schema
from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.staging.metadata import HolographicInjector, split_view

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, name: str, **split: object) -> RepositoryConfig:
    return RepositoryConfig(
        name=name,
        mirror_path=tmp_path / "mirror" / name,
        password_file=Path(f"/keys/{name}.key"),
        **split,  # type: ignore[arg-type]
    )


def _mixed_config(tmp_path: Path) -> LCSASConfig:
    """photos split 2-of-3 in its own table; documents left on [defaults]."""
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "archive.db",
        key_split=False,
        key_threshold=2,
        key_shares=5,
        repositories={
            "photos": _repo(
                tmp_path, "photos",
                key_split=True, key_threshold=2, key_shares=3,
            ),
            "documents": _repo(tmp_path, "documents"),
        },
    )


def _render(tmp_path: Path, config: LCSASConfig, name: str, **kw) -> dict:
    root = tmp_path / name
    root.mkdir()
    injector = HolographicInjector(root)
    injector.write_start_here(config, **kw)
    injector.write_key_info(config, **kw)
    return {
        f: (root / f).read_text(encoding="utf-8")
        for f in ("START_HERE.txt", "PASSWORD_INFO.txt")
    }


def _write_toml(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{tmp_path / "archive.db"}"\n' + body
    )
    return cfg


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_repo_table_overrides_defaults(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, (
        "[defaults]\nkey_split = false\nkey_threshold = 2\nkey_shares = 5\n"
        '[repos.photos]\nmirror_path = "p"\n'
        "key_split = true\nkey_threshold = 2\nkey_shares = 3\n"
        '[repos.documents]\nmirror_path = "d"\n'
    ))
    config = load_config(cfg)
    assert config.repo_split("photos") == (True, 2, 3)
    assert config.repo_split("documents") == (False, 2, 5)
    assert config.repo_split("not-configured") == (False, 2, 5)
    raw = tomllib.loads(cfg.read_text())
    assert unknown_toml_keys(raw) == []


def test_repo_table_can_turn_an_archive_wide_split_off(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, (
        "[defaults]\nkey_split = true\nkey_threshold = 3\nkey_shares = 5\n"
        '[repos.photos]\nmirror_path = "p"\n'
        '[repos.documents]\nmirror_path = "d"\nkey_split = false\n'
    ))
    config = load_config(cfg)
    assert config.repo_split("photos") == (True, 3, 5)
    assert config.repo_split("documents")[0] is False


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def _catalog_with_split(conn: sqlite3.Connection) -> list[str]:
    create_all(conn)
    register_repo(conn, "uuid-photos", "photos", "/m/photos")
    register_repo(conn, "uuid-documents", "documents", "/m/documents")
    record_split(conn, "uuid-photos", 2, 3, 4242)
    return ["uuid-photos", "uuid-documents"]


def test_mixed_archive_has_no_drift(tmp_path: Path) -> None:
    conn = get_memory_connection()
    repo_ids = _catalog_with_split(conn)
    config = _mixed_config(tmp_path)
    assert detect_escrow_drift(
        conn, repo_ids, config_split=False, config_threshold=2,
        config_shares=5, split_for=config.repo_split,
    ) is None
    # Without the per-repository resolver, the archive-wide [defaults] say
    # "not split" and the recorded photos split is drift, as before #587.
    assert detect_escrow_drift(
        conn, repo_ids, config_split=False, config_threshold=2,
        config_shares=5,
    ) is not None


def test_per_repo_kn_mismatch_is_drift_naming_the_table(tmp_path: Path) -> None:
    conn = get_memory_connection()
    repo_ids = _catalog_with_split(conn)
    config = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path,
        db_path=tmp_path / "a.db",
        repositories={
            "photos": _repo(tmp_path, "photos", key_split=True,
                            key_threshold=2, key_shares=4),
            "documents": _repo(tmp_path, "documents"),
        },
    )
    msg = detect_escrow_drift(
        conn, repo_ids, config_split=False, config_threshold=2,
        config_shares=5, split_for=config.repo_split,
    )
    assert msg is not None
    assert "'photos'" in msg and "2-of-4" in msg and "2-of-3" in msg
    assert "[repos.photos] or [defaults]" in msg


def test_unsplit_repo_with_a_record_is_still_drift(tmp_path: Path) -> None:
    conn = get_memory_connection()
    repo_ids = _catalog_with_split(conn)
    record_split(conn, "uuid-documents", 2, 5, 99)
    config = _mixed_config(tmp_path)
    msg = detect_escrow_drift(
        conn, repo_ids, config_split=False, config_threshold=2,
        config_shares=5, split_for=config.repo_split,
    )
    assert msg is not None and "'documents'" in msg and "key_split=false" in msg


def test_recorded_splits_by_name() -> None:
    conn = get_memory_connection()
    _catalog_with_split(conn)
    # Pre-#571 records were keyed by the repository's name.
    record_split(conn, "legacy", 3, 5, 7)
    mirrors = {
        "photos": Path("/m/photos"), "documents": Path("/m/documents"),
        "legacy": Path("/m/legacy"),
    }
    assert recorded_splits_by_name(conn, mirrors) == {
        "photos": (2, 3), "legacy": (3, 5),
    }


def test_catalog_name_may_differ_from_the_config_key() -> None:
    """`repo add` takes any name: the join is on the mirror path first."""
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "uuid-p", "Family Photos", "/m/photos")
    record_split(conn, "uuid-p", 2, 3, 1)
    assert config_names_by_repo_id(conn, {"photos": Path("/m/photos/")}) == {
        "uuid-p": "photos",
    }
    assert recorded_splits_by_name(conn, {"photos": Path("/m/photos")}) == {
        "photos": (2, 3),
    }
    # Name fallbacks when the mirror path doesn't match: exact, then any case.
    assert config_names_by_repo_id(conn, {"family photos": Path("/x")}) == {
        "uuid-p": "family photos",
    }


# ---------------------------------------------------------------------------
# Disc text
# ---------------------------------------------------------------------------


def test_mixed_archive_disc_text_names_each_repository(tmp_path: Path) -> None:
    texts = _render(tmp_path, _mixed_config(tmp_path), "mixed")
    start, info = texts["START_HERE.txt"], texts["PASSWORD_INFO.txt"]
    assert "SOME PASSWORDS ARE SPLIT INTO SHARE CARDS" in start
    assert "SPLIT PASSWORDS — SOME PASSWORDS ARE IN SHARE CARDS" in info
    for text in (start, info):
        assert "photos : any 2 of 3 share cards" in text
        assert "Every other repository (documents) has an ordinary password." in text
        assert "This archive's password" not in text
        assert "any 2 of 5" not in text
        assert "lcsas-keyshare" in text
    assert "Password: SPLIT into 3 share cards (any 2 rebuild it)" in info
    assert "Password: an ordinary password (not split)" in info
    # Each repository's line sits in its own entry.
    photos = info.index("Repository: photos")
    documents = info.index("Repository: documents")
    split_line = info.index("Password: SPLIT")
    plain_line = info.index("Password: an ordinary")
    assert photos < split_line and documents < plain_line < photos


def test_recorded_kn_wins_per_repository(tmp_path: Path) -> None:
    texts = _render(
        tmp_path, _mixed_config(tmp_path), "rec", recorded={"photos": (3, 4)},
    )
    for text in texts.values():
        assert "photos : any 3 of 4 share cards" in text
        assert "any 2 of 3" not in text


def test_repositories_split_differently_get_the_list(tmp_path: Path) -> None:
    config = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path,
        db_path=tmp_path / "a.db", key_split=True,
        repositories={
            "a": _repo(tmp_path, "a", key_threshold=2, key_shares=3),
            "b": _repo(tmp_path, "b", key_threshold=3, key_shares=5),
        },
    )
    view = split_view(config)
    assert view is not None and view.uniform is None and view.unsplit == ()
    start = _render(tmp_path, config, "diff")["START_HERE.txt"]
    assert "a : any 2 of 3 share cards" in start
    assert "b : any 3 of 5 share cards" in start
    assert "Every other repository" not in start


def test_split_alike_renders_the_archive_wide_text(tmp_path: Path) -> None:
    """Per-repository tables that split every repository alike render
    byte-for-byte what [defaults] key_split always rendered."""
    per_repo = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path,
        db_path=tmp_path / "a.db",
        repositories={
            n: _repo(tmp_path, n, key_split=True, key_threshold=3,
                     key_shares=5)
            for n in ("alpha", "bravo")
        },
    )
    archive_wide = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path,
        db_path=tmp_path / "a.db", key_split=True, key_threshold=3,
        key_shares=5,
        repositories={n: _repo(tmp_path, n) for n in ("alpha", "bravo")},
    )
    a = _render(tmp_path, per_repo, "per_repo")
    b = _render(tmp_path, archive_wide, "archive_wide")
    assert a == b
    assert "This archive's password is SPLIT into 5 share cards." in a[
        "PASSWORD_INFO.txt"
    ]
    assert "SOME PASSWORDS" not in a["START_HERE.txt"]


def test_no_split_anywhere_shows_no_share_text(tmp_path: Path) -> None:
    config = LCSASConfig(
        mirror_base_path=tmp_path, staging_path=tmp_path,
        db_path=tmp_path / "a.db",
        repositories={n: _repo(tmp_path, n) for n in ("alpha", "bravo")},
    )
    assert split_view(config) is None
    for text in _render(tmp_path, config, "none").values():
        assert "SHARE CARD" not in text
        assert "lcsas-keyshare" not in text


# ---------------------------------------------------------------------------
# Meta disc
# ---------------------------------------------------------------------------


def _catalog_file(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    ensure_schema(conn)
    register_repo(conn, "uuid-photos", "photos", "/m/photos")
    register_repo(conn, "uuid-documents", "documents", "/m/documents")
    record_split(conn, "uuid-photos", 3, 4, 4242)
    conn.commit()
    conn.close()
    return db


def test_meta_start_here_uses_each_repositorys_recorded_split(
    tmp_path: Path,
) -> None:
    config = _mixed_config(tmp_path)
    builder = MetaVolumeBuilder(
        tmp_path / "meta", config=config,
        catalog_db_path=_catalog_file(tmp_path),
    )
    recorded = builder._recorded_splits()
    assert recorded == {"photos": (3, 4)}
    text = builder._render_meta_start_here(config, recorded)
    assert "SPLIT PASSWORDS — SOME PASSWORDS ARE IN SHARE CARDS" in text
    assert "photos : any 3 of 4 share cards" in text
    assert "(documents) has an ordinary password" in text


def test_meta_recorded_split_without_catalog_is_empty(tmp_path: Path) -> None:
    config = _mixed_config(tmp_path)
    assert MetaVolumeBuilder(tmp_path / "m1", config=config)._recorded_splits() == {}
    assert MetaVolumeBuilder(
        tmp_path / "m2", catalog_db_path=_catalog_file(tmp_path),
    )._recorded_splits() == {}


# ---------------------------------------------------------------------------
# Estate card
# ---------------------------------------------------------------------------


def _card(**kw) -> str:
    base = dict(
        owner="O", description="D", technical_contact="T",
        repositories=["documents", "photos"], key_storage_hints="H",
        key_split=False, key_threshold=2, key_shares=5,
        label_prefix="LCSAS", disc_count=3, card_date="2026-11-03",
    )
    base.update(kw)
    return _estate_card_text(**base)  # type: ignore[arg-type]


def test_estate_card_lists_each_repository() -> None:
    card = _card(split_repos={"photos": (2, 3)}, unsplit_repos=("documents",))
    assert "SOME PASSWORDS ARE SPLIT INTO SHARE CARDS" in card
    assert "photos: any 2 of 3 share cards rebuild its password" in card
    assert "documents: an ordinary password (not split)" in card
    assert "THE PASSWORD IS SPLIT" not in card
    assert "lcsas-keyshare" in card


def test_estate_card_cli_mixed_archive_uses_the_record(tmp_path: Path) -> None:
    (tmp_path / "p").mkdir()
    (tmp_path / "d").mkdir()
    cfg = _write_toml(tmp_path, (
        "[defaults]\nkey_split = false\n"
        "[repos.photos]\n"
        f'mirror_path = "{tmp_path / "p"}"\n'
        "key_split = true\nkey_threshold = 3\nkey_shares = 4\n"
        "[repos.documents]\n"
        f'mirror_path = "{tmp_path / "d"}"\n'
    ))
    db = tmp_path / "archive.db"
    conn = get_connection(db)
    ensure_schema(conn)
    register_repo(conn, "uuid-photos", "photos", str(tmp_path / "p"))
    record_split(conn, "uuid-photos", 3, 4, 1)
    conn.commit()
    conn.close()
    out = tmp_path / "card.txt"
    assert main(["--config", str(cfg), "estate", "card", "--output", str(out)]) == 0
    card = out.read_text(encoding="utf-8")
    assert "photos: any 3 of 4 share cards rebuild its password" in card
    assert "documents: an ordinary password (not split)" in card


def test_estate_card_cli_uniform_archive_prints_recorded_kn(
    tmp_path: Path,
) -> None:
    """#573: the card prints the split actually performed, not the config."""
    (tmp_path / "p").mkdir()
    cfg = _write_toml(tmp_path, (
        "[defaults]\nkey_split = true\nkey_threshold = 2\nkey_shares = 5\n"
        "[repos.photos]\n"
        f'mirror_path = "{tmp_path / "p"}"\n'
    ))
    conn = get_connection(tmp_path / "archive.db")
    ensure_schema(conn)
    register_repo(conn, "uuid-photos", "photos", str(tmp_path / "p"))
    record_split(conn, "uuid-photos", 3, 5, 1)
    conn.commit()
    conn.close()
    out = tmp_path / "card.txt"
    assert main(["--config", str(cfg), "estate", "card", "--output", str(out)]) == 0
    card = out.read_text(encoding="utf-8")
    assert "Any 3 of 5 share cards reconstruct the" in card
    assert "Any 2 of 5" not in card
