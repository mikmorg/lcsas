"""Tests for the pure-Python zstd decompressor (``lcsas.restore._zstd_pure``).

RST-04: tier-3 recovery needs a stdlib-only zstd decoder so zstd-compressed
(rustic v2 default) repos can be restored on any architecture / CPython
minor with the native ``zstandard`` package absent.

Coverage:
  * Known-frame vector tests shared with the vendored C decoder corpus
    (``recovery/tests/test_zstd.c``) — fixed bytes, no encoder needed.
  * Round-trip property tests against the ``zstandard`` compressor across
    levels, sizes, and content shapes (skipped if zstandard is absent —
    it is only needed to PRODUCE frames, never to decode them).
  * Reject-unsupported tests (truncated / bad-magic / dictionary frames).
"""

from __future__ import annotations

import os
import struct

import pytest

from lcsas.restore._zstd_pure import ZstdError, decompress

try:
    import zstandard as _zstd  # type: ignore[import-not-found]

    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False


# ── Fixed-vector tests (no encoder required) ─────────────────────
#
# These frames are the exact corpus the vendored C decoder is tested
# against in recovery/tests/test_zstd.c — keeping both decoders validated
# against the same bytes (RST-04 "share the vector corpus").


class TestKnownFrames:
    def test_ultra_compressed_frame(self):
        # `echo -n "Hello, LCSAS zstd! "*3 (minus trailing space)` | zstd --ultra -22
        frame = bytes([
            0x28, 0xb5, 0x2f, 0xfd, 0x20, 0x38, 0xd5, 0x00, 0x00, 0xa0,
            0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x2c, 0x20, 0x4c, 0x43, 0x53,
            0x41, 0x53, 0x20, 0x7a, 0x73, 0x74, 0x64, 0x21, 0x20, 0x48,
            0x01, 0x00, 0x1a, 0x39, 0x99,
        ])
        expected = b"Hello, LCSAS zstd! Hello, LCSAS zstd! Hello, LCSAS zstd!"
        assert decompress(frame) == expected
        assert decompress(frame, max_output_size=len(expected)) == expected

    def test_streaming_no_content_size_frame(self):
        # Streaming frame (Single_Segment=0, no content-size hint).
        frame = bytes([
            0x28, 0xb5, 0x2f, 0xfd, 0x00, 0x58, 0xd1, 0x00, 0x00, 0x73,
            0x74, 0x72, 0x65, 0x61, 0x6d, 0x69, 0x6e, 0x67, 0x2d, 0x6e,
            0x6f, 0x2d, 0x63, 0x6f, 0x6e, 0x74, 0x65, 0x6e, 0x74, 0x2d,
            0x73, 0x69, 0x7a, 0x65, 0x21,
        ])
        assert decompress(frame) == b"streaming-no-content-size!"


