"""Tier-3 fast AES-256-CTR: backend choice, self-tests and fallback.

``lcsas.restore._aes_fast`` picks a native AES-256-CTR (libcrypto via
ctypes, or the ``openssl`` command) when one is safe and passes its
known-answer test, else the pure-Python code.  These tests run the real
backends where this host has them (skipping cleanly otherwise) and
drive the selection order and the fallbacks with fakes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore import _aes_fast as fast
from lcsas.restore._aes_pure import aes_ctr as pure_ctr

KEY = bytes(range(32))
IV = bytes(range(100, 116))


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LCSAS_TIER3_AES", raising=False)


def _forced(monkeypatch: pytest.MonkeyPatch, name: str) -> fast.AesCtrBackend:
    monkeypatch.setenv("LCSAS_TIER3_AES", name)
    backend, _notes = fast.select_aes_ctr_backend(allow_cli=False)
    return backend


class _WrongBackend(fast.AesCtrBackend):
    name = "fake"

    def describe(self) -> str:
        return "a fake backend"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        return bytes(len(data))


class _GoodBackend(fast.AesCtrBackend):
    name = "fake"
    min_bytes = 64

    def __init__(self) -> None:
        self.calls = 0

    def describe(self) -> str:
        return "a fake backend"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        self.calls += 1
        return pure_ctr(key, iv, data)


# ── Known-answer tests ───────────────────────────────────────────


def test_nist_vector_matches_the_pure_code() -> None:
    """The F.5.5 vector in the module is right (pure code agrees)."""
    assert pure_ctr(fast._AESF_KAT_KEY, fast._AESF_KAT_IV,
                    fast._AESF_KAT_PT) == fast._AESF_KAT_CT


def test_pure_backend_passes_its_self_test() -> None:
    assert fast._aesf_self_test(fast.AesCtrBackend()) is None


def test_ctypes_backend_on_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _forced(monkeypatch, "ctypes")
    if backend.name != "ctypes":
        pytest.skip("no usable libcrypto for ctypes on this host")
    assert backend.ctr(fast._AESF_KAT_KEY, fast._AESF_KAT_IV,
                       fast._AESF_KAT_PT) == fast._AESF_KAT_CT
    data = os.urandom(70_001)
    assert backend.ctr(KEY, b"\xff" * 16, data) == pure_ctr(KEY, b"\xff" * 16, data)
    assert backend.ctr(KEY, IV, b"") == b""


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl command")
def test_openssl_backend_on_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _forced(monkeypatch, "openssl")
    assert backend.name == "openssl", "openssl is on PATH but was not chosen"
    assert backend.ctr(fast._AESF_KAT_KEY, fast._AESF_KAT_IV,
                       fast._AESF_KAT_PT) == fast._AESF_KAT_CT
    data = os.urandom(70_001)
    assert backend.ctr(KEY, IV, data) == pure_ctr(KEY, IV, data)
    assert backend.ctr(KEY, IV, b"") == b""


def test_self_test_rejects_wrong_output() -> None:
    why = fast._aesf_self_test(_WrongBackend())
    assert why is not None and "known-answer" in why


def test_self_test_rejects_a_backend_that_raises() -> None:
    class Raising(fast.AesCtrBackend):
        def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
            raise OSError("boom")

    why = fast._aesf_self_test(Raising())
    assert why is not None and "OSError" in why


def test_self_test_catches_a_counter_carry_bug() -> None:
    """A backend right on the NIST vector but wrong across a 64-bit
    counter carry is still refused (the random cross-check)."""

    class NoCarry(fast.AesCtrBackend):
        def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
            if iv == fast._AESF_KAT_IV:
                return pure_ctr(key, iv, data)
            # Increment only the low 64 bits (a classic CTR bug).
            out = bytearray()
            hi, lo = iv[:8], int.from_bytes(iv[8:], "big")
            for i in range(0, len(data), 16):
                block_iv = hi + ((lo + i // 16) % (1 << 64)).to_bytes(8, "big")
                out += pure_ctr(key, block_iv, data[i:i + 16])
            return bytes(out)

    why = fast._aesf_self_test(NoCarry())
    assert why is not None and "differs" in why


# ── Selection order ──────────────────────────────────────────────


def test_child_probe_rejects_a_missing_library() -> None:
    why = fast._aesf_child_probe("/nonexistent/libcrypto-for-lcsas-test.so")
    assert why is not None and "probe failed" in why


def test_child_probe_that_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "/nonexistent/python3")
    why = fast._aesf_child_probe("libcrypto.so.3")
    assert why is not None and "could not run" in why


def test_failed_probe_means_no_ctypes_then_openssl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: (["libfake.so"], "unused"))
    monkeypatch.setattr(fast, "_aesf_child_probe",
                        lambda path: "the probe failed (exit 134)")
    monkeypatch.setattr(fast, "_aesf_try_openssl",
                        lambda notes: _GoodBackend())
    backend, notes = fast.select_aes_ctr_backend(allow_cli=True)
    assert backend.name == "fake"
    assert any("libfake.so" in n and "exit 134" in n for n in notes)


def test_no_library_and_no_openssl_gives_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: ([], "no libcrypto found"))
    monkeypatch.setattr(fast.shutil, "which", lambda name: None)
    backend, notes = fast.select_aes_ctr_backend(allow_cli=True)
    assert backend.name == "pure"
    assert "ctypes: no libcrypto found" in notes
    assert "openssl command: not found on PATH" in notes


def test_openssl_failing_its_self_test_gives_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: ([], "no libcrypto found"))
    monkeypatch.setattr(fast.shutil, "which", lambda name: "/fake/openssl")
    monkeypatch.setattr(fast, "_aesf_openssl_version", lambda exe: "")
    monkeypatch.setattr(fast._AesfOpensslCliBackend, "ctr",
                        lambda self, k, i, d: bytes(len(d)))
    backend, notes = fast.select_aes_ctr_backend(allow_cli=True)
    assert backend.name == "pure"
    assert any("/fake/openssl" in n and "known-answer" in n for n in notes)


def test_library_failing_its_self_test_gives_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: (["libfake.so"], "unused"))
    monkeypatch.setattr(fast, "_aesf_child_probe", lambda path: None)
    monkeypatch.setattr(fast, "_AesfCtypesBackend",
                        lambda path: _WrongBackend())
    backend, notes = fast.select_aes_ctr_backend(allow_cli=False)
    assert backend.name == "pure"
    assert any("libfake.so" in n and "known-answer" in n for n in notes)


def test_openssl_command_not_tried_unless_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: ([], "no libcrypto found"))
    tried: list[bool] = []
    monkeypatch.setattr(fast, "_aesf_try_openssl",
                        lambda notes: tried.append(True) or _GoodBackend())
    backend, notes = fast.select_aes_ctr_backend(allow_cli=False)
    assert backend.name == "pure" and not tried
    assert "openssl command: not used by this caller" in notes
    # LCSAS_TIER3_AES=openssl is the user's explicit opt-in.
    monkeypatch.setenv("LCSAS_TIER3_AES", "openssl")
    backend, _ = fast.select_aes_ctr_backend(allow_cli=False)
    assert backend.name == "fake" and tried


def test_env_pure_skips_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> tuple[list[str], str]:
        raise AssertionError("must not look for a library")

    monkeypatch.setattr(fast, "_aesf_library_candidates", boom)
    assert _forced(monkeypatch, "pure").name == "pure"


def test_env_garbage_means_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fast, "_aesf_library_candidates",
                        lambda: ([], "no libcrypto found"))
    monkeypatch.setenv("LCSAS_TIER3_AES", "fastest")
    backend, notes = fast.select_aes_ctr_backend(allow_cli=False)
    assert backend.name == "pure"
    assert any("'fastest'" in n for n in notes)


# ── Where ctypes may load a library ──────────────────────────────


def test_never_ctypes_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    paths, why = fast._aesf_library_candidates()
    assert paths == [] and "macOS" in why


def test_never_ctypes_into_a_non_glibc_python_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundled musl CPython must not load the host's libcrypto."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fast, "_aesf_running_glibc", lambda: False)
    paths, why = fast._aesf_library_candidates()
    assert paths == [] and "glibc" in why


def test_windows_prefers_the_dll_next_to_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import ctypes.util

    dll = tmp_path / "DLLs" / "libcrypto-3-x64.dll"
    dll.parent.mkdir()
    dll.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(ctypes.util, "find_library",
                        lambda name: "C:\\host\\libcrypto-3.dll")
    paths, _why = fast._aesf_library_candidates()
    assert paths[0] == str(dll)
    assert "C:\\host\\libcrypto-3.dll" in paths


def test_running_glibc_matches_this_python() -> None:
    import platform

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    assert fast._aesf_running_glibc() == (platform.libc_ver()[0] == "glibc")


# ── Use-time behaviour ───────────────────────────────────────────


def test_use_time_failure_falls_back_and_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Flaky(_GoodBackend):
        def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
            self.calls += 1
            raise RuntimeError("openssl exited 1: bad things")

    flaky = Flaky()
    monkeypatch.setattr(fast, "select_aes_ctr_backend",
                        lambda allow_cli: (flaky, []))
    lines: list[str] = []
    aes = fast.FastAesCtr(log=lines.append)
    data = os.urandom(500)
    assert aes(KEY, IV, data) == pure_ctr(KEY, IV, data)
    assert aes(KEY, IV, data) == pure_ctr(KEY, IV, data)
    assert flaky.calls == 1, "a failed backend must be switched off"
    assert aes.backend.name == "pure"
    assert len(lines) == 2  # the choice, then the one failure line
    assert "failed (RuntimeError: openssl exited 1" in lines[1]
    assert "pure-Python AES from now on" in lines[1]


def test_small_inputs_go_to_the_pure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _GoodBackend()
    monkeypatch.setattr(fast, "select_aes_ctr_backend",
                        lambda allow_cli: (good, []))
    aes = fast.FastAesCtr()
    small, big = os.urandom(63), os.urandom(64)
    assert aes(KEY, IV, small) == pure_ctr(KEY, IV, small)
    assert good.calls == 0
    assert aes(KEY, IV, big) == pure_ctr(KEY, IV, big)
    assert good.calls == 1


def test_choice_is_made_once_and_logged_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picks: list[bool] = []

    def pick(allow_cli: bool) -> tuple[fast.AesCtrBackend, list[str]]:
        picks.append(allow_cli)
        return _GoodBackend(), []

    monkeypatch.setattr(fast, "select_aes_ctr_backend", pick)
    lines: list[str] = []
    aes = fast.FastAesCtr(allow_cli=True, log=lines.append)
    assert picks == [], "nothing is chosen before the first use"
    for _ in range(3):
        aes(KEY, IV, os.urandom(100))
    assert picks == [True]
    assert lines == ["AES-256-CTR: using a fake backend"]


def test_pure_choice_log_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fast, "select_aes_ctr_backend",
        lambda allow_cli: (fast.AesCtrBackend(),
                           ["openssl command: not found on PATH"]),
    )
    lines: list[str] = []
    fast.FastAesCtr(log=lines.append)(KEY, IV, b"x")
    assert lines and "pure-Python AES" in lines[0]
    assert "openssl command: not found on PATH" in lines[0]


def test_openssl_timeout_message_hides_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_openssl_version", lambda exe: "")
    backend = fast._AesfOpensslCliBackend("/fake/openssl")

    def slow(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd, 61)

    monkeypatch.setattr(fast.subprocess, "run", slow)
    with pytest.raises(RuntimeError) as info:
        backend.ctr(KEY, IV, b"y" * 2000)
    assert KEY.hex() not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_openssl_short_output_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fast, "_aesf_openssl_version", lambda exe: "")
    backend = fast._AesfOpensslCliBackend("/fake/openssl")
    monkeypatch.setattr(
        fast.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"abc", b""),
    )
    with pytest.raises(RuntimeError, match="returned 3 bytes"):
        backend.ctr(KEY, IV, b"y" * 2000)
