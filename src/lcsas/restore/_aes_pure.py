"""Pure-Python AES-128 / AES-256 implementation.

This module provides a *minimal* AES implementation sufficient for the
LCSAS restic-fallback restore path.  It intentionally avoids any C
extensions or third-party packages so that restoration remains possible
on a bare Python 3 installation decades from now.

**Performance:** Pure-Python AES is ~1000× slower than OpenSSL.  This
is acceptable because it is only used when *no native binary works at
all* — a last-resort fallback for a 50-year survivability window.

Supported modes:
    - ECB  (single-block encrypt, used for Poly1305-AES nonce)
    - CTR  (counter mode, used for restic data encryption)

References:
    - FIPS 197: Advanced Encryption Standard
    - https://csrc.nist.gov/publications/detail/fips/197/final
"""

from __future__ import annotations

import struct

# ── AES S-Box (SubBytes) ────────────────────────────────────────

_SBOX: tuple[int, ...] = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5,
    0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0,
    0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC,
    0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A,
    0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0,
    0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B,
    0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85,
    0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17,
    0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88,
    0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C,
    0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9,
    0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6,
    0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E,
    0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94,
    0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68,
    0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

# ── Round constants ──────────────────────────────────────────────

_RCON: tuple[int, ...] = (
    0x01, 0x02, 0x04, 0x08, 0x10,
    0x20, 0x40, 0x80, 0x1B, 0x36,
)


# ── GF(2^8) helpers ─────────────────────────────────────────────

def _xtime(a: int) -> int:
    """Multiply by x in GF(2^8) with irreducible polynomial x^8+x^4+x^3+x+1."""
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _gf_mul(a: int, b: int) -> int:
    """Multiply two elements in GF(2^8)."""
    result = 0
    temp = a
    for _ in range(8):
        if b & 1:
            result ^= temp
        temp = _xtime(temp)
        b >>= 1
    return result


# ── Pre-compute multiplication tables for MixColumns ────────────

_MUL2 = tuple(_gf_mul(2, i) for i in range(256))
_MUL3 = tuple(_gf_mul(3, i) for i in range(256))


# ── Key Schedule ─────────────────────────────────────────────────

def _sub_word(w: int) -> int:
    """Apply S-box to each byte of a 32-bit word."""
    return (
        (_SBOX[(w >> 24) & 0xFF] << 24)
        | (_SBOX[(w >> 16) & 0xFF] << 16)
        | (_SBOX[(w >> 8) & 0xFF] << 8)
        | _SBOX[w & 0xFF]
    )


def _rot_word(w: int) -> int:
    """Rotate a 32-bit word left by 8 bits."""
    return ((w << 8) | (w >> 24)) & 0xFFFFFFFF


def key_schedule(key: bytes) -> list[bytes]:
    """Expand an AES key into round keys.

    Args:
        key: 16 bytes (AES-128) or 32 bytes (AES-256).

    Returns:
        List of 16-byte round keys (11 for AES-128, 15 for AES-256).
    """
    key_len = len(key)
    if key_len == 16:
        nk, nr = 4, 10
    elif key_len == 32:
        nk, nr = 8, 14
    else:
        raise ValueError(f"AES key must be 16 or 32 bytes, got {key_len}")

    # Parse key into 32-bit words
    w: list[int] = list(struct.unpack(f">{nk}I", key))

    total_words = 4 * (nr + 1)
    for i in range(nk, total_words):
        temp = w[i - 1]
        if i % nk == 0:
            temp = _sub_word(_rot_word(temp)) ^ (_RCON[i // nk - 1] << 24)
        elif nk > 6 and i % nk == 4:
            temp = _sub_word(temp)
        w.append(w[i - nk] ^ temp)

    # Pack words into 16-byte round keys
    round_keys = []
    for r in range(nr + 1):
        rk = struct.pack(">4I", w[4 * r], w[4 * r + 1], w[4 * r + 2], w[4 * r + 3])
        round_keys.append(rk)
    return round_keys


# ── AES Block Encryption (ECB, single block) ────────────────────

def _sub_bytes(state: list[int]) -> None:
    """Apply S-box substitution in-place."""
    for i in range(16):
        state[i] = _SBOX[state[i]]


def _shift_rows(state: list[int]) -> None:
    """Shift rows of the state matrix in-place.

    State is stored column-major: index = row + 4*col.
    """
    # Row 1: shift left by 1
    state[1], state[5], state[9], state[13] = (
        state[5], state[9], state[13], state[1]
    )
    # Row 2: shift left by 2
    state[2], state[6], state[10], state[14] = (
        state[10], state[14], state[2], state[6]
    )
    # Row 3: shift left by 3
    state[3], state[7], state[11], state[15] = (
        state[15], state[3], state[7], state[11]
    )


def _mix_columns(state: list[int]) -> None:
    """MixColumns transformation in-place."""
    for c in range(4):
        i = 4 * c
        s0, s1, s2, s3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i]     = _MUL2[s0] ^ _MUL3[s1] ^ s2 ^ s3
        state[i + 1] = s0 ^ _MUL2[s1] ^ _MUL3[s2] ^ s3
        state[i + 2] = s0 ^ s1 ^ _MUL2[s2] ^ _MUL3[s3]
        state[i + 3] = _MUL3[s0] ^ s1 ^ s2 ^ _MUL2[s3]


def _add_round_key(state: list[int], rk: bytes) -> None:
    """XOR state with a round key."""
    for i in range(16):
        state[i] ^= rk[i]


def aes_encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    """Encrypt a single 16-byte block using AES (ECB mode).

    Args:
        block: Exactly 16 bytes of plaintext.
        round_keys: Output of ``key_schedule()``.

    Returns:
        16 bytes of ciphertext.
    """
    nr = len(round_keys) - 1  # 10 for AES-128, 14 for AES-256
    state = list(block)

    _add_round_key(state, round_keys[0])

    for rnd in range(1, nr):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])

    # Final round (no MixColumns)
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, round_keys[nr])

    return bytes(state)


# ── CTR Mode ─────────────────────────────────────────────────────

def _inc_counter(ctr: bytearray) -> None:
    """Increment a 128-bit big-endian counter in-place."""
    for i in range(15, -1, -1):
        ctr[i] = (ctr[i] + 1) & 0xFF
        if ctr[i] != 0:
            break


def aes_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt or decrypt using AES-CTR mode.

    AES-CTR is symmetric: encrypt and decrypt are the same operation.

    Args:
        key: 16 or 32 bytes AES key.
        iv: 16-byte initialization vector (used as initial counter).
        data: Plaintext or ciphertext of any length.

    Returns:
        Ciphertext or plaintext (same length as *data*).
    """
    rk = key_schedule(key)
    counter = bytearray(iv)
    out = bytearray(len(data))
    offset = 0

    while offset < len(data):
        keystream = aes_encrypt_block(bytes(counter), rk)
        chunk = min(16, len(data) - offset)
        for i in range(chunk):
            out[offset + i] = data[offset + i] ^ keystream[i]
        offset += chunk
        _inc_counter(counter)

    return bytes(out)
