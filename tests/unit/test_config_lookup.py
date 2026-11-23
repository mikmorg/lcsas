"""The default config lookup, and only ``lcsas init`` creates a catalog (#964).

The config is ``--config``, then ``$LCSAS_CONFIG``, then
``$XDG_CONFIG_HOME/lcsas/lcsas.toml`` (``~/.config/lcsas/lcsas.toml``).
The catalog is ``--catalog``, then the config's ``[paths] catalog``; with
neither there is no catalog, and the refusal names the ways to give one.
No command but ``init`` creates a catalog (#941): a missing one is "No
catalog at X. Run `lcsas init` first."

Before #964 every command needed ``--config`` typed out, and forgetting it
made ``repo add``, ``verify --all``, ``copy destroy``, ``consolidate`` and
``volume impact`` create ``./catalog.db`` in whatever folder the owner
stood in.  The two walks below run every command the parser has.

``tests/conftest.py`` points ``XDG_CONFIG_HOME`` at an empty folder and
unsets ``LCSAS_CONFIG`` for the whole session; the tests here set them
(and ``HOME``) themselves.
"""

from __future__ import annotations

import argparse
import io
import logging
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.cli.parser import build_parser

# Every leaf command, with arguments that get it past argparse.  MIRROR is
# replaced with a backup set's mirror folder.  test_the_table_covers_every_
# command fails when the parser gains a command this table lacks.
_COMMANDS: dict[str, list[str]] = {
    "init": ["init"],
    "repo add": ["repo", "add", "alpha", "MIRROR"],
    "repo list": ["repo", "list"],
    "repo remove": ["repo", "remove", "alpha"],
    "repo retire": ["repo", "retire", "alpha"],
    "repo activate": ["repo", "activate", "alpha"],
    "scan": ["scan"],
    "pack unprune": ["pack", "unprune", "ab" * 32],
    "status": ["status"],
    "burn": ["burn", "--session", "S1", "--no-prompt"],
    "stage": ["stage", "--dry-run"],
    "burn-iso": ["burn-iso", "missing.iso"],
    "staging clean": ["staging", "clean", "--force"],
    "location list": ["location", "list"],
    "location add": ["location", "add", "Shelf"],
    "location status": ["location", "status", "Shelf"],
    "location rename": ["location", "rename", "Shelf", "Box"],
    "location remove": ["location", "remove", "Shelf"],
    "location move": ["location", "move", "--from", "Shelf", "--to", "Box",
                      "LCSAS_X_001"],
    "copy deprecate": ["copy", "deprecate", "LCSAS_X_001", "Shelf"],
    "copy destroy": ["copy", "destroy", "LCSAS_X_001", "Shelf"],
    "volume impact": ["volume", "impact", "LCSAS_X_001"],
    "catalog import-receipts": ["catalog", "import-receipts", "missing.json"],
    "catalog validate": ["catalog", "validate", "missing-disc"],
    "catalog reconcile": ["catalog", "reconcile"],
    "catalog rebuild": ["catalog", "rebuild", "--output", "OUT/rebuilt.db",
                        "missing-disc"],
    "restore plan": ["restore", "plan", "--repo", "alpha", "latest"],
    "restore exec": ["restore", "exec", "--repo", "alpha", "--password-file",
                     "missing.key", "latest", "OUT/restored"],
    "restore from-disc": ["restore", "from-disc", "--password-file",
                          "missing.key", "missing-disc", "OUT/restored"],
    "consolidate": ["consolidate", "LCSAS_X_001"],
    "verify": ["verify", "--all"],
    "session list": ["session", "list"],
    "session abort": ["session", "abort", "S1"],
    "config check": ["config", "check"],
    "meta verify": ["meta", "verify", "missing-meta"],
    "password split": ["password", "split", "--repo", "alpha"],
    "password verify": ["password", "verify", "--repo", "alpha",
                        "--password-file", "KEY"],
    "password combine": ["password", "combine"],
    "password unsplit": ["password", "unsplit", "--repo", "alpha", "--yes"],
    "password card": ["password", "card", "--repo", "alpha"],
    "estate card": ["estate", "card"],
}

# Not run here, with the reason.
_NOT_RUN = {
    "meta build": "builds a META disc from the checkout (minutes); it reads "
                  "the catalog only when the file is there",
    "recovery build": "runs make in recovery/; touches no catalog",
    "recovery test": "runs make in recovery/; touches no catalog",
    "recovery manifest": "writes recovery/ manifests; touches no catalog",
    "recovery verify": "checks recovery/ manifests; touches no catalog",
}

