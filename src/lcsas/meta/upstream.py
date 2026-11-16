"""Pinned upstream archives: read the pins, verify, and unpack them (#832).

``recovery/UPSTREAM.sha256`` pins one archive per upstream tool and target
(rustic, restic, CPython, musl's loader).  ``fetch_upstream.sh`` downloads
each one into the recovery cache and also unpacks it beside the archive.
The meta builder must not trust that unpacked copy: nothing ties it to the
pinned archive.  So this module reads the archive bytes once, checks them
against the pin, and unpacks the payload from those same bytes.  The meta
builder writes the payload onto the disc, and ``lcsas meta verify
--strict`` compares a built disc with it.

Payload paths are relative to the disc's ``recovery/bin/<target>/``:

    rustic   ->  rustic-static[.exe]
    restic   ->  restic[.exe]
    python   ->  python/...           (the CPython install tree)
    musl     ->  python/lib/ld-musl-<arch>.so.1

Each payload entry also names where ``fetch_upstream.sh`` unpacks the same
file in the cache, so the builder can refuse a cache whose unpacked copy
was changed.
"""

from __future__ import annotations

import bz2
import io
import os
import posixpath
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from lcsas.utils.hashing import sha256_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator

# Categories whose archive is unpacked onto the disc.  (dvdisaster/src is
# pinned too, but its archive itself is bundled, not unpacked.)
UNPACKED_CATEGORIES = ("rustic", "restic", "python", "musl")

# python-build-standalone publishes Windows CPython only for the *-msvc
# triple, while rustic/restic ship *-gnu.  The disc keys everything by the
# rustic triple, so the Windows python pin is looked up under msvc (#404).
PYTHON_TRIPLE_OVERRIDE = {
    "x86_64-pc-windows-gnu": "x86_64-pc-windows-msvc",
}


class UpstreamError(Exception):
    """A pinned upstream archive is corrupt, altered or malformed."""


@dataclass(frozen=True)
class UpstreamPin:
    """One row of recovery/UPSTREAM.sha256."""

    sha256: str
    category: str
    target: str
    filename: str

    @property
    def relpath(self) -> str:
        return f"{self.category}/{self.target}/{self.filename}"


@dataclass(frozen=True)
class PayloadEntry:
    """One file, directory or symlink a pinned archive puts on the disc."""

    # Relative to recovery/bin/<target>/ on the disc.
    disc_rel: PurePosixPath
    kind: str  # "file", "dir" or "symlink"
    data: bytes = b""
    linkname: str = ""
    mode: int = 0o644
    mtime: float | None = None
    # Where fetch_upstream.sh unpacks the same file, relative to the
    # cache's <category>/<cache target>/ folder.  None: not unpacked there.
    cache_rel: PurePosixPath | None = None


