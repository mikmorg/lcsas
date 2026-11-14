"""Fast AES-256-CTR for tier 3, with the pure-Python code as the floor.

The pure-Python AES in ``_aes_pure`` decrypts about 0.15 MB/s, roughly
2 hours per GB.  This module finds a native AES-256-CTR at run time and
uses it when it is safe and proven correct, else the pure code:

1. ``ctypes`` + an OpenSSL/LibreSSL ``libcrypto`` (EVP AES-256-CTR), only
   where loading it into this process is safe:

   * Windows: the ``libcrypto`` DLL that ships next to the running
     Python (python-build-standalone puts it in ``DLLs/``), then the
     standard DLL names on the search path.
   * Linux: only when this Python itself runs on glibc, and only the
     library ``ctypes.util.find_library("crypto")`` names.  The bundled
     Linux CPython is a musl build and may run through the bundled musl
     loader on a glibc host (#714); a host glibc ``libcrypto`` must never
     be loaded into that process.
   * BSDs: ``find_library("crypto")`` (one C library per system).
   * macOS: never.  Loading the system ``/usr/lib/libcrypto.dylib``
     aborts the process ("loading libcrypto in an unsafe way"), and the
     bundled CPython keeps its OpenSSL symbols private.  macOS uses the
     ``openssl`` command instead.

   A library is loaded here only after a CHILD process (the same
   interpreter) has loaded it and passed the known-answer test, so a
   crash or abort while loading costs the child, not the restore.

2. The ``openssl`` command (OpenSSL or LibreSSL), one process per
   decryption, the ciphertext streamed through its stdin.  Only when the
   caller allows it (the tier-3 restore command does; library use inside
   ``lcsas`` does not), because ``openssl enc`` takes the raw key ONLY
   as a command-line argument (``-K <hex>``): ``-pass`` and ``-kfile``
   feed a password through a key-derivation function and cannot carry a
   raw key, and OpenSSL 3 has no interactive mode that would read the
   command from stdin.  While the process runs, its command line is
   readable by other users of the same computer (on Linux,
   ``/proc/<pid>/cmdline`` is world-readable unless /proc is mounted
   with ``hidepid``).  Starting a process costs about 7 ms, so inputs
   under ``_AESF_CLI_MIN_BYTES`` go to the pure code, which is faster
   there; that also keeps the small key-file decryption out of argv.

3. Pure Python (``_aes_pure.aes_ctr``), always available.

Every native backend must pass a known-answer test (NIST SP 800-38A
F.5.5, CTR-AES256) and a random cross-check against the pure code
(counters that carry across the 32-, 64- and 128-bit boundaries) before
it is used.  An error at use time falls back to the pure code for that
call and switches the backend off for the rest of the run, with one log
line.  The choice is made once, on the first decryption, never at
import.

The environment variable ``LCSAS_TIER3_AES`` overrides the choice:
``auto`` (default), ``ctypes``, ``openssl`` (allowed even where the
caller did not allow the command) or ``pure``.

Standard library only.  This module is concatenated into the generated
``standalone_restorer.py``; every module-level name it defines carries
an ``_aesf_`` prefix (or is one of the two public classes) so it cannot
collide with names in the other concatenated modules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from lcsas.restore._aes_pure import aes_ctr as _aesf_pure_ctr

# ── Known-answer test: NIST SP 800-38A, F.5.5 CTR-AES256.Encrypt ──

_AESF_KAT_KEY = bytes.fromhex(
    "603deb1015ca71be2b73aef0857d7781"
    "1f352c073b6108d72d9810a30914dff4"
)
_AESF_KAT_IV = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
_AESF_KAT_PT = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
_AESF_KAT_CT = bytes.fromhex(
    "601ec313775789a5b7a7f504bbf3d228"
    "f443e3ca4d62b59aca84e990cacaf5c5"
    "2b0930daa23de94ce87017ba2d84988d"
    "dfc9c58db67aada613c2dd08457941a6"
)

#: Below this many bytes the ``openssl`` command is slower than the pure
#: code (process start ~7 ms; pure AES ~0.15 MB/s ≈ 6.5 ms per KiB).
_AESF_CLI_MIN_BYTES = 1024

#: Seconds the child library probe and the ``openssl version`` query may take.
_AESF_PROBE_TIMEOUT = 60

#: The ctypes binding.  It is source text so that the child probe and
#: this process run exactly the same code: the parent ``exec``s it, the
#: child runs it with ``_aesf_probe_main()`` appended.
_AESF_CTYPES_SRC = r'''
import ctypes
import sys


def _aesf_bind(path):
    """Load *path* and declare the EVP functions AES-256-CTR needs."""
    lib = ctypes.CDLL(path)
    vp = ctypes.c_void_p
    ci = ctypes.c_int
    pci = ctypes.POINTER(ctypes.c_int)
    lib.EVP_CIPHER_CTX_new.restype = vp
    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_CIPHER_CTX_free.argtypes = [vp]
    lib.EVP_aes_256_ctr.restype = vp
    lib.EVP_aes_256_ctr.argtypes = []
    lib.EVP_EncryptInit_ex.restype = ci
    lib.EVP_EncryptInit_ex.argtypes = [vp, vp, vp, ctypes.c_char_p, ctypes.c_char_p]
    lib.EVP_EncryptUpdate.restype = ci
    lib.EVP_EncryptUpdate.argtypes = [vp, vp, pci, ctypes.c_char_p, ci]
    lib.EVP_EncryptFinal_ex.restype = ci
    lib.EVP_EncryptFinal_ex.argtypes = [vp, vp, pci]
    return lib


def _aesf_lib_ctr(lib, key, iv, data):
    """AES-256-CTR of *data* (CTR: encrypt == decrypt) through EVP."""
    if len(key) != 32 or len(iv) != 16:
        raise ValueError("AES-256-CTR needs a 32-byte key and a 16-byte IV")
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_256_ctr(), None, key, iv) != 1:
            raise RuntimeError("EVP_EncryptInit_ex failed")
        n = len(data)
        out = ctypes.create_string_buffer(n + 32)
        outl = ctypes.c_int(0)
        done = 0
        step = 1 << 30
        for start in range(0, n, step):
            piece = data if n <= step else data[start:start + step]
            if not isinstance(piece, bytes):
                piece = bytes(piece)
            if lib.EVP_EncryptUpdate(
                ctx, ctypes.byref(out, done), ctypes.byref(outl),
                piece, len(piece),
            ) != 1:
                raise RuntimeError("EVP_EncryptUpdate failed")
            done += outl.value
        if lib.EVP_EncryptFinal_ex(
            ctx, ctypes.byref(out, done), ctypes.byref(outl),
        ) != 1:
            raise RuntimeError("EVP_EncryptFinal_ex failed")
        done += outl.value
        if done != n:
            raise RuntimeError(f"EVP returned {done} bytes for {n}")
        return ctypes.string_at(out, n)
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aesf_probe_main():
    """Child probe: load argv[1], run the known-answer test, print ok."""
    lib = _aesf_bind(sys.argv[1])
    key, iv, pt, ct = (bytes.fromhex(a) for a in sys.argv[2:6])
    got = _aesf_lib_ctr(lib, key, iv, pt)
    sys.stdout.write("ok" if got == ct else "mismatch:" + got.hex())
'''


def _aesf_ctypes_namespace() -> dict[str, Any]:
    """Run the binding source in this process; return its namespace."""
    ns: dict[str, Any] = {"__name__": "_aesf_ctypes"}
    exec(compile(_AESF_CTYPES_SRC, "<lcsas-aes-ctypes>", "exec"), ns)  # noqa: S102
    return ns


# ── Backends ─────────────────────────────────────────────────────


class AesCtrBackend:
    """An AES-256-CTR implementation.  This base class is the pure code."""

    #: Short name for logs and ``LCSAS_TIER3_AES``.
    name = "pure"
    #: Inputs shorter than this go to the pure code instead.
    min_bytes = 0

    def describe(self) -> str:
        """One phrase for the startup log line."""
        return "the slow built-in pure-Python AES (about 0.15 MB/s)"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        """Return AES-256-CTR(*key*, *iv*) applied to *data*."""
        return _aesf_pure_ctr(key, iv, data)


class _AesfCtypesBackend(AesCtrBackend):
    name = "ctypes"

    def __init__(self, path: str) -> None:
        ns = _aesf_ctypes_namespace()
        self._path = path
        self._lib = ns["_aesf_bind"](path)
        self._run: Callable[[Any, bytes, bytes, bytes], bytes] = ns["_aesf_lib_ctr"]

    def describe(self) -> str:
        return f"the OpenSSL library {self._path} (ctypes)"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        return self._run(self._lib, key, iv, data)


class _AesfOpensslCliBackend(AesCtrBackend):
    name = "openssl"
    min_bytes = _AESF_CLI_MIN_BYTES

    def __init__(self, exe: str) -> None:
        self._exe = exe
        self._version = _aesf_openssl_version(exe)

    def describe(self) -> str:
        what = f", {self._version}" if self._version else ""
        return f"the openssl command ({self._exe}{what})"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        if len(key) != 32 or len(iv) != 16:
            raise ValueError("AES-256-CTR needs a 32-byte key and a 16-byte IV")
        if not data:
            return b""
        # The raw key can only travel in argv; see the module docstring.
        cmd = [
            self._exe, "enc", "-d", "-aes-256-ctr", "-nopad",
            "-bufsize", "65536", "-K", key.hex(), "-iv", iv.hex(),
        ]
        timeout = 60 + len(data) // (1 << 20)
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                cmd, input=data, capture_output=True, timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Re-raised without the command line: it holds the key.
            raise RuntimeError(
                f"openssl did not finish within {timeout} s"
            ) from None
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(
                f"openssl exited {proc.returncode}: "
                f"{err[-1] if err else 'no message'}"
            )
        if len(proc.stdout) != len(data):
            raise RuntimeError(
                f"openssl returned {len(proc.stdout)} bytes for {len(data)}"
            )
        return proc.stdout


def _aesf_openssl_version(exe: str) -> str:
    """``openssl version`` output, or "" when it cannot be read."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, "version"], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=_AESF_PROBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    return lines[0] if proc.returncode == 0 and lines else ""