# Commands that run without the master catalog: they never open it
# (password verify checks the password against the mirror's keys only),
# or, the two cards, print blanks for what it would have filled in (by
# design: a card is always printable).  The mistyped-path walk checks
# only that they create nothing.
_NO_MASTER_CATALOG = {
    "burn-iso", "catalog validate", "catalog rebuild", "restore from-disc",
    "meta verify", "password combine", "config check", "estate card",
    "password card", "password verify",
}


def _leaves(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every leaf command of *parser*, as "group sub"."""
    out: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen: set[int] = set()
            for name, sub in action.choices.items():
                if id(sub) in seen:
                    continue
                seen.add(id(sub))
                out += _leaves(sub, f"{prefix}{name} ") or [f"{prefix}{name}"]
    return out


def test_the_table_covers_every_command() -> None:
    leaves = set(_leaves(build_parser()))
    assert "repo add" in leaves and "estate card" in leaves  # the walk works
    assert leaves == set(_COMMANDS) | set(_NOT_RUN)


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty HOME, no XDG_CONFIG_HOME, no LCSAS_CONFIG, cwd an empty
    folder, and stdin at end of file (a prompt reads EOF, never hangs)."""
    h = tmp_path / "home"
    h.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (tmp_path / "OUT").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LCSAS_CONFIG", raising=False)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return h


def _argv(name: str, tmp_path: Path) -> list[str]:
    return [a.replace("MIRROR", str(tmp_path / "mirror" / "alpha"))
             .replace("OUT", str(tmp_path / "OUT"))
             .replace("KEY", str(tmp_path / "alpha.key"))
            for a in _COMMANDS[name]]


def _config(path: Path, tmp_path: Path, catalog: Path) -> Path:
    """A config whose backup set and folders exist; its catalog is
    *catalog*."""
    mirror = tmp_path / "mirror" / "alpha"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "config").write_text("")
    (tmp_path / "staging").mkdir(exist_ok=True)
    key = tmp_path / "alpha.key"
    key.write_text("pw\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[paths]\n"
        f'mirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'catalog = "{catalog}"\n'
        "[defaults]\n"
        'media_type = "TEST_TINY"\n'
        'location = "Shelf"\n'
        "metadata_reserve_mb = 0\n"
        "[survivability]\n"
        'password_hints = "In the safe"\n'
        "[repos.alpha]\n"
        f'mirror_path = "{mirror}"\n'
        f'password_file = "{key}"\n'
    )
    return path


# -- the walks ------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_with_no_config_no_command_creates_a_file(
    name: str, tmp_path: Path, home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty folder, no config anywhere: every command refuses or runs
    read-only, and nothing is written (#964; on master ``repo add``
    created ./catalog.db and reported success)."""
    before = _tree(tmp_path)
    main(_argv(name, tmp_path))
    assert _tree(tmp_path) == before
    assert "Unexpected error" not in caplog.text


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_a_mistyped_catalog_is_refused_and_not_created(
    name: str, tmp_path: Path, home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A config at the default path whose ``[paths] catalog`` names a
    file that is not there: every command that opens the catalog says
    "No catalog at X ... Run `lcsas init` first", and none creates X
    (#941).  ``init`` is the one command that creates it."""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    catalog = db_dir / "catalgo.db"
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path, catalog)
    before = _tree(tmp_path)
    rc = main(_argv(name, tmp_path))
    assert "Unexpected error" not in caplog.text
    if name == "init":
        assert rc == 0
        assert catalog.is_file()
        return
    assert not catalog.exists()
    assert _tree(tmp_path) == before
    if name not in _NO_MASTER_CATALOG:
        assert rc != 0
        assert f"No catalog at {catalog}" in caplog.text
        # "Run `lcsas init` first"; password split words its own.
        assert "init" in caplog.text


# -- where the config comes from -----------------------------------------


def test_the_default_path_is_used(tmp_path: Path, home: Path) -> None:
    catalog = tmp_path / "db" / "catalog.db"
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path, catalog)
    assert main(["init"]) == 0
    assert catalog.is_file()
    assert main(["status"]) == 0


def test_xdg_config_home_is_honoured(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg = tmp_path / "xdg"
    catalog = tmp_path / "db" / "catalog.db"
    _config(xdg / "lcsas" / "lcsas.toml", tmp_path, catalog)
    # A config at ~/.config is not the one: XDG_CONFIG_HOME replaces it.
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path,
            tmp_path / "db" / "wrong.db")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert main(["init"]) == 0
    assert catalog.is_file()
    assert not (tmp_path / "db" / "wrong.db").exists()


