"""Tier 3, the Python reader: restic repository restore, the last resort.

This module implements a *complete* restic repository reader using only
Python 3 standard-library primitives (plus the vendored ``_aes_pure``,
``_aes_fast`` and ``_zstd_pure`` modules).  It exists so that data can be restored even when the
``rustic`` and ``restic`` binaries cannot execute — e.g., on a future
CPU architecture or after ABI changes have rendered all bundled native
tools unusable.

**Performance:** AES-256-CTR runs through a native OpenSSL when one is
safe and proven correct (``_aes_fast``: the ``libcrypto`` library via
ctypes, or the ``openssl`` command when the caller allows it), else
through the pure-Python code at about 0.15 MB/s.  With native AES the
pure-Python zstd decoder (about 1.2 MB/s of text) and Poly1305 (about
14 MB/s) set the pace; with pure AES, AES does (about 2 hours per GB).

Crypto stack (all self-contained — no pip packages required):

    ========================  ================================
    Primitive                 Source
    ========================  ================================
    scrypt (KDF)              ``hashlib.scrypt`` (stdlib ≥ 3.6)
    AES-256-CTR               ``lcsas.restore._aes_fast`` (native
                              OpenSSL if usable, else ``_aes_pure``)
    AES-128-ECB (Poly1305)    ``lcsas.restore._aes_pure``
    Poly1305-AES (MAC)        Implemented below
    SHA-256                   ``hashlib`` (stdlib)
    zstd decompression        ``zstandard`` if importable, else
                              ``lcsas.restore._zstd_pure``
    ========================  ================================

Usage::

    from lcsas.restore.restic_fallback import PurePythonRestorer

    restorer = PurePythonRestorer(
        repo_path=Path("/mnt/restore_cache"),
        password_file=Path("/keys/family.key"),
    )
    restorer.restore(target=Path("/home/user/restored"))

See ``docs/RESTIC_FORMAT_SPEC.md`` for the full format reference.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from lcsas.restore._aes_fast import FastAesCtr
from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)
from lcsas.restore._disc_search import (
    _DISK_FULL_ERRNOS,
    DiscSearch,
    _visible,
)

# Re-exported: callers import these from here.
from lcsas.restore._disc_search import (
    EXIT_READER_QUIT as EXIT_READER_QUIT,
)
from lcsas.restore._disc_search import (
    RestoreAbortedError as RestoreAbortedError,
)

# ── Progress reporting tunables ──────────────────────────────────
#
# Tier-3 restore is slow (a few MB/s at best).  An heir watching a quiet
# terminal can't tell "frozen" from "still working", so we emit a
# periodic status line whenever EITHER threshold is reached:
#
#   * _PROGRESS_FILES_INTERVAL files have been restored since last emit, OR
#   * _PROGRESS_BYTES_INTERVAL bytes have been written since last emit.
#
# These are module-level so tests can monkeypatch them down to fire
# on a tiny synthetic repository.  Set environment variable LCSAS_PROGRESS=0
# to silence progress output entirely.
_PROGRESS_FILES_INTERVAL = 50
_PROGRESS_BYTES_INTERVAL = 64 * 1024 * 1024  # 64 MiB

# The file-count pass reads every tree blob; the restore pass then needs
# the same trees again.  On a multi-disc archive a second read can mean
# a second disc prompt (#602), so the count pass keeps the trees it read
# (checked against their hash) for the restore pass, up to this many
# bytes of tree data.  Trees past the cap are read again, as before.
_TREE_CACHE_MAX_BYTES = 128 * 1024 * 1024  # 128 MiB

# Resume (#729): a file already at its path with the node's size and an
# mtime within this many seconds of the node's is kept, not read again.
# FAT stores times in 2-second steps (#787).
_RESUME_MTIME_SLACK = 2.0


# ── Optional zstd ────────────────────────────────────────────────

_ZSTD_MAGIC = b"\x28\xB5\x2F\xFD"

try:
    # zstandard is an optional dependency; the ignores cover the case
    # when it's not installed (and we still want mypy to pass) AND the
    # case when it IS installed but has incomplete type stubs (some
    # versions return Any from decompress()).  `unused-ignore` lets the
    # ignores stay quiet whichever mypy verdict applies on this host.
    import zstandard as _zstd  # type: ignore[import-not-found,unused-ignore]

    def _decompress_zstd(data: bytes, max_output_size: int = 0) -> bytes:
        dctx = _zstd.ZstdDecompressor()
        if max_output_size > 0:
            return dctx.decompress(data, max_output_size=max_output_size)  # type: ignore[no-any-return,unused-ignore]
        # Try without limit first; if that fails (no content size in
        # the frame header), fall back with a generous cap.
        try:
            return dctx.decompress(data)  # type: ignore[no-any-return,unused-ignore]
        except _zstd.ZstdError:
            # Generous fallback for highly-compressible data (e.g.
            # sparse database backups with long runs of zeros).
            return dctx.decompress(data, max_output_size=max(len(data) * 100, 64 * 1024 * 1024))  # type: ignore[no-any-return,unused-ignore]

    # Exception(s) _decompress_zstd raises on a corrupt zstd frame; wrapped
    # into IntegrityError at the call site so tolerant restore skips the
    # blob instead of aborting (#374).
    _ZSTD_ERRORS: tuple[type[BaseException], ...] = (_zstd.ZstdError,)

    _HAS_ZSTD = True
except ImportError:
    # No native zstandard: fall back to the vendored pure-Python zstd
    # decompressor.  It is ~100x slower but stdlib-only, so tier-3 restore
    # of zstd-compressed (rustic v2 default) repositories still works on any
    # architecture / CPython minor with nothing pre-installed.  This is the
    # whole point of tier 3 — see RST-04.
    from lcsas.restore._zstd_pure import ZstdError as _PureZstdError
    from lcsas.restore._zstd_pure import decompress as _pure_zstd_decompress

    # The pure decoder raises its own ZstdError on a corrupt frame (#374).
    _ZSTD_ERRORS = (_PureZstdError,)

    _HAS_ZSTD = False
    _warned_pure_zstd = False

    def _decompress_zstd(data: bytes, max_output_size: int = 0) -> bytes:  # noqa: F811
        global _warned_pure_zstd
        if not _warned_pure_zstd:
            _warned_pure_zstd = True
            _log(
                "zstd: using the slow built-in zstd decoder (about "
                "1.2 MB/s of text); the 'zstandard' package is not "
                "available to this Python"
            )
        return _pure_zstd_decompress(data, max_output_size)


# ── AES-256-CTR backend ─────────────────────────────────────────
#
# Chosen once, on the first decryption (see _aes_fast).  The ``openssl``
# command puts the key in its argv, so only the tier-3 restore command
# turns it on (allow_openssl_command); library use inside lcsas (stage,
# burn preflight, key checks) uses the libcrypto library or pure code.

_AES_CTR: FastAesCtr | None = None
_AES_ALLOW_OPENSSL_COMMAND = False


def allow_openssl_command() -> None:
    """Let AES use the ``openssl`` command (the key goes in its argv).

    For the tier-3 restore command only; call before the first
    decryption.
    """
    global _AES_ALLOW_OPENSSL_COMMAND, _AES_CTR
    _AES_ALLOW_OPENSSL_COMMAND = True
    if _AES_CTR is not None and _AES_CTR.backend.name == "pure":
        _AES_CTR = None  # choose again, now with the command allowed


def _aes_ctr_fast() -> FastAesCtr:
    """The process-wide AES-256-CTR, created on first use."""
    global _AES_CTR
    if _AES_CTR is None:
        _AES_CTR = FastAesCtr(allow_cli=_AES_ALLOW_OPENSSL_COMMAND, log=_log)
    return _AES_CTR


# ── Poly1305-AES MAC ────────────────────────────────────────────

def _clamp_r(r_bytes: bytes) -> int:
    """Clamp the Poly1305 r key per RFC 8439 §2.5."""
    r = int.from_bytes(r_bytes, "little")
    return r & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def _poly1305_mac(
    key_r: bytes,
    key_s: bytes,
    message: bytes,
) -> bytes:
    """Compute a Poly1305 MAC tag.

    Args:
        key_r: 16-byte Poly1305 *r* key (will be clamped).
        key_s: 16-byte Poly1305 *s* key (AES-encrypted nonce).
        message: Data to authenticate.

    Returns:
        16-byte MAC tag.
    """
    r = _clamp_r(key_r)
    s = int.from_bytes(key_s, "little")

    p = (1 << 130) - 5
    h = 0

    for i in range(0, len(message), 16):
        block = message[i : i + 16]
        n = int.from_bytes(block, "little") | (1 << (len(block) * 8))
        h = ((h + n) * r) % p

    tag = (h + s) & ((1 << 128) - 1)
    return tag.to_bytes(16, "little")


# ── Restic Authenticated Encryption ─────────────────────────────


@dataclass(frozen=True)
class MasterKey:
    """Decrypted restic master key triple."""

    encrypt: bytes   # 32 bytes — AES-256 key
    mac_k: bytes     # 16 bytes — AES-128 key for Poly1305 nonce
    mac_r: bytes     # 16 bytes — Poly1305 r key


class IntegrityError(Exception):
    """MAC verification failed — data corrupted or wrong key."""


class KeyFileCorruptError(IntegrityError):
    """A key file could not be read or parsed (#481).

    Not a password verdict: the file is unreadable, malformed, or carries
    impossible scrypt parameters.  A subclass, so code that only catches
    IntegrityError still treats it as a failed key.
    """


class NoKeyUnlockedError(IntegrityError):
    """No key file in ``keys/`` gave up the master key (#481).

    ``rejected`` lists the key files whose MAC check failed with this
    password; ``corrupt`` pairs each unreadable or malformed one with
    the reason.  Together they let a caller tell "wrong password" from
    "the key files are damaged".
    """

    def __init__(
        self,
        message: str,
        rejected: list[Path],
        corrupt: list[tuple[Path, str]],
    ) -> None:
        super().__init__(message)
        self.rejected = rejected
        self.corrupt = corrupt


#: Exit code for "a key file rejected the password" (#805).  The same
#: as tier 1's EXIT_WRONG_PASSWORD: sysexits.h EX_NOPERM.
EXIT_WRONG_PASSWORD = 77

#: Exit status of a restore stopped because the target filesystem is full.
#: The same as tier 1's EXIT_DISK_FULL: sysexits.h EX_IOERR.  Free some
#: space, or pick a bigger disk, then run the same command again.
EXIT_DISK_FULL = 74

#: Exit status of a restore that finished, but could not restore some
#: items (listed in RESTORE_FAILURES.txt under the target).  The same as
#: tier 1's EXIT_NOT_ALL_RESTORED (#872) and restore.sh's exit 4 (#799).
#: Before rc10 tier 3 exited 2 for this, which is also argparse's usage
#: error (#875).
EXIT_NOT_ALL_RESTORED = 4


def password_from_file_bytes(data: bytes) -> bytes:
    """The password a password file holds (#641): its first line.

    Read as rustic reads it: every byte before the first LF, less one CR
    at its end.  Spaces are kept, and so is a second CR.  Tier 1
    (lcsas-restore) and the ``lcsas password`` commands use the same rule, so
    every reader agrees on a file of more than one line.
    """
    line = data.split(b"\n", 1)[0]
    return line[:-1] if line.endswith(b"\r") else line


def read_password_file(path: Path) -> bytes:
    """Read *path* and return its password (see password_from_file_bytes)."""
    return password_from_file_bytes(path.read_bytes())


def _disk_full_abort(path: Path, exc: OSError) -> RestoreAbortedError:
    """Build the abort for a full target filesystem while writing *path*."""
    return RestoreAbortedError(
        f"the filesystem that holds {path.parent} is full "
        f"({exc.strerror or exc}) while writing {path.name}.  Free some "
        "space there, or restore to a bigger disk, then run the same "
        "command again.  Files restored so far are kept.",
        exit_code=EXIT_DISK_FULL,
    )


#: The attribute a blob-read error carries its RESTORE_FAILURES.txt
#: reason in (#909).  The error itself, its class and its message are
#: unchanged, so every ``except`` and every printed line stay as they were.
_REASON_ATTR = "lcsas_reason"


def _with_reason(exc: BaseException, reason: str) -> BaseException:
    """*exc*, carrying *reason* (the row text tier 1 writes too, #909)."""
    setattr(exc, _REASON_ATTR, reason)
    return exc


class _NodeFailed(Exception):  # noqa: N818 — internal control-flow sentinel, not a public error
    """Internal: a single file node could not be restored.

    Raised by ``_restore_file`` after it has already cleaned up its
    partial output and recorded the failure, so the ``_restore_tree``
    caller can log uniformly and ``continue`` without double-recording.
    Never escapes the module.
    """


# ── Node names (issue #781) ──────────────────────────────────────
#
# rustic (and restic) store a tree node's "name" as Go strconv.Quote
# text without the outer quotes when the name holds '"', '\', a
# control character or a byte that is not valid UTF-8: ``quo"te`` is
# stored as ``quo\"te``, a tab as ``\t``, the Latin-1 byte 0xE9 as
# ``\xe9``.  The decoder below is rustic's ``unescape_filename``; the
# C twin is ``lcsas_unescape_name`` in recovery/src/lcsas-restore/path.c.

_NAME_ESCAPES = {
    "\\": b"\\", '"': b'"', "'": b"'", "`": b"`",
    "a": b"\x07", "b": b"\x08", "f": b"\x0c", "n": b"\n",
    "r": b"\r", "t": b"\t", "v": b"\x0b",
}
_NAME_HEX_WIDTH = {"x": 2, "u": 4, "U": 8}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
# Bytes an NTFS name cannot hold (besides control characters).
_WINDOWS_BAD_NAME_BYTES = frozenset(b'\\:*?"<>|')
# Which host name rule applies (module globals so tests can exercise
# each rule on any host).  macOS file systems (APFS, HFS+) only hold
# names in valid UTF-8.
_WINDOWS_NAMES = os.name == "nt"
_MACOS_NAMES = sys.platform == "darwin"


class NameDecodeError(ValueError):
    """A stored node name is not valid escaped text (issue #781)."""


def unescape_node_name(stored: str) -> bytes:
    """Decode a stored (escaped) node name to the raw bytes to create.

    The escapes are ``\\\\ \\" \\' \\` \\a \\b \\f \\n \\r \\t \\v``,
    ``\\xHH`` (one raw byte), ``\\uHHHH`` and ``\\UHHHHHHHH`` (a code
    point, written as UTF-8); every other character is written as
    UTF-8.  Raises :class:`NameDecodeError` for text no encoder writes
    (unknown escape, trailing backslash, short or non-hex digits, a
    surrogate or a code point above U+10FFFF).  rustic would use such a
    name literally; we refuse it, so it is never restored under a name
    nobody chose.
    """
    out = bytearray()
    i = 0
    n = len(stored)
    while i < n:
        c = stored[i]
        i += 1
        if c != "\\":
            try:
                out += c.encode("utf-8")
            except UnicodeEncodeError as exc:  # a lone surrogate from JSON
                raise NameDecodeError("lone surrogate in name") from exc
            continue
        if i >= n:
            raise NameDecodeError("trailing backslash")
        e = stored[i]
        i += 1
        simple = _NAME_ESCAPES.get(e)
        if simple is not None:
            out += simple
            continue
        width = _NAME_HEX_WIDTH.get(e)
        if width is None:
            raise NameDecodeError(f"unknown escape \\{e}")
        digits = stored[i:i + width]
        # Strict: int(x, 16) alone would also take '+', '_' and spaces.
        if len(digits) != width or not set(digits) <= _HEX_DIGITS:
            raise NameDecodeError(f"bad \\{e} escape")
        i += width
        value = int(digits, 16)
        if e == "x":
            out.append(value)
        elif value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            raise NameDecodeError(f"\\{e}{digits} is not a character")
        else:
            out += chr(value).encode("utf-8")
    return bytes(out)


def node_name_refusal(
    raw: bytes, *, windows: bool, macos: bool = False
) -> str | None:
    """Why the decoded name *raw* cannot be created, or ``None``.

    Refuses an empty name, ``.``, ``..``, and a name holding ``/`` or a
    NUL byte.  With *windows* also a name NTFS cannot store: one holding
    ``\\ : * ? " < > |`` or a control character, one that is not valid
    UTF-8, one that ends in a dot or a space, a device name (``CON``,
    ``nul.txt``, ``COM1``...), or one over 255 UTF-16 units (#619).
    With *macos* also a name that is not valid UTF-8.  The
    reasons match tier 1's (path.c and tree.c).
    """
    if not raw:
        return "the name is empty"
    if b"\0" in raw:
        return "the name holds a NUL byte"
    if raw in (b".", b".."):
        return "the name is '.' or '..'"
    if b"/" in raw:
        return "the name holds '/'"
    if windows and any(b < 0x20 or b in _WINDOWS_BAD_NAME_BYTES for b in raw):
        return ("Windows cannot store a name that holds "
                '\\ : * ? " < > | or a control character')
    if windows or macos:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            host = "Windows" if windows else "macOS"
            return f"{host} cannot store a name that is not valid UTF-8"
        if windows:
            return _windows_name_refusal(text)
    return None


# #619: names Windows keeps for devices, alone or with any extension.
_WINDOWS_DEVICE_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"{p}{d}" for p in ("COM", "LPT") for d in "123456789\u00b9\u00b2\u00b3"]
)


def _windows_name_refusal(text: str) -> str | None:
    """The rest of the Windows rule (#619), on a name in valid UTF-8
    with no reserved character: a trailing dot or space (Windows drops
    it), a device name, or more than 255 UTF-16 units.  As tier 1's
    lcsas_name_windows_refusal (path.c)."""
    if text[-1] in ". ":
        return "Windows cannot store a name that ends in a dot or a space"
    stem = text.split(".", 1)[0].rstrip(" ")
    # Any case, but only ASCII letters change case (as in tier 1).
    if "".join(c.upper() if c.isascii() else c for c in stem) \
            in _WINDOWS_DEVICE_STEMS:
        return ("Windows keeps this name for a device "
                "(CON, PRN, AUX, NUL, COM1-9, LPT1-9)")
    if len(text.encode("utf-16-le")) // 2 > 255:
        return "Windows cannot store a name longer than 255 characters"
    return None


# ── Items not restored, links, special files (#782-#785) ─────────
#
# The same rules as tier 1 (recovery/src/lcsas-restore/tree.c); the
# reasons and summary sentences are word for word the same.

#: First lines of RESTORE_FAILURES.txt (tier 1 writes the same text,
#: with its own name on the second line).
_FAILURES_HEADER = (
    "LCSAS restore: items that were NOT restored.",
    "Written by the Python reader (tier 3).  Everything else was restored.",
    'A row whose reason starts "special file" is a pipe, socket or',
    "device that this system cannot create.  It holds no data.",
    'A row whose reason starts "link not created: the target file',
    'system" is a link that this file system cannot hold.',
    'Every other row is an error: see "ITEMS NOT RESTORED" in RESTORE.txt.',
    "",
    "path\treason\tblob_id",
)
_FAILURES_NAME = "RESTORE_FAILURES.txt"

#: Kinds of item not restored: an error, a special file this system
#: will not create, a link the target file system cannot hold.  Only
#: errors make the restore end non-zero.
_NR_FAILED, _NR_SPECIAL, _NR_LINK = "failed", "special", "link"

_LINK_UNSUPPORTED = (
    "link not created: the target file system does not support symlinks"
)
#: symlink() errors that mean "this file system cannot hold links"
#: (FAT32, exFAT, most SMB shares), as in tier 1.
_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EPERM, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.ENOSYS)}
)
#: The longest link target Linux accepts is 4095 bytes; tier 1 refuses
#: a longer one the same way.
_LINK_TARGET_MAX = 4095