# ── Self-test ────────────────────────────────────────────────────


def _aesf_self_test(backend: AesCtrBackend) -> str | None:
    """Return why *backend* is wrong, or None when it passes.

    The NIST vector, then random data under random keys with counters
    that carry across the 32-, 64- and 128-bit boundaries, compared with
    the pure code.  Inputs are sized at least ``min_bytes`` so the
    backend itself, not the pure fallback, does the work.
    """
    try:
        got = backend.ctr(_AESF_KAT_KEY, _AESF_KAT_IV, _AESF_KAT_PT)
        if got != _AESF_KAT_CT:
            return "it failed the NIST SP 800-38A known-answer test"
        size = max(80, backend.min_bytes + 37)
        prefix = os.urandom(8)
        for iv in (
            prefix + b"\x00\x00\x00\x00\xff\xff\xff\xfe",
            prefix + b"\xff" * 7 + b"\xfd",
            b"\xff" * 15 + b"\xfe",
        ):
            key = os.urandom(32)
            data = os.urandom(size)
            if backend.ctr(key, iv, data) != _aesf_pure_ctr(key, iv, data):
                return "its output differs from the pure-Python AES"
    except Exception as exc:  # noqa: BLE001 — any failure disqualifies
        return f"its self-test raised {type(exc).__name__}: {exc}"
    return None


