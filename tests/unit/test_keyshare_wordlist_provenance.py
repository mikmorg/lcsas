"""Provenance guards for the SLIP-0039 wordlist (KEY-10).

``docs/KEY_SHARE_FORMAT.md`` embeds the SHA-256 of ``wordlist.txt`` so a
future engineer can verify the exact bytes from the spec alone (the
``recovery/``-rooted MANIFEST intentionally does not pin keyshare
artifacts — they live under ``src/`` and are copied at meta-build time).
These tests keep the doc-embedded hash and the three wordlist copies
(txt, generated C array, doc claim) from drifting apart silently.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "KEY_SHARE_FORMAT.md"
WORDLIST_TXT = REPO_ROOT / "src" / "lcsas" / "keyshare" / "wordlist.txt"
WORDLIST_C = REPO_ROOT / "recovery" / "src" / "lcsas-keyshare" / "wordlist.c"


def test_spec_embedded_hash_matches_wordlist_txt() -> None:
    """The SHA-256 printed in the spec must be the hash of the real file."""
    spec = SPEC.read_text()
    match = re.search(
        r"SHA-256 of `wordlist\.txt`[^`]*`([0-9a-f]{64})`", spec
    )
    assert match is not None, "KEY_SHARE_FORMAT.md must embed the wordlist SHA-256"
    actual = hashlib.sha256(WORDLIST_TXT.read_bytes()).hexdigest()
    assert match.group(1) == actual, (
        f"doc-embedded hash {match.group(1)} != sha256(wordlist.txt) {actual}; "
        "update KEY_SHARE_FORMAT.md §5 if the wordlist legitimately changed"
    )


def test_wordlist_txt_shape() -> None:
    """1024 LF-terminated words, as the spec claims."""
    data = WORDLIST_TXT.read_bytes()
    assert data.endswith(b"\n")
    words = data.decode("ascii").splitlines()
    assert len(words) == 1024
    assert len(set(w[:4] for w in words)) == 1024  # unique 4-letter prefixes


def test_generated_c_wordlist_matches_txt() -> None:
    """``wordlist.c`` is generated from ``wordlist.txt`` — same 1024 words."""
    txt_words = WORDLIST_TXT.read_text().splitlines()
    c_src = WORDLIST_C.read_text()
    body = re.search(
        r"lcsas_slip39_wordlist\[1024\]\s*=\s*\{(.*?)\};", c_src, re.DOTALL
    )
    assert body is not None, "wordlist array not found in wordlist.c"
    c_words = re.findall(r'"([a-z]+)"', body.group(1))
    assert c_words == txt_words


def test_spec_no_longer_claims_manifest_pinning() -> None:
    """Regression guard on the false KEY-10 sentence.

    Keyshare artifacts are NOT in recovery/MANIFEST.sha256; the spec must
    not send a future reader there for them.
    """
    spec = SPEC.read_text()
    assert not re.search(
        r"pinned in\s+`?recovery/MANIFEST\.sha256`?", spec
    ), "KEY_SHARE_FORMAT.md must not claim keyshare artifacts are MANIFEST-pinned"
