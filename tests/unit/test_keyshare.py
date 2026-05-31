"""Tests for the stdlib-only SLIP-0039 key-share primitive.

Covers the official SLIP-0039 test vectors (known-answer fixtures), property
round-trips for every K-of-N subset, threshold-failure detection, single-word /
single-share corruption detection, and every error branch the vectors do not
themselves reach (to keep the module at 100% line coverage).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lcsas.keyshare import (
    KeyShareError,
    combine_mnemonics,
    generate_mnemonics,
    recover_secret,
    slip39,
    split_secret,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "keyshare"
_VECTORS = json.loads((_FIXTURES / "vectors.json").read_text())

# The official SLIP-0039 vectors are all encrypted with this passphrase.
_VECTOR_PASSPHRASE = b"TREZOR"

# A canonical 16-byte (128-bit) master secret used by the property tests.
_MS = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


# --------------------------------------------------------------------------- #
# Official SLIP-0039 test vectors.
# --------------------------------------------------------------------------- #


def _split_vectors() -> tuple[list, list]:
    valid = [v for v in _VECTORS if v[2]]
    invalid = [v for v in _VECTORS if not v[2]]
    return valid, invalid


def test_vector_fixture_shape() -> None:
    """The committed fixture is the full official set (15 valid + 30 invalid)."""
    valid, invalid = _split_vectors()
    assert len(_VECTORS) == 45
    assert len(valid) == 15
    assert len(invalid) == 30


@pytest.mark.parametrize(
    "description, mnemonics, secret_hex",
    [(v[0], v[1], v[2]) for v in _VECTORS if v[2]],
    ids=[v[0] for v in _VECTORS if v[2]],
)
def test_official_valid_vectors(
    description: str, mnemonics: list[str], secret_hex: str
) -> None:
    assert combine_mnemonics(mnemonics, _VECTOR_PASSPHRASE).hex() == secret_hex


@pytest.mark.parametrize(
    "description, mnemonics",
    [(v[0], v[1]) for v in _VECTORS if not v[2]],
    ids=[v[0] for v in _VECTORS if not v[2]],
)
def test_official_invalid_vectors(description: str, mnemonics: list[str]) -> None:
    with pytest.raises(KeyShareError):
        combine_mnemonics(mnemonics, _VECTOR_PASSPHRASE)


def test_all_official_vectors_accounted_for() -> None:
    """Belt-and-braces: 15 decode to the expected secret, 30 raise."""
    decoded = raised = 0
    for _desc, mnemonics, secret_hex, _xprv in _VECTORS:
        if secret_hex:
            assert combine_mnemonics(mnemonics, _VECTOR_PASSPHRASE).hex() == secret_hex
            decoded += 1
        else:
            with pytest.raises(KeyShareError):
                combine_mnemonics(mnemonics, _VECTOR_PASSPHRASE)
            raised += 1
    assert (decoded, raised) == (15, 30)


# --------------------------------------------------------------------------- #
# Round-trip / property tests.
# --------------------------------------------------------------------------- #


def test_single_group_round_trip_no_passphrase() -> None:
    mnemonics = split_secret(_MS, threshold=2, count=3)
    assert recover_secret(mnemonics[:2]) == _MS


def test_single_group_round_trip_with_passphrase() -> None:
    mnemonics = split_secret(_MS, threshold=3, count=5, passphrase=b"hunter2")
    assert recover_secret(mnemonics[:3], b"hunter2") == _MS


def test_wrong_passphrase_yields_different_secret() -> None:
    """SLIP-0039 has no wrong-passphrase error; it just decrypts to junk."""
    mnemonics = split_secret(_MS, threshold=2, count=3, passphrase=b"right")
    assert recover_secret(mnemonics[:2], b"wrong") != _MS


@pytest.mark.parametrize("iteration_exponent", [0, 1, 2])
def test_iteration_exponents_round_trip(iteration_exponent: int) -> None:
    mnemonics = split_secret(
        _MS, threshold=2, count=3, passphrase=b"x", iteration_exponent=iteration_exponent
    )
    assert recover_secret(mnemonics[:2], b"x") == _MS


def test_every_k_of_n_subset_reconstructs() -> None:
    from itertools import combinations

    k, n = 3, 5
    mnemonics = split_secret(_MS, threshold=k, count=n, passphrase=b"pp")
    for subset in combinations(mnemonics, k):
        assert recover_secret(list(subset), b"pp") == _MS


def test_any_k_minus_one_subset_fails() -> None:
    from itertools import combinations

    k, n = 3, 5
    mnemonics = split_secret(_MS, threshold=k, count=n, passphrase=b"pp")
    for subset in combinations(mnemonics, k - 1):
        # k-1 shares either decode to the wrong secret or fail integrity.
        result: bytes | None
        try:
            result = recover_secret(list(subset), b"pp")
        except KeyShareError:
            result = None
        assert result != _MS


def test_threshold_one_round_trip() -> None:
    """threshold==1 takes the digest-free short-circuit in split and recover."""
    mnemonics = split_secret(_MS, threshold=1, count=1)
    assert recover_secret(mnemonics) == _MS
    assert len(mnemonics) == 1


def test_256_bit_secret_round_trip() -> None:
    secret = bytes(range(32))
    mnemonics = split_secret(secret, threshold=2, count=3)
    assert recover_secret(mnemonics[:2]) == secret
    assert len(mnemonics[0].split()) == 33


@pytest.mark.parametrize("extendable", [True, False])
def test_both_extendable_flags_round_trip(extendable: bool) -> None:
    mnemonics = generate_mnemonics(
        1, [(2, 3)], _MS, b"pw", 1, extendable=extendable
    )[0]
    assert combine_mnemonics(mnemonics[:2], b"pw") == _MS
    share = slip39._Share.from_mnemonic(mnemonics[0])
    assert share.extendable is extendable


def test_grouped_round_trip() -> None:
    """2-of-3 groups, each with its own member threshold."""
    groups = generate_mnemonics(
        2, [(2, 3), (3, 5), (1, 1)], _MS, b"pw"
    )
    # Take group 0 (any 2 of 3) and group 1 (any 3 of 5).
    subset = groups[0][:2] + groups[1][:3]
    assert combine_mnemonics(subset, b"pw") == _MS


# --------------------------------------------------------------------------- #
# Corruption detection.
# --------------------------------------------------------------------------- #


def test_single_corrupted_word_is_detected() -> None:
    """Flipping one word must fail (checksum), never silently mis-decode."""
    mnemonics = split_secret(_MS, threshold=2, count=3)
    words = mnemonics[0].split()
    # Replace a value word with a different valid word.
    swap = "academic" if words[8] != "academic" else "acid"
    words[8] = swap
    mnemonics[0] = " ".join(words)
    with pytest.raises(KeyShareError):
        recover_secret(mnemonics[:2])


def test_unknown_word_is_rejected() -> None:
    mnemonics = split_secret(_MS, threshold=2, count=3)
    bad = mnemonics[0].replace(mnemonics[0].split()[-1], "notaword", 1)
    with pytest.raises(KeyShareError, match="Unknown word"):
        recover_secret([bad] + mnemonics[1:2])


def test_foreign_share_is_rejected() -> None:
    """A share from a different secret is rejected, not silently combined.

    The two shares carry different random identifiers, so the mismatch is
    caught by the common-parameter check before interpolation. (The pure
    digest-mismatch path is covered by official vectors 13 and 32.)
    """
    a = split_secret(_MS, threshold=2, count=3, passphrase=b"p")
    b_secret = bytes(range(16, 32))
    b = split_secret(b_secret, threshold=2, count=3, passphrase=b"p")
    with pytest.raises(KeyShareError):
        recover_secret([a[0], b[0]], b"p")


# --------------------------------------------------------------------------- #
# Input-validation / error branches not reached by the official vectors.
# --------------------------------------------------------------------------- #


def test_short_secret_raises() -> None:
    with pytest.raises(KeyShareError, match="at least"):
        split_secret(b"too short", threshold=2, count=3)


def test_odd_length_secret_raises() -> None:
    with pytest.raises(KeyShareError, match="even number"):
        split_secret(bytes(17), threshold=2, count=3)


def test_non_ascii_passphrase_raises() -> None:
    with pytest.raises(KeyShareError, match="printable ASCII"):
        split_secret(_MS, threshold=2, count=3, passphrase=b"\xff")


def test_group_threshold_below_one_raises() -> None:
    with pytest.raises(KeyShareError, match="positive integer"):
        generate_mnemonics(0, [(2, 3)], _MS)


def test_group_threshold_exceeds_group_count_raises() -> None:
    with pytest.raises(KeyShareError, match="exceed the number of groups"):
        generate_mnemonics(2, [(2, 3)], _MS)


def test_member_threshold_one_with_multiple_shares_raises() -> None:
    with pytest.raises(KeyShareError, match="member threshold 1"):
        generate_mnemonics(1, [(1, 3)], _MS)


def test_split_secret_threshold_zero_raises() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        slip39._split_secret(0, 3, _MS)


def test_split_secret_threshold_exceeds_count_raises() -> None:
    with pytest.raises(ValueError, match="must not exceed the number of shares"):
        slip39._split_secret(4, 3, _MS)


def test_split_secret_too_many_shares_raises() -> None:
    with pytest.raises(ValueError, match="must not exceed 16"):
        slip39._split_secret(2, 17, _MS)


def test_empty_mnemonic_list_raises() -> None:
    with pytest.raises(KeyShareError, match="empty"):
        combine_mnemonics([])


def test_empty_group_set_raises() -> None:
    with pytest.raises(KeyShareError, match="empty"):
        slip39._recover_ems({})


def test_insufficient_groups_raises() -> None:
    """Two groups required, only one supplied."""
    groups = generate_mnemonics(2, [(1, 1), (1, 1)], _MS)
    with pytest.raises(KeyShareError, match="Insufficient number"):
        combine_mnemonics(groups[0])


def test_wrong_number_of_groups_raises() -> None:
    """group_threshold == 2 but 3 distinct groups supplied."""
    groups = generate_mnemonics(2, [(1, 1), (1, 1), (1, 1)], _MS)
    supplied = [groups[0][0], groups[1][0], groups[2][0]]
    with pytest.raises(KeyShareError, match="Wrong number of mnemonic groups"):
        combine_mnemonics(supplied)


def test_wrong_member_count_in_group_raises() -> None:
    """A group needs 3 members but only 2 are supplied."""
    mnemonics = split_secret(_MS, threshold=3, count=5)
    with pytest.raises(KeyShareError, match="Wrong number of mnemonics"):
        combine_mnemonics(mnemonics[:2])


def test_mismatched_common_parameters_raises() -> None:
    """Two shares in *different* groups but with mismatched common parameters.

    A different group_index sidesteps the per-group parameter check, so the
    top-level "all mnemonics share the same common parameters" guard fires.
    """
    groups = generate_mnemonics(2, [(1, 1), (1, 1)], _MS)
    g0 = slip39._Share.from_mnemonic(groups[0][0])
    g1 = slip39._Share.from_mnemonic(groups[1][0])
    # Tamper g1's iteration exponent (a common parameter) and re-encode.
    forged = g1._replace(iteration_exponent=g1.iteration_exponent + 1)
    with pytest.raises(KeyShareError, match="must begin with the same"):
        combine_mnemonics([g0.mnemonic(), forged.mnemonic()])


def test_mismatched_group_parameters_raises() -> None:
    """Same common params, but member thresholds differ within a group index.

    We forge two shares that share common parameters but differ on a
    group-level parameter by re-encoding one share with a tweaked member
    threshold while keeping identifier/group params identical.
    """
    mnemonics = split_secret(_MS, threshold=2, count=3)
    s0 = slip39._Share.from_mnemonic(mnemonics[0])
    # A share in the same group index but with a different member_threshold.
    forged = s0._replace(member_threshold=s0.member_threshold + 1)
    with pytest.raises(KeyShareError, match="parameters don't match"):
        combine_mnemonics([mnemonics[0], forged.mnemonic()])


def test_duplicate_share_indices_raise() -> None:
    """Interpolation rejects a repeated x-coordinate."""
    shares = [slip39._RawShare(0, bytes(16)), slip39._RawShare(0, bytes(16))]
    with pytest.raises(KeyShareError, match="indices must be unique"):
        slip39._interpolate(shares, slip39.SECRET_INDEX)


def test_unequal_share_value_lengths_raise() -> None:
    shares = [slip39._RawShare(0, bytes(16)), slip39._RawShare(1, bytes(8))]
    with pytest.raises(KeyShareError, match="same length"):
        slip39._interpolate(shares, slip39.SECRET_INDEX)


def test_interpolate_at_known_x_returns_share_data() -> None:
    """Interpolating at an x that is already a share returns it verbatim."""
    data = bytes(range(16))
    shares = [slip39._RawShare(3, data), slip39._RawShare(7, bytes(16))]
    assert slip39._interpolate(shares, 3) == data


def test_short_mnemonic_raises() -> None:
    with pytest.raises(KeyShareError, match="at least"):
        slip39._Share.from_mnemonic("academic academic academic")


def test_too_long_mnemonic_padding_raises() -> None:
    """A mnemonic whose word count implies >8 padding bits is rejected."""
    valid = next(v for v in _VECTORS if v[2])[1][0]
    words = valid.split()
    # Append one extra word: 21 words -> padding_len = (10*(21-7))%16 = 12 > 8.
    bad = " ".join([*words, "academic"])
    with pytest.raises(KeyShareError, match="Invalid mnemonic length"):
        slip39._Share.from_mnemonic(bad)


def test_recover_secret_alias_matches_combine() -> None:
    mnemonics = split_secret(_MS, threshold=2, count=3, passphrase=b"z")
    assert recover_secret(mnemonics[:2], b"z") == combine_mnemonics(
        mnemonics[:2], b"z"
    )