# ── Finding candidates ───────────────────────────────────────────


def _aesf_running_glibc() -> bool:
    """True when this Python runs on glibc (not musl or anything else)."""
    try:
        return bool(os.confstr("CS_GNU_LIBC_VERSION"))
    except (AttributeError, ValueError, OSError):
        return False


def _aesf_library_candidates() -> tuple[list[str], str]:
    """libcrypto paths/names worth probing here, and why none if empty."""
    try:
        import ctypes.util
    except ImportError:
        return [], "this Python has no ctypes"
    found: list[str] = []
    if sys.platform == "win32":
        dirs: list[str] = []
        for base in (os.path.dirname(sys.executable), sys.base_prefix, sys.prefix):
            if base:
                dirs += [os.path.join(base, "DLLs"), base]
        names = (
            "libcrypto-3-x64.dll", "libcrypto-3-arm64.dll", "libcrypto-3.dll",
            "libcrypto-1_1-x64.dll", "libcrypto-1_1.dll",
        )
        for d in dirs:
            for name in names:
                path = os.path.join(d, name)
                if os.path.isfile(path) and path not in found:
                    found.append(path)
        for name in ("libcrypto-3-x64", "libcrypto-3", "libcrypto-1_1-x64"):
            hit = ctypes.util.find_library(name)
            if hit and hit not in found:
                found.append(hit)
        return found, "no libcrypto DLL found"
    if sys.platform == "darwin":
        return [], ("macOS: the system libcrypto must not be loaded "
                    "(it aborts the process)")
    if sys.platform.startswith("linux") and not _aesf_running_glibc():
        return [], ("this Python is not a glibc build (the bundled musl "
                    "CPython); a host library could use another C library")
    hit = ctypes.util.find_library("crypto")
    if hit:
        found.append(hit)
    return found, "no libcrypto found"


def _aesf_child_probe(path: str) -> str | None:
    """Load *path* in a child interpreter first; None when it passed."""
    if not sys.executable:
        return "the interpreter path is unknown"
    code = _AESF_CTYPES_SRC + "\n_aesf_probe_main()\n"
    args = [_AESF_KAT_KEY.hex(), _AESF_KAT_IV.hex(),
            _AESF_KAT_PT.hex(), _AESF_KAT_CT.hex()]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-I", "-c", code, path, *args],
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=_AESF_PROBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the probe could not run ({type(exc).__name__}: {exc})"
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode == 0 and out == "ok":
        return None
    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    detail = out or (err[-1] if err else f"exit {proc.returncode}")
    return f"the probe failed ({detail})"