_SPECIAL_LABELS = {
    "fifo": "FIFO",
    "socket": "socket",
    "dev": "block device",
    "chardev": "character device",
}

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _mode_from_go(mode: int) -> int:
    """The POSIX mode word for a node's ``mode`` (Go ``os.FileMode``).

    rustic stores setuid, setgid and sticky at bits 23, 22 and 20, not
    at the POSIX bits 11, 10 and 9; a plain ``& 0o7777`` drops them.
    The POSIX bits are honoured too.  Tier 1: mode_from_go_filemode.
    """
    posix = mode & 0o777
    if mode & (1 << 23):
        posix |= 0o4000
    if mode & (1 << 22):
        posix |= 0o2000
    if mode & (1 << 20):
        posix |= 0o1000
    return posix | (mode & 0o7000)


def _entry_kind(path: Path | str) -> str | None:
    """What is at *path* now, without following a link there: ``None``
    (nothing), ``"dir"``, ``"link"`` (a symlink; on Windows any reparse
    point: symlink, junction), ``"file"`` or ``"other"`` (pipe, socket,
    device)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or (
        getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _make_way(path: Path, want: str) -> str | None:
    """Issue #784: make room at *path* for the item the snapshot has
    there (*want* is ``"dir"``, ``"file"`` or ``"other"``: a link or a
    special file).  Returns ``None`` when the path is ready, else the
    reason it is not.

    Whatever is in the way is replaced, except a folder: a folder where
    the snapshot has a file, link or special file is never removed.  A
    symlink is never followed: it is removed and the real item made in
    its place, so no data is written through it.  On Windows a reparse
    point is reported, not removed.  A regular file where the snapshot
    has a file is left (it is replaced when the new one is complete).
    """
    kind = _entry_kind(path)
    if kind is None:
        return None
    if kind == "dir":
        return None if want == "dir" else "a folder with this name is already there"
    if kind == "file" and want == "file":
        return None
    if kind == "link" and os.name == "nt":
        return "a link or junction with this name is already there"
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return "the item already there with this name cannot be removed"
    return None


def _link_target(node: dict[str, Any]) -> tuple[bytes, str | None]:
    """The bytes a symlink node points at, and ``None``; or ``b""`` and
    the reason the target cannot be used.

    rustic stores a target that is not valid UTF-8 twice: ``linktarget``
    with each bad byte replaced by U+FFFD, and ``linktarget_raw``, the
    exact bytes in base64.  The raw bytes win when present.
    """
    raw = node.get("linktarget_raw")
    if raw is not None:
        if not isinstance(raw, str):
            return b"", "the link target cannot be decoded"
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return b"", "the link target cannot be decoded"
    else:
        text = node.get("linktarget")
        if not isinstance(text, str):
            return b"", "the link has no target"
        data = text.encode("utf-8", "surrogatepass")
    if len(data) > _LINK_TARGET_MAX:
        return b"", "the link target is too long"
    if not data:
        return b"", "the link target is empty"
    if b"\0" in data:
        return b"", "the link target holds a NUL byte"
    return data, None


def _make_special(node_type: str, path: Path, device: int) -> None:
    """Make a FIFO, socket or device as rustic does.  A FIFO and a
    socket need no privilege on Linux; a device needs root.  Raises
    OSError, or NotImplementedError where the system has no such call."""
    if not hasattr(os, "mknod") or not hasattr(os, "mkfifo"):
        raise NotImplementedError
    if node_type == "fifo":
        os.mkfifo(path, 0o600)
    elif node_type == "socket":
        os.mknod(path, 0o600 | stat.S_IFSOCK)
    elif node_type == "dev":
        os.mknod(path, 0o600 | stat.S_IFBLK, device)
    else:
        os.mknod(path, 0o600 | stat.S_IFCHR, device)


def _decrypt_authenticated(
    encrypt_key: bytes,
    mac_k: bytes,
    mac_r: bytes,
    data: bytes,
    *,
    pure: bool = False,
) -> bytes:
    """Decrypt restic authenticated ciphertext.

    Format: ``IV (16) || ciphertext || MAC (16)``.

    Args:
        encrypt_key: 32-byte AES-256 key.
        mac_k: 16-byte AES key for Poly1305 nonce encryption.
        mac_r: 16-byte Poly1305 r key.
        data: Encrypted blob (minimum 33 bytes: 16 IV + 1 ct + 16 MAC).
        pure: Use the pure-Python AES even when a native one is in use
            (the second opinion after a content-hash mismatch).

    Returns:
        Decrypted plaintext.

    Raises:
        IntegrityError: MAC verification failed.
    """
    if len(data) < 33:
        raise IntegrityError(
            f"Encrypted data too short ({len(data)} bytes, need ≥33)"
        )

    iv = data[:16]
    ciphertext = data[16:-16]
    expected_mac = data[-16:]

    # Verify MAC: s = AES-128-ECB(mac_k, iv), then Poly1305(mac_r, s, ct)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    computed_mac = _poly1305_mac(mac_r, s, ciphertext)

    if not _constant_time_eq(computed_mac, expected_mac):
        raise IntegrityError("MAC verification failed — wrong key or corrupted data")

    if pure:
        return aes_ctr(encrypt_key, iv, ciphertext)
    return _aes_ctr_fast()(encrypt_key, iv, ciphertext)


#: What parsing a decrypted key, index, snapshot or config file raises
#: when its plaintext is wrong: bad UTF-8 or JSON, bad base64 (all
#: ValueError), a wrong shape, or (index, snapshot) a bad zstd frame.
_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    ValueError, TypeError, KeyError, AttributeError, *_ZSTD_ERRORS,
)

_T = TypeVar("_T")


def _decrypt_and_parse(  # noqa: UP047 — the reader must run on Python 3.10
    encrypt_key: bytes,
    mac_k: bytes,
    mac_r: bytes,
    data: bytes,
    parse: Callable[[bytes], _T],
    what: str,
) -> _T:
    """Decrypt *data* and return ``parse(plaintext)``, with a second attempt.

    Key, index, snapshot and config files carry no content hash, so a
    native AES that turned wrong after its self-test would show only as
    a file that will not parse (#793).  When parsing fails and a native
    AES did the decryption, decrypt once more with the pure-Python AES.
    If that parses, the native AES was wrong: it is switched off for the
    rest of the run.  If not, the first parse error is raised: the file
    itself is damaged.

    Raises:
        IntegrityError: the MAC check failed.
        One of ``_PARSE_ERRORS``: *parse* failed on both attempts.
    """
    plaintext = _decrypt_authenticated(encrypt_key, mac_k, mac_r, data)
    try:
        return parse(plaintext)
    except _PARSE_ERRORS:
        if not _aes_ctr_fast().is_native(len(data) - 32):
            raise
        second = _decrypt_authenticated(
            encrypt_key, mac_k, mac_r, data, pure=True,
        )
        if second == plaintext:
            raise
        try:
            result = parse(second)
        except _PARSE_ERRORS:
            pass
        else:
            _aes_ctr_fast().disable(f"gave wrong output for {what}")
            return result
        raise


def _unpack_repo_file(data: bytes) -> bytes:
    """Strip the repository-v2 compression byte; decompress if zstd.

    Restic repository format v2 prepends a compression-type byte
    after decryption:
      - ``\\x00``  → uncompressed (strip the prefix)
      - ``\\x01``  → zstd-compressed (strip the prefix, decompress)
      - ``\\x02``  → zstd-compressed (strip the prefix, decompress)
      - ``{``       → v1 repository, raw JSON, no prefix

    After the prefix byte the data is either raw JSON or a
    zstd frame (magic ``\\x28\\xB5\\x2F\\xFD``).
    """
    # Repo v2: first byte is a compression-type indicator
    if len(data) > 5 and data[1:5] == _ZSTD_MAGIC:
        # Byte 0 is compression type (1 or 2), rest is zstd frame
        return _decompress_zstd(data[1:])
    if len(data) > 1 and data[0:1] in (b"\x00", b"\x01", b"\x02"):
        # Compression type present but no zstd magic → just strip
        return data[1:]
    return data


def _constant_time_eq(a: bytes, b: bytes) -> bool:
    """Constant-time byte comparison to prevent timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=False):
        result |= x ^ y
    return result == 0


# ── Key File Parsing ─────────────────────────────────────────────

# Limits on the key file's scrypt cost parameters (#470).  They are read
# before any MAC check, so a corrupt or hostile key file must be refused
# quickly rather than run for hours or exhaust memory.  The most
# restic/rustic write is N=2^20, r=8, p=1 (2^23 work, ~1 GiB); these
# allow 4x that work and 2 GiB in all.  Tier-1 uses the same limits
# (recovery/src/lcsas-restore/scrypt.h); test_scrypt_limits_parity.py
# keeps them equal.
SCRYPT_MAX_WORK = 1 << 25   # N * r * p
SCRYPT_MAX_MEM = 1 << 31    # 128 * r * (N + p + 3) bytes


def _scrypt_maxmem(n: object, r: object, p: object) -> int:
    """Check key-file scrypt parameters; return ``maxmem`` for hashlib.

    Raises:
        IntegrityError: a parameter is not an int, or the work or memory
            limit is exceeded.  Not a password verdict: the key file is
            corrupt, so the caller moves on.
    """
    # type() rather than isinstance(): JSON true/false load as bool, a
    # subclass of int, and 32768.0 loads as float.
    if type(n) is not int or type(r) is not int or type(p) is not int:
        raise IntegrityError(
            f"scrypt N, r or p is not an integer (N={n!r} r={r!r} p={p!r}); "
            "corrupt key file?"
        )
    if (
        n < 2 or n & (n - 1) or r < 1 or p < 1
        or n * r * p > SCRYPT_MAX_WORK
        or 128 * r * (n + p + 3) > SCRYPT_MAX_MEM
    ):
        raise IntegrityError(
            f"scrypt parameters N={n} r={r} p={p} are out of range; "
            "corrupt key file?"
        )
    # OpenSSL needs maxmem >= 128*r*(N + p + 2), which the check above
    # guarantees.  hashlib refuses maxmem above INT_MAX, so the old
    # 2 * 128*r*(N + p + 2) failed (ValueError) on a repository at restic's
    # maximum, N=2^20 and r=8.
    return min(max(128 * r * (n + p + 3), 2**25), 2**31 - 1)


def _load_master_key(key_file: Path, password: bytes) -> MasterKey:
    """Parse a restic key file and derive the master key.

    Args:
        key_file: Path to a restic key file (JSON).
        password: The repository password (raw bytes).

    Returns:
        Decrypted MasterKey.

    Raises:
        KeyFileCorruptError: The key file is unreadable or malformed.
        IntegrityError: The MAC check failed: wrong password, or a
            corrupted ``data`` field (the two look the same).
    """
    # Everything read from the file before the MAC check is untrusted.
    # A bit-rotted or truncated key file raises OSError, ValueError
    # (bad JSON, UTF-8 or base64), TypeError, KeyError or AttributeError
    # (wrong shapes); all of it means "this file is damaged", never
    # "wrong password", so try_keys can move on to the next one (#481).
    try:
        with open(key_file, encoding="utf-8") as f:
            key_doc = json.load(f)
        salt = base64.b64decode(key_doc["salt"])
        n = key_doc.get("N", 32768)
        r = key_doc.get("r", 8)
        p = key_doc.get("p", 1)
        maxmem = _scrypt_maxmem(n, r, p)
        encrypted_master = base64.b64decode(key_doc["data"])
    except IntegrityError as e:
        raise KeyFileCorruptError(f"key file {key_file}: {e}") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        raise KeyFileCorruptError(
            f"key file {key_file}: unreadable or malformed "
            f"({type(e).__name__}: {e}); corrupt key file?"
        ) from None

    # Derive key-encryption key via scrypt
    derived = hashlib.scrypt(
        password, salt=salt, n=n, r=r, p=p, dklen=64, maxmem=maxmem,
    )

    # Split derived key into AES encryption key + Poly1305 MAC keys
    kek_encrypt = derived[:32]
    kek_mac_k = derived[32:48]
    kek_mac_r = derived[48:64]

    def parse(master_json: bytes) -> MasterKey:
        master = json.loads(master_json)
        return MasterKey(
            encrypt=base64.b64decode(master["encrypt"]),
            mac_k=base64.b64decode(master["mac"]["k"]),
            mac_r=base64.b64decode(master["mac"]["r"]),
        )

    # Decrypt the master key (the MAC check raises IntegrityError: a
    # wrong password) and parse it, with a pure-AES second attempt.
    try:
        return _decrypt_and_parse(
            kek_encrypt, kek_mac_k, kek_mac_r, encrypted_master, parse,
            f"key file {key_file.name}",
        )
    except _PARSE_ERRORS as e:
        raise KeyFileCorruptError(
            f"key file {key_file}: the password is right, but the master "
            f"key inside is malformed ({type(e).__name__}: {e})"
        ) from None


def try_keys(keys_dir: Path, password: bytes) -> MasterKey:
    """Try all key files in the directory until one works.

    Args:
        keys_dir: Path to the repository ``keys/`` directory.
        password: The repository password.

    Returns:
        The first successfully decrypted MasterKey.

    A damaged key file is skipped, like tier-1 does (``repo.c``), so a
    bit-rotted file that sorts first can't hide a good one (#481).

    Raises:
        IntegrityError: The keys directory is empty.
        NoKeyUnlockedError: No key file could be decrypted.
    """
    key_files = sorted(keys_dir.iterdir())
    if not key_files:
        raise IntegrityError(f"No key files found in {keys_dir}")

    rejected: list[Path] = []
    corrupt: list[tuple[Path, str]] = []
    for kf in key_files:
        if not kf.is_file():
            continue
        try:
            return _load_master_key(kf, password)
        except KeyFileCorruptError as e:
            corrupt.append((kf, str(e)))
        except IntegrityError:
            rejected.append(kf)

    damaged = "; ".join(why for _, why in corrupt)
    if corrupt and not rejected:
        msg = (
            f"No key file in {keys_dir} could be read: all {len(corrupt)} "
            f"are unreadable or corrupt. {damaged}"
        )
    else:
        msg = f"Could not decrypt any key file in {keys_dir} — wrong password?"
        if rejected:
            msg += f" ({len(rejected)} key file(s) rejected it)"
        if corrupt:
            msg += (
                f" {len(corrupt)} more could not be read, so were not "
                f"checked: {damaged}"
            )
    raise NoKeyUnlockedError(msg, rejected, corrupt)


# ── Repository Reader ───────────────────────────────────────────

@dataclass
class BlobLocation:
    """Where a blob lives inside a pack file."""

    pack_id: str
    offset: int
    length: int
    blob_type: str  # "data" or "tree"
    uncompressed_length: int | None = None


@dataclass
class SnapshotMeta:
    """Parsed snapshot metadata."""

    snapshot_id: str
    time: str
    tree: str  # root tree blob ID
    paths: list[str] = field(default_factory=list)
    hostname: str = ""
    tags: list[str] = field(default_factory=list)


class PurePythonRestorer:
    """Tier 3, the Python reader: a restic repository restore engine.

    This is a *last-resort* fallback.  Use ``rustic`` or ``restic``
    whenever possible — they are orders of magnitude faster.

    The restorer expects a fully-assembled repository cache (i.e., all
    pack files already copied to ``repo_path/data/``).  Use
    ``RestoreExecutor.prepare_cache()`` and ``ingest_volume()`` first.
    """

    def __init__(
        self,
        repo_path: Path,
        password_file: Path | None = None,
        password: bytes | None = None,
        pack_search_paths: list[Path] | None = None,
        interactive: bool = True,
        catalog_path: Path | None = None,
        strict: bool = False,
        pack_cache_dir: Path | None = None,
        resume: bool = True,
        meta_disc: Path | None = None,
        prompt_in: Callable[[], str] | None = None,
        prompt_out: Callable[[str], None] | None = None,
        repo_id: str | None = None,
    ) -> None:
        """Initialize the restorer.

        Provide either *password_file* or raw *password*, not both.

        Args:
            repo_path: Path to an assembled restic repository.
            password_file: File containing the raw password on the first line.
            password: Raw password bytes (alternative to password_file).
            pack_search_paths: Optional list of additional roots to scan for
                pack files (each may contain ``data/<XX>/<hex>`` or ``data/<hex>``).
                Defaults to ``[repo_path / "data"]`` -- preserves legacy behaviour
                for single-disc / pre-assembled-cache callers.  Multi-disc tier-3
                invocation passes one ``--mount-point`` per data disc (see
                ``recovery/scripts/restore.sh``).
            interactive: If True (default), the disc search
                (``DiscSearch`` in ``_disc_search.py``) prints the
                LCSAS disc-swap prompt to stderr and blocks on stdin when
                a pack is not found in any configured search path, then
                rescans (up to ``DiscSearch._DISC_SWAP_MAX_RETRIES`` times).
                ``q`` or the end of input there raises
                ``RestoreAbortedError``, which stops the whole restore
                (#658).  If False, the search raises FileNotFoundError on
                a miss (matches pre-#234 behaviour; tests that pin the raise
                contract must opt out explicitly).  The tier-3 CLI sets
                this from ``--interactive on|off|auto`` (auto = isatty);
                see ``standalone_builder.py``.
            catalog_path: Optional **explicit override** path to an LCSAS
                disc catalog (``catalog.db``).  When supplied, the disc-swap
                prompt resolves the missing pack ID to one or more volume
                labels via the catalog and prints them in the SAME framed
                shape that tier-1 emits (see
                ``recovery/src/lcsas-restore/disc_locator.c::print_prompt``).
                Heirs (and the blind-restore test agent's pattern matcher)
                rely on that exact phrasing.  A missing, corrupt, or locked
                catalog falls back to the legacy "(no catalog available)"
                line; lookups never raise.

                When ``None`` (the default), the restorer auto-discovers a
                ``catalog.db`` lazily from ``pack_search_paths``: each
                disc-swap prompt cycle re-scans the mount roots so a catalog
                that becomes reachable mid-restore (e.g. the heir just
                inserted a data disc that carries a disc catalog)
                is picked up on the next prompt fire.  See
                ``DiscSearch._discover_catalog`` for the search order.
                This addresses issue #253 -- without
                it, ``restore.sh``'s one-shot catalog-pick scan misses any
                catalog that lives on a data disc not yet mounted at startup.
            strict: If False (default), tree traversal is *tolerant*: a
                corrupt blob, a missing index entry, or an unreadable
                pack (after disc-swap retries are exhausted) records a
                failure and the restore *continues*, so one bad blob can
                never abort the whole last-resort restore.  After
                traversal, ``restore()`` writes ``RESTORE_FAILURES.txt``
                under the target and exposes the count via ``.failures``.
                If True, the legacy raise-first contract is preserved:
                the first such error aborts the restore.  Tier 3 (the
                Python reader) defaults to tolerant; callers that
                want all-or-nothing semantics opt in explicitly.
            pack_cache_dir: Optional pack cache folder (#602), the
                ``LCSAS_PACK_CACHE_DIR`` of tier 1.  When a pack is found
                on a data disc, every pack of this repository on that
                disc is copied into ``<cache>/data/<XX>/<hex>``, so each
                data disc is needed once.  The cache is searched first.
                ``None`` (the default): no cache.
            resume: If True (default), a file already at its path with
                the node's size and mtime (within 2 seconds, for FAT)
                is kept and its data is not read (#729), as tier 1 and
                rustic do.  False restores every file again.
            meta_disc: Optional mount point of the meta disc (tier 1's
                ``--meta-disc``).  While it is mounted, nothing on it is
                searched, so it can be ejected and the drive reused.
                Each root is also searched one level down (a disc
                mounted at ``/media/<user>/<LABEL>`` under the root
                ``/media/<user>``), as tier 1 searches its mount
                parents.
            prompt_in: Reads one line of the answer at the disc prompt
                ("" at the end of input).  ``None`` (the default): stdin.
            prompt_out: Writes the disc prompt and the lines around it.
                ``None`` (the default): stderr.
            repo_id: The backup set being restored (#950: the reader
                passes its ``--repo`` folder's name, as in
                ``metadata/<id>``).  A disc catalog with no pack of it
                is another archive's and is not used for the prompt.
                ``None`` (the default): every catalog is used.
        """
        self.repo_path = repo_path
        self._strict = strict
        # (relative path, blob_id-or-"", reason) for each node that
        # could not be restored under tolerant traversal.  Reset at the
        # top of every restore() call.
        self._failures: list[tuple[str, str, str]] = []
        # Every item not restored, errors or not, in walk order: the rows
        # of RESTORE_FAILURES.txt (#783).  _skipped counts the ones that
        # are not errors (special files, links the target cannot hold).
        self._rows: list[tuple[str, str, str]] = []
        self._skipped: dict[str, int] = {_NR_SPECIAL: 0, _NR_LINK: 0}
        if password is not None:
            self._password = password
        elif password_file is not None:
            self._password = read_password_file(password_file)
        else:
            raise ValueError("Provide either password_file or password")

        self._master_key: MasterKey | None = None
        self._blob_index: dict[str, BlobLocation] | None = None
        # Tree blobs kept from the count pass for the restore pass
        # (#602); emptied when restore() returns.
        self._tree_cache: dict[str, bytes] = {}
        self._tree_cache_bytes = 0
        self._snapshots: list[SnapshotMeta] | None = None

        # #729: keep files an earlier run finished; count them.
        self._resume = resume
        self._resumed = 0

        # The disc search (#884): the pack cache, the search roots (the
        # legacy ``repo_path/data`` when none are given), the discs
        # mounted below them and the disc prompt.  Made after the
        # password check, so a bad call leaves no pack cache folder.
        # _log is looked up at each call, so a replaced _log is used.
        self._search = DiscSearch(
            list(pack_search_paths) if pack_search_paths
            else [repo_path / "data"],
            meta_disc=meta_disc,
            pack_cache=pack_cache_dir,
            catalog_path=catalog_path,
            interactive=interactive,
            repo_data=repo_path / "data",
            log=lambda msg: _log(msg),
            prompt_in=prompt_in,
            prompt_out=prompt_out,
            repo_id=repo_id,
        )
        # The search is told this repository's pack ids before its
        # first read (see _read_blob).
        self._index_packs_set = False

        # Progress reporting state — reset at the top of every
        # restore() call.  _progress_enabled is read from the env once
        # at construction so the heir can silence output with
        # LCSAS_PROGRESS=0 without re-reading the env per file.
        self._progress_enabled: bool = (
            os.environ.get("LCSAS_PROGRESS", "1") != "0"
        )
        self._progress_total_files: int = 0
        self._progress_done_files: int = 0
        self._progress_done_bytes: int = 0
        self._progress_last_emit_files: int = 0
        self._progress_last_emit_bytes: int = 0

    # ── Public API ───────────────────────────────────────────────

    def restore(
        self,
        target: Path,
        snapshot_id: str | None = None,
    ) -> SnapshotMeta:
        """Restore a snapshot to *target*.

        If *snapshot_id* is ``None``, restores the most recent snapshot.

        Args:
            target: Directory to write restored files into.
            snapshot_id: Optional specific snapshot to restore.

        Returns:
            SnapshotMeta for the restored snapshot.

        Raises:
            RestoreAbortedError: the reader typed ``q`` at the disc
                prompt, its input ended there, or the target filesystem
                is full (#658).  Its ``exit_code`` says which.

        Note:
            Under the default tolerant mode (``strict=False``), a
            per-node failure does not raise — the count is available on
            ``self.failures`` after this returns, and a
            ``RESTORE_FAILURES.txt`` manifest is written under *target*.
        """
        self._ensure_loaded()
        self._failures = []
        self._rows = []
        self._skipped = {_NR_SPECIAL: 0, _NR_LINK: 0}

        if snapshot_id is not None:
            snap = self._find_snapshot(snapshot_id)
        else:
            snap = self._latest_snapshot()

        _log(f"Restoring snapshot {snap.snapshot_id[:12]}... "
             f"({', '.join(snap.paths)})")
        _log(f"Target: {target}")

        # Reset progress state for this restore call.  The pre-pass
        # below only reads tree blobs (which are small), not data
        # blobs, so the cost is negligible relative to the restore.
        self._progress_done_files = 0
        self._progress_done_bytes = 0
        self._progress_last_emit_files = 0
        self._progress_last_emit_bytes = 0
        self._progress_total_files = 0
        self._resumed = 0
        if self._progress_enabled:
            self._progress_total_files = self._count_files(snap.tree)
            _log(f"Counted {self._progress_total_files} file(s) to restore.")

        target.mkdir(parents=True, exist_ok=True)
        try:
            self._restore_tree(snap.tree, target)
        except RestoreAbortedError:
            # #658: the restore stops here; still list the items that
            # were not restored before the stop.
            if self._rows:
                self._write_failure_manifest(target)
            raise
        finally:
            self._tree_cache.clear()
            self._tree_cache_bytes = 0

        # Force a final 100% line so the last reported count matches
        # the total — otherwise the heir sees e.g. 47/50 and
        # wonders if the last 3 files were skipped.
        self._emit_progress(force=True)

        if self._resumed:
            _log(
                f"{self._resumed} file(s) were already restored by an "
                "earlier run and were kept."
            )

        # #783: how many items were not restored, of each kind, and
        # where the list is.  Tier 1 prints the same sentences.  Only
        # errors keep the restore from being complete.
        if self._skipped[_NR_SPECIAL]:
            _log(
                f"{self._skipped[_NR_SPECIAL]} special file(s) were not "
                "restored: pipes, sockets or devices that this system "
                "cannot create.  They hold no data."
            )
        if self._skipped[_NR_LINK]:
            _log(
                f"{self._skipped[_NR_LINK]} link(s) were not restored: the "
                "target file system does not support symlinks."
            )
        if self._failures:
            _log(f"ERROR: {len(self._failures)} item(s) were NOT restored.")
        self._write_failure_manifest(target)
        if self._rows:
            _log(f"See {target}/{_FAILURES_NAME} for the list.")
        # #949: the discs the reader answered 's' for (tier 1 prints the
        # same lines at the end of an incomplete restore).
        for line in self._search.report_skips():
            _log(line)
        if not self._failures:
            _log("Restore complete.")
        return snap

    @property
    def failures(self) -> int:
        """Number of nodes that could not be restored in the last run.

        Zero on a clean restore.  Non-zero under tolerant mode when one
        or more items could not be restored because of an error;
        ``RESTORE_FAILURES.txt`` under the target lists them.  Special
        files this system cannot create and links the target file
        system cannot hold are not counted here (see ``skipped``).
        """
        return len(self._failures)

    @property
    def skipped(self) -> int:
        """Items not restored in the last run that are not errors (#783):
        special files this system cannot create, and links the target
        file system cannot hold.  Listed in ``RESTORE_FAILURES.txt``."""
        return sum(self._skipped.values())

    def _write_failure_manifest(self, target: Path) -> None:
        """Write ``RESTORE_FAILURES.txt``: one row per item not restored.

        Each row is the path, the reason and the blob id implicated
        (empty when the failure was not blob-scoped), tab-separated,
        exactly as tier 1 writes them.  With no rows, a file of that
        name left by an earlier run is removed (only one that starts
        with our header), so it never describes an older run.
        """
        manifest = target / _FAILURES_NAME
        if not self._rows:
            try:
                if _entry_kind(manifest) == "file":
                    with open(manifest, "rb") as f:
                        if f.read(6) == b"LCSAS ":
                            manifest.unlink()
            except OSError:
                pass
            return
        lines = list(_FAILURES_HEADER)
        for path, blob_id, reason in self._rows:
            # _visible: a tab or newline in a restored name (#781) must
            # not split the row.
            lines.append(f"{_visible(path)}\t{_visible(reason)}\t{blob_id}")
        try:
            if _make_way(manifest, "file") is not None:
                raise IsADirectoryError(str(manifest))
            # backslashreplace: a restored name that is not valid UTF-8
            # (#781) reaches here as a surrogate-escaped str.
            manifest.write_text(
                "\n".join(lines) + "\n", encoding="utf-8",
                errors="backslashreplace",
            )
        except OSError as exc:
            # The manifest is best-effort: never let writing it mask the
            # fact that the bulk of the data did restore.
            _log(f"Could not write {manifest}: {exc}")

    def list_snapshots(self) -> list[SnapshotMeta]:
        """Return all snapshots sorted by time (oldest first)."""
        self._ensure_loaded()
        assert self._snapshots is not None
        return list(self._snapshots)

    def verify_key(self) -> bool:
        """Test whether the password can decrypt the repository.

        Returns True if key decryption succeeds, False otherwise.
        """
        try:
            self._load_key()
            return True
        except IntegrityError:
            return False

    # ── Loading / Initialization ─────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Load key, index, and snapshots if not already done."""
        if self._master_key is None:
            self._load_key()
        if self._blob_index is None:
            self._load_index()
        if self._snapshots is None:
            self._load_snapshots()

    def _load_key(self) -> None:
        """Decrypt the master key from the repository's key files."""
        keys_dir = self.repo_path / "keys"
        self._master_key = try_keys(keys_dir, self._password)
        _log("Master key decrypted successfully.")

    def _decrypt(self, encrypted: bytes, *, pure: bool = False) -> bytes:
        """Decrypt data using the master key."""
        assert self._master_key is not None
        mk = self._master_key
        return _decrypt_authenticated(
            mk.encrypt, mk.mac_k, mk.mac_r, encrypted, pure=pure,
        )

    def _decrypt_file(self, path: Path) -> bytes:
        """Read, decrypt, and (if needed) decompress a repository file.

        See ``_unpack_repo_file`` for the compression byte.  No second
        AES attempt here: ``_read_json_file`` is the reader with one.
        """
        return _unpack_repo_file(self._decrypt(path.read_bytes()))

    def _read_json_file(self, path: Path) -> Any:
        """Decrypt, decompress and parse an index, snapshot or config file.

        With a pure-AES second attempt when it will not parse (#793).
        """
        assert self._master_key is not None
        mk = self._master_key
        return _decrypt_and_parse(
            mk.encrypt, mk.mac_k, mk.mac_r, path.read_bytes(),
            lambda plain: json.loads(_unpack_repo_file(plain)),
            f"{path.parent.name}/{path.name[:12]}",
        )

    def _load_index(self) -> None:
        """Read and decrypt all index files → build blob location map."""
        index_dir = self.repo_path / "index"
        if not index_dir.is_dir():
            raise FileNotFoundError(f"Index directory not found: {index_dir}")

        self._blob_index = {}
        supersedes: set[str] = set()

        # First pass: find superseded index files
        index_files = sorted(index_dir.iterdir())
        index_data: list[tuple[str, dict[str, Any]]] = []
        for idx_file in index_files:
            if not idx_file.is_file():
                continue
            idx_doc = self._read_json_file(idx_file)
            index_data.append((idx_file.name, idx_doc))
            for sup in idx_doc.get("supersedes", []):
                supersedes.add(sup)

        # Second pass: build blob index, skipping superseded
        for idx_name, idx_doc in index_data:
            if idx_name in supersedes:
                continue
            for pack_entry in idx_doc.get("packs", []):
                pack_id = pack_entry["id"]
                for blob in pack_entry.get("blobs", []):
                    blob_id = blob["id"]
                    self._blob_index[blob_id] = BlobLocation(
                        pack_id=pack_id,
                        offset=blob["offset"],
                        length=blob["length"],
                        blob_type=blob["type"],
                        uncompressed_length=blob.get("uncompressed_length"),
                    )

        _log(f"Loaded index: {len(self._blob_index)} blobs "
             f"across {len(index_data)} index files.")

    def _load_snapshots(self) -> None:
        """Read and decrypt all snapshot files."""
        snap_dir = self.repo_path / "snapshots"
        if not snap_dir.is_dir():
            raise FileNotFoundError(f"Snapshots directory not found: {snap_dir}")

        self._snapshots = []
        for snap_file in sorted(snap_dir.iterdir()):
            if not snap_file.is_file():
                continue
            snap_doc = self._read_json_file(snap_file)
            self._snapshots.append(
                SnapshotMeta(
                    snapshot_id=snap_file.name,
                    time=snap_doc.get("time", ""),
                    tree=snap_doc["tree"],
                    paths=snap_doc.get("paths", []),
                    hostname=snap_doc.get("hostname", ""),
                    tags=snap_doc.get("tags", []),
                )
            )

        # Sort by the instant, not the string (#663): two times with
        # different UTC offsets (the repeated hour when daylight saving
        # ends, a container on UTC next to a host on local time) sort
        # wrong as strings.  A time that will not parse sorts first, so
        # it is never taken as "latest"; the sort is stable, so ties keep
        # file-name order.
        self._snapshots.sort(key=_snapshot_sort_key)
        _log(f"Found {len(self._snapshots)} snapshot(s).")

    def _latest_snapshot(self) -> SnapshotMeta:
        """Return the most recent snapshot."""
        assert self._snapshots is not None
        if not self._snapshots:
            raise ValueError("No snapshots found in repository")
        return self._snapshots[-1]

    def _find_snapshot(self, snapshot_id: str) -> SnapshotMeta:
        """Find a snapshot by exact or prefix ID match."""
        assert self._snapshots is not None
        # Try exact match first
        for snap in self._snapshots:
            if snap.snapshot_id == snapshot_id:
                return snap
        # Try prefix match
        matches = [
            s for s in self._snapshots
            if s.snapshot_id.startswith(snapshot_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous snapshot prefix '{snapshot_id}' — "
                f"matches {len(matches)} snapshots"
            )
        raise ValueError(f"Snapshot not found: '{snapshot_id}'")

    # ── Blob Reading ─────────────────────────────────────────────

    def _read_blob(self, blob_id: str) -> bytes:
        """Read, decrypt, and (optionally) decompress a blob.

        Also verifies the SHA-256 of the decrypted content matches
        the blob ID.

        Returns:
            Raw blob content (file data or tree JSON).
        """
        cached = self._tree_cache.get(blob_id)
        if cached is not None:
            return cached  # a tree the count pass read and checked (#602)
        assert self._blob_index is not None
        if blob_id not in self._blob_index:
            raise _with_reason(
                KeyError(f"Blob not found in index: {blob_id}"),
                f"blob not in index: {blob_id}",
            )

        loc = self._blob_index[blob_id]
        if not self._index_packs_set:
            # Only this repository's packs are copied into the cache.
            self._search.set_index_packs(
                b.pack_id for b in self._blob_index.values()
            )
            self._index_packs_set = True
        try:
            encrypted = self._search.read_pack_range(
                loc.pack_id, loc.offset, loc.length,
            )
        except FileNotFoundError as exc:
            # A copy that was found but could not be read is set aside
            # and the search ends here too: it is unreadable, not missing.
            why = ("pack unreadable"
                   if self._search.pack_was_unreadable(loc.pack_id)
                   else "pack not found")
            _with_reason(exc, f"{why}: {loc.pack_id}")
            raise
        except OSError as exc:
            _with_reason(exc, f"pack unreadable: {loc.pack_id}")
            raise
        try:
            return self._decrypt_and_decode(blob_id, loc, encrypted)
        except IntegrityError as exc:
            # A short pack, a MAC that fails, a bad zstd frame and a
            # wrong content hash all say the same: the stored bytes are
            # not what was written.
            _with_reason(exc, f"pack damaged: {loc.pack_id}")
            raise

    def _decrypt_and_decode(
        self, blob_id: str, loc: BlobLocation, encrypted: bytes,
    ) -> bytes:
        """Decrypt, decompress and hash-check the bytes of one blob."""
        plaintext = self._decrypt(encrypted)
        try:
            return self._decode_blob(blob_id, loc, plaintext)
        except IntegrityError:
            # The MAC passed, so the ciphertext is what was written.  If a
            # native AES did the decryption, get a second opinion from the
            # pure code before calling the blob bad: a backend that turned
            # wrong after its self-test must not cost files.
            if not _aes_ctr_fast().is_native(len(encrypted) - 32):
                raise
            second = self._decrypt(encrypted, pure=True)
            if second == plaintext:
                raise
            decoded = self._decode_blob(blob_id, loc, second)
            _aes_ctr_fast().disable(
                f"gave wrong output for blob {blob_id[:12]}"
            )
            return decoded

    def _decode_blob(
        self, blob_id: str, loc: BlobLocation, plaintext: bytes,
    ) -> bytes:
        """Decompress (if the index says so) and hash-check a blob."""
        # Handle zstd compression.  The restic repository-v2 index contract is
        # the discriminator: a blob is compressed iff its index entry
        # carries `uncompressed_length`.  Auto-compression stores
        # incompressible blobs (e.g. an archived `.zst` file) UNcompressed,
        # and those can legitimately begin with the zstd magic — so never
        # infer compression from leading bytes when the index has spoken.
        try:
            if loc.uncompressed_length is not None:
                plaintext = _decompress_zstd(
                    plaintext, max_output_size=loc.uncompressed_length
                )
            elif (
                plaintext[:4] == _ZSTD_MAGIC
                and hashlib.sha256(plaintext).hexdigest() != blob_id
            ):
                # Index silent (v1 repository / legacy index): the raw SHA-256 is
                # authoritative.  Only decompress if the raw bytes do not
                # already hash to the blob id.
                plaintext = _decompress_zstd(
                    plaintext, max_output_size=len(plaintext) * 20
                )
        except _ZSTD_ERRORS as exc:
            # A blob that decrypts (MAC-valid) but whose zstd frame is
            # corrupt must count as a BAD BLOB, not an abort: raise
            # IntegrityError so tolerant tier-3 restore records the failure
            # and continues, honouring the 'one bad blob never aborts'
            # contract (#374).  Strict mode still surfaces it (as an
            # IntegrityError) exactly like a hash mismatch.
            raise IntegrityError(
                f"Blob {blob_id} failed zstd decompression: {exc}"
            ) from exc

        # Verify content hash
        actual_hash = hashlib.sha256(plaintext).hexdigest()
        if actual_hash != blob_id:
            raise IntegrityError(
                f"Blob content hash mismatch: expected {blob_id}, "
                f"got {actual_hash}"
            )

        return plaintext

    # ── Tree Traversal & File Extraction ─────────────────────────

    def _restore_tree(
        self,
        tree_id: str,
        target_dir: Path,
        hardlink_map: dict[tuple[int, int], Path] | None = None,
    ) -> None:
        """Recursively restore a tree node to *target_dir*.

        Tolerant by default: a corrupt/missing tree blob records ONE
        failure for the whole subtree and returns (the parent traversal
        continues); a per-node failure records that node and continues
        to the next sibling.  ``strict=True`` restores the legacy
        raise-first behaviour.

        Links (#782): every symlink is restored with its stored target,
        as tier 1 and rustic do: absolute, relative, pointing up and out
        of the restore, or dangling.  Making a link writes nothing
        outside the target folder; the risk is a later write THROUGH a
        link, which ``_make_way`` rules out (#784).

        Items not restored (#783): an error (refused name, unusable link
        target, unknown type, a folder in the way) is listed and makes
        ``failures`` non-zero; a special file this system will not make
        and a link the target file system cannot hold are listed only.
        """
        try:
            tree_data = self._read_blob(tree_id)
            tree_doc = json.loads(tree_data)
        except (IntegrityError, KeyError, OSError, json.JSONDecodeError) as exc:
            if self._strict:
                raise
            self._record_failure(target_dir, tree_id, exc)
            return

        # Track hardlink targets across the whole traversal (one map for
        # all directories) so cross-directory links restore as links, not
        # copies.  Keyed by (device, inode): two equal inodes on different
        # source devices must never be cross-linked.
        if hardlink_map is None:
            hardlink_map = {}

        for node in tree_doc.get("nodes", []):
            name = node.get("name")
            # A node with no type is not restored, as in tier 1: it is
            # listed as "unknown item type: ?" (it used to be read as a
            # file).
            node_type = node.get("type")
            if not isinstance(node_type, str):
                node_type = "?"
            # Issue #781: decode rustic's escaped name to raw bytes, then
            # run the path-safety checks (empty, ".", "..", "/", NUL; on
            # Windows also what NTFS cannot store) on the DECODED bytes.
            # A refused name is a failure: logged, listed in
            # RESTORE_FAILURES.txt, and the restore ends non-zero.
            safe_name = self._node_fs_name(name, target_dir)
            if safe_name is None:
                continue
            node_path = target_dir / safe_name

            # #784: never write through what an earlier restore left at
            # this path (see _make_way).  #783: an unknown type is an
            # error.
            special = _SPECIAL_LABELS.get(node_type)
            if node_type == "file":
                want = "file"
            elif node_type == "dir":
                want = "dir"
            elif node_type == "symlink" or special:
                want = "other"
            else:
                self._not_restored(
                    node_path, f"unknown item type: {node_type}"
                )
                continue
            why = _make_way(node_path, want)
            if why is not None:
                self._not_restored(node_path, why)
                continue

            # Per-node containment: one bad node never aborts the rest of
            # the tree.  ``_restore_file`` and the subtree recursion have
            # already recorded + cleaned up before raising ``_NodeFailed``;
            # we just log-and-continue here.  Raw errors raised by the
            # dir/symlink paths are recorded here.  ``strict=True`` lets
            # them propagate (the inner handlers re-raise unchanged).
            try:
                if node_type == "file":
                    inode = node.get("inode", 0)
                    links = node.get("links", 1)
                    # device absent → 0, matching legacy inode-only behavior
                    device = node.get("device_id", node.get("device", 0))
                    hl_key = (device, inode)

                    # Hardlink dedup: if inode already restored, link it
                    if inode and links > 1 and hl_key in hardlink_map:
                        src = hardlink_map[hl_key]
                        try:
                            node_path.parent.mkdir(parents=True, exist_ok=True)
                            # An older file of this name is replaced
                            # (os.link never overwrites).
                            node_path.unlink(missing_ok=True)
                            os.link(src, node_path)
                            continue
                        except OSError:
                            # Cross-device or permission issue — fall
                            # through to normal restore
                            _log(
                                f"Hardlink {node_path} → {src} failed, "
                                f"copying instead"
                            )

                    self._restore_file(node, node_path)

                    # Register as hardlink source for future occurrences
                    if inode and links > 1:
                        hardlink_map[hl_key] = node_path

                elif node_type == "dir":
                    node_path.mkdir(exist_ok=True)
                    if "subtree" in node:
                        self._restore_tree(
                            node["subtree"], node_path, hardlink_map
                        )
                    # Restore directory permissions after contents
                    self._apply_metadata(node, node_path)
                elif node_type == "symlink":
                    self._restore_symlink(node, node_path)
                else:
                    # #783: a FIFO, socket or device, made as rustic
                    # makes it; when this system will not, it is listed
                    # and the exit code is not affected.
                    try:
                        _make_special(
                            node_type, node_path, int(node.get("device", 0))
                        )
                    except (OSError, NotImplementedError, ValueError,
                            TypeError) as exc:
                        if isinstance(exc, OSError) and exc.errno:
                            why = os.strerror(exc.errno)
                        else:
                            why = "this system cannot create it"
                        self._not_restored(
                            node_path,
                            f"special file ({special}) not created: {why}",
                            _NR_SPECIAL,
                        )
                    else:
                        self._apply_metadata(node, node_path)
            except _NodeFailed:
                # Already recorded + logged by the failing leaf; the
                # subtree recursion records its own. Keep going.
                continue
            except (IntegrityError, KeyError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno in _DISK_FULL_ERRNOS:
                    # #658: a full disk would fail every later file too.
                    raise _disk_full_abort(node_path, exc) from exc
                if self._strict:
                    raise
                self._record_failure(node_path, "", exc)
                continue

    def _restore_symlink(self, node: dict[str, Any], path: Path) -> None:
        """#782: make the link with its stored target (raw bytes when
        rustic kept them in ``linktarget_raw``).  A target that cannot
        be used is an error; a target file system without links is
        listed only (#783).  Raises OSError for a full disk."""
        data, why = _link_target(node)
        if why is not None:
            self._not_restored(path, f"link refused: {why}")
            return
        try:
            target = os.fsdecode(data)
        except UnicodeDecodeError:
            self._not_restored(
                path, "link refused: this system cannot hold the link target"
            )
            return
        try:
            os.symlink(target, path)
        except NotImplementedError:
            self._not_restored(path, _LINK_UNSUPPORTED, _NR_LINK)
            return
        except OSError as exc:
            if exc.errno in _DISK_FULL_ERRNOS:
                raise
            if exc.errno in _LINK_UNSUPPORTED_ERRNOS or os.name == "nt":
                self._not_restored(path, _LINK_UNSUPPORTED, _NR_LINK)
            else:
                self._not_restored(
                    path,
                    f"link not created: {os.strerror(exc.errno or 0)}",
                )
            return
        self._apply_metadata(node, path, is_link=True)

    def _restore_file(self, node: dict[str, Any], path: Path) -> None:
        """Extract a file from its content blobs.

        Writes to a sibling ``.lcsas-partial`` file and only
        ``os.replace``s it onto the final path once every content blob has been
        written and metadata applied — so an exception mid-file never
        leaves a truncated file at the final path, and a re-run is
        idempotent.  On failure the partial is unlinked, the failure is
        recorded, and ``_NodeFailed`` is raised for the caller to log.

        Resume (#729): a file an earlier run finished (see
        ``_already_restored``) is kept and none of its data is read.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        content_ids: list[str] = node.get("content", [])
        tmp_path = path.with_name(path.name + ".lcsas-partial")

        if self._resume and _already_restored(node, path):
            self._resumed += 1
            # A partial file left by a later, stopped run is not needed.
            if _entry_kind(tmp_path) in ("file", "link"):
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            self._progress_done_files += 1
            self._emit_progress()
            return

        bytes_written = 0
        try:
            # #784: never write through a link at the partial file's
            # path: remove whatever is there, and open without following
            # one.  os.replace below swaps a link at the final path for
            # the file itself.
            if _make_way(tmp_path, "file") is not None:
                raise IsADirectoryError(errno.EISDIR, "a folder is in the way",
                                        str(tmp_path))
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as f:
                for blob_id in content_ids:
                    try:
                        chunk = self._read_blob(blob_id)
                    except (IntegrityError, KeyError, OSError) as exc:
                        if self._strict:
                            raise
                        self._record_failure(path, blob_id, exc)
                        raise _NodeFailed from exc
                    f.write(chunk)
                    bytes_written += len(chunk)
            self._apply_metadata(node, tmp_path)
            os.replace(tmp_path, path)
        except (_NodeFailed, RestoreAbortedError):
            tmp_path.unlink(missing_ok=True)
            raise
        except (IntegrityError, KeyError, OSError) as exc:
            # Failure outside the per-blob guard above (e.g. _apply_metadata
            # or os.replace) — still record + clean up under tolerant mode.
            tmp_path.unlink(missing_ok=True)
            if isinstance(exc, OSError) and exc.errno in _DISK_FULL_ERRNOS:
                # #658: stop the whole restore (removing the partial file
                # above gave its space back).
                raise _disk_full_abort(path, exc) from exc
            if self._strict:
                raise
            self._record_failure(path, "", exc)
            raise _NodeFailed from exc

        # Update progress counters and emit a status line if either
        # the file-count or byte-count threshold has been reached.
        self._progress_done_files += 1
        self._progress_done_bytes += bytes_written
        self._emit_progress()

    def _node_fs_name(self, stored: object, target_dir: Path) -> str | None:
        """The file-system name for a node whose stored name is *stored*,
        or ``None`` (after recording a failure) when it is refused.

        On POSIX the decoded bytes go through ``os.fsdecode``, so a name
        that is not valid UTF-8 is created with exactly its bytes.
        """
        shown = _visible(stored) if isinstance(stored, str) else repr(stored)
        why: str | None
        raw = b""
        if not isinstance(stored, str):
            why = "the name is too long or badly stored"
        else:
            try:
                raw = unescape_node_name(stored)
            except NameDecodeError:
                why = "the name cannot be decoded"
            else:
                why = node_name_refusal(
                    raw, windows=_WINDOWS_NAMES, macos=_MACOS_NAMES
                )
        if why is None:
            return os.fsdecode(raw)
        where = f"{_visible(str(target_dir))}/{shown}"
        if self._strict:
            raise IntegrityError(f"NOT RESTORED: {where} ({why})")
        self._not_restored(where, f"name refused: {why}")
        return None

    def _not_restored(
        self, path: Path | str, reason: str, kind: str = _NR_FAILED
    ) -> None:
        """#783: one item not restored: a "NOT RESTORED" line (worded as
        tier 1's) and a row in RESTORE_FAILURES.txt.  Only *kind*
        ``_NR_FAILED`` counts as a failure."""
        row = (str(path), "", reason)
        self._rows.append(row)
        if kind == _NR_FAILED:
            self._failures.append(row)
        else:
            self._skipped[kind] += 1
        _log(f"NOT RESTORED: {_visible(str(path))} ({_visible(reason)})")

    def _record_failure(
        self, path: Path, blob_id: str, exc: BaseException
    ) -> None:
        """Record one per-node failure for the manifest + summary.

        A blob that cannot be read gets the reason tier 1 gives it
        (#909: ``pack not found``, ``pack damaged``, ``pack unreadable``
        or ``blob not in index``, then the id); any other error its
        class and message."""
        reason = getattr(exc, _REASON_ATTR, None)
        if not isinstance(reason, str):
            reason = f"{type(exc).__name__}: {exc}"
        row = (str(path), blob_id, reason)
        self._failures.append(row)
        self._rows.append(row)
        _log(f"FAILED (continuing): {path} — {exc}")

    def _count_files(self, tree_id: str) -> int:
        """Pre-pass: count file nodes reachable from *tree_id*.

        Only tree blobs are read (which are typically a small
        fraction of total bytes), so this is cheap even on large
        repositories.  Used to provide N/M-style progress reporting.

        The trees read are kept, up to ``_TREE_CACHE_MAX_BYTES``, so the
        restore pass does not read them (and ask for their disc) again.
        """
        total = 0
        # Iterative DFS to avoid recursion-depth limits on deep trees.
        stack: list[str] = [tree_id]
        visited: set[str] = set()
        while stack:
            tid = stack.pop()
            if tid in visited:
                continue
            visited.add(tid)
            try:
                tree_data = self._read_blob(tid)
                tree_doc = json.loads(tree_data)
            except (IntegrityError, KeyError, OSError, json.JSONDecodeError):
                # Corrupted, missing or unreadable (EIO) tree blob — give
                # up the count; _restore_tree catches the same errors and
                # records the failure.  Missing OSError here aborted the
                # whole restore before the tolerant pass began (#481).
                return total
            if self._tree_cache_bytes + len(tree_data) <= _TREE_CACHE_MAX_BYTES:
                self._tree_cache[tid] = tree_data
                self._tree_cache_bytes += len(tree_data)
            for node in tree_doc.get("nodes", []):
                ntype = node.get("type")
                if ntype == "file":
                    total += 1
                elif ntype == "dir" and "subtree" in node:
                    stack.append(node["subtree"])
        return total

    def missing_packs_by_snapshot(
        self, held_packs: Collection[str],
    ) -> list[tuple[SnapshotMeta, frozenset[str]]]:
        """Issue #657 — the packs each snapshot needs that are not held.

        Walks every snapshot's trees and returns, per snapshot (oldest
        first), the set of pack IDs it references that are not in
        *held_packs*: the packs of its tree blobs and of its files' data
        blobs.  An empty set means the snapshot is fully restorable from
        the held packs.  A tree that cannot be read, or a blob the index
        does not know, counts as a missing pseudo-pack (``"?tree:<id>"``,
        ``"?blob:<id>"``), so a doubtful snapshot is never called complete.

        Trees shared between snapshots (most of them, between two
        backups of the same folders) are read once.  Only tree blobs are
        read, never file data.
        """
        self._ensure_loaded()
        assert self._blob_index is not None
        assert self._snapshots is not None
        held = held_packs if isinstance(held_packs, (set, frozenset)) else set(held_packs)
        empty: frozenset[str] = frozenset()
        memo: dict[str, frozenset[str]] = {}

        def blob_pack_missing(blob_id: str) -> str | None:
            loc = self._blob_index.get(blob_id) if self._blob_index else None
            if loc is None:
                return f"?blob:{blob_id}"
            return None if loc.pack_id in held else loc.pack_id

        def tree_missing(root: str) -> frozenset[str]:
            # Iterative post-order walk: (tree id, children expanded?).
            pending: dict[str, tuple[set[str], list[str]]] = {}
            stack: list[tuple[str, bool]] = [(root, False)]
            while stack:
                tid, expanded = stack.pop()
                if tid in memo:
                    continue
                if not expanded:
                    if tid in pending:
                        continue
                    own: set[str] = set()
                    subtrees: list[str] = []
                    m = blob_pack_missing(tid)
                    if m is not None:
                        own.add(m)
                    try:
                        doc = json.loads(self._read_blob(tid))
                    except (IntegrityError, KeyError, OSError,
                            ValueError) as exc:
                        _log(f"tree {tid[:12]} unreadable: {exc}")
                        memo[tid] = frozenset(own | {f"?tree:{tid}"})
                        continue
                    for node in doc.get("nodes") or []:
                        ntype = node.get("type")
                        if ntype == "dir" and node.get("subtree"):
                            subtrees.append(node["subtree"])
                        elif ntype == "file":
                            for blob_id in node.get("content") or []:
                                m = blob_pack_missing(blob_id)
                                if m is not None:
                                    own.add(m)
                    pending[tid] = (own, subtrees)
                    stack.append((tid, True))
                    for sub in subtrees:
                        if sub not in memo:
                            stack.append((sub, False))
                else:
                    own, subtrees = pending.pop(tid)
                    for sub in subtrees:
                        own |= memo.get(sub, empty)
                    memo[tid] = frozenset(own) if own else empty
            return memo.get(root, empty)

        return [(snap, tree_missing(snap.tree)) for snap in self._snapshots]

    def _emit_progress(self, *, force: bool = False) -> None:
        """Emit a progress line to stderr if a threshold has been hit.

        No-op when ``LCSAS_PROGRESS=0`` is set in the environment.

        Args:
            force: Bypass the threshold check (used for the final
                100% line at end of restore).
        """
        if not self._progress_enabled:
            return

        done_files = self._progress_done_files
        done_bytes = self._progress_done_bytes
        delta_files = done_files - self._progress_last_emit_files
        delta_bytes = done_bytes - self._progress_last_emit_bytes
        if not force and (
            delta_files < _PROGRESS_FILES_INTERVAL
            and delta_bytes < _PROGRESS_BYTES_INTERVAL
        ):
            return

        # Skip the force-emit if no files have been restored yet — no
        # point in printing "0/0 files restored, 0.0 MB" before a
        # restore that has nothing to do.
        if force and done_files == 0:
            return

        total = self._progress_total_files
        mb = done_bytes / (1024 * 1024)
        if total > 0:
            _log(f"Progress: {done_files}/{total} files restored, {mb:.1f} MB.")
        else:
            _log(f"Progress: {done_files} files restored, {mb:.1f} MB.")

        self._progress_last_emit_files = done_files
        self._progress_last_emit_bytes = done_bytes

    def _apply_metadata(
        self, node: dict[str, Any], path: Path, *, is_link: bool = False
    ) -> None:
        """Best-effort metadata: owner (root only), mode, xattrs, times.

        #785: rustic's order, as in tier 1.  A change of owner clears
        setuid/setgid, so the owner goes before the mode; the times go
        last.  Nothing here follows a link (*is_link*: the link's own
        owner, xattrs and times; a link has no mode of its own).
        """
        uid, gid = node.get("uid"), node.get("gid")
        if (
            hasattr(os, "geteuid") and os.geteuid() == 0
            and isinstance(uid, int) and isinstance(gid, int)
            and uid >= 0 and gid >= 0
        ):
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
            except (OSError, NotImplementedError) as exc:
                _log(f"Could not set the owner of {path}: {exc}")

        try:
            if "mode" in node and not is_link and not path.is_symlink():
                os.chmod(path, _mode_from_go(int(node["mode"])))
        except (OSError, TypeError, ValueError) as exc:
            _log(f"Could not set permissions on {path}: {exc}")

        # Extended attributes (restic stores as list of {name, value})
        for xa in node.get("extended_attributes") or []:
            try:
                xa_name = xa.get("name", "")
                xa_value = base64.b64decode(xa.get("value", ""))
                if xa_name and hasattr(os, "setxattr"):
                    os.setxattr(
                        str(path), xa_name, xa_value,
                        follow_symlinks=False,
                    )
            except (OSError, ValueError, AttributeError):
                _log(
                    f"Could not set xattr {xa.get('name')} on {path}"
                )

        try:
            mtime = node.get("mtime")
            atime = node.get("atime")
            if mtime and (not is_link or os.utime in os.supports_follow_symlinks):
                # Parse ISO 8601 nanosecond timestamp → epoch float
                mt = _parse_timestamp(mtime)
                at = _parse_timestamp(atime) if atime else mt
                os.utime(path, (at, mt), follow_symlinks=False)
        except (OSError, ValueError, NotImplementedError) as exc:
            _log(f"Could not set timestamps on {path}: {exc}")

    # ── Diagnostic / Info ────────────────────────────────────────

    def repo_info(self) -> dict[str, Any]:
        """Return basic repository information (config + snapshot count)."""
        self._ensure_loaded()

        config_path = self.repo_path / "config"
        config_doc = {}
        if config_path.is_file():
            import contextlib
            with contextlib.suppress(Exception):
                config_doc = self._read_json_file(config_path)

        assert self._snapshots is not None
        assert self._blob_index is not None
        return {
            "repository_id": config_doc.get("id", "unknown"),
            "version": config_doc.get("version", "unknown"),
            "snapshots": len(self._snapshots),
            "indexed_blobs": len(self._blob_index),
            "has_zstd": _HAS_ZSTD,
        }


# ── Resume (#729) ────────────────────────────────────────────────


def _already_restored(node: dict[str, Any], path: Path) -> bool:
    """Did an earlier run finish this file?  True when a regular file
    (not a link) is at *path* with the node's size and an mtime within
    ``_RESUME_MTIME_SLACK`` seconds of the node's.

    A finished file carries the node's mtime: it is set on the partial
    file before the rename.  The mtime check is what tells this file
    from a same-size file of another snapshot (#661).  A node with no
    usable size or mtime is never skipped.  Tier 1 and rustic skip on
    the same test.
    """
    size = node.get("size")
    mtime = node.get("mtime")
    if (not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not isinstance(mtime, str)):
        return False
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != size:
        return False
    try:
        want = _parse_timestamp(mtime)
    except (ValueError, OverflowError, OSError):
        return False
    return abs(st.st_mtime - want) <= _RESUME_MTIME_SLACK


# ── Utilities ────────────────────────────────────────────────────

# A restic time: date, "T", time, an optional fraction of any length and
# an optional UTC offset ("Z" or +HH:MM).  The offset is searched for in
# the part after the "T" only, as the date has dashes of its own.
_TIMESTAMP_RE = re.compile(
    r"^(?P<base>[^T]+T\d{1,2}:\d{1,2}:\d{1,2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})?$"
)


def _parse_instant(ts: str) -> datetime:
    """Parse a restic-style ISO 8601 timestamp to an aware datetime.

    Keeps the UTC offset (#659): ``2026-01-02T03:04:05.123456789+02:00``
    is the instant 01:04:05.123456 UTC.  The fraction is cut or padded
    to six digits (Python keeps microseconds), so a 3-digit fraction or
    none at all parses too.  A time with no offset is taken as UTC.

    Raises:
        ValueError: *ts* is not such a timestamp.
    """
    m = _TIMESTAMP_RE.match(ts.strip())
    if m is None:
        raise ValueError(f"not an ISO 8601 timestamp: {ts!r}")
    frac = (m.group("frac") or "")[:6].ljust(6, "0")
    tz = m.group("tz")
    if tz is None or tz in ("Z", "z"):
        tz = "+00:00"
    elif ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    # Python 3.10's fromisoformat takes exactly this shape: six fraction
    # digits and a +HH:MM offset (no "Z").  This file is concatenated
    # into ``python_reader.py``, which runs on 3.10.
    norm = f"{m.group('base')}.{frac}{tz}"
    try:
        return datetime.fromisoformat(norm)
    except ValueError:
        # A date or time without zero padding ("2026-1-5T..."), which
        # fromisoformat rejects and strptime accepts.
        return datetime.strptime(norm, "%Y-%m-%dT%H:%M:%S.%f%z")


def _parse_timestamp(ts: str) -> float:
    """Parse a restic-style ISO 8601 timestamp to epoch seconds.

    See ``_parse_instant``; raises ValueError on a malformed time.
    """
    return _parse_instant(ts).timestamp()


def time_sort_key(ts: str) -> tuple[int, float]:
    """Sort key for a snapshot time: its instant, not its text (#663).

    A time that does not parse gives ``(0, 0.0)``, before every real
    time, so a damaged snapshot file is never chosen as "latest".
    Shared with ``lcsas restore from-disc latest`` (#821).
    """
    try:
        return (1, _parse_timestamp(ts))
    except ValueError:
        return (0, 0.0)


def _snapshot_sort_key(snap: SnapshotMeta) -> tuple[int, float]:
    """Sort key for snapshots: :func:`time_sort_key` of ``snap.time``."""
    return time_sort_key(snap.time)


_QUIET = False


@contextlib.contextmanager
def quiet_log() -> Iterator[None]:
    """Silence :func:`_log` while the reader is used as a library.

    ``lcsas stage`` walks snapshot trees with this reader (#657); its
    progress lines ("Master key decrypted", "Loaded index") mean nothing
    to someone staging discs.
    """
    global _QUIET
    saved = _QUIET
    _QUIET = True
    try:
        yield
    finally:
        _QUIET = saved


def _log(msg: str) -> None:
    """Simple stderr logger for fallback restore progress."""
    if _QUIET:
        return
    print(f"[restic-fallback] {msg}", file=sys.stderr, flush=True)