class TestRejectUnsupported:
    def test_bad_magic_rejected(self):
        with pytest.raises(ZstdError):
            decompress(b"\x00\x00\x00\x00abcdefgh")

    def test_too_short_rejected(self):
        with pytest.raises(ZstdError):
            decompress(b"\x28\xb5")

    def test_truncated_frame_rejected(self):
        frame = bytes([0x28, 0xb5, 0x2f, 0xfd])  # magic only
        with pytest.raises(ZstdError):
            decompress(frame)

    def test_max_output_size_cap_enforced(self):
        # An RLE block expanding far beyond the cap must raise.
        if not _HAS_ZSTD:
            pytest.skip("need zstandard to build the oversized frame")
        frame = _zstd.ZstdCompressor().compress(b"z" * 50000)
        with pytest.raises(ZstdError):
            decompress(frame, max_output_size=100)

    @pytest.mark.skipif(not _HAS_ZSTD, reason="need zstandard to build a dict frame")
    def test_dictionary_frame_rejected_loud(self):
        # Dictionary-compressed frames are unsupported; they must fail loud
        # (never silently return wrong data).  A trained dictionary stamps
        # a non-zero dict id, which the decoder rejects explicitly.
        samples = [os.urandom(64) + b"common-suffix" for _ in range(64)]
        d = _zstd.train_dictionary(4096, samples)
        comp = _zstd.ZstdCompressor(dict_data=d).compress(
            b"common-suffix common-suffix common-suffix"
        )
        with pytest.raises(ZstdError):
            decompress(comp, max_output_size=1 << 20)


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstandard needed to produce frames")
class TestRoundTrip:
    """Decode frames produced by the real zstandard compressor."""

    @pytest.mark.parametrize("level", [1, 3, 6, 9, 12, 19, 22])
    def test_repeating_text(self, level):
        original = b"the quick brown fox 0123456789 ABCDEF " * 60
        comp = _zstd.ZstdCompressor(level=level).compress(original)
        assert decompress(comp, max_output_size=len(original) * 4) == original

    @pytest.mark.parametrize("size", [0, 1, 2, 33, 256, 1000, 4096, 65537])
    def test_sizes(self, size):
        original = bytes((i * 7 + i // 13) % 211 for i in range(size))
        comp = _zstd.ZstdCompressor(level=9).compress(original)
        cap = (len(original) + 1) * 8 + (1 << 16)
        assert decompress(comp, max_output_size=cap) == original

    def test_highly_compressible_zeros(self):
        original = b"\x00" * 100000
        comp = _zstd.ZstdCompressor(level=19).compress(original)
        assert decompress(comp, max_output_size=len(original) + 16) == original

    def test_incompressible_random(self):
        original = os.urandom(20000)
        comp = _zstd.ZstdCompressor(level=3).compress(original)
        assert decompress(comp, max_output_size=len(original) + 4096) == original

    def test_multi_block_high_entropy(self):
        # Large, varied content → multiple blocks, 4-stream Huffman literals,
        # FSE-compressed sequence tables.
        original = bytes((i ^ (i >> 3)) % 251 for i in range(200000))
        comp = _zstd.ZstdCompressor(level=17).compress(original)
        assert decompress(comp, max_output_size=len(original) + 4096) == original

    def test_with_content_checksum(self):
        original = b"checksum me " * 5000
        comp = _zstd.ZstdCompressor(
            level=6, write_checksum=True
        ).compress(original)
        assert decompress(comp, max_output_size=len(original) * 2) == original

    def test_corrupt_checksum_rejected(self):
        original = b"verify integrity " * 2000
        comp = bytearray(
            _zstd.ZstdCompressor(level=6, write_checksum=True).compress(original)
        )
        comp[-1] ^= 0xFF  # flip a checksum byte
        with pytest.raises(ZstdError):
            decompress(bytes(comp), max_output_size=len(original) * 2)

    @pytest.mark.parametrize("seed", range(8))
    def test_fuzz_random_shapes(self, seed):
        import random

        rng = random.Random(seed)
        for _ in range(40):
            n = rng.randint(0, 6000)
            kind = rng.choice(["rand", "rep", "zeros", "struct"])
            if kind == "rand":
                data = bytes(rng.getrandbits(8) for _ in range(n))
            elif kind == "rep":
                data = os.urandom(rng.randint(1, 30)) * ((n // 30) + 1)
                data = data[:n]
            elif kind == "zeros":
                data = b"\x00" * n
            else:
                m = rng.randint(2, 200)
                data = bytes((i * 7) % m for i in range(n))
            lvl = rng.choice([1, 3, 9, 19, 22])
            comp = _zstd.ZstdCompressor(level=lvl).compress(data)
            cap = (len(data) + 1) * 8 + (1 << 16)
            assert decompress(comp, max_output_size=cap) == data


def test_xxh64_empty_vector():
    # XXH64("", seed=0) reference value — guards the content-checksum path.
    from lcsas.restore._zstd_pure import _xxh64

    assert _xxh64(b"", 0) == 0xEF46DB3751D8E999


# ── Huffman-literal frames (block_type 2/3) ──────────────────────
#
# zstandard only emits Huffman-compressed literals when the literal
# alphabet is skewed but varied enough to pay for the tree.  Plain
# repeating English or low-entropy data yields *raw* literals inside the
# compressed block, leaving the whole Huffman decode path (weight decode,
# FSE-compressed weights, 4-stream split) unexercised.  These helpers
# craft skewed-alphabet inputs that force Huffman literals so the pure
# decoder's _build_huffman_from_weights / _fse_decode_weight_stream /
# _decode_literals_4streams paths run against REAL frames.


def _skewed_text(n: int, alphabet: bytes, seed: int) -> bytes:
    """A zipf-weighted random string over *alphabet* (no long repeats).

    A skewed-but-high-cardinality literal stream is what makes zstd choose
    Huffman literals over raw, while the lack of repeats keeps the match
    rate low so the literals actually dominate the block.
    """
    import random

    rng = random.Random(seed)
    weights = [1.0 / (i + 1) for i in range(len(alphabet))]
    chars = [alphabet[i : i + 1] for i in range(len(alphabet))]
    return b"".join(rng.choices(chars, weights=weights, k=n))


_BIG_ALPHA = b"etaoinshrdlucmfwypvbgkjqxz"


def _roundtrip(original: bytes, level: int, *, checksum: bool = False) -> None:
    comp = _zstd.ZstdCompressor(
        level=level, write_checksum=checksum
    ).compress(original)
    cap = (len(original) + 1) * 8 + (1 << 16)
    assert decompress(comp, max_output_size=cap) == original


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstandard needed to produce frames")
class TestHuffmanLiterals:
    """Force the Huffman literal-decode paths with skewed-alphabet input."""

    @pytest.mark.parametrize("level", [3, 9, 19, 22])
    def test_four_stream_fse_weights(self, level):
        # Medium skewed text → block_type 2, 4-stream Huffman, FSE-coded
        # weights (size_format 1/2).  Exercises _decode_huffman_weights
        # (FSE branch), _fse_decode_weight_stream, _build_huffman_from_weights
        # and _decode_literals_4streams.
        original = _skewed_text(20000, _BIG_ALPHA, seed=level)
        _roundtrip(original, level)

    @pytest.mark.parametrize("level", [3, 9, 19])
    def test_treeless_and_direct_weights(self, level):
        # Large skewed text → multiple blocks, a treeless (block_type 3)
        # literal block that reuses the prior block's Huffman table, plus
        # at low level a *direct* (4-bit) weight representation
        # (header_byte >= 128).
        original = _skewed_text(300000, _BIG_ALPHA, seed=100 + level)
        _roundtrip(original, level)

    def test_huffman_with_checksum(self):
        # Huffman literals + frame content checksum together.
        original = _skewed_text(40000, _BIG_ALPHA, seed=7)
        _roundtrip(original, 19, checksum=True)

    def test_many_sequences(self):
        # Many short matches → a large num_seq (multi-byte count encoding)
        # and FSE-compressed sequence tables across blocks.
        import random

        rng = random.Random(5)
        words = [b"apple ", b"banana ", b"cherry ", b"date ", b"fig "]
        original = b"".join(rng.choice(words) for _ in range(20000))
        _roundtrip(original, 19)

    @pytest.mark.parametrize("seed", range(6))
    def test_fuzz_huffman_shapes(self, seed):
        # Vary size and level so size_format 1/2/3 and treeless reuse all
        # occur; assert byte-identical decode every time.  Sizes are kept
        # >= 1 KB so zstd uses 4-stream Huffman literals (the single-stream
        # layout has a known pure-decoder defect — see PR notes / #324).
        import random

        rng = random.Random(1000 + seed)
        for _ in range(20):
            n = rng.randint(1024, 50000)
            original = _skewed_text(n, _BIG_ALPHA, seed=rng.randint(0, 1 << 30))
            _roundtrip(original, rng.choice([3, 9, 19]))


# ── Hand-crafted minimal frames (paths zstandard won't emit) ─────
#
# The reference compressor never chooses an RLE *block* or RLE *literals*
# for normal input, so those decoder branches stay dark under round-trip
# tests.  We hand-build the smallest valid frames that use them and
# cross-check the expected plaintext against the native decoder when it
# is available.


def _single_segment_frame(content_size: int, block_payload: bytes) -> bytes:
    """Wrap *block_payload* (a full block incl. 3-byte header) in a frame.

    Single_Segment=1, no checksum, no dict, 1-byte Frame_Content_Size.
    """
    assert content_size <= 255
    fhd = 1 << 5  # single_segment, fcs_flag=0 → 1-byte content size
    return (
        bytes([0x28, 0xB5, 0x2F, 0xFD, fhd, content_size]) + block_payload
    )


def _block_header(last: int, block_type: int, size: int) -> bytes:
    bh = last | (block_type << 1) | (size << 3)
    return struct.pack("<I", bh)[:3]


class TestHandCraftedBlocks:
    def test_rle_block(self):
        # Whole-block RLE: one byte repeated block_size times (block_type 1).
        payload = _block_header(1, 1, 200) + b"q"
        frame = _single_segment_frame(200, payload)
        assert decompress(frame) == b"q" * 200
        if _HAS_ZSTD:
            assert _zstd.ZstdDecompressor().decompress(frame) == b"q" * 200

    def test_raw_block(self):
        # Raw block (block_type 0): bytes copied verbatim.
        body = bytes(range(50))
        payload = _block_header(1, 0, len(body)) + body
        frame = _single_segment_frame(len(body), payload)
        assert decompress(frame) == body
        if _HAS_ZSTD:
            assert _zstd.ZstdDecompressor().decompress(frame) == body

    def test_reserved_block_type_rejected(self):
        payload = _block_header(1, 3, 0)
        frame = _single_segment_frame(0, payload)
        with pytest.raises(ZstdError, match="reserved block type"):
            decompress(frame)

    def test_truncated_block_header_rejected(self):
        # Frame header complete, but the block header is cut short.
        frame = bytes([0x28, 0xB5, 0x2F, 0xFD, 1 << 5, 0x10]) + b"\x00"
        with pytest.raises(ZstdError, match="truncated block header"):
            decompress(frame)

    def test_truncated_compressed_block_rejected(self):
        # block_type 2 claims more bytes than present.
        payload = _block_header(1, 2, 100) + b"\x00\x00"
        frame = _single_segment_frame(10, payload)
        with pytest.raises(ZstdError, match="truncated compressed block"):
            decompress(frame)

    def test_missing_content_checksum_rejected(self):
        # content_checksum flag set but the 4 trailing bytes are absent.
        fhd = (1 << 5) | (1 << 2)  # single_segment + content_checksum
        payload = _block_header(1, 1, 3) + b"z"
        frame = bytes([0x28, 0xB5, 0x2F, 0xFD, fhd, 3]) + payload
        with pytest.raises(ZstdError, match="content checksum"):
            decompress(frame)

    def test_raw_literals_three_byte_header(self):
        # Compressed block whose Literals_Section is RAW with size_format 3
        # (3-byte header), followed by an empty Sequences_Section.  zstd
        # never picks this for typical input, so it is built by hand.
        regen = 20
        lits = bytes(range(regen))
        b0 = 0 | (3 << 2) | ((regen & 0xF) << 4)
        litsec = bytes([b0, (regen >> 4) & 0xFF, (regen >> 12) & 0xFF]) + lits
        block = litsec + b"\x00"  # num_seq = 0
        frame = _single_segment_frame(regen, _block_header(1, 2, len(block)) + block)
        assert decompress(frame) == lits

    def test_rle_literals(self):
        # Compressed block with RLE Literals_Section (block_type 1): one
        # literal byte repeated regen times, no sequences.
        regen = 30
        b0 = 1 | (0 << 2) | (regen << 3)  # RLE, size_format 0
        block = bytes([b0, ord("Z")]) + b"\x00"
        frame = _single_segment_frame(regen, _block_header(1, 2, len(block)) + block)
        assert decompress(frame) == b"Z" * regen

    def test_truncated_raw_literals_rejected(self):
        # RAW literals claiming more bytes than the block carries.
        regen = 50
        b0 = 0 | (1 << 2) | ((regen & 0xF) << 4)  # size_format 1
        litsec = bytes([b0, regen >> 4]) + bytes(range(10))  # only 10 present
        block = litsec + b"\x00"
        frame = _single_segment_frame(10, _block_header(1, 2, len(block)) + block)
        with pytest.raises(ZstdError, match="truncated raw literals"):
            decompress(frame)

    def test_repeat_seq_table_without_prior_rejected(self):
        # A Sequences_Section that selects Repeat_Mode for a table when no
        # prior table exists must fail loud (not silently mis-decode).
        from lcsas.restore._zstd_pure import _decode_sequences, _SeqDTable

        # num_seq=1; compression_modes = all Repeat_Mode (0b11 per table).
        src = bytes([1, 0b11_11_11_00])
        prev: dict[str, _SeqDTable | None] = {"ll": None, "of": None, "ml": None}
        with pytest.raises(ZstdError, match="repeat mode"):
            _decode_sequences(src, prev, [1, 4, 8])


# ── Bit readers and helper-level units ───────────────────────────


class TestBitReaders:
    def test_forward_peek_zero(self):
        from lcsas.restore._zstd_pure import _ForwardBitReader

        assert _ForwardBitReader(b"\x01").peek(0) == 0

    def test_backward_empty_stream(self):
        from lcsas.restore._zstd_pure import _BackwardBitReader

        with pytest.raises(ZstdError, match="empty bitstream"):
            _BackwardBitReader(b"")

    def test_backward_zero_final_byte(self):
        from lcsas.restore._zstd_pure import _BackwardBitReader

        # A trailing zero byte means there is no sentinel "1" bit.
        with pytest.raises(ZstdError, match="no sentinel"):
            _BackwardBitReader(b"\x01\x00")

    def test_backward_read_past_start_returns_zero(self):
        from lcsas.restore._zstd_pure import _BackwardBitReader

        # Sentinel at bit 1 → one readable bit, then exhausted.
        br = _BackwardBitReader(bytes([0b0000_0010]))
        assert not br.finished()
        br.read(1)
        assert br.finished()
        # Reading past the start yields zero (zstd zero-extends).
        assert br.read(2) == 0

    def test_backward_partial_read_zero_extends(self):
        from lcsas.restore._zstd_pure import _BackwardBitReader

        # Sentinel at bit 5 → 5 readable bits.  Consume 3, then request 5:
        # only 2 bits remain so the low 3 are zero-extended.
        br = _BackwardBitReader(bytes([0b0010_1010]))
        assert br.read(3) == 0b010
        assert br.read(5) == 0b10_000


class TestHelperUnits:
    def test_read32_little_endian(self):
        from lcsas.restore._zstd_pure import _read32

        assert _read32(b"\x01\x02\x03\x04", 0) == 0x04030201

    def test_xxh64_tail_paths(self):
        # Lengths that drive the 8-/4-/1-byte tail loops and the >=32-byte
        # bulk loop of XXH64.  Reference digests from the canonical xxHash
        # implementation (seed 0); they pin _read32 / _read64 / the merge
        # rounds independently of the frame-checksum round-trip tests.
        from lcsas.restore._zstd_pure import _xxh64

        assert _xxh64(b"a", 0) == 0xD24EC4F1A98C6E5B
        assert _xxh64(b"abcd", 0) == 0xDE0327B0D25D92CC
        assert _xxh64(b"abcdefgh", 0) == 0x3AD351775B4634B7
        assert (
            _xxh64(b"0123456789abcdefghijklmnopqrstuvwxyz", 0)
            == 0x69196C1B3AF0BFF9
        )

    def test_build_huffman_empty_weights_rejected(self):
        from lcsas.restore._zstd_pure import _build_huffman_from_weights

        with pytest.raises(ZstdError, match="empty Huffman weight table"):
            _build_huffman_from_weights([0, 0])

    def test_build_huffman_invalid_distribution_rejected(self):
        from lcsas.restore._zstd_pure import _build_huffman_from_weights

        # A single weight-1 symbol can never complete a power-of-two table.
        with pytest.raises(ZstdError, match="invalid Huffman weight"):
            _build_huffman_from_weights([1])

    def test_fse_accuracy_log_too_high_rejected(self):
        from lcsas.restore._zstd_pure import (
            _ForwardBitReader,
            _read_fse_distribution,
        )

        # First nibble 0xF → accuracy_log 5+15 = 20, over the cap of 9.
        reader = _ForwardBitReader(b"\x0f" + b"\xff" * 7)
        with pytest.raises(ZstdError, match="accuracy_log"):
            _read_fse_distribution(reader, max_symbol=35, max_accuracy=9)

    def test_fse_distribution_sum_mismatch_rejected(self):
        from lcsas.restore._zstd_pure import (
            _ForwardBitReader,
            _read_fse_distribution,
        )

        # Counts that exhaust max_symbol before the distribution sums to the
        # table size → the running total never reaches 1.
        reader = _ForwardBitReader(bytes([0x00, 0x06, 0x00, 0x00]))
        with pytest.raises(ZstdError, match="did not sum"):
            _read_fse_distribution(reader, max_symbol=0, max_accuracy=9)


class TestLiteralDefensiveBranches:
    """Defensive raises inside _decode_literals that the encoder never hits."""

    def test_treeless_without_prior_table_rejected(self):
        from lcsas.restore._zstd_pure import _decode_literals

        # block_type 3 (treeless) but no previous Huffman table to reuse.
        b0 = 3 | (0 << 2) | (5 << 4)  # size_format 0, single stream
        src = bytes([b0, 0x00, 0x00])
        with pytest.raises(ZstdError, match="treeless literals with no prior"):
            _decode_literals(src, None)

    def test_truncated_compressed_literals_rejected(self):
        from lcsas.restore._zstd_pure import _decode_literals

        # comp claims 3 payload bytes but only 1 is present.
        b0 = 2 | (0 << 2) | (1 << 4)
        src = bytes([b0, 0b1100_0000, 0x00, 0x00])  # comp = 3, payload = 1
        with pytest.raises(ZstdError, match="truncated compressed literals"):
            _decode_literals(src, None)

    def test_truncated_four_stream_jump_table_rejected(self):
        from lcsas.restore._zstd_pure import (
            _build_huffman_from_weights,
            _decode_literals,
        )

        prev = _build_huffman_from_weights([2, 1])
        # Treeless 4-stream (size_format 1) with a < 6-byte payload.
        b0 = 3 | (1 << 2) | (1 << 4)
        src = bytes([b0, 0b0100_0000, 0x00, 0x00])  # comp = 1
        with pytest.raises(ZstdError, match="jump table"):
            _decode_literals(src, prev)

    def test_invalid_four_stream_sizes_rejected(self):
        from lcsas.restore._zstd_pure import (
            _build_huffman_from_weights,
            _decode_literals,
        )

        prev = _build_huffman_from_weights([2, 1])
        # Jump-table stream sizes (s1+s2+s3) exceed the available bytes.
        b0 = 3 | (1 << 2) | (1 << 4)
        payload = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0])
        src = bytes([b0, 0x00, 0x02]) + payload  # comp = 8
        with pytest.raises(ZstdError, match="invalid 4-stream"):
            _decode_literals(src, prev)


class TestSequenceDefensiveBranches:
    def test_match_offset_before_output_start_rejected(self):
        from lcsas.restore._zstd_pure import _execute_sequences

        out = bytearray(b"abc")
        # offset 100 with only 3 bytes of output so far → corrupt frame.
        with pytest.raises(ZstdError, match="before start of output"):
            _execute_sequences(b"", [(0, 100, 3)], out)

    def test_repeat_offset_resolving_to_zero_rejected(self):
        from lcsas.restore._zstd_pure import _resolve_rep_code

        # code 3 with prevOffset[0] == 1 → temp = 0, which zstd rejects.
        with pytest.raises(ZstdError, match="invalid repeat offset"):
            _resolve_rep_code([1, 4, 8], 3)

    def test_rle_offset_sequence_table(self):
        from lcsas.restore._zstd_pure import _resolve_seq_table, _SeqDTable

        # RLE_Mode (mode 1) for the offset table: a single offset *code*
        # byte yields a one-entry table carrying that code's base+extra-bits.
        prev: dict[str, _SeqDTable | None] = {"ll": None, "of": None, "ml": None}
        table, used = _resolve_seq_table(bytes([5]), 0, 1, prev, "of")
        assert used == 1
        assert table.is_offset
        assert table.base_value == [0x1D]  # _OF_CODE_BASE[5]
        assert table.add_bits == [5]
