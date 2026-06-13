"""Unit tests for :mod:`lcsas.keyshare.cards` (KEY-01).

``extract_mnemonic`` is the shared card-tolerant extractor used by all
three combiners; ``is_mnemonic_line`` is its line predicate.  These tests
pin the accept/reject contract on bare mnemonics, full cards, wrapped and
CRLF variants, and assert the card template can never be absorbed by the
wordlist filter (design point 6 of the plan).
"""

from __future__ import annotations

import pytest

from lcsas.cli.main import _share_card_text
from lcsas.keyshare import (
    KeyShareError,
    encode_master_secret,
    extract_mnemonic,
    is_mnemonic_line,
    split_secret,
)


@pytest.fixture(scope="module")
def share() -> str:
    """One real SLIP-0039 share mnemonic (2-of-5 split of a password)."""
    ms = encode_master_secret(b"correct horse battery staple")
    return split_secret(ms, 2, 5)[0]


class TestIsMnemonicLine:
    def test_all_wordlist_tokens(self, share: str) -> None:
        assert is_mnemonic_line(share) is True

    def test_case_insensitive(self, share: str) -> None:
        assert is_mnemonic_line(share.upper()) is True

    def test_blank_line(self) -> None:
        assert is_mnemonic_line("") is False
        assert is_mnemonic_line("   \t ") is False

    def test_prose_line(self) -> None:
        assert is_mnemonic_line("This is not a share.") is False

    def test_single_non_wordlist_token_rejects(self, share: str) -> None:
        # One bogus token poisons the whole line.
        assert is_mnemonic_line(share + " ================") is False


class TestExtractMnemonic:
    def test_bare_mnemonic(self, share: str) -> None:
        assert extract_mnemonic(share) == share

    def test_full_card_text(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, share)
        assert extract_mnemonic(card) == share

    def test_wrapped_mnemonic(self, share: str) -> None:
        # A hand-retyped card splitting the words across three lines.
        words = share.split()
        third = len(words) // 3
        wrapped = (
            "THE SHARE WORDS\n"
            "  " + " ".join(words[:third]) + "\n"
            "  " + " ".join(words[third:2 * third]) + "\n"
            "  " + " ".join(words[2 * third:]) + "\n"
        )
        assert extract_mnemonic(wrapped) == share

    def test_crlf_line_endings(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, share)
        crlf = card.replace("\n", "\r\n")
        assert extract_mnemonic(crlf) == share

    def test_mixed_case(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, share.upper())
        assert extract_mnemonic(card) == share

    def test_empty_file_rejected(self) -> None:
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic("", source="empty.txt")
        assert "empty.txt" in str(exc.value)
        assert "0 share words" in str(exc.value)

    def test_prose_only_rejected(self) -> None:
        text = "Repository : alpha\n(the holder lost the words)\n"
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic(text, source="prose.txt")
        assert "prose.txt" in str(exc.value)

    def test_truncated_card_rejected(self, share: str) -> None:
        # Only the first five words survive -> below the 20-word floor.
        text = "THE SHARE WORDS\n  " + " ".join(share.split()[:5]) + "\n"
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic(text, source="trunc.txt")
        assert "5 share words" in str(exc.value)


def test_card_template_lines_unambiguous() -> None:
    """Every fixed prose line of the card must hold a non-wordlist token.

    If any prose line were composed entirely of wordlist words, the
    extractor would absorb it into the mnemonic and the word-count check
    would then reject a genuine card.  Render the template with a
    sentinel mnemonic and assert only that sentinel line is wordlist-only.
    """
    ms = encode_master_secret(b"correct horse battery staple")
    mnemonic = split_secret(ms, 2, 5)[0]
    card = _share_card_text("alpha", 1, 2, 5, mnemonic)

    mnemonic_lines = [
        line for line in card.splitlines() if is_mnemonic_line(line)
    ]
    # Exactly one wordlist-only line: the share words themselves.
    assert mnemonic_lines == [f"  {mnemonic}"]
