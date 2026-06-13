"""Tests for the whole-archive Recovery Card generator (UX-09).

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
    assert "LCSAS RECOVERY CARD (ARCHIVE)" in out
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
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volumes import create_volume

    conn = get_connection(db_path)
    ensure_schema(conn)
    # Two physical discs (BURNED, VERIFIED) + one in-progress STAGING that
    # must NOT count toward the heir's disc inventory.
    create_volume(
        conn, "SMITH_001", "uuid-1", "BD25", 25_000_000_000,
        status=STATUS_BURNED,
    )
    create_volume(
        conn, "SMITH_002", "uuid-2", "BD25", 25_000_000_000,
        status=STATUS_VERIFIED,
    )
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
