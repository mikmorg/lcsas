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
