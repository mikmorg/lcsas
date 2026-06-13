"""Extract a share mnemonic from a bare mnemonic file or a printed card.

``lcsas key split`` writes two artifacts per share: a bare mnemonic file
(``{repo}-share-N.txt``, words only) and a plain-language printable *card*
(``{repo}-share-N-card.txt``) with header lines, prose, and the mnemonic
under a "THE SHARE WORDS" heading.  The card is the artifact an owner is
told to hand to holders, so every combiner must accept it.

The extractor identifies the share by its cryptographic content rather
than by the card's prose framing: a line is a *mnemonic line* iff it is
non-blank and every whitespace-separated token is in the SLIP-0039
wordlist.  All mnemonic lines in one file are joined into ONE mnemonic
(each card holds exactly one share; joining also tolerates print-wrap
when a card is hand-retyped across lines).  Keying on the wordlist — not
on header text — keeps this working across card-format drift, partial
photocopies, and re-typed files.

This module is stdlib-only and imports only :mod:`lcsas.keyshare.wordlist`
so it ships in the meta-volume bundle and works under both the
``lcsas.keyshare`` (dev/source) and top-level ``keyshare`` (meta-volume)
import layouts.
"""

from __future__ import annotations

from .slip39 import MIN_MNEMONIC_LENGTH_WORDS, KeyShareError
from .wordlist import _WORD_TO_INDEX

# A SLIP-0039 share is METADATA_LENGTH_WORDS (7) plus one word per 10 bits
# of master secret.  A 128-bit secret yields 20 words; LCSAS frames a
# variable-length password into the master secret, so real shares are at
# least MIN_MNEMONIC_LENGTH_WORDS (20) and grow with the password — there
# is no fixed upper bound.  The RS1024 checksum in the combiner is the
# backstop that rejects a wordlist-only line that is not a true share.
_MIN_WORDS = MIN_MNEMONIC_LENGTH_WORDS


def is_mnemonic_line(line: str) -> bool:
    """Return True iff *line* is a non-blank line of only wordlist words.

    Tokens are matched case-insensitively against the SLIP-0039 wordlist.
    A blank line (or one with any non-wordlist token, e.g. a card header
    or prose) returns False.
    """
    tokens = line.split()
    if not tokens:
        return False
    return all(tok.lower() in _WORD_TO_INDEX for tok in tokens)


def extract_mnemonic(text: str, source: str = "input") -> str:
    """Return the single share mnemonic embedded in *text*.

    *text* may be a bare mnemonic file or a printed share card.  Every
    line whose tokens are all SLIP-0039 words is treated as part of the
    mnemonic and joined (in order) into one space-separated mnemonic;
    header and prose lines are skipped.

    *source* is a human-readable label (typically the file path) used in
    error messages.  Raises :class:`KeyShareError` naming the offending
    word count if the joined result is not a valid share length.
    """
    # Lowercase the words: the canonical SLIP-0039 wordlist is lowercase,
    # and this keeps the Python extractor's output identical to the C
    # combiner's (which also lowercases) so the two paths never diverge.
    words: list[str] = []
    for line in text.splitlines():
        if is_mnemonic_line(line):
            words.extend(tok.lower() for tok in line.split())

    if len(words) < _MIN_WORDS:
        raise KeyShareError(
            f"'{source}': found {len(words)} share words, expected at least "
            f"{_MIN_WORDS} — is this a complete share card?"
        )
    return " ".join(words)
