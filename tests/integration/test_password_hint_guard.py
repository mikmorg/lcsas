"""#960: no disc is staged with a placeholder where the password hint goes.

``[survivability] password_hints`` is the one line on a disc that tells
the heir where the password is.  ``config check`` called a placeholder,
an empty or a missing hint valid, ``stage`` proceeded, and every disc's
START_HERE.txt then said "The archive's owner should have stored the
password in a safe place."  A disc cannot be corrected after it is
burned.

Now ``stage`` and ``estate card`` refuse and ``config check`` reports an
error, unless ``--allow-no-password-hint``: then the volume records the
override and the disc says plainly that no hint was given.

Real rustic, real xorriso, every command through ``main()``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.integration.scratch_install import HINT, ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_BASE = '[survivability]\narchive_owner = "Test Owner"\n'
_NO_HINTS = {
    "placeholder": _BASE + 'password_hints = "EDIT ME: where each password is kept"\n',
    "empty": _BASE + 'password_hints = ""\n',
    "missing": "",
    "example": _BASE + 'password_hints = "Sealed envelope with the family '
                       'attorney (see my will)"\n',
}
_MESSAGE = "[survivability] password_hints is not set"


def _install(root: Path, survivability: str) -> ScratchInstall:
    inst = ScratchInstall(root, repos=("alpha",), survivability=survivability)
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def _start_here(inst: ScratchInstall) -> str:
    (tree,) = inst.disc_trees().values()
    return (tree / "START_HERE.txt").read_text()


@pytest.mark.parametrize("kind", sorted(_NO_HINTS))
def test_no_hint_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str,
) -> None:
    inst = _install(tmp_path / "x", _NO_HINTS[kind])
    before = inst.snapshot()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 1
        assert _MESSAGE in caplog.text
        assert inst.snapshot() == before
        caplog.clear()
        assert inst.lcsas("config", "check") == 1
        assert _MESSAGE in caplog.text
        caplog.clear()
        assert inst.lcsas("estate", "card") == 1
        assert _MESSAGE in caplog.text


def test_the_flag_stages_records_it_and_the_disc_says_so(
    tmp_path: Path,
) -> None:
    inst = _install(tmp_path / "x", _NO_HINTS["placeholder"])
    assert inst.lcsas("stage", "--allow-no-password-hint") == 0
    notes = [str(r[0]) for r in inst.query(
        "SELECT detail FROM volume_events WHERE event_type = 'NOTE'")]
    assert any("--allow-no-password-hint" in n for n in notes), notes
    text = _start_here(inst)
    assert "gave no hint of where the password is kept" in text
    assert "should have stored the password in a safe place" not in text
    assert "EDIT ME" not in text


def test_a_real_hint_is_unchanged(tmp_path: Path) -> None:
    inst = _install(tmp_path / "x", None)  # type: ignore[arg-type]
    assert inst.lcsas("config", "check") == 0
    assert inst.lcsas("stage") == 0
    assert HINT in _start_here(inst)
    assert not inst.query(
        "SELECT 1 FROM volume_events WHERE detail LIKE '%password hint%'")
