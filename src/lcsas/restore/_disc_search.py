"""Tier 3, the Python reader: the disc search (#884).

Finds the bytes of a pack in the pack cache or on any reachable data
disc, and asks for a disc when none has it.  ``PurePythonRestorer``
(``restic_fallback.py``) holds one search and reads packs only through
it.

This file is inlined into the generated ``standalone_restorer.py``
ahead of ``restic_fallback.py`` (see ``standalone_builder.py``), so it
imports nothing but the standard library, compiles under Python 3.10,
and defines no top-level name that another inlined file defines.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

# The pack cache (#602), as in tier 1 (disc_locator.c, drain_disc).
# When a pack is found on a data disc, every pack of this repository on
# that disc is copied into <cache>/data/<XX>/<hex>, so the disc is not
# asked for again.  A disc is copied only when the cache's drive keeps a
# margin free afterwards: 5% of the drive, at most 1 GiB.
_MIB = 1024 * 1024
_DRAIN_MARGIN_MAX = 1024 * _MIB
#: Progress lines while a disc is copied: one every 1/20 of the copy,
#: but no more often than every 16 MiB and no less than every 256 MiB.
_DRAIN_STEP_MIN = 16 * _MIB
_DRAIN_STEP_MAX = 256 * _MIB
#: A one-time note when the cache holds more than this.
_DRAIN_BIG_CACHE = 1024 * _MIB

#: Exit code for "the reader typed q at the disc prompt (or its input
#: ended there)".  The same as tier 1's EXIT_READER_QUIT: sysexits.h
#: EX_TEMPFAIL, "try again later".
EXIT_READER_QUIT = 75


class RestoreAbortedError(Exception):
    """The whole restore must stop now, not just the current file (#658).

    Raised when the reader types ``q`` at the disc prompt, when input
    ends there, and when the target filesystem is full.  Deliberately
    NOT an ``OSError`` (nor an ``IntegrityError``): the tolerant
    per-file handlers catch those and go on with the next file, which
    would ask for the same disc again, or fail every remaining file in
    turn on a full disk.

    ``exit_code`` is what the tier-3 command exits with:
    ``EXIT_READER_QUIT`` (75) for a stop at the disc prompt,
    ``EXIT_DISK_FULL`` (74) for a full disk.
    """

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


#: "No space left" and "quota exceeded" (EDQUOT is absent on Windows).
_DISK_FULL_ERRNOS = frozenset(
    {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
)
#: Errors copying into the pack cache that mean the cache's drive
#: cannot take more (#602): stop copying that disc.
_CACHE_SIDE_ERRNOS = _DISK_FULL_ERRNOS | {errno.EROFS}


def _visible(text: str) -> str:
    """*text* with control characters, DEL and lone surrogates shown as
    escapes, so a name cannot move the cursor or split a line of the
    log or of RESTORE_FAILURES.txt (tier 1 prints them the same way)."""
    out = []
    for ch in text:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        elif 0xDC80 <= o <= 0xDCFF:  # a raw byte kept by os.fsdecode
            out.append(f"\\x{o - 0xDC00:02x}")
        elif 0xD800 <= o <= 0xDFFF:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


# ── Prompt streams ───────────────────────────────────────────────


def _ds_read_line() -> str:
    """One line from stdin (looked up on each call, so a replaced
    ``sys.stdin`` is the one read)."""
    return sys.stdin.readline()


def _ds_write(text: str) -> None:
    """*text* to stderr, flushed: the disc prompt ends in "> " with no
    newline, and must show before the reader is waited for."""
    sys.stderr.write(text)
    sys.stderr.flush()


# ── The disc search ──────────────────────────────────────────────


class DiscSearch:
    """Where tier 3 finds a pack: the pack cache, each search root, the
    discs mounted one level below a root, and the disc prompt.

    The interface is three calls: ``read_pack_range`` (the bytes of a
    pack), ``set_index_packs`` (the packs the pack cache may copy) and
    ``close`` (remove the catalog copies).  Tier 1 does the same job in
    ``recovery/src/lcsas-restore/disc_locator.c``.
    """

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        meta_disc: Path | None = None,
        pack_cache: Path | None = None,
        catalog_path: Path | None = None,
        interactive: bool = False,
        repo_data: Path,
        log: Callable[[str], None],
        prompt_in: Callable[[], str] | None = None,
        prompt_out: Callable[[str], None] | None = None,
    ) -> None:
        """Set up the search.

        Args:
            roots: The folders to search for packs, in order (each may
                hold ``data/<XX>/<hex>``, ``data/<hex>``, ``<XX>/<hex>``
                or ``<hex>``).  A disc mounted one level below a root is
                searched too.
            meta_disc: Mount point of the meta disc.  While it is
                mounted, nothing on it is searched.
            pack_cache: The pack cache folder (#602), created if
                missing; ``None``: no cache.  A folder that cannot be
                made turns the cache off, with a log line.
            catalog_path: An explicit disc catalog (``catalog.db``) for
                the prompt.  ``None``: one is looked for on the roots at
                each prompt.
            interactive: Prompt for a disc when a pack is not found
                (else ``read_pack_range`` raises FileNotFoundError).
            repo_data: The repository's own ``data/`` folder: never
                copied into the cache, never listed for discs.
            log: Where progress and warnings go (one line each).
            prompt_in: Reads one line of the reader's answer at the
                prompt ("" at the end of input).  Default: stdin.
            prompt_out: Writes the prompt and the lines around it.
                Default: stderr, flushed.
        """
        self.interactive = interactive
        self.catalog_path = catalog_path
        self._repo_data = repo_data
        self._log = log
        self._prompt_in = prompt_in if prompt_in is not None else _ds_read_line
        self._prompt_out = prompt_out if prompt_out is not None else _ds_write
        # The roots, in search order.
        self._pack_search_paths: list[Path] = list(roots)

        # #635: local copies of every disc catalog seen so far, keyed by
        # the SHA-256 of its bytes.  The catalog on disc N lists only
        # discs 1..N, and it is gone once that disc is swapped out, so
        # the prompt combines all the copies and keeps naming discs
        # after a swap.  _newest_known_label is the newest disc any of
        # them lists (set by _lookup_volume_labels for the prompt).
        self._catalog_copies: dict[str, Path] = {}
        self._catalog_copy_dir: Path | None = None
        self._newest_known_label: str | None = None

        # Discs found one level below a root (refreshed on a miss), and
        # the meta disc, which is skipped while it is mounted.
        self._discovered: list[Path] = []
        # Pack copies that could not be opened or read (a folder only root
        # may read, a damaged disc): never chosen again this run, so the
        # search goes on to another copy or to the disc prompt.
        self._unreadable: set[str] = set()
        self._meta_disc = meta_disc if meta_disc and str(meta_disc) else None

        # #602: the pack cache (None: off), this repository's pack ids
        # (only those are copied), and the discs not copied for want of
        # room, so they are not listed again for every pack.
        self._pack_cache: Path | None = None
        self._index_packs: frozenset[str] | None = None
        self._drain_refused: set[tuple[str, int, int, int]] = set()
        # Packs that could not be copied (read error, wrong bytes): not
        # tried again, so a scratched pack is not re-read for each blob.
        self._drain_failed: set[str] = set()
        self._drain_warned: set[str] = set()
        if pack_cache is not None and str(pack_cache):
            try:
                pack_cache.mkdir(parents=True, exist_ok=True)
                self._pack_cache = pack_cache
            except OSError as exc:
                self._log(f"Cannot create the pack cache {pack_cache}: {exc}; "
                          "the discs are read directly.")

    # ── Interface ────────────────────────────────────────────────

    def set_index_packs(self, ids: Iterable[str]) -> None:
        """The pack ids this repository's index names: the only packs
        the pack cache copies off a disc."""
        self._index_packs = frozenset(ids)

    def close(self) -> None:
        """Remove the local catalog copies made for the prompt (#635).

        Safe to call more than once.  A copy folder left behind (a run
        that never calls this) is removed when the process exits.
        """
        if self._catalog_copy_dir is not None:
            shutil.rmtree(self._catalog_copy_dir, ignore_errors=True)
        self._catalog_copy_dir = None
        self._catalog_copies = {}

    # ── Search ───────────────────────────────────────────────────

    # Max prompt iterations before giving up (bounded loop -- if the
    # heir can't get the right disc into the drive after this many
    # tries, something else is wrong and the restore should fail loudly).
    _DISC_SWAP_MAX_RETRIES = 50

    def _scan_pack_search_paths(self, pack_id: str) -> Path | None:
        """Search every configured root for the named pack.

        Each root is probed for both the LCSAS data-disc ``data/<XX>/<hex>``
        / ``data/<hex>`` layout AND the legacy "root IS the data dir"
        layout (``<XX>/<hex>`` / ``<hex>``).  Returns the first hit or None.

        With a pack cache (#602) the cache is searched first, and a hit
        on a disc copies that disc's packs into the cache
        (``_drain_disc``); the cached copy is returned then.

        A disc mounted one level below a root (``/media/<user>/<LABEL>``,
        ``/Volumes/<LABEL>``) is searched too, as tier 1 walks its
        mount parents: the folders found last time first, then the
        roots are listed again for a disc mounted since.  Nothing on
        the meta disc is used while it is mounted.
        """
        cached = self._cached_pack(pack_id)
        if cached is not None:
            return cached
        hit = self._probe_roots(self._live_roots(), pack_id)
        if hit is None and self._discovered:
            hit = self._probe_roots(self._discovered, pack_id)
        if hit is None:
            known = set(self._discovered)
            self._refresh_discovered()
            fresh = [d for d in self._discovered if d not in known]
            if fresh:
                hit = self._probe_roots(fresh, pack_id)
        return hit

    def _probe_roots(self, roots: list[Path], pack_id: str) -> Path | None:
        """The first copy of *pack_id* under *roots* (see
        ``_scan_pack_search_paths``), copying its disc into the cache."""
        prefix = pack_id[:2]
        for root in roots:
            # Legacy default (root = repo_path/"data"): no nested "data/".
            for candidate, data_dir in (
                (root / prefix / pack_id, root),
                (root / pack_id, root),
                (root / "data" / prefix / pack_id, root / "data"),
                (root / "data" / pack_id, root / "data"),
            ):
                if _is_file(candidate):
                    if self._on_live_meta(candidate):
                        continue
                    # A copy this user cannot read (a folder only root may
                    # read, say) is not a copy: look on, and ask for the
                    # disc if none is found, as tier 1 does.
                    if str(candidate) in self._unreadable:
                        continue
                    if not os.access(candidate, os.R_OK):
                        continue
                    if self._pack_cache is not None:
                        self._drain_disc(data_dir)
                        cached = self._cached_pack(pack_id)
                        if cached is not None:
                            return cached
                    return candidate
        return None

    # ── Mount folders and the meta disc ──────────────────────────

    def _meta_is_live(self) -> bool:
        """Is the meta disc still mounted where we were told?  It is
        while ``recovery/scripts/restore.sh`` is there, which no data
        disc carries (tier 1: meta_disc_is_live).  Once it is ejected a
        data disc may be mounted at the same place."""
        meta = self._meta_disc
        if meta is None:
            return False
        return _is_file(meta / "recovery" / "scripts" / "restore.sh")

    def _on_live_meta(self, path: Path) -> bool:
        """Is *path* on the meta disc, while it is mounted?  (tier 1:
        path_under, by the path's text.)"""
        if self._meta_disc is None or not self._meta_is_live():
            return False
        meta = os.path.abspath(self._meta_disc).rstrip(os.sep) or os.sep
        here = os.path.abspath(path)
        return here == meta or here.startswith(meta.rstrip(os.sep) + os.sep)

    def _live_roots(self) -> list[Path]:
        """The configured roots, less any on the mounted meta disc."""
        return [r for r in self._pack_search_paths if not self._on_live_meta(r)]

    def _all_roots(self) -> list[Path]:
        """The configured roots and the discs found one level below
        them, less anything on the mounted meta disc."""
        return self._live_roots() + [
            d for d in self._discovered if not self._on_live_meta(d)
        ]

    def _refresh_discovered(self) -> None:
        """List each root's folders again: a folder there may be a disc
        mounted under a mount folder (tier 1: refresh_discovered, which
        lists each mount parent one level deep).

        A root that is itself a disc (it has ``data/``), the
        repository's own data folder, the pack cache, folders named
        like a pack folder (two hex digits) and the meta disc are not
        listed.  Folders already among the roots are not added twice.
        """
        roots = self._live_roots()
        have = {os.path.abspath(r) for r in roots}
        skip = {os.path.abspath(self._repo_data)}
        if self._pack_cache is not None:
            skip.add(os.path.abspath(self._pack_cache))
        found: list[Path] = []
        for root in roots:
            if os.path.abspath(root) in skip or _is_dir(root / "data"):
                continue
            try:
                names = sorted(os.listdir(root))
            except OSError:
                continue
            for name in names:
                # Hidden folders are never a mounted disc.
                if name.startswith("."):
                    continue
                if len(name) == 2 and all(c in "0123456789abcdef" for c in name):
                    continue
                child = root / name
                key = os.path.abspath(child)
                if key in have or key in skip:
                    continue
                if not _is_dir(child) or self._on_live_meta(child):
                    continue
                have.add(key)
                found.append(child)
        self._discovered = found

    # ── Pack cache (#602) ────────────────────────────────────────

    def _cached_pack(self, pack_id: str) -> Path | None:
        """The cache's copy of *pack_id*, or None (no cache, no copy)."""
        if self._pack_cache is None:
            return None
        path = self._pack_cache / "data" / pack_id[:2] / pack_id
        if str(path) in self._unreadable:
            return None
        return path if _is_file(path) else None

    def _drain_visible_discs(self) -> None:
        """Copy every data disc now in a search root into the cache.

        Run after each ENTER at the disc prompt, so a disc is copied
        the first time it is seen even if it is not the one asked for
        (tier 1: visit_root).  No-op without a cache.
        """
        if self._pack_cache is None:
            return
        self._refresh_discovered()
        for root in self._all_roots():
            data_dir = root / "data"
            if _is_dir(data_dir):
                self._drain_disc(data_dir)

    def _is_not_a_disc(self, data_dir: Path) -> bool:
        """True for the repository's own data folder and for the cache
        (or a folder inside it): those are never copied."""
        assert self._pack_cache is not None
        try:
            real = os.path.realpath(data_dir)
            cache = os.path.realpath(self._pack_cache)
            own = os.path.realpath(self._repo_data)
        except (OSError, ValueError):
            return True
        if real == own:
            return True
        return real == cache or real.startswith(cache.rstrip(os.sep) + os.sep)

    def _repo_pack_ids(self) -> frozenset[str]:
        """Every pack id this repository's index names."""
        if self._index_packs is None:
            return frozenset()
        return self._index_packs

    def _disc_packs(self, data_dir: Path) -> list[tuple[Path, str, int]]:
        """(path, pack id, size) of each pack under *data_dir* that this
        repository uses and the cache lacks.  ``data/<XX>/<hex>`` and
        ``data/<hex>`` are both read."""
        cache = self._pack_cache
        assert cache is not None
        wanted = self._repo_pack_ids()
        found: list[tuple[Path, str, int]] = []

        def consider(path: Path, name: str) -> None:
            if name not in wanted or name in self._drain_failed:
                return
            if (cache / "data" / name[:2] / name).exists():
                return
            try:
                st = path.stat()
            except OSError:
                return
            if stat.S_ISREG(st.st_mode):
                found.append((path, name, st.st_size))

        for entry in _listdir(data_dir):
            path = data_dir / entry
            if len(entry) == 2 and _is_dir(path):
                for name in _listdir(path):
                    consider(path / name, name)
            else:
                consider(path, entry)
        return found

    def _drain_disc(self, data_dir: Path) -> None:
        """Copy the packs of this repository on the disc at *data_dir*
        (its ``data/`` folder) into the cache: tier 1's drain_disc.

        Copies are written to ``<pack>.part``, checked against the pack
        id (the SHA-256 of the pack) and only then renamed, so the cache
        never holds a short or damaged pack.  The copy is opportunistic:
        any error stops it and the packs are read from the disc.
        """
        if self._pack_cache is None or self._is_not_a_disc(data_dir):
            return
        try:
            st = os.stat(data_dir)
            key = (str(data_dir), st.st_dev, st.st_ino, st.st_mtime_ns)
            if key in self._drain_refused:
                return
            todo = self._disc_packs(data_dir)
        except OSError:
            return
        if not todo:
            return
        need = sum(size for _path, _pid, size in todo)
        name = _disc_name(data_dir.parent)
        cache = self._pack_cache
        self._note_big_cache(cache)
        try:
            usage = shutil.disk_usage(cache)
        except OSError:
            usage = None
        if usage is not None and not _drain_fits(usage.free, usage.total, need):
            self._drain_refused.add(key)
            if "space" not in self._drain_warned:
                self._drain_warned.add("space")
                self._log(
                    f"WARNING: the cache at {cache} has "
                    f"{usage.free // _MIB} MB free, too little to copy "
                    f"disc {name} ({_mb_ceil(need)} MB) and keep some to "
                    "spare; not copying discs that do not fit, so they "
                    "may be asked for again.  Set LCSAS_PACK_CACHE_DIR "
                    "to a folder on a roomier drive."
                )
            return

        step = min(max(need // 20, _DRAIN_STEP_MIN), _DRAIN_STEP_MAX)
        progress = {"done": 0, "next": step}

        def tick(n: int) -> None:
            progress["done"] += n
            if progress["next"] <= progress["done"] < need:
                self._log(f"copying disc {name} to the cache: "
                          f"{progress['done'] // _MIB} of {_mb_ceil(need)} MB")
                while progress["next"] <= progress["done"]:
                    progress["next"] += step

        self._log(f"copying disc {name} to the cache: {_mb_ceil(need)} MB.  "
                  "From a disc this can take several minutes.")
        for src, pid, _size in todo:
            dst = cache / "data" / pid[:2] / pid
            try:
                ok = _copy_pack(src, dst, pid, tick)
            except OSError as exc:
                if exc.errno in _CACHE_SIDE_ERRNOS:
                    # The cache's drive is full or read-only: stop.
                    self._drain_refused.add(key)
                    self._log(f"could not copy disc {name} to the cache "
                              f"({exc}); its packs are read from the disc.")
                    break
                # One unreadable pack: copy the rest (tier 1 does).
                self._drain_failed.add(pid)
                self._log(f"could not copy pack {pid[:16]}... of disc {name} "
                          f"to the cache ({exc}); it is read from the disc.")
                continue
            if not ok:
                self._drain_failed.add(pid)
                self._log(f"pack {pid[:16]}... on disc {name} does not match "
                          "its id; not copied to the cache.")
        self._log(f"copied {_mb_ceil(progress['done'])} of {_mb_ceil(need)} MB "
                  f"from disc {name} to the cache.")

    def _note_big_cache(self, cache: Path) -> None:
        """Say once when the cache holds more than 1 GiB (tier 1 too)."""
        if "big" in self._drain_warned:
            return
        used = 0
        with contextlib.suppress(OSError):
            for dirpath, _dirs, files in os.walk(cache / "data"):
                for f in files:
                    with contextlib.suppress(OSError):
                        used += os.stat(os.path.join(dirpath, f)).st_size
        if used > _DRAIN_BIG_CACHE:
            self._drain_warned.add("big")
            self._log(f"WARNING: pack cache at {cache} is "
                      f"{used / (1 << 30):.1f} GB; set LCSAS_PACK_CACHE_DIR= "
                      "(empty) to disable further copies if disk is tight.")

    # Sentinels returned by _lookup_volume_labels to disambiguate the
    # three catalog states the framed prompt needs to print, mirroring
    # the branches in disc_locator.c::print_prompt:
    #   * labels = non-empty list  -> "It is on any disc labelled:" + each label
    #   * labels = []  and  catalog is reachable  -> _CATALOG_HIT_NO_VOLS
    #   * labels = []  and  pack is NOT in catalog -> _CATALOG_MISS
    #   * lookup_volume_labels returned None       -> no catalog (legacy line)
    _CATALOG_HIT_NO_VOLS = "hit-no-vols"
    _CATALOG_MISS = "miss"

    def _discover_catalog(self) -> Path | None:
        """Walk pack search paths looking for a disc catalog (``catalog.db``).

        The LCSAS ``HolographicInjector`` stamps ``catalog.db`` at the
        ISO image root (see ``staging/metadata.py::inject_catalog``), so on a
        mounted data disc the file lives at ``<mount-root>/catalog.db``.
        Callers (notably ``restore.sh``) may pass either the disc root
        (``--mount-point /mnt``) OR the disc's data subdir
        (``--pack-search /mnt/data``) -- both rewrite to identical mount
        points in tier-3's ``--mount-point`` flags, but the legacy default
        of ``repo_path/data`` still bottoms out one level deep.  So we
        probe both the root itself AND its parent (up to one level), and
        also a ``recovery/`` subpath (a local recovery tree laid out like
        the meta disc's; the meta disc itself holds no catalog).

        Probe order (first existing hit wins, stable across calls):

          1. ``<root>/catalog.db``         -- disc root, normal data-disc layout
          2. ``<root>/../catalog.db``      -- root passed as ``data/`` subdir
          3. ``<root>/recovery/catalog.db``-- local recovery tree

        Returns ``None`` if nothing is found.  Issue #253: this enables the
        catalog hint to materialise on the FIRST disc-swap retry after the
        heir inserts a data disc -- restore.sh's one-shot startup scan
        cannot, because no data disc is mounted yet at that point.
        """
        found = self._discover_catalogs()
        return found[0] if found else None

    def _discover_catalogs(self) -> list[Path]:
        """Every ``catalog.db`` reachable now, in ``_discover_catalog`` order.

        A path reached twice (the same root listed twice, or a root and
        its ``data/`` subdirectory) is listed once.
        """
        found: list[Path] = []
        seen: set[Path] = set()
        for root in self._all_roots():
            for candidate in (
                root / "catalog.db",
                root.parent / "catalog.db",
                root / "recovery" / "catalog.db",
            ):
                # resolve(strict=False): the file itself need not exist.
                try:
                    key = candidate.resolve(strict=False)
                except OSError:
                    key = candidate
                if key in seen:
                    continue
                seen.add(key)
                if _is_file(candidate):
                    found.append(candidate)
        return found

    def _keep_catalog_copy(self, path: Path) -> None:
        """Copy a reachable catalog to a private temp folder, once per content.

        The copy outlives the disc it came from (#635).  Errors are
        swallowed: the prompt must never fail because a copy could not
        be made.
        """
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in self._catalog_copies:
                return
            if self._catalog_copy_dir is None:
                self._catalog_copy_dir = Path(
                    tempfile.mkdtemp(prefix="lcsas-tier3-catalogs-")
                )
                atexit.register(
                    shutil.rmtree, str(self._catalog_copy_dir), True
                )
            dst = self._catalog_copy_dir / f"{digest[:16]}.db"
            shutil.copyfile(path, dst)
            self._catalog_copies[digest] = dst
        except OSError:
            return

    @staticmethod
    def _query_catalog(
        path: Path, pack_id: str,
    ) -> tuple[list[str], bool, int, str | None]:
        """(labels, pack known, number of discs listed, newest disc listed).

        The one place the catalog is queried.  Raises sqlite3.Error on an
        unreadable catalog.
        """
        # immutable=1: ISO 9660 is read-only so SQLite must not try to
        # create WAL/shm sidecar files.  Without this, sqlite3.connect()
        # with mode=ro raises OperationalError on iso9660 mounts AND
        # (critically) leaves a kernel reference on the mount point that
        # makes `umount /media` fail with EBUSY, preventing disc swaps.
        # Issue #284 root cause 2.
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            # Three-table join: volume_packs.pack_id is the INT FK, not
            # the sha256 hex we have in hand -- so we go through
            # packs.sha256.  ORDER BY label keeps the output stable
            # across SQLite query-planner versions.
            labels = [row[0] for row in conn.execute(
                "SELECT v.label FROM volumes v "
                "JOIN volume_packs vp ON v.volume_id = vp.volume_id "
                "JOIN packs p ON p.pack_id = vp.pack_id "
                "WHERE p.sha256 = ? ORDER BY v.label",
                (pack_id,),
            )]
            known = bool(labels) or conn.execute(
                "SELECT 1 FROM packs WHERE sha256 = ? LIMIT 1", (pack_id,),
            ).fetchone() is not None
            n_vols = conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
            newest = conn.execute(
                "SELECT label FROM volumes ORDER BY volume_id DESC LIMIT 1"
            ).fetchone()
            return labels, known, int(n_vols), newest[0] if newest else None
        finally:
            # Explicit close is required: `with sqlite3.connect() as c:`
            # is a transaction context manager, not a connection closer.
            # Leaving the connection open keeps a fd on the iso9660 mount
            # and prevents `umount /media` during disc swaps.
            conn.close()

    def _lookup_volume_labels(
        self, pack_id: str,
    ) -> tuple[list[str], str] | None:
        """Resolve a pack ID to disc labels using every catalog seen (#635).

        Same contract as ``_lookup_catalog_path``, which consults
        ``catalog_path`` (explicit or auto-discovered).  On top of that,
        every catalog reachable now is copied locally, and all copies
        are consulted: the labels are the union, and the pack counts
        as known if any catalog knows it.  Sets
        ``_newest_known_label`` to the newest disc in the catalog that
        lists the most discs, for the prompt's "newer than" hint.
        """
        primary = self._lookup_catalog_path(pack_id)
        reachable = self._discover_catalogs()
        if self.catalog_path is not None and _is_file(self.catalog_path):
            reachable.insert(0, self.catalog_path)
        for cat in reachable:
            self._keep_catalog_copy(cat)
        labels: set[str] = set(primary[0]) if primary else set()
        known = primary is not None and (
            bool(primary[0]) or primary[1] == self._CATALOG_HIT_NO_VOLS
        )
        any_ok = primary is not None
        best_n = -1
        for copy in self._catalog_copies.values():
            try:
                c_labels, c_known, n_vols, newest = self._query_catalog(
                    copy, pack_id,
                )
            except sqlite3.Error:
                continue
            any_ok = True
            labels.update(c_labels)
            known = known or c_known
            if n_vols > best_n:
                best_n = n_vols
                self._newest_known_label = newest
        if not any_ok:
            return None
        if labels:
            return sorted(labels), ""
        return [], self._CATALOG_HIT_NO_VOLS if known else self._CATALOG_MISS

    def _lookup_catalog_path(
        self, pack_id: str,
    ) -> tuple[list[str], str] | None:
        """Resolve a pack ID to the labels of the volumes that carry it.

        Returns
        -------
        ``None``
            No catalog could be reached -- either no ``catalog_path``
            override was passed AND auto-discovery turned up nothing
            under any pack-search root, or the discovered file could not
            be opened / queried.  Caller should fall back to the legacy
            "(no catalog available)" framed-prompt line.
        ``(labels, status)``
            ``labels`` is the (possibly-empty) list of volume labels for
            the pack, ordered lexicographically.  ``status`` is one of
            the ``_CATALOG_*`` sentinels above.

        Auto-discovery (issue #253): when ``self.catalog_path is None``,
        every call walks ``self._pack_search_paths`` looking for a
        ``catalog.db``.  On first hit the path is cached on the instance
        so subsequent prompt frames don't re-stat the disc; on miss we
        return ``None`` and try again on the next call -- letting a
        catalog that materialises mid-restore (after the heir inserts
        a data disc) be picked up on the next swap prompt.

        The catalog is opened read-only.  Any sqlite3 error is swallowed
        and treated as "catalog unavailable" -- the restore must never
        crash inside a disc-swap prompt because the catalog file is
        corrupt / locked / on a half-mounted disc.
        """
        was_auto_discovered = False
        if self.catalog_path is None:
            # Lazy discovery: scan the mount roots every prompt cycle
            # until we find one.  On hit we cache to avoid re-statting
            # the disc on subsequent prompts.
            discovered = self._discover_catalog()
            if discovered is None:
                return None
            self.catalog_path = discovered
            was_auto_discovered = True
            self._prompt_out(
                f"[lcsas-restore] auto-discovered catalog at {discovered}\n"
            )
        try:
            labels, known, _n, _newest = self._query_catalog(
                self.catalog_path, pack_id,
            )
        except sqlite3.Error:
            # Corrupt / locked / missing-tables catalog: treat as "no
            # catalog" so the heir at least sees the legacy prompt.
            #
            # If this catalog was auto-discovered (vs. an explicit
            # --catalog override), invalidate the cache so the NEXT
            # prompt cycle gets to re-scan the mount roots and possibly
            # find a different, healthier catalog on a later-inserted
            # disc.  An explicit override stays cached -- the heir
            # asked for THAT catalog and we won't silently substitute.
            if was_auto_discovered:
                self.catalog_path = None
            return None
        if labels:
            return labels, ""
        # Pack ID present in the catalog but no volume mapping?
        # Disambiguate that from "pack ID absent from catalog".
        return [], self._CATALOG_HIT_NO_VOLS if known else self._CATALOG_MISS

    @staticmethod
    def _frame_indented(text: str) -> str:
        """Render one indented line of the framed disc-swap prompt.

        Mirrors ``disc_locator.c::print_search_path`` -- text longer
        than 54 cols is prefixed with ``...`` and truncated from the
        left so the trailing component (filename / label) stays
        visible.
        """
        if len(text) > 54:
            text = "..." + text[-51:]
        return f"|   {text:<54s} |"

    def _print_swap_prompt(self, pack_id: str) -> None:
        """Render the LCSAS disc-swap prompt to stderr.

        Mirrors the framed prompt printed by ``recovery/src/lcsas-restore/
        disc_locator.c::print_prompt`` so the blind-restore harness sees
        an identical PASS marker regardless of which tier ran.
        """
        bar = "+" + "-" * 58 + "+"
        lines = [
            "",
            bar,
            f"| Pack {pack_id[:16]}... is required for the next file.  |",
        ]

        # The catalog only adds a hint; a disc or folder that cannot be
        # read must never stop the prompt (rc10 sweep).
        try:
            cat_result = self._lookup_volume_labels(pack_id)
        except OSError:
            cat_result = None
        if cat_result is None:
            # No --catalog supplied (or catalog unreadable).  Keep the
            # legacy "no catalog available" wording so backwards-compat
            # callers / tests that match on it still pass.
            lines.append(
                "| (tier-3 Python reader: no catalog available)             |"
            )
            # #635: say how to get one.  Every data disc carries the
            # catalog, and the next prompt reads it.
            lines.extend([
                "| Insert any data disc, your newest if you know which:     |",
                "| its catalog names the disc this pack is on.              |",
            ])
        else:
            labels, status = cat_result
            if labels:
                # Byte-for-byte match with disc_locator.c so the
                # blind-restore agent's pattern matcher can't tell tiers
                # apart on this line.
                lines.append(
                    "| It is on any disc labelled:                              |"
                )
                lines.extend(self._frame_indented(lbl) for lbl in labels)
            elif status == self._CATALOG_HIT_NO_VOLS:
                lines.append(
                    "| (catalog has the pack, but lists no disc for it)         |"
                )
                # #635: a disc's catalog lists only the discs burned
                # before it, so the pack is on a newer disc.
                newest = getattr(self, "_newest_known_label", None)
                if newest:
                    lines.append(
                        "| It is on a disc newer than:                              |"
                    )
                    lines.append(self._frame_indented(newest))
                    lines.append(
                        "| Insert your newest data disc: its catalog names it.      |"
                    )
            else:
                # _CATALOG_MISS -- pack ID not in catalog at all.
                lines.append(
                    "| (catalog has no record of this pack ID)                  |"
                )
                # #657 (tier-1 parity): the snapshot may be newer than
                # every disc.  Say how to pick an older one, with this
                # reader's own option names.
                lines.extend([
                    "| If the NEWEST data disc has already been read, this pack |",
                    "| was never burned: the snapshot is newer than the discs.  |",
                    "| Type 'q', then list the snapshots (--list-snapshots)     |",
                    "| and restore an older one with --snapshot <id>.           |",
                ])

        lines.extend([
            "|                                                          |",
            "| Currently searching:                                     |",
        ])
        lines.extend(self._frame_indented(str(p)) for p in self._all_roots())
        lines.extend([
            "|                                                          |",
            "| Insert the right disc and press ENTER to retry.          |",
            "| Type 'q' then ENTER to abort.                            |",
            bar,
            "> ",
        ])
        self._prompt_out("\n".join(lines))

    def _find_pack_path(self, pack_id: str) -> Path:
        """Locate a pack file in any configured search path.

        Supports both flat and two-level layouts.  When *interactive*
        is True and no copy is found, prints the LCSAS disc-swap prompt
        to stderr, waits for ENTER on stdin, then rescans -- up to
        ``_DISC_SWAP_MAX_RETRIES`` times.  ``q`` or the end of input
        raises ``RestoreAbortedError`` (#658); running out of retries
        raises FileNotFoundError, which fails only the current file.
        """
        hit = self._scan_pack_search_paths(pack_id)
        if hit is not None:
            return hit

        if not self.interactive:
            searched = ", ".join(str(p) for p in self._pack_search_paths)
            raise FileNotFoundError(
                f"Pack file not found: {pack_id}\n"
                f"Looked in: {searched}"
            )

        # Interactive: prompt-and-retry loop, bounded.  'q' or the end of
        # input stops the WHOLE restore (#658), as in tier 1: failing
        # just this file would ask for a disc again for the next one.
        for _ in range(self._DISC_SWAP_MAX_RETRIES):
            self._print_swap_prompt(pack_id)
            line = self._prompt_in()
            if not line or line.strip().lower() == "q":
                if line:
                    self._prompt_out("[lcsas-restore] aborted by user\n")
                else:
                    # The cursor still sits after "> ": end that line.
                    self._prompt_out("\n[lcsas-restore] input ended\n")
                raise RestoreAbortedError(
                    "stopped at the disc prompt.  Files restored so far "
                    "are kept.  To carry on, run the same command again.",
                    exit_code=EXIT_READER_QUIT,
                )
            # #602: copy whatever disc is in now, asked for or not.
            self._drain_visible_discs()
            hit = self._scan_pack_search_paths(pack_id)
            if hit is not None:
                self._prompt_out(
                    f"[lcsas-restore] found {pack_id[:16]}...; continuing.\n"
                )
                return hit
            self._prompt_out(
                "(still not found -- check the disc label and try again)\n"
            )

        searched = ", ".join(str(p) for p in self._pack_search_paths)
        raise FileNotFoundError(
            f"Pack file not found: {pack_id}\n"
            f"Looked in: {searched}"
        )

    def read_pack_range(self, pack_id: str, offset: int, length: int) -> bytes:
        """Read *length* bytes at *offset* of the first copy of *pack_id*
        that opens.  A copy that fails to open or read (``PermissionError``
        where a mode check said yes, an I/O error on a damaged disc) is
        set aside for the run and the search goes on, ending at the disc
        prompt as for a pack not found (rc10 sweep, tier1-missing)."""
        while True:
            pack_path = self._find_pack_path(pack_id)
            try:
                with open(pack_path, "rb") as f:
                    f.seek(offset)
                    return f.read(length)
            except OSError as exc:
                if str(pack_path) in self._unreadable:
                    raise
                self._unreadable.add(str(pack_path))
                self._log(f"cannot read {pack_path} ({exc.strerror or exc}); "
                          "looking for another copy.")


# ── Pack cache helpers (#602) ────────────────────────────────────


def _drain_fits(free: int, total: int, need: int) -> bool:
    """Is there room to copy *need* bytes and keep a margin free (5% of
    the drive, at most 1 GiB)?  Tier 1: lcsas_drain_fits."""
    margin = min(total // 20, _DRAIN_MARGIN_MAX)
    return need <= free and free - need >= margin


def _mb_ceil(n: int) -> int:
    """*n* bytes in MiB, rounded up (as tier 1 prints them)."""
    return (n + _MIB - 1) // _MIB


def _disc_name(root: Path) -> str:
    """The disc's label from its ``volume_info.json``, else its path."""
    try:
        with open(root / "volume_info.json", "rb") as f:
            doc = json.loads(f.read(1024 * 1024))
        label = doc.get("label") if isinstance(doc, dict) else None
        if isinstance(label, str) and label.strip():
            return _visible(label.strip())
    except (OSError, ValueError):
        pass
    return str(root)



def _is_file(path: Path) -> bool:
    """``path.is_file()`` that says False for a path it may not look at.

    pathlib raises PermissionError (not False) when a folder on the way
    cannot be entered; a disc search must pass over such a folder.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    """``path.is_dir()`` that says False for a path it may not look at."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _listdir(path: Path) -> list[str]:
    """Sorted names in *path*; empty for a folder it may not list."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []

def _copy_pack(
    src: Path, dst: Path, pack_id: str, tick: Callable[[int], None],
) -> bool:
    """Copy one pack into the cache.  Returns False (and keeps nothing)
    when the bytes read do not hash to *pack_id*.

    Written to ``<dst>.part`` and renamed once complete, so a scratched
    disc, a full drive or a killed run never leaves a short pack that
    the cache would trust from then on.  Raises OSError.
    """
    part = dst.with_name(dst.name + ".part")
    digest = hashlib.sha256()
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while True:
                chunk = fin.read(_MIB)
                if not chunk:
                    break
                digest.update(chunk)
                fout.write(chunk)
                tick(len(chunk))
        if digest.hexdigest() != pack_id:
            part.unlink(missing_ok=True)
            return False
        os.replace(part, dst)
    except OSError:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    return True
