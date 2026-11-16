"""The lock-holder label names the full command (#826)."""

from __future__ import annotations

import pytest

from lcsas.cli.main import _holder_label
from lcsas.cli.parser import build_parser


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["session", "abort"], "lcsas session abort"),
        (["catalog", "rebuild", "disc", "--output", "out.db"],
         "lcsas catalog rebuild"),
        (["location", "move", "V", "--from", "A", "--to", "B"],
         "lcsas location move"),
        (["key", "split", "--repo", "r"], "lcsas key split"),
        (["status"], "lcsas status"),
    ],
)
def test_holder_label_names_the_subcommand(argv: list[str], label: str) -> None:
    assert _holder_label(build_parser().parse_args(argv)) == label
