"""``--config`` placed AFTER the subcommand (issue #517, the "low" bullet).

``--config`` (like ``--db``) is a flag of the top-level parser only, so the
working form is ``lcsas --config foo.toml scan``.  Operators naturally write
``lcsas scan --config foo.toml``.  ``docs/workflows/init-and-config.md``
(Notes & gaps) records what happens: argparse rejects it with a bare
``unrecognized arguments: --config foo.toml`` that never says the flag belongs
before the subcommand ("Still open: usability only").

These tests pin the behaviour: a hard, early argparse exit 2 (never a silent
fallback to another catalog, never a half-run command) whose message now says
the flag is a top-level option that goes before the subcommand.
"""

from __future__ import annotations

import re

import pytest

from lcsas.cli.main import main

# The doc's own example (config check) plus the most common one (scan), in
# both ``--config X`` and ``--config=X`` spellings.
_MISPLACED = [
    pytest.param(["scan", "--config", "x.toml"], id="scan-space"),
    pytest.param(["scan", "--config=x.toml"], id="scan-equals"),
    pytest.param(["config", "check", "--config", "x.toml"], id="config-check"),
    pytest.param(["stage", "--dry-run", "--config", "x.toml"], id="stage-after-flag"),
]


def _run_expecting_exit(argv: list[str], capsys) -> tuple[object, str]:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code, capsys.readouterr().err


@pytest.mark.parametrize("argv", _MISPLACED)
def test_config_after_subcommand_is_an_argparse_usage_error(
    argv, capsys, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    code, err = _run_expecting_exit(argv, capsys)

    assert code == 2  # argparse usage error, not a handler's return value
    assert "unrecognized arguments:" in err
    assert "--config" in err.split("unrecognized arguments:", 1)[1]
    # Nothing ran: no archive.db was created in the cwd by a handler that
    # silently fell back to the default catalog, and the (nonexistent)
    # x.toml was never opened.
    assert list(tmp_path.iterdir()) == []


def test_config_before_the_subcommand_is_honoured(tmp_path, capsys, monkeypatch):
    """The contrast case: the documented form reaches the handler, which then
    reports on the config file itself (rc 1, a real message) -- not argparse."""
    monkeypatch.chdir(tmp_path)

    rc = main(["--config", str(tmp_path / "missing.toml"), "scan"])

    assert rc == 1
    out = capsys.readouterr()
    assert "--config path does not exist" in out.out + out.err


@pytest.mark.parametrize("argv", _MISPLACED)
def test_error_tells_the_operator_config_goes_before_the_subcommand(
    argv, capsys, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    _code, err = _run_expecting_exit(argv, capsys)

    assert re.search(
        r"before (the )?sub-?command|top-level (option|flag)|global (option|flag)",
        err,
        re.IGNORECASE,
    ), err


def test_misplaced_db_gets_the_same_hint(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code, err = _run_expecting_exit(["status", "--db", "x.db"], capsys)

    assert code == 2
    assert "--db: top-level option(s); put them before the subcommand" in err
    assert list(tmp_path.iterdir()) == []


def test_other_unrecognized_arguments_get_no_hint(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code, err = _run_expecting_exit(["scan", "--no-such-flag"], capsys)

    assert code == 2
    assert "unrecognized arguments: --no-such-flag" in err
    assert "top-level option" not in err
