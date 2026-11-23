"""The printed cards describe the restore the heir meets (#955).

The share, password and estate cards are what an heir reads first.
Since #607 the restore asks for the share cards itself, so the cards say
to start the restore and type each card's words when asked; rebuilding
the password by hand is the fallback, and its password is typed after
answering P at the share-card question.  The share words are printed in
numbered rows of four with a word count, and every line fits a printed
page (66 characters).
"""

from __future__ import annotations

import re

import pytest

from lcsas.cli.main import _estate_card_text, _recovery_card_text, _share_card_text
from lcsas.keyshare import encode_master_secret, extract_mnemonic, split_secret

#: The widest line a card may have.
WIDTH = 66


def _shares(password_len: int, k: int = 2, n: int = 5) -> list[str]:
    return split_secret(encode_master_secret(b"p" * password_len), k, n)


def _share_card(password_len: int = 64, repo: str = "Family documents",
                k: int = 2, n: int = 5) -> tuple[str, str]:
    m = _shares(password_len, k, n)[0]
    return _share_card_text(repo, 1, k, n, m, "2026-11-23", 1234), m


def _estate(**over: object) -> str:
    kw: dict[str, object] = dict(
        owner="Jane Smith", description="Family photos 2000-2025",
        technical_contact="Bob (bob@example.org)",
        repositories=["Family documents", "Holiday photos"],
        key_storage_hints="Safe deposit box #1234", key_split=True,
        key_threshold=2, key_shares=5, label_prefix="LCSAS", disc_count=8,
        card_date="2026-11-23",
    )
    kw.update(over)
    return _estate_card_text(**kw)  # type: ignore[arg-type]


def _password_card(check_code: str | None = "fdcc") -> str:
    return _recovery_card_text(
        repo="Family documents", key_file_name="documents.pw",
        storage_hints="Home safe", card_date="2026-11-23",
        label_prefix="LCSAS", check_code=check_code,
    )


#: A backup set name of 30 characters, and six sets.
LONG = "Grandmother's letters and maps"
SIX = ["Family documents", "Holiday photos", LONG, "Tax records 1990-2025",
       "Recipes", "Home videos"]


def _all_cards() -> list[tuple[str, str]]:
    cards = []
    for pw_len in (8, 16, 32, 64, 88, 128):
        cards.append((f"share card, {pw_len}-byte password", _share_card(pw_len)[0]))
    cards.append(("share card, 9 of 16", _share_card(64, k=9, n=16)[0]))
    cards.append(("share card, long name", _share_card(64, repo=LONG)[0]))
    cards.append(("password card", _password_card()))
    cards.append(("password card, no check code", _password_card(None)))
    cards.append(("password card, long name", _recovery_card_text(
        repo=LONG, key_file_name="grandmothers-letters.pw",
        storage_hints="One copy in the safe at home, one with the solicitor "
                      "in town, one in the bank's deposit box",
        card_date="2026-11-23", label_prefix="LCSAS", check_code="fdcc")))
    cards.append(("estate card, six sets", _estate(repositories=SIX)))
    cards.append(("estate card, six sets mixed", _estate(
        repositories=SIX, key_split=False,
        split_repos={LONG: (2, 5), "Family documents": (3, 5)},
        unsplit_repos=("Holiday photos", "Tax records 1990-2025", "Recipes",
                       "Home videos"))))
    cards.append(("estate card, split", _estate()))
    cards.append(("estate card, not split", _estate(key_split=False)))
    cards.append(("estate card, no disc count", _estate(disc_count=None)))
    cards.append(("estate card, mixed", _estate(
        key_split=False, split_repos={"Family documents": (2, 5)},
        unsplit_repos=("Holiday photos",))))
    return cards


@pytest.mark.parametrize("name,card", _all_cards(), ids=[n for n, _ in _all_cards()])
def test_no_line_wider_than_a_page(name: str, card: str) -> None:
    wide = [ln for ln in card.splitlines() if len(ln) > WIDTH]
    assert not wide, f"{name}: lines over {WIDTH} characters:\n" + "\n".join(wide)


