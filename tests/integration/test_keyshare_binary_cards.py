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


# ── rc6 (#476 drill): the exit-status contract and more than K shares ──
#
# restore.bat / restore.sh drive the committed binary one share at a time
# and branch on its exit status: 0 rebuilt, 1 the shares could not be
# combined (too few, mixed splits), 2 usage / unreadable file / no shares,
# 3 a share is not valid (re-ask that card).  These pin it on the
# committed binary, so a stale bin cannot ship the old all-ones contract.


def _run(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_KEYSHARE_BIN), *args], input=stdin, capture_output=True, check=False
    )


def test_committed_binary_accepts_more_than_k_cards(tmp_path: Path) -> None:
    """The cards say "any K"; a reader who types every card they hold
    (4 of a 2-of-5 here, one of them twice) must get the password."""
    cards = _split_cards(tmp_path)
    proc = _run([str(cards[4]), str(cards[0]), str(cards[2]), str(cards[0])])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == _PASSWORD


def test_committed_binary_typo_is_exit_3_and_named(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    words = [ln for ln in cards[1].read_text().splitlines()
             if ln.startswith("  ") and len(ln.split()) >= 20][0].split()
    words[6] = "buidling"
    bad = tmp_path / "typed-share.txt"
    bad.write_text(" ".join(words) + "\n")
    proc = _run([str(bad)])
    assert proc.returncode == 3, proc.stderr
    assert b"word 7 'buidling' is not a share word" in proc.stderr
    assert proc.stdout == b""


def test_committed_binary_too_few_or_mixed_is_exit_1(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    proc = _run([str(cards[2])])
    assert proc.returncode == 1, proc.stderr
    assert b"SAME archive" in proc.stderr
    assert proc.stdout == b""


def test_committed_binary_usage_errors_are_exit_2(tmp_path: Path) -> None:
    assert _run([str(tmp_path / "no-such-card.txt")]).returncode == 2
    assert _run(["--bogus"]).returncode == 2
    assert _run([], stdin=b"no shares here\n").returncode == 2


def test_committed_binary_truncated_card_is_exit_3(tmp_path: Path) -> None:
    cards = _split_cards(tmp_path)
    trunc = tmp_path / "words-cut-off.txt"
    trunc.write_text("academic acid acne\n")
    proc = _run([str(trunc), str(cards[1])])
    assert proc.returncode == 3, proc.stderr
    assert b"is this a complete share card?" in proc.stderr
    assert proc.stderr.isascii(), proc.stderr


def test_committed_binary_stdin_typo_is_named_not_skipped(tmp_path: Path) -> None:
    """On stdin a mistyped share line used to be dropped as prose."""
    cards = _split_cards(tmp_path)
    good = cards[0].read_bytes()
    words = [ln for ln in cards[3].read_text().splitlines()
             if ln.startswith("  ") and len(ln.split()) >= 20][0].split()
    words[4] = "zzzz"
    piped = good + (" ".join(words) + "\n").encode()
    proc = _run([], stdin=piped)
    assert proc.returncode == 3, proc.stderr
    assert b"word 5 'zzzz' is not a share word" in proc.stderr
    # The card's prose lines (one has 14 tokens) are still skipped.
    assert b"'tip:'" not in proc.stderr.lower()


def test_committed_binary_help_documents_exit_status() -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    for line in (b"  0  the password was rebuilt", b"  1  the shares could not",
                 b"  2  usage error", b"  3  a share is not valid"):
        assert line in proc.stdout
    assert proc.stdout.isascii()
