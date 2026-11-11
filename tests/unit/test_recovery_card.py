"""Tests for the whole-archive estate card generator (UX-09).

Drives the real CLI ``lcsas estate card`` entry point and the underlying
``_estate_card_text`` builder.  Covers:

  * split and non-split configs (K/N line presence),
  * owner / key_storage_hints / repositories rendered from config,
  * disc count from a catalog when present, fill-in blanks when absent,
  * the per-OS restore commands passing UX-02's restore.sh flag contract.

Always-on (``make test-unit``) — no external tools.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.cli.main import _estate_card_text, main

# Reuse UX-02's canonical "flags restore.sh actually accepts" helper so this
# test inherits the same contract source the doc gate uses.
from tests.unit.test_doc_command_contract import (
    _FLAG_TOKEN,
    _accepted_restore_sh_flags,
    _extract_restore_sh_commands,
)


def _write_config(
    tmp_path: Path,
    *,
    split: bool,
    repos: tuple[str, ...] = ("alpha",),
    label_prefix: str = "SMITH",
) -> Path:
    lines = [
        "[paths]",
        f'mirror_base = "{tmp_path / "mirror_base"}"',
        f'staging = "{tmp_path / "staging"}"',
        f'database = "{tmp_path / "archive.db"}"',
        "[defaults]",
        f"label_prefix = \"{label_prefix}\"",
        f"key_split = {str(split).lower()}",
        "key_threshold = 2",
        "key_shares = 5",
        "[survivability]",
        'archive_owner = "Jane Smith"',
        'archive_description = "Family photos 2000-2025"',
        'key_storage_hints = "Safe deposit box #1234; home safe"',
        'technical_contact = "Bob (bob@example.org)"',
    ]
    for r in repos:
        mirror = tmp_path / f"mirror-{r}"
        mirror.mkdir(exist_ok=True)
        pw = tmp_path / f"{r}.pw"
        pw.write_bytes(b"pw\n")
        lines += [
            f"[repos.{r}]",
            f'mirror_path = "{mirror}"',
            f'password_file = "{pw}"',
        ]
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


# --------------------------------------------------------------------------
# Builder-level: field rendering for split / non-split.
# --------------------------------------------------------------------------

def test_builder_split_shows_kn_and_combiner() -> None:
    card = _estate_card_text(
        owner="Jane Smith",
        description="Family photos",
        technical_contact="Bob",
        repositories=["alpha", "beta"],
        key_storage_hints="Home safe",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        label_prefix="SMITH",
        disc_count=8,
        card_date="2026-06-13",
    )
    assert "Jane Smith" in card
    assert "Home safe" in card
    assert "alpha, beta" in card
    assert "Any 2 of 5 share cards" in card
    # On-disc combiner path, not the installed-lcsas `key combine`.
    assert "python3 keyshare_combine.py" in card
    assert "8 disc(s), labeled SMITH_*" in card


def test_builder_non_split_omits_kn() -> None:
    card = _estate_card_text(
        owner="Jane Smith",
        description="Family photos",
        technical_contact="Bob",
        repositories=["alpha"],
        key_storage_hints="Home safe",
        key_split=False,
        key_threshold=2,
        key_shares=5,
        label_prefix="SMITH",
        disc_count=None,
        card_date="2026-06-13",
    )
    assert "share cards reconstruct" not in card
    assert "keyshare_combine.py" not in card
    # No catalog -> disc inventory is a fill-in blank, still printable.
    assert "______" in card


def test_builder_blanks_when_fields_empty() -> None:
    card = _estate_card_text(
        owner="",
        description="",
        technical_contact="",
        repositories=[],
        key_storage_hints="",
        key_split=False,
        key_threshold=2,
        key_shares=5,
        label_prefix="LCSAS",
        disc_count=None,
        card_date="2026-06-13",
    )
    assert "______" in card


# --------------------------------------------------------------------------
# CLI-level: drive `lcsas estate card`.
# --------------------------------------------------------------------------

def test_cli_split_to_file(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path, split=True, repos=("alpha", "gamma"))
    out = tmp_path / "card.txt"
    rc = main(["--config", str(cfg), "estate", "card", "--output", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Jane Smith" in text
    assert "Safe deposit box #1234" in text
    assert "alpha, gamma" in text
    assert "Any 2 of 5 share cards" in text


def test_cli_non_split_to_stdout(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path, split=False)
    rc = main(["--config", str(cfg), "estate", "card"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LCSAS ESTATE CARD" in out
    assert "share cards reconstruct" not in out


def test_cli_requires_config(capsys) -> None:
    rc = main(["estate", "card"])
    assert rc == 1
    assert "--config is required" in capsys.readouterr().out


def test_cli_disc_count_from_catalog(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, split=False)
    db_path = tmp_path / "archive.db"

    from lcsas.constants import STATUS_BURNED, STATUS_STAGING, STATUS_VERIFIED
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import create_location
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_copies import add_volume_copy
    from lcsas.db.volumes import create_volume

    conn = get_connection(db_path)
    ensure_schema(conn)
    create_location(conn, "Home_Shelf")
    # Two physical discs (BURNED, VERIFIED) + one in-progress STAGING that
    # must NOT count toward the heir's disc inventory.  #684: a burned
    # volume counts by its ACTIVE disc, as a real burn records one.
    for label, status in (
        ("SMITH_001", STATUS_BURNED),
        ("SMITH_002", STATUS_VERIFIED),
    ):
        vol = create_volume(
            conn, label, f"uuid-{label}", "BD25", 25_000_000_000,
            status=status,
        )
        add_volume_copy(conn, vol.volume_id, "Home_Shelf")
    create_volume(
        conn, "SMITH_003", "uuid-3", "BD25", 25_000_000_000,
        status=STATUS_STAGING,
    )
    conn.commit()
    conn.close()

    out = tmp_path / "card.txt"
    rc = main(["--config", str(cfg), "estate", "card", "--output", str(out)])
    assert rc == 0
    assert "2 disc(s), labeled SMITH_*" in out.read_text(encoding="utf-8")
    # Sanity: STATUS_STAGING constant referenced so the count rule is pinned.
    assert STATUS_STAGING == "STAGING"


def test_cli_disc_count_skips_superseded_destroyed_volumes(
    tmp_path: Path,
) -> None:
    """#684: 5 volumes, 2 superseded by consolidation and their discs
    destroyed -> the card says 3."""
    from lcsas.constants import STATUS_DEPRECATED, STATUS_VERIFIED
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import create_location
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volume_copies import add_volume_copy, destroy_copy
    from lcsas.db.volumes import create_volume

    cfg = _write_config(tmp_path, split=False)
    conn = get_connection(tmp_path / "archive.db")
    ensure_schema(conn)
    create_location(conn, "Home_Shelf")
    for i in range(1, 6):
        superseded = i <= 2
        vol = create_volume(
            conn, f"SMITH_00{i}", f"uuid-{i}", "BD25", 25_000_000_000,
            status=STATUS_DEPRECATED if superseded else STATUS_VERIFIED,
        )
        add_volume_copy(conn, vol.volume_id, "Home_Shelf")
        if superseded:
            destroy_copy(conn, vol.volume_id, "Home_Shelf")
    conn.commit()
    conn.close()

    out = tmp_path / "card.txt"
    assert main(["--config", str(cfg), "estate", "card", "--output", str(out)]) == 0
    assert "3 disc(s), labeled SMITH_*" in out.read_text(encoding="utf-8")


def test_cli_ensure_schema_runs_under_the_catalog_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """#684: ensure_schema may migrate the catalog, so it must run while
    the catalog lock is held."""
    import fcntl

    import lcsas.db.schema as schema_mod
    from lcsas.db.connection import get_connection

    cfg = _write_config(tmp_path, split=False)
    db_path = tmp_path / "archive.db"
    conn = get_connection(db_path)
    schema_mod.ensure_schema(conn)
    conn.close()

    real = schema_mod.ensure_schema
    seen: list[bool] = []

    def _spy(c):  # type: ignore[no-untyped-def]
        # A second exclusive non-blocking lock fails while the card holds it.
        with open(str(db_path) + ".lock", "a+") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                seen.append(True)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                seen.append(False)
        return real(c)

    monkeypatch.setattr(schema_mod, "ensure_schema", _spy)
    out = tmp_path / "card.txt"
    assert main(["--config", str(cfg), "estate", "card", "--output", str(out)]) == 0
    assert seen == [True]


# --------------------------------------------------------------------------
# UX-02 contract: the card's restore commands use only real restore.sh flags.
# --------------------------------------------------------------------------

def test_card_commands_pass_restore_sh_flag_contract() -> None:
    accepted = _accepted_restore_sh_flags()
    card = _estate_card_text(
        owner="Jane Smith",
        description="Family photos",
        technical_contact="Bob",
        repositories=["alpha"],
        key_storage_hints="Home safe",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        label_prefix="LCSAS",
        disc_count=8,
        card_date="2026-06-13",
    )
    found = _extract_restore_sh_commands(card)
    assert found, "card should contain restore.sh invocations"
    for _lineno, cmd in found:
        for flag in _FLAG_TOKEN.findall(cmd):
            assert flag in accepted, f"phantom restore.sh flag on card: {flag}"


def test_cli_no_burned_disc_leaves_the_count_blank(tmp_path: Path) -> None:
    """A card printed before the first burn must not say "0 disc(s)"."""
    from lcsas.constants import STATUS_STAGING
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volumes import create_volume

    cfg = _write_config(tmp_path, split=False)
    conn = get_connection(tmp_path / "archive.db")
    ensure_schema(conn)
    create_volume(
        conn, "SMITH_001", "uuid-1", "BD25", 25_000_000_000,
        status=STATUS_STAGING,
    )
    conn.commit()
    conn.close()

    out = tmp_path / "card.txt"
    assert main(["--config", str(cfg), "estate", "card", "--output", str(out)]) == 0
    card = out.read_text(encoding="utf-8")
    assert "0 disc(s)" not in card
    assert "______________________________  (labeled SMITH_*)" in card
