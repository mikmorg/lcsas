"""Extract a share mnemonic from a bare mnemonic file or a printed share card.

``lcsas password split`` writes two artifacts per share: a bare mnemonic file
(``{repo}-share-N.txt``, words only) and a plain-language printable *share
card* (``{repo}-share-N-card.txt``) with header lines, prose, and the mnemonic
under a "THE SHARE WORDS" heading.  The share card is the artifact an owner is
told to hand to share holders, so every combiner must accept it.

The extractor identifies the share by its cryptographic content rather
than by the card's prose framing: a line is a *mnemonic line* iff it
holds a share word and every other whitespace-separated token is a
SLIP-0039 share word, either a full wordlist word or the unique
4+-letter prefix of one (the card tells the holder they may type just
the first 4 letters), or a number.  A card prints its words in numbered
rows of four (#955); the numbers (digits, with an optional ``.`` or
``)``) are skipped wherever share words are read, so a card typed with
its numbers combines and "word N" in a message is the word numbered N
on the card.  All mnemonic lines in one file are joined into ONE
mnemonic (each card holds exactly one share; joining also reads the
card's rows and tolerates print-wrap when a card is hand-retyped across
lines), with every prefix expanded to its full word.
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

import re
from typing import TextIO

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


def decode_share_text(data: bytes) -> str:
    """Return the text of a share file or of standard input (#967).

    UTF-16 with its byte-order mark (what Windows PowerShell's ``>``
    writes) is decoded; a UTF-8 BOM is dropped; anything else is read as
    UTF-8, a byte that is not becoming U+FFFD, which no share word holds.
    So a file that is not text is read like the C combiner reads it: it
    has no share words, and no decoding error escapes.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def read_share_stream(stream: TextIO) -> str:
    """Return all of *stream* (standard input) as :func:`decode_share_text`
    reads it: through its bytes when it has them, else as the text it is."""
    raw = getattr(stream, "buffer", None)
    if raw is not None:
        return decode_share_text(raw.read())
    return stream.read()


_TOKEN_SPLIT = re.compile(r"[ \t\r]+")
_NUMBER = re.compile(r"[0-9]+[.)]?")


def is_number_token(token: str) -> bool:
    """Return True iff *token* is a number as a share card prints it
    beside a word: ASCII digits, optionally followed by one ``.`` or
    ``)`` (#955).  Such tokens are skipped wherever share words are read,
    so a card typed with its numbers combines.  Mirrors
    ``lcsas_keyshare_is_number`` in recovery/src/lcsas-keyshare."""
    return _NUMBER.fullmatch(token) is not None


def _tokens(line: str) -> list[str]:
    """The tokens of *line* that are not numbers, split as the C combiner
    splits them (on spaces, tabs and carriage returns only)."""
    return [t for t in _TOKEN_SPLIT.split(line) if t and not is_number_token(t)]


def _lines(text: str) -> list[str]:
    """The lines of *text*, split as the C combiner splits them (on
    ``\\n`` only; a line's trailing ``\\r`` is a separator in
    :func:`_tokens`)."""
    return text.split("\n")


def _share_word(token: str) -> str | None:
    """Return the full wordlist word *token* stands for, or ``None``.

    *token* matches case-insensitively, as a full word or as the unique
    4+-letter prefix of one (see :func:`lcsas.keyshare.slip39._word_index`).
    """
    try:
        return WORDLIST[_word_index(token)]
    except KeyShareError:
        return None


def _stats(line: str) -> tuple[int, int]:
    """(tokens, share words) of *line*, numbers not counted."""
    tokens = _tokens(line)
    return len(tokens), sum(1 for t in tokens if _share_word(t) is not None)


def _is_near(tokens: int, words: int) -> bool:
    """A line that may be part of a share: it holds a share word, and at
    most one other token or at most a quarter of them (a card's row of
    four words with one mistyped).  Mirrors ``is_near_line`` in the C
    combiner."""
    bad = tokens - words
    return words >= 1 and (bad <= 1 or bad * 4 <= tokens)


def _is_share_like(tokens: int, words: int) -> bool:
    """Lines read together that hold a share, perhaps with typing
    mistakes, rather than prose: enough tokens for a share and at most a
    quarter of them not share words.  Mirrors ``is_share_like``."""
    return tokens >= _MIN_CANDIDATE_TOKENS and (tokens - words) * 4 <= tokens


def normalize_share_line(line: str) -> str:
    """Return *line* as one space-separated share mnemonic.

    Every token that is a share word becomes its full lowercase word (a
    4-letter prefix is expanded); a number token (:func:`is_number_token`)
    is left out; any other token is kept, lowercased, so the per-share
    check (:func:`check_share`) can name it and its position.
    """
    out: list[str] = []
    for tok in _tokens(line):
        word = _share_word(tok)
        out.append(word if word is not None else tok.lower())
    return " ".join(out)


def is_mnemonic_line(line: str) -> bool:
    """Return True iff *line* holds share words and nothing else but numbers.

    A share word is a SLIP-0039 wordlist word or the unique 4+-letter
    prefix of one, matched case-insensitively, so full words, prefixes and
    a mix all qualify; the numbers a card prints beside its words
    (:func:`is_number_token`) are allowed.  A blank line (or one with any
    other token, e.g. a share card header, "Tip:" line or prose) returns
    False.
    """
    tokens = _tokens(line)
    if not tokens:
        return False
    return all(_share_word(tok) is not None for tok in tokens)


def is_mistyped_share_line(line: str) -> bool:
    """Return True iff *line* looks like a share with typing mistakes.

    A line with enough tokens to be a share (13, numbers not counted) and
    at most a quarter of them not share words (full words or unique
    4+-letter prefixes) is a mistyped share, not prose, so the caller can
    name the bad word.  Mirrors ``is_share_like`` in
    recovery/src/lcsas-keyshare.
    """
    return _is_share_like(*_stats(line))


def is_share_line(line: str) -> bool:
    """Return True iff a *line* is, on its own, to be read as one share.

    A line of at least 13 tokens that is nearly all share words (see
    :func:`is_mistyped_share_line`); shorter lines are read as a share
    only together with the rows next to them (:func:`split_shares`).
    """
    return is_mistyped_share_line(line)


def split_shares(text: str) -> tuple[list[tuple[int, str]], int]:
    """Read the shares in *text*, standard input of a combiner.

    A share is a line, or a run of lines with no other line between them
    (a card's rows, #955), that is nearly all share words
    (:func:`is_mistyped_share_line`'s rule over the run).  A line long
    enough to be a share on its own (13 tokens or more) is one, so
    shares typed one a line stay apart.  Blank lines and card prose are
    skipped, so ``cat card1 card2 | ...`` works.  A share with typing
    mistakes is kept, normalised by :func:`normalize_share_line`, so the
    per-share check names the bad word.

    Returns ``([(first line number, mnemonic), ...], skipped)``, where
    *skipped* counts the lines that are not blank and were not read.
    Mirrors the stdin reader of the C combiner.
    """
    shares: list[tuple[int, str]] = []
    skipped = 0
    group: list[str] = []
    first = 0
    g_tokens = g_words = 0

    def end_group() -> None:
        nonlocal skipped, group
        if group:
            if _is_share_like(g_tokens, g_words):
                shares.append((first, normalize_share_line(" ".join(group))))
            else:
                skipped += len(group)
        group = []

    for lineno, line in enumerate(_lines(text), start=1):
        tokens, words = _stats(line)
        near = _is_near(tokens, words)
        if group and (not near or tokens >= _MIN_CANDIDATE_TOKENS):
            end_group()
        if near:
            if not group:
                first, g_tokens, g_words = lineno, 0, 0
            group.append(line)
            g_tokens += tokens
            g_words += words
            if tokens >= _MIN_CANDIDATE_TOKENS:
                end_group()
        elif _TOKEN_SPLIT.sub("", line):
            skipped += 1
    end_group()
    return shares, skipped


def _candidate_block(text: str) -> tuple[str, bool] | None:
    """Return the share with typing mistakes in *text*, if there is one.

    The fallback when a file's share words are not a valid share: of the
    runs of consecutive lines that may be part of a share (a share word,
    and at most one other token or a quarter of them), the first with the
    most tokens that is nearly all share words.  A card's rows with a
    mistyped word in one of them, or a share typed on one line with
    mistakes, are such a run, so the check names the bad word by its
    place in the share.  #844: a card's prose is never taken.  The C
    combiner's ``find_candidate`` uses the same rule.

    Returns ``(mnemonic, has_bad_token)``, the mnemonic normalised by
    :func:`normalize_share_line`, or ``None``.
    """
    best: tuple[list[str], int, int] | None = None
    run: list[str] = []
    r_tokens = r_words = 0
    for line in [*_lines(text), ""]:
        tokens, words = _stats(line)
        if _is_near(tokens, words):
            run.append(line)
            r_tokens += tokens
            r_words += words
            continue
        if run and _is_share_like(r_tokens, r_words) and (
                best is None or r_tokens > best[1]):
            best = (run, r_tokens, r_words)
        run, r_tokens, r_words = [], 0, 0
    if best is None:
        return None
    lines, tokens, words = best
    return normalize_share_line(" ".join(lines)), words < tokens


def extract_mnemonic(
    text: str, source: str = "input", *, keep_mistyped: bool = False
) -> str:
    """Return the single share mnemonic embedded in *text*.

    *text* may be a bare mnemonic file or a printed share card.  Every
    line of share words (full words or 4-letter prefixes, any case, with
    or without the numbers a card prints beside them) is treated as part
    of the mnemonic and joined (in order) into one space-separated
    mnemonic of full lowercase words; header and prose lines are skipped.

    When those words are not a valid share and a run of lines that are
    nearly all share words holds a word that is not one
    (:func:`_candidate_block`), that run is taken to be the share with
    typing mistakes, so its bad word is named by its place in the share:
    "word 23" is the word numbered 23 on the card (#955).  With
    *keep_mistyped* it is returned as it is, so the caller's per-share
    check names the bad word, as the C combiner does.  Otherwise a
    :class:`KeyShareError` names the bad word and its position.

    *source* is a human-readable label (typically the file path) used in
    error messages.  Raises :class:`KeyShareError` naming the word count
    if no share is found.
    """
    # Full lowercase words: the canonical SLIP-0039 wordlist is lowercase.
    # The C combiner keeps a prefix as typed and expands it when it decodes
    # the share; both give the same share.
    words: list[str] = []
    for line in _lines(text):
        if is_mnemonic_line(line):
            words.extend(normalize_share_line(line).split())
    strict = " ".join(words)
    strict_ok = len(words) >= _MIN_WORDS
    if strict_ok and check_share(strict) is None:
        return strict

    candidate = _candidate_block(text)
    if candidate is not None and (candidate[1] or not strict_ok):
        if keep_mistyped:
            return candidate[0]
        reason = check_share(candidate[0])
        if reason is not None:
            raise KeyShareError(f"'{source}': {reason}")
        return candidate[0]
    if strict_ok:
        return strict
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