def _aesf_try_ctypes(notes: list[str]) -> AesCtrBackend | None:
    paths, why_none = _aesf_library_candidates()
    if not paths:
        notes.append(f"ctypes: {why_none}")
    for path in paths:
        failed = _aesf_child_probe(path)
        if failed is None:
            try:
                backend: AesCtrBackend = _AesfCtypesBackend(path)
            except Exception as exc:  # noqa: BLE001 — any failure disqualifies
                failed = f"loading it failed ({type(exc).__name__}: {exc})"
            else:
                failed = _aesf_self_test(backend)
                if failed is None:
                    return backend
        notes.append(f"ctypes {path}: {failed}")
    return None


def _aesf_try_openssl(notes: list[str]) -> AesCtrBackend | None:
    exe = shutil.which("openssl")
    if exe is None:
        notes.append("openssl command: not found on PATH")
        return None
    backend = _AesfOpensslCliBackend(exe)
    failed = _aesf_self_test(backend)
    if failed is None:
        return backend
    notes.append(f"openssl command {exe}: {failed}")
    return None


def select_aes_ctr_backend(allow_cli: bool) -> tuple[AesCtrBackend, list[str]]:
    """Pick the backend for this run; also return why others were skipped.

    Order: ctypes libcrypto, then (if *allow_cli*) the ``openssl``
    command, then pure.  ``LCSAS_TIER3_AES`` overrides; a forced backend
    that does not pass its self-test gives the pure code.
    """
    notes: list[str] = []
    want = os.environ.get("LCSAS_TIER3_AES", "auto").strip().lower() or "auto"
    if want not in ("auto", "ctypes", "openssl", "pure"):
        notes.append(f"LCSAS_TIER3_AES={want!r} is not auto, ctypes, "
                     "openssl or pure; using auto")
        want = "auto"
    if want == "pure":
        notes.append("LCSAS_TIER3_AES=pure")
        return AesCtrBackend(), notes
    if want in ("auto", "ctypes"):
        backend = _aesf_try_ctypes(notes)
        if backend is not None:
            return backend, notes
    if want == "openssl" or (want == "auto" and allow_cli):
        backend = _aesf_try_openssl(notes)
        if backend is not None:
            return backend, notes
    elif want == "auto":
        notes.append("openssl command: not used by this caller")
    return AesCtrBackend(), notes


# ── The function callers use ─────────────────────────────────────


class FastAesCtr:
    """AES-256-CTR through the fastest proven backend, pure as the floor.

    Callable like ``_aes_pure.aes_ctr(key, iv, data)``.  The backend is
    chosen on the first call; *log* receives one line naming it.
    """

    def __init__(
        self, *, allow_cli: bool = False, log: Callable[[str], None] | None = None,
    ) -> None:
        self._allow_cli = allow_cli
        self._log: Callable[[str], None] = log or (lambda _msg: None)
        self._backend: AesCtrBackend | None = None

    @property
    def backend(self) -> AesCtrBackend:
        """The backend in use (chosen now if not yet chosen)."""
        if self._backend is None:
            try:
                backend, notes = select_aes_ctr_backend(self._allow_cli)
            except Exception as exc:  # noqa: BLE001 — choosing must never stop a restore
                backend = AesCtrBackend()
                notes = [f"choosing a native AES failed "
                         f"({type(exc).__name__}: {exc})"]
            self._backend = backend
            line = f"AES-256-CTR: using {backend.describe()}"
            if backend.name == "pure" and notes:
                line += " -- " + "; ".join(notes)
            self._log(line)
        return self._backend

    def is_native(self, size: int) -> bool:
        """Would an input of *size* bytes go to a native backend?"""
        b = self.backend
        return b.name != "pure" and size >= b.min_bytes

    def disable(self, reason: str) -> None:
        """Switch to the pure code for the rest of the run."""
        b = self.backend
        if b.name != "pure":
            self._log(f"AES-256-CTR: {b.describe()} {reason}; "
                      "using the slow built-in pure-Python AES from now on")
            self._backend = AesCtrBackend()

    def __call__(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        if not self.is_native(len(data)):
            return _aesf_pure_ctr(key, iv, data)
        try:
            return self.backend.ctr(key, iv, data)
        except Exception as exc:  # noqa: BLE001 — never lose a restore to a backend
            self.disable(f"failed ({type(exc).__name__}: {exc})")
            return _aesf_pure_ctr(key, iv, data)
