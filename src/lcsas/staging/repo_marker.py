"""The backup set marker: ``metadata/<repo_id>/LCSAS_REPO.txt`` (rc6 C1).

Every disc keeps a repository's metadata in a folder named by its
catalog repo_id, and ``repo add`` makes that a UUID.  The marker inside
the folder gives the restore scripts the set's name and whether its
password is split into share cards, with no SQLite needed.  It lives
INSIDE the folder because every metadata copy (relocation, merge,
robocopy) copies whole folders, so a file beside the folder would be
dropped.

Format (contract C1): UTF-8 without a BOM, LF line endings, one
``key=value`` per line, split on the first ``=``::

    # LCSAS backup set marker. Restore works without this file.
    format=1
    id=<repo_id>
    name=<display name>
    name_cmd=<cmd-safe name>
    split=none | <K>-of-<N>
    split_id=<5-digit SLIP-39 id as on the share cards, or empty>

Readers strip a trailing CR, skip lines that start with ``#``, ignore
unknown keys, and ignore the whole file when ``id`` is not the folder's
name.  ``restore.bat`` prints only ``name_cmd`` (its menu re-parses the
text); ``restore.sh`` prints ``name``.  The marker is a hint: nothing may
trust it for security, and the share combiner validates the shares.

``name`` and ``name_cmd`` come from the repository's name, which
``repo add`` takes as given, so both are sanitised here (see
:func:`display_name` and :func:`cmd_name`).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lcsas.config.settings import LCSASConfig
from lcsas.db.key_escrow import can_record_splits, get_split_for_repo
from lcsas.db.models import KeyEscrow

_logger = logging.getLogger(__name__)

MARKER_FILENAME = "LCSAS_REPO.txt"
MARKER_FORMAT = 1
MARKER_HEADER = "# LCSAS backup set marker. Restore works without this file."

NAME_MAX_CHARS = 128
NAME_CMD_MAX_CHARS = 64
# Every character here is inert in cmd.exe: inside ``set "X=%%B"``, under
# enabledelayedexpansion, and even after ``call echo`` re-parses the line.
NAME_CMD_CHARS = frozenset(string.ascii_letters + string.digits + " ._+,-")


def _is_control(ch: str) -> bool:
    """True for a character that must never reach a marker line.

    C0 controls (CR, LF, NUL, ESC, ...) as C1 requires, plus DEL, the C1
    controls (U+0080-U+009F: U+0085 is a line break to some readers and
    U+009B starts a terminal escape) and the Unicode line and paragraph
    separators, which Python's ``str.splitlines`` also breaks on.
    """
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F or ch in "  "


def display_name(raw: str) -> str:
    """The ``name=`` value for *raw*: one safe line, at most 128 characters.

    Control characters (see :func:`_is_control`) become a space, the
    result is trimmed, and it is cut to 128 characters.  A lone UTF-16
    surrogate becomes U+FFFD so the file always encodes as UTF-8.
    Non-ASCII letters are kept.  May return ``""``.
    """
    cleaned = "".join(
        " " if _is_control(ch)
        else "�" if 0xD800 <= ord(ch) <= 0xDFFF
        else ch
        for ch in raw
    )
    return cleaned.strip()[:NAME_MAX_CHARS].strip()


def _cmd_safe(text: str) -> str:
    safe = "".join(ch if ch in NAME_CMD_CHARS else "_" for ch in text)
    return safe[:NAME_CMD_MAX_CHARS].strip(" ")


def cmd_name(name: str, repo_id: str) -> str:
    """The ``name_cmd=`` value: *name* with only ``[A-Za-z0-9 ._+,-]``.

    Every other character, non-ASCII included, becomes ``_``, and the
    result is cut to 64 characters.  A result with no ASCII letter or
    digit (empty, or only ``_`` and punctuation, as for a name written
    wholly in another script) falls back to the equally restricted
    repo_id: ``restore.bat``'s menu shows only ``name_cmd``, and two
    sets shown as ``__`` could not be told apart.
    """
    safe = _cmd_safe(name)
    if any(ch.isascii() and ch.isalnum() for ch in safe):
        return safe
    return _cmd_safe(repo_id)


def _check_repo_id(repo_id: str) -> None:
    if not repo_id.strip():
        raise ValueError("a repository id must not be empty")
    if any(_is_control(ch) for ch in repo_id):
        raise ValueError(
            f"repository id {repo_id!r} holds a control character"
        )


@dataclass(frozen=True)
class RepoMarker:
    """What one ``LCSAS_REPO.txt`` says.  Build it with :func:`make_repo_marker`.

    ``split`` is ``(K, N)`` or None for an ordinary password.
    ``split_id`` is the SLIP-39 identifier printed on the share cards
    (``Split ID : 01234``); only a recorded split knows it.
    """

    repo_id: str
    name: str
    split: tuple[int, int] | None = None
    split_id: int | None = None

    def __post_init__(self) -> None:
        _check_repo_id(self.repo_id)
        if not self.name or display_name(self.name) != self.name:
            raise ValueError(
                f"marker name {self.name!r} is not a clean display name"
            )
        if self.split is not None:
            k, n = self.split
            if not 1 <= k <= n:
                raise ValueError(f"bad split {k}-of-{n}")
        if self.split_id is not None and (
            self.split is None or not 0 <= self.split_id <= 99_999
        ):
            raise ValueError(f"bad split_id {self.split_id!r}")

    @property
    def name_cmd(self) -> str:
        return cmd_name(self.name, self.repo_id)


def make_repo_marker(
    repo_id: str,
    *,
    config_name: str | None = None,
    catalog_name: str | None = None,
    record: KeyEscrow | None = None,
    config_split: tuple[bool, int, int] | None = None,
) -> RepoMarker:
    """The marker for one repository, by C1's precedence rules.

    Name: the config key that maps this repo_id, else the catalog's
    ``repositories.name``, else the id; the first that is not empty once
    sanitised.  Split: the recorded split (``key_escrow``) wins, with its
    SLIP-39 id; else *config_split*, the config's ``(split, K, N)`` for
    this repository, with no split id; else none.  Pass *config_split*
    only when the catalog cannot record a split (see :func:`repo_markers`).

    Raises ValueError if *repo_id* is empty or holds a control character.
    """
    _check_repo_id(repo_id)
    name = display_name(repo_id)  # never empty: _check_repo_id
    for candidate in (config_name, catalog_name):
        clean = display_name(candidate or "")
        if clean:
            name = clean
            break
    split: tuple[int, int] | None = None
    split_id: int | None = None
    if record is not None:
        split = (record.threshold, record.shares)
        split_id = record.slip39_id
    elif config_split is not None and config_split[0]:
        split = (config_split[1], config_split[2])
    return RepoMarker(repo_id=repo_id, name=name, split=split, split_id=split_id)


def render_repo_marker(marker: RepoMarker) -> str:
    """The exact text of ``LCSAS_REPO.txt`` for *marker* (LF line endings)."""
    split = "none" if marker.split is None else "{}-of-{}".format(*marker.split)
    split_id = "" if marker.split_id is None else f"{marker.split_id:05d}"
    lines = [
        MARKER_HEADER,
        f"format={MARKER_FORMAT}",
        f"id={marker.repo_id}",
        f"name={marker.name}",
        f"name_cmd={marker.name_cmd}",
        f"split={split}",
        f"split_id={split_id}",
    ]
    return "\n".join(lines) + "\n"


def write_repo_marker(repo_dir: Path, marker: RepoMarker) -> Path:
    """Write *marker* into the repository's metadata folder *repo_dir*.

    The folder must be named by the marker's id: readers ignore a marker
    whose ``id`` is not its folder's name.  Bytes are written as is (no
    newline translation) and synced, like the other disc text files.
    """
    if repo_dir.name != marker.repo_id:
        raise ValueError(
            f"marker for {marker.repo_id!r} does not belong in {repo_dir}"
        )
    path = repo_dir / MARKER_FILENAME
    with open(path, "wb") as fh:
        fh.write(render_repo_marker(marker).encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return path


def parse_repo_marker(text: str) -> dict[str, str]:
    """The ``key=value`` fields of a marker's text, read tolerantly.

    A trailing CR is stripped from each line; blank lines, lines that
    start with ``#`` and lines with no ``=`` are skipped.  Each line is
    split on its first ``=``.  Unknown keys are kept for the caller to
    ignore.  A repeated key keeps its last value, as the scripts' line
    loops do.
    """
    fields: dict[str, str] = {}
    for line in text.removeprefix("﻿").split("\n"):
        line = line.removesuffix("\r")
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    return fields


def read_repo_marker(repo_dir: Path) -> dict[str, str] | None:
    """The fields of ``<repo_dir>/LCSAS_REPO.txt``, or None.

    None when the file is missing or unreadable, or when its ``id`` is
    not *repo_dir*'s name (a marker copied into the wrong folder).
    """
    try:
        raw = (repo_dir / MARKER_FILENAME).read_bytes()
    except OSError:
        return None
    fields = parse_repo_marker(raw.decode("utf-8", errors="replace"))
    if fields.get("id") != repo_dir.name:
        return None
    return fields


def _catalog_repo_names(conn: sqlite3.Connection) -> dict[str, str]:
    """``repo_id`` -> ``repositories.name``; empty for a catalog without names.

    Hand-rolled and legacy catalogs (meta-disc tests, partial catalogs)
    may have a ``repositories`` table with no ``name`` column, or none.
    """
    try:
        cols = {
            row[1] for row in
            conn.execute("PRAGMA table_info(repositories)").fetchall()
        }
        if "name" not in cols:
            return {}
        return {
            str(row[0]): str(row[1]) for row in
            conn.execute("SELECT repo_id, name FROM repositories").fetchall()
            if row[1] is not None
        }
    except sqlite3.DatabaseError:
        return {}


def repo_markers(
    conn: sqlite3.Connection,
    repo_ids: Iterable[str],
    *,
    config: LCSASConfig | None = None,
    config_names: Mapping[str, str] | None = None,
) -> dict[str, RepoMarker]:
    """Each repository's marker, by repo_id, for the data and meta discs.

    *conn* is the catalog, opened with ``sqlite3.Row`` rows (the split
    reader needs them).  A catalog without ``key_escrow`` or without a
    ``name`` column is fine.  *config_names* maps repo_id to config key
    (``config_names_by_repo_id`` or the burn's own join).

    Split: a catalog that can record splits (it has ``key_escrow``,
    schema v9+) decides alone.  A record gives ``K-of-N`` and the split
    id; no record gives ``split=none``, even when the config says the
    password is split: no record means no share cards were made (the
    burn's drift guard), and the guard checks only the sets with packs
    on the volume, while every set gets a marker.  Only a catalog with
    no ``key_escrow`` table (before v9) falls back to the config's split,
    looked up by the config key, else the catalog name, else the id: the
    key the drift guard uses.

    A repository whose id cannot be written into a marker is left out,
    with a warning: restore works without the marker.
    """
    names = config_names or {}
    catalog = _catalog_repo_names(conn)
    try:
        records_known = can_record_splits(conn)
    except sqlite3.DatabaseError:
        records_known = False
    out: dict[str, RepoMarker] = {}
    for repo_id in repo_ids:
        catalog_name = catalog.get(repo_id)
        key = names.get(repo_id)
        try:
            record = get_split_for_repo(conn, repo_id)
        except sqlite3.DatabaseError:
            record = None
        config_split = (
            config.repo_split(key or catalog_name or repo_id)
            if config is not None and not records_known else None
        )
        try:
            out[repo_id] = make_repo_marker(
                repo_id,
                config_name=key,
                catalog_name=catalog_name,
                record=record,
                config_split=config_split,
            )
        except ValueError as exc:
            _logger.warning(
                "Repository '%s': no %s written: %s",
                repo_id, MARKER_FILENAME, exc,
            )
    return out