def default_cache_root() -> Path:
    """The recovery cache: $LCSAS_RECOVERY_CACHE, or the per-user default."""
    env = os.environ.get("LCSAS_RECOVERY_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "lcsas" / "recovery-binaries"


def read_upstream_pins(manifest: Path) -> list[UpstreamPin]:
    """Parse UPSTREAM.sha256.  Empty when the file is absent."""
    if not manifest.is_file():
        return []
    pins: list[UpstreamPin] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        rel = parts[-1].split("/")
        if len(rel) != 3:
            continue
        pins.append(UpstreamPin(parts[0].lower(), rel[0], rel[1], rel[2]))
    return pins


def pin_for(
    pins: list[UpstreamPin], category: str, disc_target: str
) -> UpstreamPin | None:
    """The pin of *category* that serves the disc's *disc_target*."""
    want = disc_target
    if category == "python":
        want = PYTHON_TRIPLE_OVERRIDE.get(disc_target, disc_target)
    for pin in pins:
        if pin.category == category and pin.target == want:
            return pin
    return None


def read_verified_archive(cache_root: Path, pin: UpstreamPin) -> bytes | None:
    """Return the archive's bytes, checked against the pin.

    None when the archive is not in the cache (a partial cache is a
    legitimate single-arch build).  Raises UpstreamError when the bytes do
    not match the pin.  The caller unpacks these same bytes, so nothing can
    change between the check and the use.
    """
    path = cache_root / pin.relpath
    if not path.is_file():
        return None
    data = path.read_bytes()
    got = sha256_bytes(data)
    if got != pin.sha256:
        raise UpstreamError(
            f"{pin.relpath}: expected {pin.sha256}, got {got}"
        )
    return data


def _member_path(name: str) -> PurePosixPath:
    """A tar/zip member name as a safe relative path, or UpstreamError."""
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm in ("", "."):
        return PurePosixPath(".")
    rel = PurePosixPath(norm)
    if rel.is_absolute() or ".." in rel.parts:
        raise UpstreamError(f"unsafe member path in archive: {name!r}")
    return rel


def _safe_symlink(rel: PurePosixPath, linkname: str) -> str:
    """Refuse a symlink that is absolute or points outside its tree."""
    if posixpath.isabs(linkname):
        raise UpstreamError(f"absolute symlink in archive: {rel} -> {linkname}")
    resolved = posixpath.normpath(posixpath.join(str(rel.parent), linkname))
    if resolved == ".." or resolved.startswith("../"):
        raise UpstreamError(
            f"symlink leaves the archive tree: {rel} -> {linkname}"
        )
    return linkname


def _mode(member_mode: int) -> int:
    return 0o755 if member_mode & 0o111 else 0o644


def _rustic_payload(data: bytes) -> list[PayloadEntry]:
    # The rustic binary sits at the top of the tarball, or one folder
    # down (fetch_upstream.sh flattens that case); README, config/ and
    # docs/ are left out, as before.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            rel = _member_path(member.name)
            if rel.name not in ("rustic", "rustic.exe") or len(rel.parts) > 2:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            suffix = ".exe" if rel.name.endswith(".exe") else ""
            return [PayloadEntry(
                disc_rel=PurePosixPath(f"rustic-static{suffix}"),
                kind="file", data=fh.read(), mode=0o755,
                mtime=member.mtime, cache_rel=PurePosixPath(rel.name),
            )]
    raise UpstreamError("no rustic binary in the rustic archive")


def _restic_payload(filename: str, data: bytes) -> list[PayloadEntry]:
    if filename.endswith(".bz2"):
        try:
            body = bz2.decompress(data)
        except (OSError, ValueError) as exc:
            raise UpstreamError(f"cannot decompress {filename}: {exc}") from exc
        name = "restic"
    elif filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = [i for i in zf.infolist() if not i.is_dir()]
            if len(files) != 1:
                raise UpstreamError(
                    f"{filename}: expected one file, found {len(files)}"
                )
            body = zf.read(files[0])
        name = "restic.exe"
    else:
        raise UpstreamError(f"unknown restic archive type: {filename}")
    return [PayloadEntry(
        disc_rel=PurePosixPath(name), kind="file", data=body, mode=0o755,
        cache_rel=PurePosixPath(name),
    )]


def _python_payload(data: bytes) -> list[PayloadEntry]:
    # python-build-standalone tarballs hold one "python/" tree.  An archive
    # without that prefix is a flat install: all of it goes under python/.
    entries: list[PayloadEntry] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members = [(m, _member_path(m.name)) for m in tar.getmembers()]
        prefixed = all(
            rel.parts[:1] == ("python",) for _m, rel in members
            if rel != PurePosixPath(".")
        )
        for member, rel in members:
            if rel == PurePosixPath("."):
                continue
            inner = PurePosixPath(*rel.parts[1:]) if prefixed else rel
            if inner == PurePosixPath("."):
                continue
            disc_rel = PurePosixPath("python") / inner
            cache_rel = PurePosixPath("python") / inner if prefixed else inner
            if member.isdir():
                entries.append(PayloadEntry(disc_rel, "dir", mode=0o755))
            elif member.issym():
                entries.append(PayloadEntry(
                    disc_rel, "symlink",
                    linkname=_safe_symlink(inner, member.linkname),
                    cache_rel=cache_rel,
                ))
            elif member.isfile() or member.islnk():
                fh = tar.extractfile(member)
                if fh is None:
                    raise UpstreamError(f"cannot read {member.name}")
                entries.append(PayloadEntry(
                    disc_rel, "file", data=fh.read(),
                    mode=_mode(member.mode), mtime=member.mtime,
                    cache_rel=cache_rel,
                ))
            else:
                raise UpstreamError(
                    f"unsupported member type in python archive: {member.name}"
                )
    if not entries:
        raise UpstreamError("the python archive is empty")
    return entries


def _musl_payload(loader_name: str, data: bytes) -> list[PayloadEntry]:
    # Only musl's dynamic loader is wanted from the Alpine minirootfs.
    want = PurePosixPath("lib") / loader_name
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar:
            if not member.isfile() or _member_path(member.name) != want:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                break
            return [PayloadEntry(
                disc_rel=PurePosixPath("python") / "lib" / loader_name,
                kind="file", data=fh.read(), mode=0o755, mtime=member.mtime,
                cache_rel=PurePosixPath(loader_name),
            )]
    raise UpstreamError(f"no lib/{loader_name} in the musl archive")


def payload(
    pin: UpstreamPin, data: bytes, *, musl_loader: str | None = None
) -> list[PayloadEntry]:
    """What the pinned archive *data* puts under recovery/bin/<target>/.

    *data* must already be checked against *pin*
    (``read_verified_archive``).  ``musl_loader`` names the loader to take
    from a musl archive.
    """
    try:
        if pin.category == "rustic":
            return _rustic_payload(data)
        if pin.category == "restic":
            return _restic_payload(pin.filename, data)
        if pin.category == "python":
            return _python_payload(data)
        if pin.category == "musl":
            if musl_loader is None:
                raise UpstreamError("no musl loader name for this target")
            return _musl_payload(musl_loader, data)
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise UpstreamError(f"{pin.relpath}: cannot read archive: {exc}") from exc
    raise UpstreamError(f"{pin.relpath}: category is not unpacked")


def write_payload(entries: list[PayloadEntry], dest: Path) -> None:
    """Write *entries* under *dest* (recovery/bin/<target>/ on the disc)."""
    for entry in entries:
        path = dest / entry.disc_rel
        if entry.kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        if entry.kind == "symlink":
            os.symlink(entry.linkname, path)
            continue
        path.write_bytes(entry.data)
        os.chmod(path, entry.mode)
        if entry.mtime is not None:
            os.utime(path, (entry.mtime, entry.mtime))


def stale_cache_copies(
    entries: list[PayloadEntry], cache_dir: Path
) -> list[str]:
    """Unpacked cache files that differ from the pinned archive.

    *cache_dir* is the cache's <category>/<cache target>/ folder.  A file
    the cache does not hold is fine (``fetch_upstream.sh --no-extract``);
    extra files in the cache are ignored, since nothing reads them.
    Compiled ``__pycache__`` files are skipped too: running the cached
    CPython (the tier-3 and wine tests do) rewrites them, and the disc
    gets the archive's copy either way.
    """
    bad: list[str] = []
    for entry in entries:
        if entry.cache_rel is None or entry.kind == "dir":
            continue
        if "__pycache__" in entry.cache_rel.parts:
            continue
        path = cache_dir / entry.cache_rel
        if entry.kind == "symlink":
            if path.is_symlink() and os.readlink(path) != entry.linkname:
                bad.append(str(path))
            continue
        if not path.is_file():
            continue
        if path.read_bytes() != entry.data:
            bad.append(str(path))
    return bad


def iter_payload_mismatches(
    entries: list[PayloadEntry], bin_dir: Path
) -> Iterator[str]:
    """Disc paths under *bin_dir* that do not match the archive payload."""
    for entry in entries:
        path = bin_dir / entry.disc_rel
        if entry.kind == "dir":
            continue
        if entry.kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry.linkname:
                yield entry.disc_rel.as_posix()
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or path.read_bytes() != entry.data
        ):
            yield entry.disc_rel.as_posix()


