"""#968 items 24 to 27: passwords.

24. ``password split --shares 1 --threshold 1`` was accepted and printed
    "Give the 1 shares to separate share holders".  A split needs N >= 2.
25. ``password split`` against a master it could not open split first,
    then printed "recording it in the catalog failed", exit 0, with the
    share files written.  The catalog is now opened (as the record step
    opens it) before any share is made.
26. ``password verify`` on a wrong password printed the split command's
    text ("You were about to split a wrong password ... pass
    --no-verify-repo"); verify has no such flag.
27. ``config check`` warned for a multi-line password file only.  A BOM,
    a space at either end, a tab or a letter outside ASCII is part of the
    password to rustic, and the heir types it from a card: each is now
    warned about.

Real rustic (the repository's real key files), every command through
``main()``.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


@pytest.fixture
def inst(tmp_path: Path) -> ScratchInstall:
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    return inst


def _split(inst: ScratchInstall, out: Path, *extra: str,
           catalog: Path | None = None) -> int:
    pre = ["--catalog", str(catalog)] if catalog is not None else []
    return main(["--config", str(inst.config), *pre, "password", "split",
                 "--repo", "alpha", "--out", str(out), *extra])


# -- 24 ---------------------------------------------------------------------

def test_split_refuses_one_share(
    inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
) -> None:
    out = inst.root / "shares"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert _split(inst, out, "--shares", "1", "--threshold", "1") == 1
    assert "1-of-1 is no split. A split needs at least 2 shares" in caplog.text
    assert not out.exists()
    assert not inst.query("SELECT 1 FROM key_escrow")


# -- 25 ---------------------------------------------------------------------

def _newer_copy(inst: ScratchInstall) -> Path:
    newer = inst.root / "db" / "newer.db"
    shutil.copy(inst.db, newer)
    conn = sqlite3.connect(newer)
    conn.execute("INSERT INTO schema_version (version) VALUES (99)")
    conn.commit()
    conn.close()
    return newer


def _damaged_copy(inst: ScratchInstall) -> Path:
    damaged = inst.root / "db" / "damaged.db"
    damaged.write_bytes(b"not an SQLite catalog " * 400)
    return damaged


@pytest.mark.parametrize("which", ["newer", "damaged"])
def test_split_refuses_before_any_share_when_the_catalog_cannot_open(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str], which: str,
) -> None:
    catalog = _newer_copy(inst) if which == "newer" else _damaged_copy(inst)
    out = inst.root / "shares"
    assert _split(inst, out, catalog=catalog) == 1
    text = capsys.readouterr()
    said = text.out + text.err
    assert f"Could not open the catalog at {catalog}" in said
    assert "No shares were written." in said
    assert "recording it in the catalog failed" not in said
    assert "Wrote " not in said
    assert not out.exists()


# -- 26 ---------------------------------------------------------------------

def test_verify_of_a_wrong_password_says_so_in_verify_words(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    wrong = inst.root / "wrong.key"
    wrong.write_text("not the password\n")
    assert main(["--config", str(inst.config), "password", "verify",
                 "--repo", "alpha", "--password-file", str(wrong)]) == 1
    said = capsys.readouterr().out
    assert "the password in this file does NOT unlock repository 'alpha'" in said
    assert "You were about to split" not in said
    assert "--no-verify-repo" not in said


def test_verify_of_wrong_shares_names_the_shares(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    """Shares of another password: the message names what was checked."""
    other = inst.root / "other.key"
    other.write_text("another password altogether\n")
    out = inst.root / "other-shares"
    assert _split(inst, out, "--password-file", str(other),
                  "--no-verify-repo") == 0
    capsys.readouterr()
    cards = sorted(out.glob("alpha-share-*-card.txt"))[:2]
    argv = ["--config", str(inst.config), "password", "verify",
            "--repo", "alpha"]
    for card in cards:
        argv += ["--share-file", str(card)]
    assert main(argv) == 1
    said = capsys.readouterr().out
    assert "the password these 2 share(s) rebuild does NOT unlock" in said
    assert "--no-verify-repo" not in said


def test_verify_with_no_keys_folder_does_not_offer_a_split_flag(
    inst: ScratchInstall, capsys: pytest.CaptureFixture[str],
) -> None:
    keys = inst.mirror("alpha") / "keys"
    keys.rename(keys.with_name("keys.away"))
    assert main(["--config", str(inst.config), "password", "verify",
                 "--repo", "alpha", "--password-file",
                 str(inst.key("alpha"))]) == 1
    said = capsys.readouterr().out
    assert "keys directory not found" in said
    assert "--no-verify-repo" not in said


# -- 27 ---------------------------------------------------------------------

_ODD = {
    "bom": (b"\xef\xbb\xbfpw-in-a-file\n", "starts with a byte-order mark"),
    "leading space": (b" pw-in-a-file\n", "the password begins with a space"),
    "trailing space": (b"pw-in-a-file \n", "the password ends with a space"),
    "tab": (b"pw-in\ta-file\n", "the password holds a tab"),
    "non-ascii": ("pw-in-a-filé\n".encode(),
                  "a letter outside ASCII (é)"),
    "empty": (b"", "is empty, so the password is empty"),
}


@pytest.mark.parametrize("kind", sorted(_ODD))
def test_config_check_warns_for_a_password_nobody_can_type(
    inst: ScratchInstall, caplog: pytest.LogCaptureFixture, kind: str,
) -> None:
    data, words = _ODD[kind]
    inst.key("alpha").write_bytes(data)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        inst.lcsas("config", "check")
    warned = [r.getMessage() for r in caplog.records
              if r.levelno == logging.WARNING and words in r.getMessage()]
    assert len(warned) == 1, caplog.text
    assert str(inst.key("alpha")) in warned[0]


def test_an_ordinary_password_file_gets_no_typing_warning(
    inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("config", "check") == 0
    assert "password_file" not in caplog.text


@pytest.mark.parametrize("kind", ["bom", "leading space", "tab"])
def test_rustic_keeps_what_the_warning_names(tmp_path: Path, kind: str) -> None:
    """Why it matters: rustic takes these bytes as part of the password, so
    the visible text typed from a card does not open the repository."""
    data, _words = _ODD[kind]
    odd = tmp_path / "odd.key"
    odd.write_bytes(data)
    typed = tmp_path / "typed.key"
    typed.write_text(data.decode("utf-8-sig").strip().replace("\t", " ") + "\n")
    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "PATH": os.environ.get("PATH", "")}

    def rustic(key: Path, *args: str) -> int:
        return subprocess.run(
            ["rustic", "-r", str(tmp_path / "repo"), "--password-file",
             str(key), *args], capture_output=True, env=env,
        ).returncode

    assert rustic(odd, "init") == 0
    assert rustic(odd, "snapshots") == 0
    assert rustic(typed, "snapshots") != 0
