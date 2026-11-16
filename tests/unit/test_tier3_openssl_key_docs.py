"""#794: the docs say what the openssl command exposes, and how to avoid it.

The ``openssl`` command backend of tier 3 takes the repository's data
key on its command line.  The heir-facing text (TIERS.txt, ENV_VARS.txt
and the generated reader's header) must say that the command line can
be logged, which key it is, and how to choose the pure-Python AES.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.restore import _aes_fast
from lcsas.restore.standalone_builder import build_standalone

_DOCS = Path(__file__).resolve().parents[2] / "recovery" / "docs"


def _flat(text: str) -> str:
    return " ".join(text.replace("#", " ").split())


@pytest.mark.parametrize("where", ["TIERS.txt", "ENV_VARS.txt", "header"])
def test_key_exposure_is_described(where: str) -> None:
    if where == "header":
        text = build_standalone().split("from __future__", 1)[0]
    else:
        text = (_DOCS / where).read_text(encoding="utf-8")
    flat = _flat(text)
    assert "data key" in flat
    assert "not the password" in flat or "not your password" in flat
    assert "audit log" in flat
    assert "LCSAS_TIER3_AES=pure" in flat


def test_the_variable_named_is_the_one_read() -> None:
    """The docs name LCSAS_TIER3_AES; _aes_fast reads exactly that."""
    import inspect

    assert 'os.environ.get("LCSAS_TIER3_AES"' in inspect.getsource(
        _aes_fast.select_aes_ctr_backend)
