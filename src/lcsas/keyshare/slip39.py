"""Pure-Python, stdlib-only SLIP-0039 Shamir Secret Sharing.

A faithful implementation of SLIP-0039 (https://github.com/satoshilabs/slips/
blob/master/slip-0039.md): split a master secret into ``N`` checksummed
word-mnemonic shares with a ``K``-of-``N`` threshold (optionally grouped) and
recombine any ``>= K`` shares back to the secret.

The full scheme is implemented: RS1024 checksum, GF(256) Shamir over the
Rijndael field, the SLIP-0039 4-round Feistel passphrase encryption (PBKDF2-
HMAC-SHA256), the HMAC-SHA256 digest share-integrity check, and the grouped
(group_threshold / groups) two-level structure.

This module is deliberately self-contained and depends only on the Python
standard library (``hashlib``, ``hmac``, ``secrets``) plus the bundled wordlist,
so it can be shipped as a standalone combiner on the LCSAS meta-volume.  It does
*not* import the rest of ``lcsas``.

Algorithm ported from the MIT-licensed reference implementation
``trezor/python-shamir-mnemonic`` (Copyright (c) 2018 Andrew R. Kozlik) and the
SLIP-0039 specification; cross-checked against the official test vectors.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from .wordlist import _WORD_TO_INDEX, WORDLIST


class KeyShareError(Exception):
    """A SLIP-0039 share set could not be processed.

    Raised on: malformed or too-short mnemonics, RS1024 checksum failure,
    padding errors, an unknown word, mismatched share-set parameters, an
    insufficient number of shares (below threshold), or a failed
    digest/integrity check.  Self-contained on purpose (does not inherit from
    ``lcsas.exceptions``) so the combiner can be bundled standalone.
    """


# --------------------------------------------------------------------------- #
# Constants (SLIP-0039 §"Master secret encryption" and the share format).
# --------------------------------------------------------------------------- #

RADIX_BITS = 10
"""The length of the radix in bits (the wordlist has 2**10 = 1024 words)."""

RADIX = 1 << RADIX_BITS

ID_LENGTH_BITS = 15
"""Length of the random identifier in bits."""

EXTENDABLE_FLAG_LENGTH_BITS = 1
"""Length of the extendable backup flag in bits."""

ITERATION_EXP_LENGTH_BITS = 4
"""Length of the iteration exponent in bits."""


def _bits_to_bytes(n: int) -> int:
    """Round a bit count up to whole bytes."""
    return (n + 7) // 8


def _bits_to_words(n: int) -> int:
    """Round a bit count up to a whole number of 10-bit words."""
    return (n + RADIX_BITS - 1) // RADIX_BITS


ID_EXP_LENGTH_WORDS = _bits_to_words(
    ID_LENGTH_BITS + EXTENDABLE_FLAG_LENGTH_BITS + ITERATION_EXP_LENGTH_BITS
)
"""Words spanning the identifier, extendable flag and iteration exponent."""

MAX_SHARE_COUNT = 16
"""Maximum number of shares per group (member index is 4 bits)."""

CHECKSUM_LENGTH_WORDS = 3
"""Length of the RS1024 checksum in words."""

DIGEST_LENGTH_BYTES = 4
"""Length of the truncated HMAC-SHA256 digest in bytes."""

CUSTOMIZATION_STRING_ORIG = b"shamir"
"""RS1024/PBKDF2 customization string for non-extendable shares."""

CUSTOMIZATION_STRING_EXTENDABLE = b"shamir_extendable"
"""RS1024 customization string for extendable shares."""

GROUP_PREFIX_LENGTH_WORDS = ID_EXP_LENGTH_WORDS + 1
"""Words common to every share in a group (used only for error messages)."""

METADATA_LENGTH_WORDS = ID_EXP_LENGTH_WORDS + 2 + CHECKSUM_LENGTH_WORDS
"""Length of a mnemonic in words, excluding the share value."""

MIN_STRENGTH_BITS = 128
"""Minimum allowed entropy of the master secret."""

MIN_MNEMONIC_LENGTH_WORDS = METADATA_LENGTH_WORDS + _bits_to_words(MIN_STRENGTH_BITS)
"""Minimum allowed length of a mnemonic in words."""

BASE_ITERATION_COUNT = 10000
"""Base total PBKDF2 iteration count (split across the 4 Feistel rounds)."""

ROUND_COUNT = 4
"""Number of Feistel rounds."""

SECRET_INDEX = 255
"""x-coordinate at which f(x) equals the shared secret."""

DIGEST_INDEX = 254
"""x-coordinate at which f(x) equals the digest share."""


# --------------------------------------------------------------------------- #
# RS1024 checksum (SLIP-0039 §"Checksum").
# --------------------------------------------------------------------------- #

_RS1024_GEN = (
    0xE0E040,
    0x1C1C080,
    0x3838100,
    0x7070200,
    0xE0E0009,
    0x1C0C2412,
    0x38086C24,
    0x3090FC48,
    0x21B1F890,
    0x3F3F120,
)


def _rs1024_polymod(values: Iterable[int]) -> int:
    chk = 1
    for v in values:
        b = chk >> 20
        chk = (chk & 0xFFFFF) << 10 ^ v
        for i in range(10):
            chk ^= _RS1024_GEN[i] if ((b >> i) & 1) else 0
    return chk


def _rs1024_create_checksum(data: Sequence[int], customization: bytes) -> list[int]:
    values = list(customization) + list(data) + [0] * CHECKSUM_LENGTH_WORDS
    polymod = _rs1024_polymod(values) ^ 1
    return [(polymod >> 10 * i) & 1023 for i in reversed(range(CHECKSUM_LENGTH_WORDS))]


def _rs1024_verify_checksum(data: Sequence[int], customization: bytes) -> bool:
    return _rs1024_polymod(list(customization) + list(data)) == 1


def _customization_string(extendable: bool) -> bytes:
    return CUSTOMIZATION_STRING_EXTENDABLE if extendable else CUSTOMIZATION_STRING_ORIG


# --------------------------------------------------------------------------- #
# Word <-> integer conversion.
# --------------------------------------------------------------------------- #


def _int_to_word_indices(value: int, length: int) -> list[int]:
    """Convert an integer to ``length`` base-1024 indices, big endian."""
    mask = RADIX - 1
    return [(value >> (i * RADIX_BITS)) & mask for i in reversed(range(length))]


def _int_from_word_indices(indices: Iterable[int]) -> int:
    """Convert base-1024 indices (big endian) back to an integer."""
    value = 0
    for index in indices:
        value = value * RADIX + index
    return value


def _mnemonic_to_indices(mnemonic: str) -> list[int]:
    try:
        return [_WORD_TO_INDEX[word.lower()] for word in mnemonic.split()]
    except KeyError as exc:
        raise KeyShareError(f"Unknown word in mnemonic: {exc.args[0]!r}.") from None


def _words_from_indices(indices: Iterable[int]) -> list[str]:
    return [WORDLIST[i] for i in indices]


# --------------------------------------------------------------------------- #
# GF(256) arithmetic and Lagrange interpolation (SLIP-0039 §"Shamir...").
# --------------------------------------------------------------------------- #


def _precompute_exp_log() -> tuple[list[int], list[int]]:
    exp = [0] * 255
    log = [0] * 256
    poly = 1
    for i in range(255):
        exp[i] = poly
        log[poly] = i
        # Multiply poly by (x + 1) ...
        poly = (poly << 1) ^ poly
        # ... and reduce modulo the Rijndael polynomial x^8 + x^4 + x^3 + x + 1.
        if poly & 0x100:
            poly ^= 0x11B
    return exp, log


_EXP_TABLE, _LOG_TABLE = _precompute_exp_log()


class _RawShare(NamedTuple):
    x: int
    data: bytes


def _interpolate(shares: Sequence[_RawShare], x: int) -> bytes:
    """Evaluate the interpolating polynomial(s) at ``x`` over GF(256)."""
    x_coordinates = {share.x for share in shares}
    if len(x_coordinates) != len(shares):
        raise KeyShareError("Invalid set of shares. Share indices must be unique.")

    share_value_lengths = {len(share.data) for share in shares}
    if len(share_value_lengths) != 1:
        raise KeyShareError(
            "Invalid set of shares. All share values must have the same length."
        )

    if x in x_coordinates:
        for share in shares:
            if share.x == x:
                return share.data

    # Logarithm of the product of (x_i - x) for all shares.
    log_prod = sum(_LOG_TABLE[share.x ^ x] for share in shares)

    result = bytes(share_value_lengths.pop())
    for share in shares:
        log_basis_eval = (
            log_prod
            - _LOG_TABLE[share.x ^ x]
            - sum(_LOG_TABLE[share.x ^ other.x] for other in shares)
        ) % 255
        result = bytes(
            intermediate_sum
            ^ (
                _EXP_TABLE[(_LOG_TABLE[share_val] + log_basis_eval) % 255]
                if share_val != 0
                else 0
            )
            for share_val, intermediate_sum in zip(share.data, result, strict=True)
        )
    return result


def _create_digest(random_data: bytes, shared_secret: bytes) -> bytes:
    return hmac.new(random_data, shared_secret, "sha256").digest()[:DIGEST_LENGTH_BYTES]


def _split_secret(
    threshold: int, share_count: int, shared_secret: bytes
) -> list[_RawShare]:
    if threshold < 1:
        raise ValueError("The requested threshold must be a positive integer.")
    if threshold > share_count:
        raise ValueError("The requested threshold must not exceed the number of shares.")
    if share_count > MAX_SHARE_COUNT:
        raise ValueError(
            f"The requested number of shares must not exceed {MAX_SHARE_COUNT}."
        )

    # With threshold 1 the digest is unused: every share is the secret itself.
    if threshold == 1:
        return [_RawShare(i, shared_secret) for i in range(share_count)]

    random_share_count = threshold - 2
    shares = [
        _RawShare(i, _random_bytes(len(shared_secret)))
        for i in range(random_share_count)
    ]
    random_part = _random_bytes(len(shared_secret) - DIGEST_LENGTH_BYTES)
    digest = _create_digest(random_part, shared_secret)

    base_shares = [
        *shares,
        _RawShare(DIGEST_INDEX, digest + random_part),
        _RawShare(SECRET_INDEX, shared_secret),
    ]
    for i in range(random_share_count, share_count):
        shares.append(_RawShare(i, _interpolate(base_shares, i)))
    return shares


def _recover_secret(threshold: int, shares: Sequence[_RawShare]) -> bytes:
    if threshold == 1:
        return shares[0].data

    shared_secret = _interpolate(shares, SECRET_INDEX)
    digest_share = _interpolate(shares, DIGEST_INDEX)
    digest = digest_share[:DIGEST_LENGTH_BYTES]
    random_part = digest_share[DIGEST_LENGTH_BYTES:]
    if digest != _create_digest(random_part, shared_secret):
        raise KeyShareError("Invalid digest of the shared secret.")
    return shared_secret


# --------------------------------------------------------------------------- #
# Passphrase encryption: 4-round Feistel with a PBKDF2-HMAC-SHA256 round
# function (SLIP-0039 §"Master secret encryption").
# --------------------------------------------------------------------------- #


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _round_function(
    i: int, passphrase: bytes, exponent: int, salt: bytes, r: bytes
) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        bytes([i]) + passphrase,
        salt + r,
        (BASE_ITERATION_COUNT << exponent) // ROUND_COUNT,
        dklen=len(r),
    )


def _get_salt(identifier: int, extendable: bool) -> bytes:
    if extendable:
        return b""
    return CUSTOMIZATION_STRING_ORIG + identifier.to_bytes(
        _bits_to_bytes(ID_LENGTH_BITS), "big"
    )


def _encrypt(
    master_secret: bytes,
    passphrase: bytes,
    iteration_exponent: int,
    identifier: int,
    extendable: bool,
) -> bytes:
    left = master_secret[: len(master_secret) // 2]
    right = master_secret[len(master_secret) // 2 :]
    salt = _get_salt(identifier, extendable)
    for i in range(ROUND_COUNT):
        f = _round_function(i, passphrase, iteration_exponent, salt, right)
        left, right = right, _xor(left, f)
    return right + left


def _decrypt(
    encrypted_master_secret: bytes,
    passphrase: bytes,
    iteration_exponent: int,
    identifier: int,
    extendable: bool,
) -> bytes:
    left = encrypted_master_secret[: len(encrypted_master_secret) // 2]
    right = encrypted_master_secret[len(encrypted_master_secret) // 2 :]
    salt = _get_salt(identifier, extendable)
    for i in reversed(range(ROUND_COUNT)):
        f = _round_function(i, passphrase, iteration_exponent, salt, right)
        left, right = right, _xor(left, f)
    return right + left


# --------------------------------------------------------------------------- #
# Share metadata encode/decode (SLIP-0039 §"Share format").
# --------------------------------------------------------------------------- #


class _ShareCommonParameters(NamedTuple):
    identifier: int
    extendable: bool
    iteration_exponent: int
    group_threshold: int
    group_count: int


class _ShareGroupParameters(NamedTuple):
    identifier: int
    extendable: bool
    iteration_exponent: int
    group_index: int
    group_threshold: int
    group_count: int
    member_threshold: int


class _Share(NamedTuple):
    identifier: int
    extendable: bool
    iteration_exponent: int
    group_index: int
    group_threshold: int
    group_count: int
    member_index: int
    member_threshold: int
    value: bytes

    def common_parameters(self) -> _ShareCommonParameters:
        return _ShareCommonParameters(
            self.identifier,
            self.extendable,
            self.iteration_exponent,
            self.group_threshold,
            self.group_count,
        )

    def group_parameters(self) -> _ShareGroupParameters:
        return _ShareGroupParameters(
            self.identifier,
            self.extendable,
            self.iteration_exponent,
            self.group_index,
            self.group_threshold,
            self.group_count,
            self.member_threshold,
        )

    def _encode_id_exp(self) -> list[int]:
        value = self.identifier << (
            ITERATION_EXP_LENGTH_BITS + EXTENDABLE_FLAG_LENGTH_BITS
        )
        value += int(self.extendable) << ITERATION_EXP_LENGTH_BITS
        value += self.iteration_exponent
        return _int_to_word_indices(value, ID_EXP_LENGTH_WORDS)

    def _encode_share_params(self) -> list[int]:
        value = self.group_index
        value <<= 4
        value += self.group_threshold - 1
        value <<= 4
        value += self.group_count - 1
        value <<= 4
        value += self.member_index
        value <<= 4
        value += self.member_threshold - 1
        return _int_to_word_indices(value, 2)

    def words(self) -> list[str]:
        value_word_count = _bits_to_words(len(self.value) * 8)
        value_int = int.from_bytes(self.value, "big")
        value_data = _int_to_word_indices(value_int, value_word_count)
        share_data = self._encode_id_exp() + self._encode_share_params() + value_data
        checksum = _rs1024_create_checksum(
            share_data, _customization_string(self.extendable)
        )
        return _words_from_indices(share_data + checksum)

    def mnemonic(self) -> str:
        return " ".join(self.words())

    @classmethod
    def from_mnemonic(cls, mnemonic: str) -> _Share:
        data = _mnemonic_to_indices(mnemonic)

        if len(data) < MIN_MNEMONIC_LENGTH_WORDS:
            raise KeyShareError(
                "Invalid mnemonic length. Each mnemonic must be at least "
                f"{MIN_MNEMONIC_LENGTH_WORDS} words."
            )

        padding_len = (RADIX_BITS * (len(data) - METADATA_LENGTH_WORDS)) % 16
        if padding_len > 8:
            raise KeyShareError("Invalid mnemonic length.")

        id_exp_int = _int_from_word_indices(data[:ID_EXP_LENGTH_WORDS])
        identifier = id_exp_int >> (
            EXTENDABLE_FLAG_LENGTH_BITS + ITERATION_EXP_LENGTH_BITS
        )
        extendable = bool((id_exp_int >> ITERATION_EXP_LENGTH_BITS) & 1)
        iteration_exponent = id_exp_int & ((1 << ITERATION_EXP_LENGTH_BITS) - 1)

        if not _rs1024_verify_checksum(data, _customization_string(extendable)):
            raise KeyShareError(
                "Invalid mnemonic checksum for "
                f'"{" ".join(mnemonic.split()[: ID_EXP_LENGTH_WORDS + 2])} ...".'
            )

        share_params_int = _int_from_word_indices(
            data[ID_EXP_LENGTH_WORDS : ID_EXP_LENGTH_WORDS + 2]
        )
        mask = (1 << 4) - 1
        params = [(share_params_int >> (i * 4)) & mask for i in reversed(range(5))]
        group_index, group_threshold, group_count, member_index, member_threshold = (
            params
        )

        if group_count < group_threshold:
            raise KeyShareError(
                "Invalid mnemonic. Group threshold cannot exceed group count."
            )

        value_data = data[ID_EXP_LENGTH_WORDS + 2 : -CHECKSUM_LENGTH_WORDS]
        value_byte_count = _bits_to_bytes(RADIX_BITS * len(value_data) - padding_len)
        value_int = _int_from_word_indices(value_data)
        try:
            value = value_int.to_bytes(value_byte_count, "big")
        except OverflowError:
            raise KeyShareError("Invalid mnemonic padding.") from None

        return cls(
            identifier,
            extendable,
            iteration_exponent,
            group_index,
            group_threshold + 1,
            group_count + 1,
            member_index,
            member_threshold + 1,
            value,
        )


# --------------------------------------------------------------------------- #
# Group container (SLIP-0039 grouped structure).
# --------------------------------------------------------------------------- #


class _ShareGroup:
    def __init__(self) -> None:
        self.shares: set[_Share] = set()

    def __len__(self) -> int:
        return len(self.shares)

    def add(self, share: _Share) -> None:
        if self.shares and self.group_parameters() != share.group_parameters():
            mismatch = next(
                name
                for name, x, y in zip(
                    _ShareGroupParameters._fields,
                    self.group_parameters(),
                    share.group_parameters(),
                    strict=True,
                )
                if x != y
            )
            raise KeyShareError(
                f"Invalid set of mnemonics. The {mismatch} parameters don't match."
            )
        self.shares.add(share)

    def to_raw_shares(self) -> list[_RawShare]:
        return [_RawShare(s.member_index, s.value) for s in self.shares]

    def common_parameters(self) -> _ShareCommonParameters:
        return next(iter(self.shares)).common_parameters()

    def group_parameters(self) -> _ShareGroupParameters:
        return next(iter(self.shares)).group_parameters()

    def member_threshold(self) -> int:
        return next(iter(self.shares)).member_threshold


# --------------------------------------------------------------------------- #
# Encrypted master secret container.
# --------------------------------------------------------------------------- #


class _EncryptedMasterSecret(NamedTuple):
    identifier: int
    extendable: bool
    iteration_exponent: int
    ciphertext: bytes

    @classmethod
    def from_master_secret(
        cls,
        master_secret: bytes,
        passphrase: bytes,
        identifier: int,
        extendable: bool,
        iteration_exponent: int,
    ) -> _EncryptedMasterSecret:
        ciphertext = _encrypt(
            master_secret, passphrase, iteration_exponent, identifier, extendable
        )
        return cls(identifier, extendable, iteration_exponent, ciphertext)

    def decrypt(self, passphrase: bytes) -> bytes:
        return _decrypt(
            self.ciphertext,
            passphrase,
            self.iteration_exponent,
            self.identifier,
            self.extendable,
        )


# --------------------------------------------------------------------------- #
# Randomness (overridable for deterministic testing).
# --------------------------------------------------------------------------- #

_random_bytes = secrets.token_bytes


def _random_identifier() -> int:
    raw = int.from_bytes(_random_bytes(_bits_to_bytes(ID_LENGTH_BITS)), "big")
    return raw & ((1 << ID_LENGTH_BITS) - 1)


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #


def _check_master_secret(master_secret: bytes) -> None:
    """Enforce the SLIP-0039 master-secret constraints.

    The master secret must be at least 128 bits (16 bytes) and an even number of
    bytes (the Feistel network splits it in half).  Callers that hold a short or
    odd-length value (such as a variable-length repository password) are
    responsible for encoding it into a valid master secret first; this primitive
    deliberately raises rather than silently padding, so that round-tripping
    returns exactly the bytes that were split.
    """
    if len(master_secret) * 8 < MIN_STRENGTH_BITS:
        raise KeyShareError(
            "The master secret must be at least "
            f"{_bits_to_bytes(MIN_STRENGTH_BITS)} bytes "
            f"({MIN_STRENGTH_BITS} bits) long."
        )
    if len(master_secret) % 2 != 0:
        raise KeyShareError(
            "The master secret length in bytes must be an even number."
        )


def _check_passphrase(passphrase: bytes) -> None:
    if not all(32 <= c <= 126 for c in passphrase):
        raise KeyShareError(
            "The passphrase must contain only printable ASCII characters "
            "(code points 32-126)."
        )


def generate_mnemonics(
    group_threshold: int,
    groups: list[tuple[int, int]],
    master_secret: bytes,
    passphrase: bytes = b"",
    iteration_exponent: int = 1,
    *,
    extendable: bool = True,
) -> list[list[str]]:
    """Split ``master_secret`` into grouped SLIP-0039 mnemonic shares.

    :param group_threshold: number of groups required to reconstruct the secret.
    :param groups: one ``(member_threshold, member_count)`` pair per group.
    :param master_secret: the secret to split (>=16 bytes, even length).
    :param passphrase: optional encryption passphrase (printable ASCII bytes).
    :param iteration_exponent: PBKDF2 iteration exponent (0-15).
    :param extendable: use the extendable-backup salt construction (default
        True, matching the official vectors' generation path).
    :returns: a list (one entry per group) of lists of mnemonic strings.
    :raises KeyShareError: on an invalid secret, passphrase, or group/threshold
        configuration.
    """
    _check_master_secret(master_secret)
    _check_passphrase(passphrase)

    if group_threshold < 1:
        raise KeyShareError("The group threshold must be a positive integer.")
    if group_threshold > len(groups):
        raise KeyShareError(
            "The group threshold must not exceed the number of groups."
        )
    if any(
        member_threshold == 1 and member_count > 1
        for member_threshold, member_count in groups
    ):
        raise KeyShareError(
            "Creating multiple member shares with member threshold 1 is not "
            "allowed. Use 1-of-1 member sharing instead."
        )

    identifier = _random_identifier()
    ems = _EncryptedMasterSecret.from_master_secret(
        master_secret, passphrase, identifier, extendable, iteration_exponent
    )

    group_shares = _split_secret(group_threshold, len(groups), ems.ciphertext)

    result: list[list[str]] = []
    for (member_threshold, member_count), (group_index, group_secret) in zip(
        groups, group_shares, strict=True
    ):
        group_mnemonics = [
            _Share(
                identifier,
                extendable,
                iteration_exponent,
                group_index,
                group_threshold,
                len(groups),
                member_index,
                member_threshold,
                value,
            ).mnemonic()
            for member_index, value in _split_secret(
                member_threshold, member_count, group_secret
            )
        ]
        result.append(group_mnemonics)
    return result


def _decode_mnemonics(mnemonics: Iterable[str]) -> dict[int, _ShareGroup]:
    common_params: set[_ShareCommonParameters] = set()
    groups: dict[int, _ShareGroup] = {}
    for mnemonic in mnemonics:
        share = _Share.from_mnemonic(mnemonic)
        common_params.add(share.common_parameters())
        group = groups.setdefault(share.group_index, _ShareGroup())
        group.add(share)

    if len(common_params) != 1:
        raise KeyShareError(
            "Invalid set of mnemonics. All mnemonics must begin with the same "
            f"{ID_EXP_LENGTH_WORDS} words and share the same group threshold "
            "and group count."
        )
    return groups


def _recover_ems(groups: dict[int, _ShareGroup]) -> _EncryptedMasterSecret:
    if not groups:
        raise KeyShareError("The set of shares is empty.")

    params = next(iter(groups.values())).common_parameters()

    if len(groups) < params.group_threshold:
        raise KeyShareError(
            "Insufficient number of mnemonic groups. The required number of "
            f"groups is {params.group_threshold}."
        )
    if len(groups) != params.group_threshold:
        raise KeyShareError(
            "Wrong number of mnemonic groups. Expected "
            f"{params.group_threshold} groups, but {len(groups)} were provided."
        )

    for group in groups.values():
        if len(group) != group.member_threshold():
            raise KeyShareError(
                "Wrong number of mnemonics. Expected "
                f"{group.member_threshold()} mnemonics in the group, but "
                f"{len(group)} were provided."
            )

    group_shares = [
        _RawShare(
            group_index,
            _recover_secret(group.member_threshold(), group.to_raw_shares()),
        )
        for group_index, group in groups.items()
    ]
    ciphertext = _recover_secret(params.group_threshold, group_shares)
    return _EncryptedMasterSecret(
        params.identifier, params.extendable, params.iteration_exponent, ciphertext
    )


def combine_mnemonics(mnemonics: list[str], passphrase: bytes = b"") -> bytes:
    """Recombine SLIP-0039 mnemonic shares into the master secret.

    :param mnemonics: at least ``K`` valid mnemonic strings from one share set.
    :param passphrase: the passphrase used when the secret was split.
    :returns: the recovered master secret.
    :raises KeyShareError: if the list is empty, a mnemonic is malformed, the
        checksum or digest fails, the parameters mismatch, or fewer than the
        required number of shares are supplied.
    """
    if not mnemonics:
        raise KeyShareError("The list of mnemonics is empty.")
    groups = _decode_mnemonics(mnemonics)
    ems = _recover_ems(groups)
    return ems.decrypt(passphrase)


def split_secret(
    secret: bytes,
    threshold: int,
    count: int,
    passphrase: bytes = b"",
    iteration_exponent: int = 1,
) -> list[str]:
    """Convenience: split ``secret`` into a single group of ``count`` shares.

    Any ``threshold`` of the returned mnemonics reconstruct the secret.

    :raises KeyShareError: on an invalid secret/passphrase or threshold/count.
    """
    return generate_mnemonics(
        1,
        [(threshold, count)],
        secret,
        passphrase,
        iteration_exponent,
    )[0]


def recover_secret(mnemonics: list[str], passphrase: bytes = b"") -> bytes:
    """Convenience alias for :func:`combine_mnemonics`."""
    return combine_mnemonics(mnemonics, passphrase)