def test_lcsas_config_outranks_the_default_path(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "db" / "catalog.db"
    cfg = _config(tmp_path / "elsewhere.toml", tmp_path, catalog)
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path,
            tmp_path / "db" / "wrong.db")
    monkeypatch.setenv("LCSAS_CONFIG", str(cfg))
    assert main(["init"]) == 0
    assert catalog.is_file()
    assert not (tmp_path / "db" / "wrong.db").exists()


def test_config_flag_outranks_lcsas_config(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "db" / "catalog.db"
    cfg = _config(tmp_path / "typed.toml", tmp_path, catalog)
    env = _config(tmp_path / "env.toml", tmp_path, tmp_path / "db" / "env.db")
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path,
            tmp_path / "db" / "default.db")
    monkeypatch.setenv("LCSAS_CONFIG", str(env))
    assert main(["--config", str(cfg), "init"]) == 0
    assert catalog.is_file()
    assert not (tmp_path / "db" / "env.db").exists()
    assert not (tmp_path / "db" / "default.db").exists()


def test_config_check_reads_the_config_found_by_lookup(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``config check`` opens the config file itself; the lookup must
    reach it too."""
    cfg = _config(tmp_path / "env.toml", tmp_path, tmp_path / "db" / "c.db")
    monkeypatch.setenv("LCSAS_CONFIG", str(cfg))
    (tmp_path / "db").mkdir()
    main(["config", "check"])
    assert "A config is required" not in caplog.text
    assert "is required" not in caplog.text


def test_lcsas_config_naming_a_missing_file_is_refused(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A variable the owner set is not a default: a path that is not
    there is an error that names the variable, not a quiet fallback."""
    _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path,
            tmp_path / "db" / "default.db")
    monkeypatch.setenv("LCSAS_CONFIG", str(tmp_path / "typo.toml"))
    before = _tree(tmp_path)
    assert main(["init"]) == 1
    assert f"LCSAS_CONFIG names {tmp_path / 'typo.toml'}" in caplog.text
    assert _tree(tmp_path) == before


def test_verbose_says_which_config_is_in_use(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _config(home / ".config" / "lcsas" / "lcsas.toml", tmp_path,
                  tmp_path / "db" / "catalog.db")
    assert main(["--verbose", "init"]) == 0
    out = capsys.readouterr().out
    assert out.count(f"Using the config {cfg}") == 1


# -- no config ------------------------------------------------------------


@pytest.mark.parametrize("argv", [["status"], ["init"], ["repo", "list"]])
def test_no_config_and_no_catalog_names_the_ways_to_give_one(
    argv: list[str], tmp_path: Path, home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="lcsas"):
        assert main(argv) == 1
    text = caplog.text
    assert "No config found" in text
    assert "--config" in text
    assert "LCSAS_CONFIG" in text
    assert str(home / ".config" / "lcsas" / "lcsas.toml") in text
    assert "--catalog" in text
    assert not (tmp_path / "cwd" / "catalog.db").exists()


def test_a_command_that_needs_a_config_names_the_ways_to_give_one(
    tmp_path: Path, home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    assert main(["scan"]) == 1
    assert "A config is required for scan, and none was found" in caplog.text
    assert "LCSAS_CONFIG" in caplog.text


def test_no_home_directory_is_a_worded_refusal(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No HOME and no passwd entry (a container run as a bare uid):
    ``Path.home()`` raises, and the lookup must still end in words."""
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    assert main(["status"]) == 1
    assert "No config found" in caplog.text
    assert "Unexpected error" not in caplog.text


def test_init_catalog_path_needs_no_config(tmp_path: Path, home: Path) -> None:
    db = tmp_path / "new" / "catalog.db"
    assert main(["init", "--catalog-path", str(db)]) == 0
    assert db.is_file()
    assert main(["--catalog", str(db), "repo", "list"]) == 0


def test_repo_add_with_catalog_flag_and_no_init_is_refused(
    tmp_path: Path, home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The #941 case: ``--catalog`` at a path with no catalog."""
    db = tmp_path / "new.db"
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "config").write_text("")
    assert main(["--catalog", str(db), "repo", "add", "work", str(mirror)]) == 1
    assert f"No catalog at {db}" in caplog.text
    assert "Run `lcsas init` first" in caplog.text
    assert not db.exists()
