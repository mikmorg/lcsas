"""Unit tests for :mod:`lcsas.keyshare.cards` (KEY-01).

``extract_mnemonic`` is the shared card-tolerant extractor used by all
three combiners; ``is_mnemonic_line`` is its line predicate.  These tests
pin the accept/reject contract on bare mnemonics, full cards, wrapped and
CRLF variants, and assert the card template can never be absorbed by the
wordlist filter (design point 6 of the plan).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.cli.main import _share_card_text, _share_word_rows
from lcsas.keyshare import (
    KeyShareError,
    encode_master_secret,
    extract_mnemonic,
    is_mnemonic_line,
    is_share_line,
    normalize_share_line,
    split_secret,
)
from lcsas.keyshare.wordlist import WORDLIST

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _prefix4(mnemonic: str) -> str:
    return " ".join(w[:4] for w in mnemonic.split())


def _mixed(mnemonic: str) -> str:
    # Full words and 4-letter prefixes, alternating, in mixed case.
    return " ".join(
        (w.upper() if i % 3 == 0 else w) if i % 2 else w[:4].title()
        for i, w in enumerate(mnemonic.split())
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

    def test_four_letter_prefixes(self, share: str) -> None:
        # #797: the card says a word may be cut to its first 4 letters.
        assert is_mnemonic_line(_prefix4(share)) is True

    def test_mix_of_full_words_and_prefixes_any_case(self, share: str) -> None:
        assert is_mnemonic_line(_mixed(share)) is True

    def test_three_letters_is_not_a_share_word(self, share: str) -> None:
        words = share.split()
        words[2] = words[2][:3]
        assert is_mnemonic_line(" ".join(words)) is False

    def test_token_longer_than_a_prefix_must_still_match(self, share: str) -> None:
        # The first 4 letters match a word, the 5th does not.
        words = share.split()
        word = words[2]
        bad = next(
            word[:4] + c for c in "qxzjv"
            if not any(w.startswith(word[:4] + c) for w in WORDLIST)
        )
        words[2] = bad
        assert is_mnemonic_line(" ".join(words)) is False


def test_every_word_is_unique_in_its_first_four_letters() -> None:
    """#797: the prefix rule rests on this property of the wordlist."""
    assert len(WORDLIST) == 1024
    assert all(len(w) >= 4 for w in WORDLIST)
    assert len({w[:4] for w in WORDLIST}) == 1024
    # So every 4-letter prefix stands for exactly its own word.
    for w in WORDLIST:
        assert normalize_share_line(w[:4]) == w


class TestNormalizeShareLine:
    def test_expands_prefixes_and_lowercases(self, share: str) -> None:
        assert normalize_share_line(_mixed(share)) == share

    def test_keeps_a_bad_token_lowercased_in_place(self, share: str) -> None:
        words = _prefix4(share).split()
        words[6] = "QQQQ"
        out = normalize_share_line("  " + " ".join(words) + " \r").split()
        assert out[6] == "qqqq"
        assert out[:6] == share.split()[:6]


class TestIsShareLine:
    """The stdin rule, the same as the C combiner's."""

    def test_full_and_prefix_shares(self, share: str) -> None:
        assert is_share_line(share) is True
        assert is_share_line(_prefix4(share)) is True
        assert is_share_line(_mixed(share)) is True

    def test_share_with_a_typo_is_kept(self, share: str) -> None:
        words = share.split()
        words[6] = "buidling"
        assert is_share_line(" ".join(words)) is True

    def test_short_lines_and_prose_are_skipped(self, share: str) -> None:
        # Short card words ("that", "type", "just") are now share words as
        # 4-letter prefixes, so a short line must not count as a share.
        assert is_share_line("that") is False
        assert is_share_line(" ".join(share.split()[:5])) is False
        assert is_share_line("") is False
        assert is_share_line(
            "  Tip: you may type just the first 4 letters of each word"
        ) is False


