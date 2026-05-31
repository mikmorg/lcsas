"""Reversible password <-> SLIP-0039 master-secret framing.

SLIP-0039 master secrets must be an **even** number of bytes and at least
16 bytes long (the Feistel network splits the secret in half).  An LCSAS
repository password is arbitrary bytes of arbitrary length, so it cannot be
fed to :func:`lcsas.keyshare.split_secret` directly.

This module frames a password into a valid master secret and back, with an
exact, byte-for-byte reversible encoding:

- a 2-byte big-endian length prefix records the true password length, then
- the password bytes follow, then
- the result is zero-padded up to the smallest even length that is >= 16.

The length prefix means the trailing zero padding (and any zero bytes that
are legitimately part of the password) survive the round-trip unambiguously.

This codec lives in the ``keyshare`` package on purpose: the recovery path
(Phase 2) decodes a reconstructed master secret back into the password
standalone, with nothing imported but this package.
"""

from __future__ import annotations

from .slip39 import KeyShareError

# A 2-byte big-endian length prefix caps the password at 65535 bytes.
_MAX_PASSWORD_LEN = 0xFFFF
_LENGTH_PREFIX_BYTES = 2
_MIN_MASTER_SECRET_BYTES = 16


def encode_master_secret(pw: bytes) -> bytes:
    """Frame a repository password into a SLIP-0039 master secret.

    The returned value is always an even number of bytes and at least
    :data:`_MIN_MASTER_SECRET_BYTES` (16) bytes long, satisfying the
    SLIP-0039 master-secret constraints.

    :param pw: the raw password bytes (0..65535 bytes).
    :returns: a valid SLIP-0039 master secret encoding *pw*.
    :raises KeyShareError: if *pw* is longer than 65535 bytes.
    """
    if len(pw) > _MAX_PASSWORD_LEN:
        raise KeyShareError(
            f"Password is too long to escrow: {len(pw)} bytes "
            f"(maximum {_MAX_PASSWORD_LEN})."
        )
    body = len(pw).to_bytes(_LENGTH_PREFIX_BYTES, "big") + pw
    # Smallest even length that is also >= the 16-byte minimum.
    target = max(_MIN_MASTER_SECRET_BYTES, len(body) + (len(body) & 1))
    return body + b"\x00" * (target - len(body))


def decode_master_secret(ms: bytes) -> bytes:
    """Recover the original password from a framed master secret.

    Inverse of :func:`encode_master_secret`:
    ``decode_master_secret(encode_master_secret(pw)) == pw`` for every
    valid *pw*.

    :param ms: a master secret produced by :func:`encode_master_secret`.
    :returns: the original password bytes.
    :raises KeyShareError: if *ms* is too short to hold a length prefix or
        the recorded length runs past the end of *ms* (corrupt / truncated).
    """
    if len(ms) < _LENGTH_PREFIX_BYTES:
        raise KeyShareError(
            "Master secret is too short to contain a length prefix "
            f"({len(ms)} bytes); it is corrupt or truncated."
        )
    n = int.from_bytes(ms[:_LENGTH_PREFIX_BYTES], "big")
    if _LENGTH_PREFIX_BYTES + n > len(ms):
        raise KeyShareError(
            f"Master secret claims a {n}-byte password but holds only "
            f"{len(ms) - _LENGTH_PREFIX_BYTES} bytes; it is corrupt or truncated."
        )
    return ms[_LENGTH_PREFIX_BYTES : _LENGTH_PREFIX_BYTES + n]
