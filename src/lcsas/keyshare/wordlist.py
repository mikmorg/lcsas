"""The official SLIP-0039 wordlist and word<->index helpers.

The 1024-word list is bundled as ``wordlist.txt`` (one word per line, in the
canonical SLIP-0039 order) and loaded once at import time.  The list is *not*
sorted or deduplicated here: a share's value is the base-1024 integer formed by
the file-order index of each word, so the order is load-bearing and must match
the published list byte-for-byte.

This module is intentionally self-contained (stdlib only) so the combiner can be
bundled standalone on the meta-volume.
"""

from __future__ import annotations

from pathlib import Path

_WORDLIST_PATH = Path(__file__).with_name("wordlist.txt")

WORDLIST: tuple[str, ...] = tuple(
    _WORDLIST_PATH.read_text(encoding="ascii").split()
)
"""The 1024 SLIP-0039 words, in canonical order. ``WORDLIST[i]`` has index ``i``."""

# Sanity: the radix is fixed at 1024 by the spec.  A corrupted bundle is a
# durability hazard, so fail loudly at import rather than silently mis-encode.
if len(WORDLIST) != 1024:  # pragma: no cover - guards against a corrupted bundle
    raise RuntimeError(
        f"SLIP-0039 wordlist must contain exactly 1024 words, got {len(WORDLIST)}."
    )

_WORD_TO_INDEX: dict[str, int] = {word: index for index, word in enumerate(WORDLIST)}

# Every SLIP-0039 word is uniquely identified by its first four letters
# (docs/KEY_SHARE_FORMAT.md §2), so a heir may type just that prefix.  This
# maps each 4-letter prefix to its word index; a full-word match always wins
# (exact lookups go through _WORD_TO_INDEX first).  The uniqueness invariant
# is asserted here so a corrupted bundle fails loudly rather than silently
# resolving a prefix to the wrong word.
_PREFIX4_TO_INDEX: dict[str, int] = {word[:4]: index for index, word in enumerate(WORDLIST)}
if len(_PREFIX4_TO_INDEX) != 1024:  # pragma: no cover - guards a corrupted bundle
    raise RuntimeError(
        "SLIP-0039 wordlist 4-letter prefixes are not unique; bundle is corrupt."
    )