@pytest.mark.parametrize("name,card", _all_cards(), ids=[n for n, _ in _all_cards()])
def test_no_card_says_enter_it_at_the_restore_prompt(name: str, card: str) -> None:
    """A rebuilt password typed at the share-card question is read as a
    bad card (#955): no card may send the heir there."""
    assert "enter it at the restore prompt" not in card


@pytest.mark.parametrize("pw_len", [8, 16, 32, 64, 88, 128])
def test_share_words_numbered_in_rows_of_four_with_a_count(pw_len: int) -> None:
    card, mnemonic = _share_card(pw_len)
    words = mnemonic.split()
    m = re.search(r"^THE SHARE WORDS \((\d+) words; keep every word, in order\)$",
                  card, re.MULTILINE)
    assert m, card
    assert int(m.group(1)) == len(words)
    body = card[m.end():].split("\n\n", 1)[0].strip("\n").splitlines()
    assert len(body) == -(-len(words) // 4)
    got: list[str] = []
    for row in body:
        cells = re.findall(r"(\d+) (\S+)", row)
        assert 1 <= len(cells) <= 4, row
        for num, word in cells:
            assert int(num) == len(got) + 1, row
            got.append(word)
    assert got == words
    assert "the numbers are there to keep your place" in card
    # The card combines as it is printed.
    assert extract_mnemonic(card) == mnemonic


def test_share_card_starts_with_the_restore() -> None:
    """The card says to start the restore and type the words when it
    asks; the combiner commands are the fallback, after it."""
    card, _ = _share_card()
    flat = " ".join(card.split())
    start = flat.find("Start the restore")
    asks = flat.find("When it asks for share card 1, type the words of this card")
    fallback = flat.find("lcsas-shares")
    assert 0 <= start < asks < fallback, card
    assert "It asks for 2 cards" in flat
    assert "first 4 letters of each word are enough" in flat
    assert "put each card's words in a text file" in flat
    # The machine folder is not left to guess: the card points at the
    # META disc's instructions for it.
    assert "RESTORE.txt" in flat and "RESTORE_WINDOWS.txt" in flat


def test_estate_card_starts_the_restore_first_and_says_p() -> None:
    for card in (_estate(), _estate(key_split=False, split_repos={"Family documents": (2, 5)},
                                    unsplit_repos=("Holiday photos",))):
        flat = " ".join(card.split())
        first = flat.find("FIRST STEP")
        shares = flat.lower().find("it asks for the share cards")
        assert 0 <= first < shares, card
        assert ("if you rebuilt the password by hand, type p at the share-card "
                "question and then the password") in flat.lower()
    # Not split: nothing about share cards.
    plain = _estate(key_split=False)
    assert "share card" not in plain.lower()


def test_estate_card_counts_data_discs_apart_from_the_meta_disc() -> None:
    card = _estate(disc_count=8)
    assert re.search(r"^  Data discs\s*: 8, labeled LCSAS_\*", card, re.MULTILINE), card
    assert re.search(r"^  META disc\s*: LCSAS_META", card, re.MULTILINE), card
    assert "disc(s)" not in card
    blank = _estate(disc_count=None)
    assert re.search(r"^  Data discs\s*: _+, labeled LCSAS_\*", blank, re.MULTILINE), blank


def test_password_card_heir_lines() -> None:
    """The heir types the password when the restore asks for it; the
    password file name and the check command are for the owner."""
    card = _password_card()
    flat = " ".join(card.split())
    assert "type this password when the restore asks for it" in flat
    owner = card.index("FOR THE OWNER")
    for needle in ("documents.pw", "lcsas password card --check"):
        assert needle in card
        assert card.index(needle) > owner, needle
    assert "Password file :" not in card
