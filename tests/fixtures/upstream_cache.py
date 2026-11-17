"""Synthetic upstream cache for meta-builder tests (#832).

``lcsas meta build`` unpacks the archives pinned in
``recovery/UPSTREAM.sha256`` from the recovery cache.  ``add_upstream``
builds small fake archives, pins them in a test ``UPSTREAM.sha256`` and,
like ``recovery/scripts/fetch_upstream.sh``, unpacks a copy beside each
archive.  Pass the same ``recovery_dir`` to ``MetaVolumeBuilder``.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from lcsas.meta.required_contents import MUSL_LOADER_ARCH
from lcsas.meta.upstream import PYTHON_TRIPLE_OVERRIDE

WINDOWS = "x86_64-pc-windows-gnu"


def _tar_gz(members: list[tuple[str, bytes | str | None, int]]) -> bytes:
    """members: (name, bytes for a file / str for a symlink target /
    None for a directory, mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = 1_700_000_000
            if body is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(body, str):
                info.type = tarfile.SYMTYPE
                info.linkname = body
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _pin(recovery_dir: Path, rel: str, data: bytes) -> None:
    manifest = recovery_dir / "UPSTREAM.sha256"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    old = manifest.read_text() if manifest.is_file() else ""
    # One pin per category and target: replace any earlier one (a copy of
    # the real recovery/ tree carries the real pins).
    folder = rel.rsplit("/", 1)[0] + "/"
    rows = [ln for ln in old.splitlines() if not ln.split("  ")[-1].startswith(folder)]
    rows.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
    manifest.write_text("\n".join(rows) + "\n")


def _store(cache_root: Path, recovery_dir: Path, rel: str, data: bytes) -> Path:
    path = cache_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _pin(recovery_dir, rel, data)
    return path.parent


def _unpack_tar(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python < 3.11.4: the fixture's own archives are trusted
            tar.extractall(dest)  # noqa: S202


def add_upstream(
    cache_root: Path,
    recovery_dir: Path,
    target: str,
    *,
    rustic: bool = True,
    restic: bool = False,
    python: bool = True,
    musl: bool = False,
    python_flat: bool = False,
    extract: bool = True,
    rustic_body: bytes = b"#!/bin/sh\necho fake rustic\n",
    python_extra: tuple[tuple[str, bytes | str | None, int], ...] = (),
) -> None:
    """Pin and cache fake upstream archives for one disc *target*."""
    win = target == WINDOWS
    if rustic:
        exe = "rustic.exe" if win else "rustic"
        data = _tar_gz([
            ("config", None, 0o755),
            ("config/rustic.toml", b"# fake\n", 0o644),
            ("README.md", b"fake readme\n", 0o644),
            (exe, rustic_body, 0o755),
        ])
        d = _store(cache_root, recovery_dir,
                   f"rustic/{target}/rustic-v0.0.0-{target}.tar.gz", data)
        if extract:
            _unpack_tar(data, d)
    if restic:
        body = b"MZ fake restic" if win else b"\x7fELF fake restic"
        if win:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("restic_0.0.0_windows_amd64.exe", body)
            data, name, out = buf.getvalue(), "restic_0.0.0_windows_amd64.zip", "restic.exe"
        else:
            data, name, out = bz2.compress(body), "restic_0.0.0_linux.bz2", "restic"
        d = _store(cache_root, recovery_dir, f"restic/{target}/{name}", data)
        if extract:
            (d / out).write_bytes(body)
    if python:
        py_target = PYTHON_TRIPLE_OVERRIDE.get(target, target)
        if win:
            files: list[tuple[str, bytes | str | None, int]] = [
                ("python.exe", b"MZ fake-cpython", 0o755),
                ("Lib", None, 0o755),
                ("Lib/os.py", b"# stdlib\n", 0o644),
            ]
        else:
            files = [
                ("bin", None, 0o755),
                ("bin/python3", b"#!/bin/sh\necho fake python3\n", 0o755),
                ("bin/python", "python3", 0o777),
                ("lib", None, 0o755),
                ("lib/os.py", b"# stdlib\n", 0o644),
            ]
        files = files + list(python_extra)
        if not python_flat:
            files = [("python", None, 0o755)] + [
                (f"python/{n}", b, m) for n, b, m in files
            ]
        data = _tar_gz(files)
        d = _store(cache_root, recovery_dir,
                   f"python/{py_target}/cpython-0.0.0-{py_target}.tar.gz", data)
        if extract:
            _unpack_tar(data, d)
    if musl:
        arch = MUSL_LOADER_ARCH[target]
        loader = f"ld-musl-{arch}.so.1"
        data = _tar_gz([
            ("./lib", None, 0o755),
            (f"./lib/{loader}", b"\x7fELF fake " + arch.encode(), 0o755),
            ("./bin", None, 0o755),
            ("./bin/busybox", b"\x7fELF busybox", 0o755),
        ])
        d = _store(cache_root, recovery_dir,
                   f"musl/{target}/alpine-minirootfs-0.0.0-{arch}.tar.gz", data)
        if extract:
            (d / loader).write_bytes(b"\x7fELF fake " + arch.encode())
