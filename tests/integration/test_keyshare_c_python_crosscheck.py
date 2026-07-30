"""C-vs-Python parity for prefix entry + per-share diagnostics (KEY-07).

Runs prefix-typed and typo'd share sets through BOTH the committed static
``lcsas-keyshare`` binary and the Python ``lcsas.keyshare`` path, and asserts
they agree: identical accept/reject verdicts, byte-identical recovered
passwords on accept, and the same offending-word diagnosis on reject.

Skipped when the committed binary is absent (e.g. a source-only checkout).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main as cli_main
from lcsas.keyshare import (
    KeyShareError,
    check_share,
    decode_master_secret,
    extract_mnemonic,
    recover_secret,
)

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


def _mnemonic_of(card: Path) -> str:
    return extract_mnemonic(card.read_text(encoding="utf-8"), source=str(card))


def _prefix4(mnemonic: str) -> str:
    return " ".join(w[:4] for w in mnemonic.split())


def _c_recover(files: list[Path]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_KEYSHARE_BIN), *map(str, files)],
        capture_output=True,
        check=False,
    )


def test_prefix_entry_parity(tmp_path: Path) -> None:
    """4-letter-prefix files recover byte-exact in BOTH implementations."""
    cards = _split_cards(tmp_path)
    m0, m1 = _mnemonic_of(cards[0]), _mnemonic_of(cards[1])

    # Python path.
    py = decode_master_secret(recover_secret([_prefix4(m0), _prefix4(m1)]))
    assert py == _PASSWORD

    # C path: write prefix-only bare-mnemonic files and recover.
    p0 = tmp_path / "p0.txt"
    p1 = tmp_path / "p1.txt"
    p0.write_text(_prefix4(m0) + "\n")
    p1.write_text(_prefix4(m1) + "\n")
    proc = _c_recover([p0, p1])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == _PASSWORD == py


def test_typo_rejected_by_both(tmp_path: Path) -> None:
    """A mistyped word is rejected by BOTH, naming position + token."""
    cards = _split_cards(tmp_path)
    m0, m1 = _mnemonic_of(cards[0]), _mnemonic_of(cards[1])
    words = m0.split()
    words[6] = "buidling"  # word 7: not a word, not a unique prefix
    bad = " ".join(words)

    # Python verdict via the per-share check.
    py_reason = check_share(bad)
    assert py_reason is not None
    assert "word 7" in py_reason
    assert "buidling" in py_reason
    with pytest.raises(KeyShareError):
        recover_secret([bad, m1])

    # C verdict: write the typo'd share and a good one; expect a non-zero exit,
    # the named diagnostic on stderr, and no password printed.
    b0 = tmp_path / "bad0.txt"
    g1 = tmp_path / "good1.txt"
    b0.write_text(bad + "\n")
    g1.write_text(m1 + "\n")
    proc = _c_recover([b0, g1])
    assert proc.returncode != 0
    stderr = proc.stderr.decode("utf-8", "replace")
    assert "word 7" in stderr
    assert "buidling" in stderr
    assert proc.stdout == b""


def test_mismatched_split_parity(tmp_path: Path) -> None:
    """Individually-valid shares from different splits fail in BOTH paths."""
    cards_a = _split_cards(tmp_path)
    out_b = tmp_path / "shares_b"
    pw_b = tmp_path / "pw_b"
    pw_b.write_bytes(b"a different password")
    assert cli_main([
        "key", "split", "--repo", "beta",
        "--threshold", "2", "--shares", "5",
        "--password-file", str(pw_b), "--out", str(out_b),
    ]) == 0
    cards_b = sorted(out_b.glob("beta-share-*-card.txt"))

    a0 = _mnemonic_of(cards_a[0])
    b0 = _mnemonic_of(cards_b[0])

    # Each share is individually valid (passes the per-share check)...
    assert check_share(a0) is None
    assert check_share(b0) is None
    # ...but the cross-split set cannot recover.
    with pytest.raises(KeyShareError):
        recover_secret([a0, b0])

    proc = _c_recover([cards_a[0], cards_b[0]])
    assert proc.returncode != 0
    stderr = proc.stderr.decode("utf-8", "replace")
    # Both shares reported OK individually, then a set-level failure + hint.
    assert "OK" in stderr
    assert "SAME archive" in stderr
    assert proc.stdout == b""
