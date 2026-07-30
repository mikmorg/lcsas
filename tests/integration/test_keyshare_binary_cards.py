"""Binary-level proof that the committed lcsas-keyshare accepts cards (KEY-01).

Subprocesses the committed static ``recovery/bin/x86_64/lcsas-keyshare``
with real ``-card.txt`` artifacts (generated via ``lcsas key split``) and
asserts a byte-exact password.  Catches the "library fixed but shipped
binary stale" failure: if the committed bin predates the source fix this
test fails even though the unit tests pass.

Skipped when the committed binary is absent (e.g. a source-only checkout).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main as cli_main

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KEYSHARE_BIN = _REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-keyshare"

_PASSWORD = b"correct horse battery staple\x01"

# `make test-integration` selects with `-m integration`; without the marker
# this module is deselected and runs in no gate target at all (#426).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _KEYSHARE_BIN.is_file(),
        reason="committed recovery/bin/x86_64/lcsas-keyshare not present",
    ),
]


def _split_cards(tmp_path: Path) -> list[Path]:
    pw_file = tmp_path / "pw"
    pw_file.write_bytes(_PASSWORD)
    out = tmp_path / "shares"
    assert cli_main([
        "key", "split", "--repo", "alpha",
        "--threshold", "2", "--shares", "5",
        "--password-file", str(pw_file), "--out", str(out),
    ]) == 0
    cards = sorted(out.glob("alpha-share-*-card.txt"))
    assert len(cards) == 5
    return cards


def test_committed_binary_accepts_card_files(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    proc = subprocess.run(
        [str(_KEYSHARE_BIN), str(cards[0]), str(cards[3])],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == _PASSWORD


def test_committed_binary_accepts_card_stdin(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    piped = cards[0].read_bytes() + cards[1].read_bytes()
    proc = subprocess.run(
        [str(_KEYSHARE_BIN)],
        input=piped,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == _PASSWORD


def test_committed_binary_rejects_truncated_card(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    # Strip the share-words line so only the header survives.
    from lcsas.keyshare import is_mnemonic_line
    kept = [
        ln for ln in cards[0].read_text().splitlines()
        if not is_mnemonic_line(ln)
    ]
    trunc = tmp_path / "trunc-card.txt"
    trunc.write_text("\n".join(kept) + "\n")
    proc = subprocess.run(
        [str(_KEYSHARE_BIN), str(trunc), str(cards[1])],
        capture_output=True,
        check=False,
    )
    assert proc.returncode != 0
    # KEY-07: a truncated card is rejected loudly.  Either it has no
    # plausible share line ("share words"/"complete share card"), or the
    # per-share pre-pass flags the densest surviving (prose) line as not a
    # valid share — both name the problem; never a silent partial password.
    assert (
        b"share words" in proc.stderr
        or b"complete share card" in proc.stderr
        or b"failed individual validation" in proc.stderr
    )
    assert proc.stdout == b""
