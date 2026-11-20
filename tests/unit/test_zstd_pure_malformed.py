"""#792: a malformed zstd frame gives ZstdError, never IndexError.

The pure-Python zstd decoder indexes into the frame's bytes in many
places.  Before #792 a damaged frame could raise ``IndexError`` there;
the tier-3 restore catches only the decoder's own ``ZstdError`` (as one
bad blob), so the IndexError stopped the whole restore.

The corpus below is fixed bytes (no zstd encoder needed): the frames a
fuzz run found that raised something other than ZstdError before the
fix, plus hand-made truncated blocks.  A small seeded mutation sweep
over two good frames checks the same contract more widely.
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore import _zstd_pure
from lcsas.restore import restic_fallback as fb
from lcsas.restore._zstd_pure import ZstdError, decompress
from lcsas.restore.standalone_builder import build_standalone

from .tier3_repo_builder import PASSWORD, Blob, build_repo

# Good frames: zstd -3 with a content checksum of
# b"hello world " * 50 and
# b"The quick brown fox jumps over the lazy dog. " * 40.
GOOD_FRAMES = {
    b"hello world " * 50: bytes.fromhex(
        "28b52ffd6458019d00006068656c6c6f20776f726c64200100495e9524de827e"
        "8e"
    ),
    b"The quick brown fox jumps over the lazy dog. " * 40: bytes.fromhex(
        "28b52ffd640806b50100d40254686520717569636b2062726f776e20666f7820"
        "6a756d7073206f76657220746865206c617a7920646f672e200100c516feaa0c"
        "d7fc9310"
    ),
}

MALFORMED = {
    # IndexError at line 913 (f0f846d): an RLE-mode length code past the table.
    "indexerror-913": bytes.fromhex(
        "28b52ffd6458019d0000916865766c6f20776f726c64200100495e9524de827e"
        "85"
    ),
    # IndexError at line 909 (f0f846d): an RLE-mode offset code past the table.
    "indexerror-909": bytes.fromhex(
        "28b52ffd6458019d00003d68656c6c14207739b16c64200100495e9524de827e"
        "8e"
    ),
    # IndexError at line 907 (f0f846d): an RLE-mode sequence table with no symbol byte.
    "indexerror-907": bytes.fromhex(
        "28b52ffd6058019d00006068656c6c6f20776f726c64200187645e9524"
    ),
    # IndexError at line 610 (f0f846d): a compressed block with no literals header.
    "indexerror-610": bytes.fromhex(
        "28b52ffd6058010400006068656c6cd720776f726c6420c36d495e9524"
    ),
    # IndexError at line 766 (f0f846d): a block with no sequences header.
    "indexerror-766": bytes.fromhex(
        "28b52ffd6058016c0000606865e26c6b20776f721f64200100495e9524"
    ),
    # IndexError at line 656 (f0f846d): Huffman literals with an empty tree description.
    "indexerror-656": bytes.fromhex(
        "28b52ffd6000135508005610000102030405060708090a0b0c0d0e0f10111213"
        "1415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233"
        "3435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f50515253"
        "5455565758595a5b5c5d5e5f606162636465666768696a6b6c6d6e6f707172a9"
        "7475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f90919293"
        "9495969798999a9b9c9d9e9fa0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3"
        "b4b5b6b7b8b9babbbcbdbebfc0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3"
        "d4d5d6d7d8d9dadbdcdddedfe0e1e2e3e4e5e6e7e8e9eaebecedeeeff0f1f2f3"
        "f4f5f6f7f8f9fafbfcfdfeff010000fd32d0af29"
    ),
    # IndexError at line 1043 (f0f846d): an RLE block with no byte to repeat.
    "indexerror-1043": bytes.fromhex(
        "28b52ffd645801a000006068656c6c6f20776f726c64200100495e9524de827e"
        "8e"
    ),
    # IndexError at line 779 (f0f846d): a sequences section with no table-modes byte.
    "indexerror-779": bytes.fromhex(
        "28b52ffd64001355080074100001020304c6060708090a0b0c0d0e0f10111213"
        "1415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233"
        "3435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f50515253"
        "5455565758595a5b5c5d5e5f606162636465666768696a6ba66d6e6f70717273"
        "7475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f90919293"
        "9495969798999a9b9c9d9e9fa0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3"
        "b4b5b6b7b8b9babbbcbdbebfc0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3"
        "d4d5d6d7d8d9dadbdcdddedfe0e1e2e3e4e5e6e7e8e9eaebecedeeeff0f1f2f3"
        "f4f5f6f7f8f9fafbfcfdfeff010000fd32d0af299fded613"
    ),
    # Hand-made: single-segment frame, one RLE block of 5 with no byte.
    "rle-block-no-byte": bytes.fromhex("28b52ffd2005" "2b0000"),
    # Hand-made: a raw block that says 10 bytes and holds 3.
    "raw-block-truncated": bytes.fromhex("28b52ffd200a" "510000") + b"abc",
}


def test_good_frames_decode() -> None:
    for plain, frame in GOOD_FRAMES.items():
        assert decompress(frame) == plain


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_frame_gives_zstd_error(name: str) -> None:
    with pytest.raises(ZstdError):
        decompress(MALFORMED[name])


def test_raw_block_truncation_is_not_silent() -> None:
    """A short raw block used to return short output with no error."""
    with pytest.raises(ZstdError, match="truncated raw block"):
        decompress(MALFORMED["raw-block-truncated"])


def test_any_decoder_error_becomes_zstd_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the decoder raises inside, the caller sees ZstdError."""
    import struct

    for exc in (IndexError("x"), struct.error("x"), ValueError("x"),
                KeyError("x"), AssertionError("x"), ZeroDivisionError()):
        def boom(_data: bytes, exc: Exception = exc) -> bytes:
            raise exc
        monkeypatch.setattr(_zstd_pure, "_decompress_frame", boom)
        with pytest.raises(ZstdError) as info:
            decompress(b"\x28\xb5\x2f\xfd\x00")
        assert info.value.__cause__ is exc


