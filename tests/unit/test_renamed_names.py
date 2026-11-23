"""The names retired by #934 fail with "was renamed", and the new names work.

ADR 0005: no aliases.  An old command, flag or config key exits 2 (usage)
or is an error naming the new one; it never runs.  The table of names is
``tests/contract/renames.py``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.cli.parser import build_parser, renamed_message
from lcsas.config.settings import _xdg_db_path, load_config
from tests.contract.renames import RENAMES, renamed

# ── the owner's command line ────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "old", "new"),
    [
        (["key", "split", "--repo", "r"], "lcsas key", "lcsas password"),
        (["key", "card", "--repo", "r"], "lcsas key", "lcsas password"),
        (["--config", "x.toml", "key", "verify"], "lcsas key", "lcsas password"),
        (["restore", "standalone", "d", "out", "--password-file", "p"],
         "lcsas restore standalone", "lcsas restore from-disc"),
        (["--db", "x.db", "status"], "--db", "--catalog"),
        (["--db=x.db", "status"], "--db", "--catalog"),
        (["init", "--db-path", "x.db"], "--db-path", "--catalog-path"),
        (["stage", "--allow-escrow-drift"], "--allow-escrow-drift",
         "--allow-split-drift"),
        (["consolidate", "V1", "--target-media", "BD25"], "--target-media",
         "--media-type"),
    ],
)
def test_old_name_exits_2_naming_the_new(argv, old, new, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.strip() == f"lcsas: {old} was renamed: use {new}"


def test_the_renames_match_the_contract_table():
    for old, new in [
        ("lcsas key", "lcsas password"),
        ("lcsas restore standalone", "lcsas restore from-disc"),
        ("--db", "--catalog"),
        ("--db-path", "--catalog-path"),
        ("--allow-escrow-drift", "--allow-split-drift"),
        ("--target-media", "--media-type"),
    ]:
        assert renamed(old).new == new


@pytest.mark.parametrize(
    "argv",
    [
        ["password", "split", "--repo", "r", "--key", "x"],  # not renamed here
        ["catalog", "rebuild", "--output", "key"],
        ["restore", "from-disc", "d", "out", "--password-file", "p"],
        ["--catalog", "restore", "status"],  # a value, not a command
    ],
)
def test_new_and_unrelated_names_pass_the_check(argv):
    assert renamed_message(argv) is None


# ── the new names parse ─────────────────────────────────────────────


def test_new_flags_and_commands_parse():
    p = build_parser()
    assert p.parse_args(["--catalog", "c.db", "status"]).db == Path("c.db")
    assert p.parse_args(["init", "--catalog-path", "c.db"]).db_path == Path("c.db")
    assert p.parse_args(["stage", "--allow-split-drift"]).allow_escrow_drift is True
    assert p.parse_args(["consolidate", "V1", "--media-type", "BD25"]).target_media == "BD25"
    a = p.parse_args(["restore", "exec", "snap", "/out", "--repo", "r",
                      "--password-file", "pw"])
    assert a.restore_folder == Path("/out")
    b = p.parse_args(["restore", "from-disc", "/disc", "/out", "--password-file", "pw"])
    assert b.restore_command == "from-disc"
    assert b.restore_folder == Path("/out")
    c = p.parse_args(["password", "split", "--repo", "r"])
    assert c.command == "password"


def test_from_disc_keeps_its_own_catalog_option_apart_from_the_top_one():
    a = build_parser().parse_args(
        ["--catalog", "top.db", "restore", "from-disc", "/d", "/o",
         "--password-file", "p", "--catalog", "disc.db"])
    assert a.db == Path("top.db")
    assert a.catalog == Path("disc.db")


# ── the config file ─────────────────────────────────────────────────


def _toml(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(text)
    return cfg


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[paths]\ndatabase = "x.db"\n', "[paths] database was renamed: use catalog"),
        ("[defaults]\nkey_split = true\n", "[defaults] key_split was renamed: use split"),
        ("[defaults]\nkey_threshold = 2\n",
         "[defaults] key_threshold was renamed: use split_threshold"),
        ("[defaults]\nkey_shares = 3\n",
         "[defaults] key_shares was renamed: use split_shares"),
        ('[survivability]\nkey_storage_hints = "safe"\n',
         "[survivability] key_storage_hints was renamed: use password_hints"),
        ('[repos.a]\nmirror_path = "m"\nkey_split = true\n',
         "[repos.a] key_split was renamed: use split"),
        ('[repos.a]\nmirror_path = "m"\nkey_threshold = 2\n',
         "[repos.a] key_threshold was renamed: use split_threshold"),
        ('[repos.a]\nmirror_path = "m"\nkey_shares = 3\n',
         "[repos.a] key_shares was renamed: use split_shares"),
    ],
)
def test_old_config_key_is_an_error_naming_the_new_key(tmp_path, text, message):
    with pytest.raises(ValueError, match=r"was renamed") as exc:
        load_config(_toml(tmp_path, text))
    assert message in str(exc.value)


def test_old_config_key_fails_a_command_with_the_message(tmp_path, capsys):
    cfg = _toml(tmp_path, '[paths]\ndatabase = "x.db"\n')
    assert main(["--config", str(cfg), "status"]) == 1
    assert "[paths] database was renamed: use catalog" in capsys.readouterr().out


def test_new_config_keys_load(tmp_path):
    cfg = load_config(_toml(
        tmp_path,
        '[paths]\ncatalog = "c.db"\n'
        "[defaults]\nsplit = true\nsplit_threshold = 3\nsplit_shares = 5\n"
        '[survivability]\npassword_hints = "the attorney"\n'
        '[repos.a]\nmirror_path = "m"\nsplit = true\nsplit_threshold = 2\n'
        "split_shares = 3\n",
    ))
    assert cfg.db_path == tmp_path / "c.db"
    assert (cfg.key_split, cfg.key_threshold, cfg.key_shares) == (True, 3, 5)
    assert cfg.key_storage_hints == "the attorney"
    assert cfg.repositories["a"].key_split is True


def test_default_master_catalog_is_catalog_db(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert _xdg_db_path() == str(tmp_path / "lcsas" / "catalog.db")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert _xdg_db_path().endswith("/lcsas/catalog.db")


# ── the Python reader ───────────────────────────────────────────────


def test_python_reader_refuses_the_old_target_flag(tmp_path):
    from lcsas.restore.standalone_builder import build_standalone

    reader = tmp_path / "python_reader.py"
    reader.write_text(build_standalone())
    for flag in (["--target", "out"], ["--target=out"]):
        r = subprocess.run(
            [sys.executable, "-I", str(reader), "--repo", str(tmp_path),
             "--password-file", str(tmp_path / "pw"), *flag],
            capture_output=True, text=True, check=False,
        )
        assert r.returncode == 2
        assert r.stderr.strip() == (
            "python_reader.py: --target was renamed: use --restore-folder"
        )
    r = subprocess.run(
        [sys.executable, "-I", str(reader), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert "--restore-folder" in r.stdout


# ── the docs ────────────────────────────────────────────────────────


def test_the_docs_name_no_retired_lcsas_command():
    """No doc names a retired ``lcsas`` command, in prose or in code (#940:
    docs/README.md still said "the `lcsas key` commands").  A line that
    says the name was renamed is the exception."""
    root = Path(__file__).resolve().parents[2]
    docs = [root / "README.md", *sorted((root / "docs").rglob("*.md")),
            *sorted(p for p in (root / "recovery" / "docs").iterdir() if p.is_file())]
    assert len(docs) > 50, f"self-check: only {len(docs)} docs found"
    commands = [r.old for r in RENAMES if r.kind == "command"]
    assert "lcsas key" in commands
    patterns = [(old, re.compile(r"(?<![\w.-])" + re.escape(old) + r"(?![\w-])"))
                for old in commands]
    problems = []
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\brenamed\b", line):
                continue
            problems.extend(f"{doc.relative_to(root)}:{n}: {old!r}"
                            for old, pat in patterns if pat.search(line))
    assert not problems, (
        "docs name a retired lcsas command (tests/contract/renames.py has its "
        "replacement):\n" + "\n".join(problems))