@dataclass
class BundledCheck:
    """What ``check_bundled_upstream`` found on a built disc."""

    checked: int = 0
    problems: list[str] = field(default_factory=list)
    # Pinned archives a bundled file comes from, absent from the cache.
    unchecked: list[str] = field(default_factory=list)


def _owned(category: str, path: PurePosixPath, loader: str | None) -> bool:
    """Does *path* (relative to bin/<target>/) come from *category*?"""
    name = path.as_posix()
    if category == "rustic":
        return name in ("rustic-static", "rustic-static.exe")
    if category == "restic":
        return name in ("restic", "restic.exe")
    if category == "musl":
        return loader is not None and name == f"python/lib/{loader}"
    # python: the whole python/ tree except the musl loader.
    return path.parts[:1] == ("python",) and not _owned("musl", path, loader)


def check_bundled_upstream(
    recovery_root: Path, cache_root: Path, targets: tuple[str, ...]
) -> BundledCheck:
    """Compare a built disc's upstream binaries with the pinned archives.

    For every target folder under ``recovery_root/bin/``, each file that
    came from an upstream archive (rustic-static, restic, the python tree,
    the musl loader) must match what the archive pinned in the disc's
    ``recovery/UPSTREAM.sha256`` unpacks to, and the python tree must hold
    nothing else.  Archives absent from *cache_root* are listed in
    ``unchecked``: a mounted disc carries no archives, so this check runs
    where they were fetched.
    """
    from lcsas.meta.required_contents import musl_loader_name

    result = BundledCheck()
    pin_file = recovery_root / "UPSTREAM.sha256"
    if not pin_file.is_file():
        # The manifest check reports the missing pin file itself (it is a
        # tracked file); nothing here can be checked without it.
        if any((recovery_root / "bin" / t).is_dir() for t in targets):
            result.unchecked.append("recovery/UPSTREAM.sha256 (absent)")
        return result
    pins = read_upstream_pins(pin_file)
    for target in targets:
        bin_dir = recovery_root / "bin" / target
        if not bin_dir.is_dir():
            continue
        loader = musl_loader_name(target)
        on_disc = [
            PurePosixPath(p.relative_to(bin_dir).as_posix())
            for p in sorted(bin_dir.rglob("*"))
            if p.is_file() or p.is_symlink()
        ]
        for category in UNPACKED_CATEGORIES:
            owned = [p for p in on_disc if _owned(category, p, loader)]
            if not owned:
                continue
            where = f"bin/{target}"
            pin = pin_for(pins, category, target)
            if pin is None:
                result.problems.extend(
                    f"{where}/{p}: no pinned {category} archive in "
                    "recovery/UPSTREAM.sha256" for p in owned
                )
                continue
            try:
                data = read_verified_archive(cache_root, pin)
                if data is None:
                    result.unchecked.append(pin.relpath)
                    continue
                entries = payload(pin, data, musl_loader=loader)
            except UpstreamError as exc:
                result.problems.append(f"{pin.relpath}: {exc}")
                continue
            for rel in iter_payload_mismatches(entries, bin_dir):
                result.problems.append(
                    f"{where}/{rel}: differs from {pin.relpath}"
                )
            expected = {e.disc_rel for e in entries if e.kind != "dir"}
            for rel_path in owned:
                if rel_path not in expected:
                    result.problems.append(
                        f"{where}/{rel_path}: not in {pin.relpath}"
                    )
            result.checked += len(expected)
    return result
