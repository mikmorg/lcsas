"""Extract a share mnemonic from a bare mnemonic file or a printed share card.

``lcsas password split`` writes two artifacts per share: a bare mnemonic file
(``{repo}-share-N.txt``, words only) and a plain-language printable *share
card* (``{repo}-share-N-card.txt``) with header lines, prose, and the mnemonic
under a "THE SHARE WORDS" heading.  The share card is the artifact an owner is
told to hand to share holders, so every combiner must accept it.

The extractor identifies the share by its cryptographic content rather
than by the card's prose framing: a line is a *mnemonic line* iff it is
non-blank and every whitespace-separated token is a SLIP-0039 share word,
either a full wordlist word or the unique 4+-letter prefix of one (the
card tells the holder they may type just the first 4 letters).  All
mnemonic lines in one file are joined into ONE mnemonic (each card holds
exactly one share; joining also tolerates print-wrap when a card is
hand-retyped across lines), with every prefix expanded to its full word.
Keying on the wordlist — not on header text — keeps this working across
card-format drift, partial photocopies, and re-typed files.  The rules
mirror ``line_is_mnemonic`` / ``lcsas_keyshare_extract`` and the file and
stdin readers of the C combiner in recovery/src/lcsas-keyshare.

This module is stdlib-only and imports only its sibling modules
(:mod:`lcsas.keyshare.slip39`, :mod:`lcsas.keyshare.wordlist`) so it
ships in the meta disc bundle and works under both the ``lcsas.keyshare``
(dev/source) and top-level ``keyshare`` (meta disc) import layouts.
"""

from __future__ import annotations

from .slip39 import MIN_MNEMONIC_LENGTH_WORDS, KeyShareError, _word_index, check_share
from .wordlist import WORDLIST

# A SLIP-0039 share is METADATA_LENGTH_WORDS (7) plus one word per 10 bits
# of master secret.  A 128-bit secret yields 20 words; LCSAS frames a
# variable-length password into the master secret, so real shares are at
# least MIN_MNEMONIC_LENGTH_WORDS (20) and grow with the password — there
# is no fixed upper bound.  The RS1024 checksum in the combiner is the
# backstop that rejects a wordlist-only line that is not a true share.
_MIN_WORDS = MIN_MNEMONIC_LENGTH_WORDS

# A line needs this many tokens to be taken as a share with typing
# mistakes rather than prose (the C combiner uses the same floor for a
# file's fallback line and for a mistyped stdin line).
_MIN_CANDIDATE_TOKENS = 13


def _share_word(token: str) -> str | None:
    """Return the full wordlist word *token* stands for, or ``None``.

    *token* matches case-insensitively, as a full word or as the unique
    4+-letter prefix of one (see :func:`lcsas.keyshare.slip39._word_index`).
    """
    try:
        return WORDLIST[_word_index(token)]
    except KeyShareError:
        return None


def normalize_share_line(line: str) -> str:
    """Return *line* as one space-separated share mnemonic.

    Every token that is a share word becomes its full lowercase word (a
    4-letter prefix is expanded); any other token is kept, lowercased, so
    the per-share check (:func:`check_share`) can name it and its position.
    """
    out: list[str] = []
    for tok in line.split():
        word = _share_word(tok)
        out.append(word if word is not None else tok.lower())
    return " ".join(out)


def is_mnemonic_line(line: str) -> bool:
    """Return True iff *line* is a non-blank line of only share words.

    A share word is a SLIP-0039 wordlist word or the unique 4+-letter
    prefix of one, matched case-insensitively, so full words, prefixes and
    a mix all qualify.  A blank line (or one with any other token, e.g. a
    share card header, "Tip:" line or prose) returns False.
    """
    tokens = line.split()
    if not tokens:
        return False
    return all(_share_word(tok) is not None for tok in tokens)


def is_mistyped_share_line(line: str) -> bool:
    """Return True iff *line* looks like a share with typing mistakes.

    For one-share-per-line input (stdin), where a line that is not all
    wordlist words is skipped as prose: a line with enough tokens to be a
    share (13, as the C combiner requires of a file) and at most a quarter
    of them not share words (full words or unique 4+-letter prefixes) is a
    mistyped share, not prose, so the caller can name the bad word.
    Mirrors ``is_mistyped_share_line`` in recovery/src/lcsas-keyshare.
    """
    tokens = line.split()
    if len(tokens) < _MIN_CANDIDATE_TOKENS:
        return False
    bad = sum(1 for tok in tokens if _share_word(tok) is None)
    return bad * 4 <= len(tokens)


def is_share_line(line: str) -> bool:
    """Return True iff a stdin *line* is to be read as one share.

    The stdin rule of the C combiner: a line of at least 20 share words is
    a share, and so is a line that is nearly all share words (see
    :func:`is_mistyped_share_line`), so the check names its bad word.
    Anything else (blank lines, card prose, a short line) is skipped.
    Every line of the first kind is also of the second kind, so this is
    the same test.
    """
    return is_mistyped_share_line(line)


def _candidate_line(text: str) -> str | None:
    """Return the share line with typing mistakes in *text*, if there is one.

    The fallback when no complete share is found: of the lines that are
    nearly all share words (:func:`is_mistyped_share_line`), the first one
    with the most tokens is most likely the share with typing mistakes, so
    it is kept for the per-share check to name the bad word.  The C
    combiner's ``copy_candidate_line`` uses the same rule (#844), so for a
    card whose share words are missing neither names a word of a prose
    line: both say no share words were found.
    """
    best: str | None = None
    best_tokens = 0
    for line in text.splitlines():
        tokens = len(line.split())
        if tokens > best_tokens and is_mistyped_share_line(line):
            best, best_tokens = line, tokens
    return None if best is None else normalize_share_line(best)


def extract_mnemonic(
    text: str, source: str = "input", *, keep_mistyped: bool = False
) -> str:
    """Return the single share mnemonic embedded in *text*.

    *text* may be a bare mnemonic file or a printed share card.  Every
    line whose tokens are all share words (full words or 4-letter
    prefixes, any case) is treated as part of the mnemonic and joined (in
    order) into one space-separated mnemonic of full lowercase words;
    header and prose lines are skipped.

    When that finds fewer than 20 words but a line of 13 or more tokens is
    nearly all share words, that line is taken to be the share with typing
    mistakes.  With *keep_mistyped* it is returned as it is (normalised by
    :func:`normalize_share_line`), so the caller's per-share check names
    the bad word, as the C combiner does.  Otherwise a
    :class:`KeyShareError` names the bad word and its position.

    *source* is a human-readable label (typically the file path) used in
    error messages.  Raises :class:`KeyShareError` naming the word count
    if no share is found.
    """
    # Full lowercase words: the canonical SLIP-0039 wordlist is lowercase.
    # The C combiner keeps a prefix as typed and expands it when it decodes
    # the share; both give the same share.
    words: list[str] = []
    for line in text.splitlines():
        if is_mnemonic_line(line):
            words.extend(normalize_share_line(line).split())

    if len(words) >= _MIN_WORDS:
        return " ".join(words)

    candidate = _candidate_line(text)
    if candidate is not None:
        if keep_mistyped:
            return candidate
        reason = check_share(candidate)
        if reason is not None:
            raise KeyShareError(f"'{source}': {reason}")
    if not words:
        # #844: say so plainly, as the C combiner does.
        raise KeyShareError(
            f"'{source}': no share words were found in this file - is it "
            "a share card?"
        )
    raise KeyShareError(
        f"'{source}': found {len(words)} share words, expected at least "
        f"{_MIN_WORDS} - is this a complete share card?"
    )
