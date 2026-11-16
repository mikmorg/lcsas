"""#795: native AES agrees with the pure code on large inputs.

The start-up self-test compares short inputs only.  These tests compare
each native backend this host has with the pure-Python AES on inputs
that cross 64 KiB (the openssl command's buffer) and end mid-block, and
drive the ctypes chunk loop with a small chunk size, so that a chunk
starts in the middle of an AES block, under a counter that carries
across the 32- and 64-bit boundaries.  Backends this host lacks skip.
"""

from __future__ import annotations

import random
import shutil
from typing import Any

import pytest

from lcsas.restore import _aes_fast as fast
from lcsas.restore._aes_pure import aes_ctr as pure_ctr

_RNG = random.Random(795)
KEY = _RNG.randbytes(32)
# The low 64 bits start 3 blocks below a carry out of bit 32 and bit 64.
IV = _RNG.randbytes(8) + b"\xff\xff\xff\xff\xff\xff\xff\xfd"
DATA = _RNG.randbytes(2 * 65536 + 37)  # crosses 64 KiB twice, ends mid-block
EXPECT = pure_ctr(KEY, IV, DATA)


@pytest.fixture(scope="module")
def ctypes_lib() -> tuple[dict[str, Any], Any]:
    paths, _why = fast._aesf_library_candidates()
    for path in paths:
        if fast._aesf_child_probe(path) is None:
            ns = fast._aesf_ctypes_namespace()
            return ns, ns["_aesf_bind"](path)
    pytest.skip("no usable libcrypto for ctypes on this host")


def test_pure_reference_is_self_consistent() -> None:
    """Decrypting in two pieces at a block boundary gives the same bytes
    (the reference the native output is compared with)."""
    cut = 65536
    second_iv = (int.from_bytes(IV, "big") + cut // 16) % (1 << 128)
    tail = pure_ctr(KEY, second_iv.to_bytes(16, "big"), DATA[cut:])
    assert pure_ctr(KEY, IV, DATA[:cut]) + tail == EXPECT


def test_ctypes_whole_input(ctypes_lib: tuple[dict[str, Any], Any]) -> None:
    ns, lib = ctypes_lib
    assert ns["_aesf_lib_ctr"](lib, KEY, IV, DATA) == EXPECT


@pytest.mark.parametrize("step", [1000, 16 * 4096 + 7, 65536, len(DATA) - 1])
def test_ctypes_chunk_loop(
    ctypes_lib: tuple[dict[str, Any], Any], step: int,
) -> None:
    """Chunks of *step* bytes: every chunk after the first that is not a
    multiple of 16 starts in the middle of a block."""
    ns, lib = ctypes_lib
    assert ns["_aesf_lib_ctr"](lib, KEY, IV, DATA, step) == EXPECT


def test_ctypes_chunk_loop_on_a_memoryview(
    ctypes_lib: tuple[dict[str, Any], Any],
) -> None:
    """A non-bytes input is copied piece by piece, not lost."""
    ns, lib = ctypes_lib
    assert ns["_aesf_lib_ctr"](lib, KEY, IV, memoryview(DATA), 4099) == EXPECT


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl command")
def test_openssl_command_large_input() -> None:
    exe = shutil.which("openssl")
    assert exe is not None
    backend = fast._AesfOpensslCliBackend(exe)
    if fast._aesf_self_test(backend) is not None:
        pytest.skip("this openssl fails the self-test")
    assert backend.ctr(KEY, IV, DATA) == EXPECT
