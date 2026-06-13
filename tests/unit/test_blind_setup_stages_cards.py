"""KEY-04: the docs-driven blind variant stages production card artifacts.

The split-key-docs variant must stage the printable ``-card.txt`` files an
heir actually receives — header + usage text + mnemonic — NOT bare mnemonic
files.  This pins the card-staging helper in setup.py: the staged files end
in ``-card.txt``, carry the ``LCSAS KEY SHARE`` header AND a full-length
SLIP-0039 mnemonic line, are mode 0600, and reconstruct the password.  Only
the holder's quorum (2 of 5) is staged; no bare share file is written.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from lcsas.keyshare import (
    combine_mnemonics,
    decode_master_secret,
    extract_mnemonic,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_SETUP_PY = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "setup.py"

_spec = importlib.util.spec_from_file_location("blind_setup", _SETUP_PY)
assert _spec is not None and _spec.loader is not None
_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_setup)

_SECRET = b"correct horse battery staple"


@pytest.fixture
def staged(tmp_path: Path) -> list[Path]:
    return _setup._stage_split_key_cards(
        "alpha", threshold=2, count=5, out_dir=tmp_path, secret=_SECRET
    )


def test_only_quorum_staged(staged: list[Path]) -> None:
    assert len(staged) == 2


def test_filenames_are_card_files(staged: list[Path]) -> None:
    for p in staged:
        assert p.name.endswith("-card.txt"), p.name
    names = sorted(p.name for p in staged)
    assert names == ["alpha-share-1-card.txt", "alpha-share-2-card.txt"]


def test_no_bare_share_file_staged(staged: list[Path]) -> None:
    out_dir = staged[0].parent
    bare = list(out_dir.glob("alpha-share-*.txt"))
    # Every staged file must be a -card.txt; a bare alpha-share-N.txt
    # (no -card) would defeat the card-parsing exercise.
    assert all(p.name.endswith("-card.txt") for p in bare), [
        p.name for p in bare
    ]


def test_cards_have_header_and_mnemonic(staged: list[Path]) -> None:
    for p in staged:
        text = p.read_text(encoding="utf-8")
        assert "LCSAS KEY SHARE" in text
        mnemonic = extract_mnemonic(text, source=str(p))
        # A SLIP-0039 share mnemonic is always >= 20 words (the share
        # header alone occupies the first several); the exact count grows
        # with the framed-secret length.  The recombine test below proves
        # the words are genuine; here we just pin a full-length line, not a
        # truncated one.
        assert len(mnemonic.split()) >= 20


def test_cards_are_mode_0600(staged: list[Path]) -> None:
    for p in staged:
        assert (p.stat().st_mode & 0o777) == 0o600


def test_cards_reconstruct_password(staged: list[Path]) -> None:
    mnemonics = [
        extract_mnemonic(p.read_text(encoding="utf-8"), source=str(p))
        for p in staged
    ]
    master = combine_mnemonics(mnemonics)
    assert decode_master_secret(master) == _SECRET
