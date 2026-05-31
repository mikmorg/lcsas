"""SLIP-0039 Shamir Secret Sharing for LCSAS key escrow.

Pure-Python, stdlib-only.  Splits a master secret (in LCSAS, a per-repo
password) into ``N`` checksummed word-mnemonic shares with a ``K``-of-``N``
threshold; any ``K`` shares reconstruct the secret, ``K-1`` reveal nothing.

Public API:

- :func:`generate_mnemonics` -- full grouped split.
- :func:`split_secret` / :func:`recover_secret` -- single-group convenience.
- :func:`combine_mnemonics` -- recombine shares.
- :class:`KeyShareError` -- raised on any failure (too few shares, bad
  checksum, integrity/digest failure, mismatched parameters, etc.).
"""

from __future__ import annotations

from .slip39 import (
    KeyShareError,
    combine_mnemonics,
    generate_mnemonics,
    recover_secret,
    split_secret,
)

__all__ = [
    "KeyShareError",
    "combine_mnemonics",
    "generate_mnemonics",
    "recover_secret",
    "split_secret",
]