class TestExtractMnemonic:
    def test_bare_mnemonic(self, share: str) -> None:
        assert extract_mnemonic(share) == share

    def test_full_card_text(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, share, "2026-06-13", 12345)
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
        card = _share_card_text("alpha", 1, 2, 5, share, "2026-06-13", 12345)
        crlf = card.replace("\n", "\r\n")
        assert extract_mnemonic(crlf) == share

    def test_mixed_case(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, share.upper(), "2026-06-13", 12345)
        assert extract_mnemonic(card) == share

    def test_prefix_card_gives_the_full_words(self, share: str) -> None:
        # #797: a card typed as 4-letter prefixes gives the same share.
        card = _share_card_text(
            "alpha", 1, 2, 5, _prefix4(share), "2026-06-13", 12345
        )
        assert extract_mnemonic(card) == share

    def test_mixed_card_gives_the_full_words(self, share: str) -> None:
        card = _share_card_text("alpha", 1, 2, 5, _mixed(share), "2026-06-13", 12345)
        assert extract_mnemonic(card.replace("\n", "\r\n")) == share

    def test_wrapped_prefix_lines(self, share: str) -> None:
        words = _prefix4(share).split()
        text = "  " + " ".join(words[:9]) + "\n  " + " ".join(words[9:]) + "\n"
        assert extract_mnemonic(text) == share

    def test_typo_is_named_with_its_position(self, share: str) -> None:
        words = _prefix4(share).split()
        words[6] = "qqqq"
        card = _share_card_text(
            "alpha", 1, 2, 5, " ".join(words), "2026-06-13", 12345
        )
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic(card, source="card1.txt")
        assert str(exc.value) == "'card1.txt': word 7 'qqqq' is not a share word"

    def test_keep_mistyped_returns_the_share_line(self, share: str) -> None:
        # For a combiner that checks each share itself (and names the word).
        words = _prefix4(share).split()
        words[6] = "QQQQ"
        card = _share_card_text(
            "alpha", 1, 2, 5, " ".join(words), "2026-06-13", 12345
        )
        got = extract_mnemonic(card, keep_mistyped=True).split()
        assert got[6] == "qqqq"
        assert got[:6] == share.split()[:6]

    def test_empty_file_rejected(self) -> None:
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic("", source="empty.txt")
        assert "empty.txt" in str(exc.value)
        # #844: zero share words is said plainly.
        assert "no share words were found in this file" in str(exc.value)

    def test_prose_only_rejected(self) -> None:
        text = "Backup set : alpha\n(the holder lost the words)\n"
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic(text, source="prose.txt")
        assert "prose.txt" in str(exc.value)

    def test_truncated_card_rejected(self, share: str) -> None:
        # Only the first five words survive -> below the 20-word floor.
        text = "THE SHARE WORDS\n  " + " ".join(share.split()[:5]) + "\n"
        with pytest.raises(KeyShareError) as exc:
            extract_mnemonic(text, source="trunc.txt")
        assert "5 share words" in str(exc.value)


@pytest.mark.parametrize(
    ("repo", "k", "n"),
    [("alpha", 2, 5), ("photos", 3, 5), ("academic", 2, 3), ("home", 1, 1)],
)
def test_card_template_lines_unambiguous(repo: str, k: int, n: int) -> None:
    """Every fixed prose line of the card must hold a non-share token.

    If any prose line were composed entirely of share words (now including
    4-letter prefixes such as "that" or "type", #797), the extractor would
    absorb it into the mnemonic and the checksum would then reject a
    genuine card.  Render the template with a real mnemonic and assert only
    that line is a mnemonic line.
    """
    ms = encode_master_secret(b"correct horse battery staple")
    mnemonic = split_secret(ms, 2, 5)[0]
    for words in (mnemonic, _prefix4(mnemonic)):
        card = _share_card_text(repo, 1, k, n, words, "2026-06-13", 12345)
        mnemonic_lines = [
            line for line in card.splitlines() if is_mnemonic_line(line)
        ]
        # The share-word lines are the numbered rows of the words (#955).
        assert mnemonic_lines == _share_word_rows(words)
        assert extract_mnemonic(card) == mnemonic


def test_older_card_layout_lines_unambiguous() -> None:
    """An older card layout (the fuzz seed) has one share-word line only."""
    seed = _REPO_ROOT / "recovery" / "fuzz" / "corpus" / "slip39" / "seed_card_full.txt"
    text = seed.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if is_mnemonic_line(line)]
    assert len(lines) == 1
    assert lines[0].startswith("  already email academic")
