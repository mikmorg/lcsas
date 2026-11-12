"""``lcsas --help`` and every subcommand's ``--help`` are pinned (#690).

#690 splits ``src/lcsas/cli/main.py`` into a parser module and per-command
modules.  That move must not change a single line of help text, so the
help of every parser in the tree (the top level, each subcommand, each
sub-subcommand) is compared with a golden file generated before the split.

A group given no subcommand (``lcsas repo``) prints a usage line and exits
1; those lines are pinned here too, because the dispatcher that prints
them is what #690 replaces.

If you change help text on purpose, regenerate the golden file in the same
commit and say so in the PR::

    PYTHONPATH=src python3 tests/unit/test_cli_help_golden.py \\
        > tests/unit/golden/cli_help.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent / "golden" / "cli_help.txt"

# argparse wraps help to the terminal width; pin it.
_COLUMNS = "80"


def _walk(
    parser: argparse.ArgumentParser, path: list[str]
) -> list[tuple[list[str], argparse.ArgumentParser]]:
    """Every parser in the tree, depth first, in the order they were added."""
    found = [(path, parser)]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.extend(_walk(sub, [*path, name]))
    return found


def help_dump() -> str:
    """The help text of every parser, one section per command path."""
    from lcsas.cli.main import build_parser

    sections = []
    for path, parser in _walk(build_parser(), []):
        title = " ".join(["lcsas", *path])
        sections.append(f"===== {title} --help =====\n{parser.format_help()}")
    return "".join(sections)


@pytest.mark.skipif(
    sys.version_info >= (3, 13),
    reason="argparse reformats help from Python 3.13; the golden is from 3.11/3.12",
)
def test_help_output_matches_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    expected = GOLDEN.read_text(encoding="utf-8")
    actual = help_dump()
    assert actual == expected, (
        "CLI help text changed.  If that is intended, regenerate "
        f"{GOLDEN.name} (see this module's docstring)."
    )


def test_golden_covers_every_command() -> None:
    """The walk reaches every top-level command and their subcommands."""
    text = GOLDEN.read_text(encoding="utf-8")
    for title in ("lcsas --help", "lcsas repo add --help",
                  "lcsas key split --help", "lcsas restore standalone --help",
                  "lcsas estate card --help"):
        assert f"===== {title} =====" in text


# A group with subcommands, given none: (argv, usage line).  Exit code 1.
_BARE_GROUPS = [
    (["repo"], "Usage: lcsas repo {add,list,remove,retire,activate}"),
    (["pack"], "Usage: lcsas pack {unprune}"),
    (["config"], "Usage: lcsas config {check}"),
    (["staging"], "Usage: lcsas staging {clean}"),
    (["copy"], "Usage: lcsas copy {deprecate,destroy}"),
    (["volume"], "Usage: lcsas volume {impact}"),
    (["catalog"], "Usage: lcsas catalog {import-receipts|validate|reconcile|rebuild}"),
    (["session"], "Usage: lcsas session {list,abort}"),
    (["restore"], "Usage: lcsas restore {plan,exec,standalone}"),
    (["meta"], "Usage: lcsas meta {build,verify}"),
    (["key"], "Usage: lcsas key {split,unsplit,combine,verify,card}"),
    (["estate"], "Usage: lcsas estate card"),
    (["recovery"], "Usage: lcsas recovery {build,test,manifest,verify}"),
]


@pytest.mark.parametrize(("argv", "usage"), _BARE_GROUPS,
                         ids=[a[0] for a, _ in _BARE_GROUPS])
def test_bare_group_prints_usage(
    argv: list[str], usage: str, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lcsas.cli.main import main

    with caplog.at_level(logging.ERROR, logger="lcsas"):
        rc = main(argv)
    assert rc == 1
    assert [r.getMessage() for r in caplog.records] == [usage]
    # The log handler writes to stdout: the usage line is all there is.
    out, _ = capsys.readouterr()
    assert out.splitlines() == [usage]


def test_bare_location_needs_config(caplog: pytest.LogCaptureFixture) -> None:
    """``lcsas location`` goes to its handler, which asks for --config."""
    from lcsas.cli.main import main

    with caplog.at_level(logging.ERROR, logger="lcsas"):
        rc = main(["location"])
    assert rc == 1
    assert [r.getMessage() for r in caplog.records] == [
        "--config is required for location."
    ]


def test_no_command_prints_top_level_help(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lcsas.cli.main import build_parser, main

    monkeypatch.setenv("COLUMNS", _COLUMNS)
    assert main([]) == 0
    out, _ = capsys.readouterr()
    assert out == build_parser().format_help()


if __name__ == "__main__":
    os.environ["COLUMNS"] = _COLUMNS
    sys.stdout.write(help_dump())