def test_seeded_mutations_give_output_or_zstd_error() -> None:
    """Every truncation and 400 seeded byte changes: a result or ZstdError."""
    rng = random.Random(792)
    cases: list[bytes] = []
    for frame in GOOD_FRAMES.values():
        cases += [frame[:n] for n in range(len(frame))]
        for _ in range(200):
            buf = bytearray(frame)
            for _ in range(rng.randint(1, 4)):
                buf[rng.randrange(len(buf))] = rng.randrange(256)
            cases.append(bytes(buf))
    for case in cases:
        try:
            out = decompress(case, max_output_size=1 << 20)
        except ZstdError:
            continue
        assert isinstance(out, bytes)


# ── The restore goes on (#792 acceptance) ─────────────────────────


def _repo_with_bad_frame(tmp_path: Path) -> Path:
    bad = MALFORMED["indexerror-913"]
    return build_repo(tmp_path, {
        "good.txt": b"this file restores\n",
        "bad.txt": [Blob(bad, blob_id="ab" * 32, uncompressed_length=600)],
        "sub": {"after.txt": b"so does this one\n"},
    })


def test_restore_marks_the_file_failed_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the pure decoder (no zstandard), a bad frame fails one file."""
    monkeypatch.setattr(
        fb, "_decompress_zstd",
        lambda data, max_output_size=0: decompress(data, max_output_size),
    )
    monkeypatch.setattr(fb, "_ZSTD_ERRORS", (ZstdError,))
    repo = _repo_with_bad_frame(tmp_path)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    target = tmp_path / "out"
    restorer.restore(target)
    assert restorer.failures == 1
    assert (target / "good.txt").read_bytes() == b"this file restores\n"
    assert (target / "sub" / "after.txt").read_bytes() == b"so does this one\n"
    assert not (target / "bad.txt").exists()
    assert "bad.txt" in (target / "RESTORE_FAILURES.txt").read_text()


def test_standalone_without_zstandard_exits_4(tmp_path: Path) -> None:
    """The generated reader, on a Python with no zstandard, exits 4
    ("some files failed", #875) with no traceback."""
    repo = _repo_with_bad_frame(tmp_path)
    script = tmp_path / "python_reader.py"
    script.write_text(build_standalone())
    pw = tmp_path / "pw"
    pw.write_bytes(PASSWORD)
    target = tmp_path / "out"
    # -I -S: no site-packages, so no zstandard; the pure decoder runs.
    res = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--repo", str(repo),
         "--password-file", str(pw), "--restore-folder", str(target),
         "--interactive", "off"],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 4, res.stderr
    assert "Traceback" not in res.stderr
    assert "slow built-in zstd decoder" in res.stderr
    assert (target / "good.txt").is_file()
    assert (target / "sub" / "after.txt").is_file()
    assert "bad.txt" in (target / "RESTORE_FAILURES.txt").read_text()
