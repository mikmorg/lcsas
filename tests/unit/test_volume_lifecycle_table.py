"""What happens to a volume today, for every event and start state (#891).

"What may happen to a volume in status X?" had no one answer in the code:
the ``VALID_TRANSITIONS`` graph (``db/volumes.py``), 20 ``update_status``
calls that stepped through it, 5 raw ``UPDATE volumes SET status`` writes
that skipped it, and 5 ``delete_volume`` calls, each behind its own guard.
This file wrote the behaviour down as one table, so the lifecycle module
(#892, ``db/lifecycle.py``, which now holds all of it) could be shown to
keep it, and so the owner can read the whole list.  It pins behaviour;
it does not judge it.  Rows whose behaviour looks wrong carry a
``suspect`` note; they are not fixed here.

One row per (event, start state): every event crossed with every start,
except the three staging events, which make the volume and so start from
none, and "a lost volume is re-burned", which only a lost volume can be.
Each row is driven through the real command (``lcsas.cli.main.main``) or
orchestrator method, with only xorriso, the drive and the ISO checker
faked; never by calling ``update_status``.  The start state is forced by
one helper, :func:`seed`, the only place this file writes the catalog
directly.  ``test_every_status_write_is_reached_by_a_row`` checks that the
events reach every status write: since #892 the calls of the
``VolumeLifecycle`` events and the writes inside ``db/lifecycle.py``
(``--sites`` lists which rows reach each).

Columns of a row (all observed on the volume the event acts on):

- ``event``: what the owner does or what happens (:data:`EVENTS`).
- ``start``: the volume's state before it (:data:`STARTS`).
- ``end``: its status after, or ``deleted``.
- ``discs``: its disc records after, ``<location> <disc status>``.
- ``events``: the volume events the event wrote (NOTE details shortened).
- ``rc``: the exit code, or the exception a method raised.
- ``says``: the first error the owner sees (else the first warning when
  the event failed), shortened; ``<L>`` is the volume's label.
- ``deleted_by``: the deleting commands (:data:`DELETERS`) that would
  delete the volume in its end state.
- ``suspect``: why the present behaviour looks wrong, for the owner.

Render the table for reading::

    PYTHONPATH=src python -m tests.unit.test_volume_lifecycle_table --markdown

After a deliberate behaviour change, print the observed table (as the
``TABLE`` literal, suspect notes kept) and compare::

    PYTHONPATH=src python -m tests.unit.test_volume_lifecycle_table --observe

List which rows reach each status write::

    PYTHONPATH=src python -m tests.unit.test_volume_lifecycle_table --sites

The whole table runs once per module (about 600 rows, under a minute on
tmpfs).
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "lcsas"

# ---------------------------------------------------------------------
# Start states
# ---------------------------------------------------------------------

NEW = "(no volume yet)"


@dataclass(frozen=True)
class Start:
    """A volume's state: its status plus the history that changes what
    happens to it (disc records, NOTE events, burn evidence)."""

    status: str
    # "ACTIVE": one in-service disc at Home; "lost": its only disc, at
    # Home, recorded lost; None: no disc record.
    disc: str | None = None
    # NOTE details written before the event, oldest first.
    notes: tuple[str, ...] = ()
    # (event_type, location, detail) events written before the event.
    events: tuple[tuple[str, str | None, str], ...] = ()
    what: str = ""


def _changed(a: str, b: str) -> str:
    return f"Status changed: {a} → {b}"


_REBUILD_NOTE = ("Catalog rebuild: burn state unknown: this catalog was "
                 "rebuilt from discs, and none of them shows whether {label} "
                 "was burned")
_LOST_NOTES = ("disc at Home recorded as lost (whereabouts unknown)",
               "auto-deprecated: last in-service disc lost at Home")

STARTS: dict[str, Start] = {
    "STAGING": Start(
        "STAGING", what="staged, never burned (what 'lcsas stage' leaves)"),
    "STAGING, rebuilt": Start(
        "STAGING", notes=(_REBUILD_NOTE,),
        what="a catalog rebuilt from discs does not know whether it was "
             "burned (#758)"),
    "STAGING, burn evidence": Start(
        "STAGING", events=(("VERIFY_PASS", "Home", "Disc verify"),),
        what="no disc record, but an event says a disc of it passed (#737)"),
    "STAGING + disc": Start(
        "STAGING", disc="ACTIVE", what="an in-service disc is recorded"),
    "BURNING": Start(
        "BURNING", notes=(_changed("STAGING", "BURNING"),),
        what="a burn of a staged volume was killed outright (#668)"),
    "BURNING, was BURNED": Start(
        "BURNING", notes=(_changed("BURNED", "BURNING"),),
        what="an older LCSAS moved a BURNED volume to BURNING, then the "
             "burn was killed (#810)"),
    "BURNING + disc": Start(
        "BURNING", disc="ACTIVE", notes=(_changed("STAGING", "BURNING"),),
        what="an in-service disc is recorded"),
    "BURNED, rebuilt": Start(
        "BURNED",
        what="no disc record: a catalog rebuild read its own disc (#678)"),
    "BURNED + disc": Start(
        "BURNED", disc="ACTIVE",
        what="its disc was never read back (burn-iso --no-verify)"),
    "VERIFIED + disc": Start(
        "VERIFIED", disc="ACTIVE", what="burned and verified at Home"),
    "VERIFIED": Start(
        "VERIFIED", what="no disc record (a catalog from before discs were "
                         "recorded)"),
    "CONSOLIDATING + disc": Start(
        "CONSOLIDATING", disc="ACTIVE",
        notes=(_changed("VERIFIED", "CONSOLIDATING"),),
        what="the source of a consolidation, its disc at Home"),
    "CONSOLIDATING": Start(
        "CONSOLIDATING", notes=(_changed("VERIFIED", "CONSOLIDATING"),),
        what="the source of a consolidation, no disc record"),
    "DEPRECATED + disc": Start(
        "DEPRECATED", disc="ACTIVE",
        notes=(_changed("VERIFIED", "CONSOLIDATING"),
               _changed("CONSOLIDATING", "DEPRECATED")),
        what="deprecated by consolidation, its disc still in service"),
    "DEPRECATED": Start(
        "DEPRECATED",
        notes=(_changed("VERIFIED", "CONSOLIDATING"),
               _changed("CONSOLIDATING", "DEPRECATED")),
        what="deprecated by consolidation, no disc record"),
    "DEPRECATED, lost": Start(
        "DEPRECATED", disc="lost", notes=_LOST_NOTES,
        what="a lost volume: its only disc recorded lost (ADR 0003)"),
    # The volume-wide DESTROYED status left the graph (#907) and no
    # command writes it, but the schema keeps it: an old catalog may hold
    # it, so what each event does to such a volume stays pinned.
    "DESTROYED + disc": Start(
        "DESTROYED", disc="ACTIVE",
        what="not in the status graph (#907); only an old catalog holds it; "
             "seeded directly"),
    "DESTROYED": Start(
        "DESTROYED", what="not in the status graph (#907); only an old "
                          "catalog holds it; seeded directly"),
}

# ---------------------------------------------------------------------
# The environment a row runs in
# ---------------------------------------------------------------------

_DAY_AGO = timedelta(days=1)
_HOUR_AGO = timedelta(hours=1)


def _ts(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="milliseconds")


@dataclass
class Env:
    """One catalog with one staged volume (the subject of every row) in
    burn session ``session_id``, its ISO in the staging area, and the
    locations Home (where it was staged for) and Bank."""

    root: Path
    config: Any
    label: str
    uuid: str
    session_id: str
    iso_sha256: str
    iso_size: int
    conn: sqlite3.Connection = field(init=False)
    # A second burn session an event's preparation staged.
    other_session: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.open()

    @property
    def db(self) -> Path:
        return Path(self.config.db_path)

    @property
    def cfg(self) -> str:
        return str(self.root / "lcsas.toml")

    @property
    def volume_id(self) -> int:
        row = self.conn.execute(
            "SELECT volume_id FROM volumes WHERE label = ?", (self.label,),
        ).fetchone()
        assert row is not None, f"{self.label} is not in the catalog"
        return int(row[0])

    def open(self) -> None:
        from lcsas.db.connection import get_connection

        self.conn = get_connection(self.db)

    def close(self) -> None:
        self.conn.close()

    def snapshot_db(self, path: Path) -> None:
        """Copy the catalog as it is on disk now (committed rows only)."""
        src, dst = sqlite3.connect(self.db), sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    def restore_db(self, path: Path) -> None:
        self.close()
        src, dst = sqlite3.connect(path), sqlite3.connect(self.db)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        self.open()

    def orch(self, *, bad_disc: bool = False,
             burn_iso: Callable[..., Any] | None = None) -> Any:
        """A BurnOrchestrator whose xorriso and drive are fakes: the ISO is
        1 KiB of zeros, and the burned disc reads back as it (or not, with
        *bad_disc*)."""
        from lcsas.burn.operator import NullOperatorPrompt
        from lcsas.burn.orchestrator import BurnOrchestrator
        from tests.unit.test_session_pipeline import _wire_drive_identity

        xorriso = MagicMock()

        def _create_iso(source_dir, output_iso, volume_label, **kwargs):
            Path(output_iso).write_bytes(b"\x00" * 1024)
            return Path(output_iso)

        xorriso.create_iso.side_effect = _create_iso
        if burn_iso is not None:
            xorriso.burn_iso.side_effect = burn_iso
        _wire_drive_identity(xorriso)

        def _reader(device: str, n: int) -> str:
            return hashlib.sha256((b"\x01" if bad_disc else b"\x00") * n
                                  ).hexdigest()

        return BurnOrchestrator(self.config, self.conn, xorriso, MagicMock(),
                                device_reader=_reader,
                                prompt=NullOperatorPrompt())


def _write_cfg(root: Path, config: Any) -> None:
    [(name, repo)] = config.repositories.items()
    (root / "lcsas.toml").write_text(
        "[paths]\n"
        f'mirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'catalog = "{config.db_path}"\n'
        "[survivability]\npassword_hints = 'Test hint: sealed envelope'\n[defaults]\n"
        'media_type = "TEST_TINY"\n'
        'label_prefix = "TEST"\n'
        'location = "Home"\n'
        "metadata_reserve_mb = 1\n"
        "allow_unverified_repo_format = true\n"
        f"[repos.{name}]\n"
        f'mirror_path = "{repo.mirror_path}"\n'
    )


@dataclass
class Templates:
    """Two catalogs built once and copied back before each row, at the
    same absolute path so the paths recorded in them stay right."""

    base: Path
    config: Any
    label: str
    uuid: str
    session_id: str
    iso_sha256: str
    iso_size: int

    @property
    def live(self) -> Path:
        return self.base / "live"

    def env(self, which: str) -> Env:
        """Restore template *which* ("packs" or "staged") and open it."""
        if self.live.exists():
            shutil.rmtree(self.live)
        shutil.copytree(self.base / which, self.live)
        return Env(self.live, self.config, self.label, self.uuid,
                   self.session_id, self.iso_sha256, self.iso_size)


def build_templates(base: Path) -> Templates:
    """"packs": a catalog with one repository, three packs in the mirror
    and the locations Home and Bank.  "staged": the same after ``lcsas
    stage --for-location Home`` made one volume of them, two days ago (so
    ``catalog reconcile`` may count it a ghost)."""
    from lcsas.db.connection import get_connection
    from lcsas.db.locations import ensure_location
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from tests.unit.test_session_pipeline import _make_config, _seed_packs

    live = base / "live"
    config = _make_config(live, num_repos=1)
    conn = get_connection(config.db_path)
    create_all(conn)
    for name, repo in config.repositories.items():
        register_repo(conn, name, name.title(), str(repo.mirror_path))
    _seed_packs(conn, config, num_packs=3, pack_size=50)
    ensure_location(conn, "Home")
    ensure_location(conn, "Bank")
    conn.commit()
    conn.close()
    _write_cfg(live, config)
    shutil.copytree(live, base / "packs")

    env = Env(live, config, "", "", "", "", 0)
    with _patches(env):
        result = env.orch().stage(for_location="Home")
    [manifest] = result.manifests
    env.conn.execute(
        "UPDATE volumes SET created_at = datetime('now', '-48 hours') "
        "WHERE volume_id = ?", (manifest.volume_id,))
    env.conn.commit()
    sha, size, uuid_ = env.conn.execute(
        "SELECT sv.iso_sha256, sv.iso_size_bytes, v.uuid "
        "FROM session_volumes sv JOIN volumes v USING (volume_id) "
        "WHERE v.volume_id = ?", (manifest.volume_id,)).fetchone()
    env.close()
    shutil.copytree(live, base / "staged")
    return Templates(base, config, manifest.volume_label, uuid_,
                     result.session_id, sha, size)


def seed(env: Env, start: Start) -> None:
    """Force *start* onto the staged volume with raw SQL.

    The one place this file forces a volume's status (through the test
    helper ``force_status``, #892); :func:`seed_replica` and
    :func:`seed_consolidation_tie` add the other rows a few events need.
    Every NOTE is the text production writes for that history, so the
    rules that read NOTEs (in ``db/lifecycle.py``: the status before a
    burn, burn evidence, the #758 rebuild flag; ``deprecated_by_loss``)
    see what they would.  Seeded history is
    dated an hour ago, so what an event writes is always newer (the #666
    guard compares a receipt's burn date with when its disc was recorded
    lost).
    """
    from tests.unit.volume_status import force_status

    conn, vid = env.conn, env.volume_id
    durable = start.status not in ("STAGING", "BURNING")
    verified = start.status not in ("STAGING", "BURNING", "BURNED")
    force_status(conn, vid, start.status,
                 closed_at=_ts(_DAY_AGO) if durable else None,
                 verified_at=_ts(_DAY_AGO) if verified else None)
    if start.disc is not None:
        conn.execute(
            "INSERT INTO volume_copies (volume_id, location, status, "
            "burn_date, iso_sha256, iso_size_bytes, last_verified_at) "
            "VALUES (?, 'Home', ?, ?, ?, ?, ?)",
            (vid, "ACTIVE" if start.disc == "ACTIVE" else "DESTROYED",
             _ts(_DAY_AGO), env.iso_sha256, env.iso_size,
             _ts(_DAY_AGO) if verified else None))
    for detail in start.notes:
        _add_event(env, "NOTE", None, detail.format(label=env.label))
    for event_type, location, detail in start.events:
        _add_event(env, event_type, location, detail)
    conn.commit()


def _add_event(env: Env, event_type: str, location: str | None,
               detail: str) -> None:
    env.conn.execute(
        "INSERT INTO volume_events (volume_id, event_type, event_date, "
        "location, detail) VALUES (?, ?, ?, ?, ?)",
        (env.volume_id, event_type, _ts(_HOUR_AGO), location, detail))


def seed_replica(env: Env) -> None:
    """A second VERIFIED volume holding the same packs, its disc at Bank:
    the packs survive the volume being deprecated."""
    cur = env.conn.execute(
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
        "location, status, created_at, closed_at, verified_at) VALUES "
        "('REPLICA', 'uuid-replica', 'TEST_TINY', 2097152, 'Bank', "
        "'VERIFIED', ?, ?, ?)", (_ts(_DAY_AGO),) * 3)
    env.conn.execute(
        "INSERT INTO volume_packs (volume_id, pack_id) SELECT ?, pack_id "
        "FROM volume_packs WHERE volume_id = ?",
        (cur.lastrowid, env.volume_id))
    env.conn.execute(
        "INSERT INTO volume_copies (volume_id, location, burn_date, "
        "last_verified_at) VALUES (?, 'Bank', ?, ?)",
        (cur.lastrowid, _ts(_DAY_AGO), _ts(_DAY_AGO)))
    env.conn.commit()


def seed_consolidation_tie(env: Env, session_id: str) -> None:
    """The NOTE ``consolidate --execute`` writes on each source, tying it
    to the burn session its packs were staged into (#677)."""
    from lcsas.consolidate.merger import consolidation_note

    _add_event(env, "NOTE", None, consolidation_note(session_id))
    env.conn.commit()


@functools.cache
def _parser() -> Any:
    from lcsas.cli.parser import build_parser

    return build_parser()


@contextlib.contextmanager
def _patches(env: Env) -> Iterator[None]:
    """The fakes every row runs with: no tool version check, no tray, a
    drive that holds the volume's disc and reads back its ISO, no ECC
    tool (ISO checks fall back to SHA-256), and "yes" to every prompt."""
    from lcsas.burn.orchestrator import BurnOrchestrator

    def _create_iso(self, source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xr = "lcsas.iso.xorriso.SubprocessXorrisoRunner"
    with (
        # Speed only: build the argparse tree once, not per command.
        patch("lcsas.cli.main.build_parser", _parser),
        patch.object(BurnOrchestrator, "_tool_version_preflight",
                     lambda self, *, apply_ecc: None),
        patch("lcsas.burn.orchestrator.eject_tray", lambda _d: True),
        patch(f"{xr}.create_iso", _create_iso),
        patch(f"{xr}.read_disc_volume_id",
              lambda self, device=None: env.label),
        patch(f"{xr}.verify_disc", lambda self, device=None: True),
        patch("lcsas.burn.device_verify.read_device_sha256",
              lambda device, n: hashlib.sha256(b"\x00" * n).hexdigest()),
        patch("lcsas.ecc.dvdisaster.select_ecc_runner", lambda: None),
        patch("builtins.input", lambda *_a: "yes"),
    ):
        yield


# ---------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------


class _Killed(BaseException):
    """The process dies: nothing after this runs (power loss, kill -9)."""


def _main(*argv: str) -> int:
    from lcsas.cli.main import main

    return main(list(argv))


def _burn(env: Env, **kwargs: Any) -> int:
    env.orch(**kwargs).burn_session(env.session_id, location="Home",
                                    device="/dev/sr0", interactive=True)
    return 0


def _burn_killed(env: Env) -> int:
    """Kill the burn while the disc is written: the catalog is left as it
    was on disk at that moment."""
    snap = env.root / "killed.db"

    def _burn_iso(iso: Any, device: Any) -> None:
        env.snapshot_db(snap)
        raise _Killed

    try:
        _burn(env, burn_iso=_burn_iso)
    except _Killed:
        env.restore_db(snap)
    return 0


def _burn_error(iso: Any, device: Any) -> None:
    raise RuntimeError("drive error")


def _receipt(env: Env, verify_passed: bool | None) -> int:
    path = env.root / "receipt.json"
    path.write_text(json.dumps({
        "volume_label": env.label, "location": "Home",
        "iso_sha256": env.iso_sha256, "iso_size_bytes": env.iso_size,
        "burn_date": _ts(), "verify_passed": verify_passed,
        "session_id": env.session_id, "device": "/dev/sr0", "pack_ids": [],
    }))
    return _main("--config", env.cfg, "catalog", "import-receipts", str(path))


def _pack_shas(env: Env) -> list[str]:
    return [r[0] for r in env.conn.execute(
        "SELECT p.sha256 FROM packs p JOIN volume_packs vp USING (pack_id) "
        "WHERE vp.volume_id = ? ORDER BY p.sha256", (env.volume_id,))]


def _stage_consolidation(env: Env) -> None:
    """Stage the volume's packs into a new burn session as ``consolidate
    --execute`` does, and tie the volume to it."""
    result = env.orch().stage(for_location="Home",
                              pack_sha256s=_pack_shas(env))
    env.other_session = result.session_id
    seed_consolidation_tie(env, result.session_id)


def _later_disc(env: Env) -> Path:
    """Stage one more volume, of a new pack: its staging tree is a disc
    made after the volume reached its state, carrying the catalog as it
    is now."""
    from lcsas.db.packs import register_pack
    from tests.unit.test_session_pipeline import _pack_content

    [repo] = env.config.repositories.values()
    sha, content = _pack_content(99, 50)
    (repo.mirror_path / "data" / sha).write_bytes(content)
    register_pack(env.conn, sha256=sha, size_bytes=len(content),
                  repo_id=next(iter(env.config.repositories)))
    env.conn.commit()
    result = env.orch().stage(for_location="Home", pack_sha256s=[sha])
    [manifest] = result.manifests
    return Path(manifest.staging_path)


def _own_disc(env: Env) -> Path:
    return env.config.staging_path / _session_dir(env) / env.label


def _session_dir(env: Env) -> str:
    row = env.conn.execute(
        "SELECT staging_dir FROM burn_sessions WHERE session_id = ?",
        (env.session_id,)).fetchone()
    return Path(row[0]).name


def _adopt(env: Env, rebuilt: Path) -> None:
    """The owner makes the rebuilt catalog the master catalog."""
    env.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{env.db}{suffix}").unlink(missing_ok=True)
    src, dst = sqlite3.connect(rebuilt), sqlite3.connect(env.db)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    env.open()


def _rebuild(env: Env, *, own_disc: bool) -> int:
    later = _later_disc(env)
    discs = [str(_own_disc(env))] if own_disc else []
    out = env.root / "rebuilt.db"
    rc = _main("catalog", "rebuild", "--output", str(out), *discs, str(later))
    _adopt(env, out)
    return rc


def _rebuild_into_old_catalog(env: Env) -> int:
    """The catalog as it was when the volume was staged (its own disc's
    copy) is brought up to date from a newer disc."""
    later = _later_disc(env)
    out = env.root / "old.db"
    shutil.copy(_own_disc(env) / "catalog.db", out)
    rc = _main("catalog", "rebuild", "--output", str(out), str(later))
    _adopt(env, out)
    return rc


def _watch_label(env: Env, orch: Any) -> None:
    """Observe the first volume *orch* masters (the one the stage events
    make, and which may not survive)."""
    master, seen = orch._master_volume, []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        if not seen:
            seen.append(args[2])
            env.label = args[2]
        return master(*args, **kwargs)

    orch._master_volume = _spy


def _stage_new(env: Env) -> int:
    result = env.orch().stage(for_location="Home")
    [manifest] = result.manifests
    env.label, env.session_id = manifest.volume_label, result.session_id
    return 0


def _stage_fails(env: Env) -> int:
    def _fail(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("xorriso failed")

    orch = env.orch()
    orch._xorriso.create_iso.side_effect = _fail
    _watch_label(env, orch)
    orch.stage(for_location="Home")
    return 0


def _stage_stops_remaster_fails(env: Env) -> int:
    """#813: the stage stops after the first of three volumes; the kept
    volume carries a snapshot it cannot restore and cannot be mastered
    again (``test_stage_partial_snapshots`` recipe, its own catalog,
    which then replaces this one)."""
    from tests.unit.test_stage_partial_snapshots import _env

    with pytest.MonkeyPatch.context() as mp:
        orch, conn, _x = _env(env.root / "partial", mp, fail_on=2,
                              fail_remaster=True)
        _watch_label(env, orch)
        try:
            orch.stage()
        finally:
            env.close()
            dst = sqlite3.connect(env.db)
            try:
                conn.backup(dst)
            finally:
                dst.close()
                conn.close()
            env.open()
    return 0


@dataclass(frozen=True)
class Event:
    command: str
    run: Callable[[Env], int]
    starts: tuple[str, ...] = tuple(STARTS)
    template: str = "staged"
    # Setup after the start is seeded, before the event (not observed).
    prepare: Callable[[Env], None] | None = None


def _verify(*extra: str) -> Callable[[Env], int]:
    return lambda env: _main("--catalog", str(env.db), "verify", env.label,
                             *extra)


EVENTS: dict[str, Event] = {
    # --- staging ------------------------------------------------------
    "stage a volume": Event(
        "lcsas stage --for-location Home", _stage_new, (NEW,), "packs"),
    "stage fails while mastering the volume": Event(
        "lcsas stage (xorriso fails)", _stage_fails, (NEW,), "packs"),
    "stage stops part-way; a kept volume cannot be mastered again": Event(
        "lcsas stage (2nd of 3 volumes fails, then the re-master of the "
        "1st)", _stage_stops_remaster_fails, (NEW,), "packs"),
    # --- burning ------------------------------------------------------
    "a burn starts, and is killed while the disc is written": Event(
        "lcsas burn --session S --location Home (killed in the burn)",
        _burn_killed),
    "burn and verify succeed": Event(
        "lcsas burn --session S --location Home", _burn),
    "burn fails its read-back": Event(
        "lcsas burn --session S --location Home (disc reads back wrong)",
        lambda env: _burn(env, bad_disc=True)),
    "burn stopped by an error or Ctrl-C": Event(
        "lcsas burn --session S --location Home (drive error)",
        lambda env: _burn(env, burn_iso=_burn_error)),
    "burn killed, then the next burn succeeds": Event(
        "lcsas burn ... (killed), then lcsas burn --session S --location "
        "Home", lambda env: _burn_killed(env) or _burn(env)),
    # The burn rows above from start "DEPRECATED, lost" are this event
    # too; listed on its own because the owner asks about it (#726).
    "a lost volume is re-burned": Event(
        "lcsas burn --session S --location Home", _burn,
        ("DEPRECATED, lost",)),
    # --- verifying ----------------------------------------------------
    "verify passes on a staged volume (--mark-verified)": Event(
        "lcsas verify L --mark-verified --location Home",
        _verify("--mark-verified", "--location", "Home")),
    "disc check passes": Event(
        "lcsas verify L --disc", _verify("--disc", "--device", "/dev/sr0")),
    "disc check passes where no disc is recorded": Event(
        "lcsas verify L --disc --location Bank",
        _verify("--disc", "--device", "/dev/sr0", "--location", "Bank")),
    "ISO check passes": Event("lcsas verify L", _verify()),
    "batch ISO check passes": Event(
        "lcsas verify --all",
        lambda env: _main("--catalog", str(env.db), "verify", "--all")),
    "batch disc check passes": Event(
        "lcsas verify --all --disc",
        lambda env: _main("--catalog", str(env.db), "verify", "--all", "--disc",
                          "--device", "/dev/sr0")),
    # --- burn receipts ------------------------------------------------
    "a passing receipt is imported": Event(
        "lcsas catalog import-receipts r.json (verify_passed: true)",
        lambda env: _receipt(env, True)),
    "a failing receipt is imported": Event(
        "lcsas catalog import-receipts r.json (verify_passed: false)",
        lambda env: _receipt(env, False)),
    "an unverified receipt is imported": Event(
        "lcsas catalog import-receipts r.json (verify_passed: null, "
        "burn-iso --no-verify)", lambda env: _receipt(env, None)),
    # --- discs --------------------------------------------------------
    "the disc is taken out of service": Event(
        "lcsas copy deprecate L Home",
        lambda env: _main("--catalog", str(env.db), "copy", "deprecate",
                          env.label, "Home")),
    "the disc is destroyed": Event(
        "lcsas copy destroy L Home",
        lambda env: _main("--catalog", str(env.db), "copy", "destroy",
                          env.label, "Home")),
    "the last disc is marked lost": Event(
        "lcsas copy destroy L Home --lost",
        lambda env: _main("--catalog", str(env.db), "copy", "destroy",
                          env.label, "Home", "--lost")),
    # --- consolidation and deprecation --------------------------------
    "consolidation starts": Event(
        "lcsas consolidate L --media-type TEST_TINY --execute",
        lambda env: _main("--config", env.cfg, "consolidate", env.label,
                          "--media-type", "TEST_TINY", "--execute")),
    "consolidation finishes (packs on another volume too)": Event(
        "lcsas consolidate L --media-type TEST_TINY --deprecate",
        lambda env: _main("--config", env.cfg, "consolidate", env.label,
                          "--media-type", "TEST_TINY", "--deprecate"),
        prepare=seed_replica),
    "deprecate by hand (packs on no other volume)": Event(
        "lcsas consolidate L --media-type TEST_TINY --deprecate",
        lambda env: _main("--config", env.cfg, "consolidate", env.label,
                          "--media-type", "TEST_TINY", "--deprecate")),
    "consolidation abandoned (its session aborted)": Event(
        "lcsas session abort <consolidation session>",
        lambda env: _main("--config", env.cfg, "session", "abort",
                          env.other_session),
        prepare=_stage_consolidation),
    # --- deleting -----------------------------------------------------
    "session abort": Event(
        "lcsas session abort S",
        lambda env: _main("--config", env.cfg, "session", "abort",
                          env.session_id)),
    "session abort --never-burned": Event(
        "lcsas session abort S --never-burned",
        lambda env: _main("--config", env.cfg, "session", "abort",
                          env.session_id, "--never-burned")),
    "session abort --volume": Event(
        "lcsas session abort --volume L",
        lambda env: _main("--config", env.cfg, "session", "abort",
                          "--volume", env.label)),
    "session abort --volume --never-burned": Event(
        "lcsas session abort --volume L --never-burned",
        lambda env: _main("--config", env.cfg, "session", "abort",
                          "--volume", env.label, "--never-burned")),
    "stage --clean": Event(
        "lcsas stage --clean --session S",
        lambda env: _main("--config", env.cfg, "stage", "--clean",
                          "--session", env.session_id)),
    "stage --clean --force": Event(
        "lcsas stage --clean --force --session S",
        lambda env: _main("--config", env.cfg, "stage", "--clean",
                          "--force", "--session", env.session_id)),
    "catalog reconcile --fix": Event(
        "lcsas catalog reconcile --fix --yes",
        lambda env: _main("--catalog", str(env.db), "catalog", "reconcile",
                          "--fix", "--yes")),
    "catalog reconcile --fix --include-waiting": Event(
        "lcsas catalog reconcile --fix --yes --include-waiting",
        lambda env: _main("--catalog", str(env.db), "catalog", "reconcile",
                          "--fix", "--yes", "--include-waiting")),
    # --- catalog rebuild ----------------------------------------------
    "catalog rebuilt from discs, its own disc among them": Event(
        "lcsas catalog rebuild --output NEW <its disc> <a later disc>",
        lambda env: _rebuild(env, own_disc=True)),
    "catalog rebuilt from discs, without its own disc": Event(
        "lcsas catalog rebuild --output NEW <a later disc>",
        lambda env: _rebuild(env, own_disc=False)),
    "an old catalog is brought up to date from a later disc": Event(
        "lcsas catalog rebuild --output OLD <a later disc>",
        _rebuild_into_old_catalog),
}

# The commands that delete volumes.  ``session abort`` decides with the
# same check as ``stage --clean --force`` (it calls it), so it stands for
# both.  Each runs on the catalog as the event left it; only ``session
# abort`` removes files (the ISO), so it runs last.
DELETERS: dict[str, str] = {
    "reconcile": "catalog reconcile --fix",
    "reconcile-waiting": "catalog reconcile --fix --include-waiting",
    "abort-volume": "session abort --volume",
    "abort": "session abort",
}

# ---------------------------------------------------------------------
# Observing a row
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Observed:
    end: str
    discs: str
    events: str
    rc: str
    says: str


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_SESSION_ID = re.compile(
    r"\d{4}-\d\d-\d\dT[0-9:.]+\+00:00-[0-9a-f]{8}")
_SESSION_DIR = re.compile(
    r"\d{4}-\d\d-\d\dT[0-9-]+\.\d+\+00-00-[0-9a-f]{8}")
_LABEL = re.compile(r"TEST_TEST_TINY_\d{4}_\d{4}")


def _norm(text: str, env: Env, limit: int = 100) -> str:
    text = text.replace(str(env.root), "<tmp>")
    text = text.replace(env.label, "<L>")
    text = _LABEL.sub("<L2>", text)
    text = _SESSION_ID.sub("<S>", _SESSION_DIR.sub("<S>", text))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _event_text(env: Env, event_type: str, location: str | None,
                detail: str) -> str:
    if event_type != "NOTE":
        return event_type + (f"@{location}" if location else "")
    if detail.startswith("Catalog rebuild: burn state unknown"):
        detail = "Catalog rebuild: burn state unknown"
    detail = detail.removeprefix("Status changed: ").split("; ")[0]
    return f"NOTE {_norm(detail, env, 60)}"


def _volume_facts(env: Env) -> tuple[str, str, list[str]]:
    row = env.conn.execute(
        "SELECT volume_id, status FROM volumes WHERE label = ?",
        (env.label,)).fetchone()
    if row is None:
        return "deleted", "", []
    vid, status = row
    discs = "; ".join(
        f"{loc} {st}" for loc, st in env.conn.execute(
            "SELECT location, status FROM volume_copies WHERE volume_id = ? "
            "ORDER BY location", (vid,)))
    events = [
        _event_text(env, t, loc, d or "") for t, loc, d in env.conn.execute(
            "SELECT event_type, location, detail FROM volume_events "
            "WHERE volume_id = ? ORDER BY event_id", (vid,))]
    return status, discs, events


def _new_events(before: list[str], after: list[str]) -> str:
    left = Counter(before)
    new = []
    for e in after:
        if left[e]:
            left[e] -= 1
        else:
            new.append(e)
    return ", ".join(new)


@contextlib.contextmanager
def _captured() -> Iterator[_Capture]:
    cap = _Capture()
    lg = logging.getLogger("lcsas")
    lg.addHandler(cap)
    try:
        yield cap
    finally:
        lg.removeHandler(cap)


def _run_event(env: Env, event: Event) -> tuple[str, str]:
    """Run *event*; return (rc, what the owner is told)."""
    message = ""
    with _captured() as cap:
        try:
            rc = str(event.run(env))
        except Exception as exc:  # the refusal is the row's result
            rc = type(exc).__name__
            message = str(exc).splitlines()[0] if str(exc) else rc
    errors = [r.getMessage() for r in cap.records if r.levelno >= logging.ERROR]
    warnings = [r.getMessage() for r in cap.records]
    if not message:
        if errors:
            message = errors[0]
        elif rc != "0" and warnings:
            message = warnings[0]
    return rc, _norm(message, env)


def run_row(t: Templates, event_name: str, start_name: str,
            during: Callable[[], contextlib.AbstractContextManager[Any]]
            = contextlib.nullcontext) -> tuple[Observed, str]:
    """Run one row: seed *start_name*, run *event_name* (inside
    *during*), observe the volume as the catalog on disk has it.  Then
    run each of :data:`DELETERS` on its own copy of that end state; the
    second value names those that delete the volume."""
    event = EVENTS[event_name]
    env = t.env(event.template)
    try:
        if start_name != NEW:
            seed(env, STARTS[start_name])
        if event.prepare is not None:
            with _patches(env):
                event.prepare(env)
        _status, _discs, before = _volume_facts(env)
        with _patches(env), during():
            rc, says = _run_event(env, event)
        env.close()  # drops what a failed event left uncommitted
        env.open()
        end, discs, after = _volume_facts(env)
        observed = Observed(end, discs, _new_events(before, after), rc, says)
        found = []
        if end != "deleted":
            snap = t.base / "after.db"
            env.snapshot_db(snap)
            for key, deleter in DELETERS.items():
                env.restore_db(snap)
                with _patches(env):
                    _run_event(env, EVENTS[deleter])
                env.close()
                env.open()
                if _volume_facts(env)[0] == "deleted":
                    found.append(key)
        return observed, " ".join(found)
    finally:
        env.close()


# ---------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    event: str
    start: str
    end: str
    discs: str = ""
    events: str = ""
    rc: str = "0"
    says: str = ""
    deleted_by: str = ""
    suspect: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.event, self.start)

    def result(self) -> tuple[Observed, str]:
        return (Observed(self.end, self.discs, self.events, self.rc,
                         self.says), self.deleted_by)


def R(start: str, end: str, discs: str = "", *,  # noqa: N802
      events: str = "", rc: str = "0", says: str = "", deleted_by: str = "",
      suspect: str = "") -> dict[str, str]:
    """One row of :data:`TABLE` (its event is the key it is listed under)."""
    return {"start": start, "end": end, "discs": discs, "events": events,
            "rc": rc, "says": says, "deleted_by": deleted_by,
            "suspect": suspect}


# fmt: off
TABLE: dict[str, list[dict[str, str]]] = {
    "stage a volume": [
        R("(no volume yet)", "STAGING",
          deleted_by="abort-volume abort"),
    ],
    "stage fails while mastering the volume": [
        R("(no volume yet)", "deleted",
          rc="RuntimeError",
          says="xorriso failed"),
    ],
    "stage stops part-way; a kept volume cannot be mastered again": [
        R("(no volume yet)", "deleted",
          rc="RuntimeError",
          says="xorriso failed"),
    ],
    "a burn starts, and is killed while the disc is written": [
        R("STAGING", "BURNING",
          events="NOTE STAGING → BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "BURNING",
          events="NOTE STAGING → BURNING"),
        R("STAGING, burn evidence", "BURNING",
          events="NOTE STAGING → BURNING"),
        R("STAGING + disc", "BURNING", "Home ACTIVE",
          events="NOTE STAGING → BURNING"),
        R("BURNING", "BURNING",
          events="NOTE BURNING → STAGING, NOTE STAGING → BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNED",
          events="NOTE BURNING → BURNED"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is CONSOLIDATING; its packs are being consolidated "
                "onto a new volume; bu…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DEPRECATED; its packs are on other volumes, so no "
                "new disc of it is b…")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DESTROYED; it is out of service, so no new disc "
                "of it is burned. Noth…")),
    ],
    "burn and verify succeed": [
        R("STAGING", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, VERIFY_PASS@Home, NOTE BURNING → VERIFIED"),
        R("STAGING, rebuilt", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, VERIFY_PASS@Home, NOTE BURNING → VERIFIED"),
        R("STAGING, burn evidence", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, VERIFY_PASS@Home, NOTE BURNING → VERIFIED"),
        R("STAGING + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, VERIFY_PASS@Home, NOTE BURNING → VERIFIED"),
        R("BURNING", "VERIFIED", "Home ACTIVE",
          events=("NOTE BURNING → STAGING, NOTE STAGING → BURNING, VERIFY_PASS@Home, NOTE "
                  "BURNING → VERIFIED")),
        R("BURNING, was BURNED", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNING → BURNED, VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNED, rebuilt", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is CONSOLIDATING; its packs are being consolidated "
                "onto a new volume; bu…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DEPRECATED; its packs are on other volumes, so no "
                "new disc of it is b…")),
        R("DEPRECATED, lost", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE DEPRECATED → VERIFIED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DESTROYED; it is out of service, so no new disc "
                "of it is burned. Noth…")),
    ],
    "burn fails its read-back": [
        R("STAGING", "STAGING",
          events="NOTE STAGING → BURNING, VERIFY_FAIL@Home, NOTE BURNING → STAGING",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          events="NOTE STAGING → BURNING, VERIFY_FAIL@Home, NOTE BURNING → STAGING",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("STAGING, burn evidence", "STAGING",
          events="NOTE STAGING → BURNING, VERIFY_FAIL@Home, NOTE BURNING → STAGING",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          events="NOTE STAGING → BURNING, VERIFY_FAIL@Home, NOTE BURNING → STAGING",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("BURNING", "STAGING",
          events=("NOTE BURNING → STAGING, NOTE STAGING → BURNING, VERIFY_FAIL@Home, NOTE "
                  "BURNING → STAGING"),
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNED",
          events="NOTE BURNING → BURNED, VERIFY_FAIL@Home",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNED, rebuilt", "BURNED",
          events="VERIFY_FAIL@Home",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          events="VERIFY_FAIL@Home",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED",
          events="VERIFY_FAIL@Home, VERIFY_FAIL_REBURN@Home",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is CONSOLIDATING; its packs are being consolidated "
                "onto a new volume; bu…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DEPRECATED; its packs are on other volumes, so no "
                "new disc of it is b…")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          events="VERIFY_FAIL@Home, VERIFY_FAIL_REBURN@Home",
          says=("Burn at Home FAILED verification for <L>. NO disc was recorded — this location "
                "still needs this vol…")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DESTROYED; it is out of service, so no new disc "
                "of it is burned. Noth…")),
    ],
    "burn stopped by an error or Ctrl-C": [
        R("STAGING", "STAGING",
          events="NOTE STAGING → BURNING, NOTE BURNING → STAGING",
          rc="RuntimeError",
          says="drive error",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          events="NOTE STAGING → BURNING, NOTE BURNING → STAGING",
          rc="RuntimeError",
          says="drive error"),
        R("STAGING, burn evidence", "STAGING",
          events="NOTE STAGING → BURNING, NOTE BURNING → STAGING",
          rc="RuntimeError",
          says="drive error"),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → STAGING",
          rc="RuntimeError",
          says="drive error"),
        R("BURNING", "STAGING",
          events="NOTE BURNING → STAGING, NOTE STAGING → BURNING, NOTE BURNING → STAGING",
          rc="RuntimeError",
          says="drive error",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNED",
          events="NOTE BURNING → BURNED",
          rc="RuntimeError",
          says="drive error"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNED, rebuilt", "BURNED",
          rc="RuntimeError",
          says="drive error"),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="RuntimeError",
          says="drive error"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED",
          rc="RuntimeError",
          says="drive error"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is CONSOLIDATING; its packs are being consolidated "
                "onto a new volume; bu…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DEPRECATED; its packs are on other volumes, so no "
                "new disc of it is b…")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="RuntimeError",
          says="drive error"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DESTROYED; it is out of service, so no new disc "
                "of it is burned. Noth…")),
    ],
    "burn killed, then the next burn succeeds": [
        R("STAGING", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → STAGING, NOTE STAGING → BURNING, "
                  "VERIFY_PASS@Home, NOTE BURNING → VERIFIED")),
        R("STAGING, rebuilt", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → STAGING, NOTE STAGING → BURNING, "
                  "VERIFY_PASS@Home, NOTE BURNING → VERIFIED")),
        R("STAGING, burn evidence", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → STAGING, NOTE STAGING → BURNING, "
                  "VERIFY_PASS@Home, NOTE BURNING → VERIFIED")),
        R("STAGING + disc", "BURNING", "Home ACTIVE",
          events="NOTE STAGING → BURNING",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNING", "VERIFIED", "Home ACTIVE",
          events=("NOTE BURNING → STAGING, NOTE STAGING → BURNING, NOTE BURNING → STAGING, NOTE "
                  "STAGING → BURNING, VERIFY_PASS@Home, NOTE BURNING → VERIFIED")),
        R("BURNING, was BURNED", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNING → BURNED, VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="BurnError",
          says="Volume <L> is BURNING but already has a disc recorded. Nothing was burned."),
        R("BURNED, rebuilt", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is CONSOLIDATING; its packs are being consolidated "
                "onto a new volume; bu…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DEPRECATED; its packs are on other volumes, so no "
                "new disc of it is b…")),
        R("DEPRECATED, lost", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE DEPRECATED → VERIFIED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED",
          rc="BurnError",
          says=("Cannot burn volume <L>: it is DESTROYED; it is out of service, so no new disc "
                "of it is burned. Noth…")),
    ],
    "a lost volume is re-burned": [
        R("DEPRECATED, lost", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE DEPRECATED → VERIFIED"),
    ],
    "verify passes on a staged volume (--mark-verified)": [
        R("STAGING", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home"),
        R("STAGING, rebuilt", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home"),
        R("STAGING, burn evidence", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home"),
        R("STAGING + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home"),
        R("BURNING", "BURNING",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is BURNING (only BURNED or STAGING "
                "allowed)"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is BURNING (only BURNED or STAGING "
                "allowed)")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is BURNING (only BURNED or STAGING "
                "allowed)")),
        R("BURNED, rebuilt", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNED → VERIFIED, VERIFY_PASS@Home"),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNED → VERIFIED, VERIFY_PASS@Home"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is VERIFIED (only BURNED or STAGING "
                "allowed)")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is VERIFIED (only BURNED or STAGING "
                "allowed)")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is CONSOLIDATING (only BURNED or "
                "STAGING allowed)")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is CONSOLIDATING (only BURNED or "
                "STAGING allowed)")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is DEPRECATED (only BURNED or STAGING "
                "allowed)")),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is DEPRECATED (only BURNED or STAGING "
                "allowed)")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says="Cannot mark volume <L> VERIFIED: status is LOST (only BURNED or STAGING allowed)"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is DESTROYED (only BURNED or STAGING "
                "allowed)")),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Cannot mark volume <L> VERIFIED: status is DESTROYED (only BURNED or STAGING "
                "allowed)")),
    ],
    "disc check passes": [
        R("STAGING", "STAGING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("BURNING", "BURNING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS, NOTE BURNED → VERIFIED"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Volume <L> has no disc recorded. Give --location to say where the disc in the "
                "drive is kept, and a …")),
    ],
    "disc check passes where no disc is recorded": [
        R("STAGING", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE STAGING → VERIFIED"),
        R("STAGING, rebuilt", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE STAGING → VERIFIED"),
        R("STAGING, burn evidence", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE STAGING → VERIFIED"),
        R("STAGING + disc", "VERIFIED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS, NOTE STAGING → VERIFIED"),
        R("BURNING", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE BURNING → VERIFIED"),
        R("BURNING, was BURNED", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE BURNING → VERIFIED"),
        R("BURNING + disc", "VERIFIED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS, NOTE BURNING → VERIFIED"),
        R("BURNED, rebuilt", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS, NOTE BURNED → VERIFIED"),
        R("BURNED + disc", "VERIFIED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS, NOTE BURNED → VERIFIED"),
        R("VERIFIED + disc", "VERIFIED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED", "VERIFIED", "Bank ACTIVE",
          events="VERIFY_PASS"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS"),
        R("CONSOLIDATING", "CONSOLIDATING", "Bank ACTIVE",
          events="VERIFY_PASS"),
        R("DEPRECATED + disc", "DEPRECATED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS"),
        R("DEPRECATED", "DEPRECATED", "Bank ACTIVE",
          events="VERIFY_PASS"),
        R("DEPRECATED, lost", "VERIFIED", "Bank ACTIVE; Home DESTROYED",
          events="NOTE DEPRECATED → VERIFIED, VERIFY_PASS"),
        R("DESTROYED + disc", "DESTROYED", "Bank ACTIVE; Home ACTIVE",
          events="VERIFY_PASS"),
        R("DESTROYED", "DESTROYED", "Bank ACTIVE",
          events="VERIFY_PASS"),
    ],
    "ISO check passes": [
        R("STAGING", "STAGING",
          events="VERIFY_PASS",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          events="VERIFY_PASS"),
        R("STAGING, burn evidence", "STAGING",
          events="VERIFY_PASS"),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("BURNING", "BURNING",
          events="VERIFY_PASS",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          events="VERIFY_PASS"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("BURNED, rebuilt", "BURNED",
          events="VERIFY_PASS"),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED", "VERIFIED",
          events="VERIFY_PASS"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("CONSOLIDATING", "CONSOLIDATING",
          events="VERIFY_PASS"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("DEPRECATED", "DEPRECATED",
          events="VERIFY_PASS"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          events="VERIFY_PASS"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("DESTROYED", "DESTROYED",
          events="VERIFY_PASS"),
    ],
    "batch ISO check passes": [
        R("STAGING", "STAGING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED",
          events="VERIFY_PASS"),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS"),
        R("VERIFIED", "VERIFIED",
          events="VERIFY_PASS"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "batch disc check passes": [
        R("STAGING", "STAGING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE BURNED → VERIFIED"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "a passing receipt is imported": [
        R("STAGING", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home, "
                  "BURN_RECEIPT_IMPORTED@Home")),
        R("STAGING, rebuilt", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home, "
                  "BURN_RECEIPT_IMPORTED@Home")),
        R("STAGING, burn evidence", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home, "
                  "BURN_RECEIPT_IMPORTED@Home")),
        R("STAGING + disc", "VERIFIED", "Home ACTIVE",
          events=("NOTE STAGING → BURNING, NOTE BURNING → VERIFIED, VERIFY_PASS@Home, "
                  "BURN_RECEIPT_IMPORTED@Home")),
        R("BURNING", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNING → VERIFIED, VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING, was BURNED", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNING → VERIFIED, VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNING → VERIFIED, VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED, rebuilt", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNED → VERIFIED, VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE BURNED → VERIFIED, VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING", "CONSOLIDATING", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED", "DEPRECATED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED, lost", "VERIFIED", "Home ACTIVE",
          events="VERIFY_PASS@Home, NOTE DEPRECATED → VERIFIED, BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED", "DESTROYED", "Home ACTIVE",
          events="VERIFY_PASS@Home, BURN_RECEIPT_IMPORTED@Home"),
    ],
    "a failing receipt is imported": [
        R("STAGING", "STAGING",
          events="VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          events="VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("STAGING, burn evidence", "STAGING",
          events="VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING", "STAGING",
          events="NOTE BURNING → STAGING, VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNED",
          events="NOTE BURNING → BURNED, VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED, rebuilt", "BURNED",
          events="VERIFY_FAIL@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED", "VERIFIED",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING", "CONSOLIDATING",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED", "DEPRECATED",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED", "DESTROYED",
          events="VERIFY_FAIL_REBURN@Home, BURN_RECEIPT_IMPORTED@Home"),
    ],
    "an unverified receipt is imported": [
        R("STAGING", "BURNED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("STAGING, rebuilt", "BURNED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("STAGING, burn evidence", "BURNED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("STAGING + disc", "BURNED", "Home ACTIVE",
          events="NOTE STAGING → BURNING, NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING", "BURNED", "Home ACTIVE",
          events="NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING, was BURNED", "BURNED", "Home ACTIVE",
          events="NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNING + disc", "BURNED", "Home ACTIVE",
          events="NOTE BURNING → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED, rebuilt", "BURNED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("VERIFIED", "VERIFIED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("CONSOLIDATING", "CONSOLIDATING", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED", "DEPRECATED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("DEPRECATED, lost", "BURNED", "Home ACTIVE",
          events="NOTE DEPRECATED → BURNED, BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
        R("DESTROYED", "DESTROYED", "Home ACTIVE",
          events="BURN_RECEIPT_IMPORTED@Home"),
    ],
    "the disc is taken out of service": [
        R("STAGING", "STAGING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("STAGING + disc", "DEPRECATED", "Home DEPRECATED",
          events=("NOTE disc at Home recorded as deprecated (out of service), NOTE "
                  "auto-deprecated: last in-service disc deprecated at Home")),
        R("BURNING", "BURNING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("BURNING + disc", "DEPRECATED", "Home DEPRECATED",
          events=("NOTE disc at Home recorded as deprecated (out of service), NOTE "
                  "auto-deprecated: last in-service disc deprecated at Home")),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("BURNED + disc", "DEPRECATED", "Home DEPRECATED",
          events=("NOTE disc at Home recorded as deprecated (out of service), NOTE "
                  "auto-deprecated: last in-service disc deprecated at Home")),
        R("VERIFIED + disc", "DEPRECATED", "Home DEPRECATED",
          events=("NOTE disc at Home recorded as deprecated (out of service), NOTE "
                  "auto-deprecated: last in-service disc deprecated at Home")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("CONSOLIDATING + disc", "DEPRECATED", "Home DEPRECATED",
          events=("NOTE disc at Home recorded as deprecated (out of service), NOTE "
                  "auto-deprecated: last in-service disc deprecated at Home")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("DEPRECATED + disc", "DEPRECATED", "Home DEPRECATED",
          events="NOTE disc at Home recorded as deprecated (out of service)"),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
        R("DESTROYED + disc", "DESTROYED", "Home DEPRECATED",
          events="NOTE disc at Home recorded as deprecated (out of service)"),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says="No in-service disc of volume 1 at 'Home'"),
    ],
    "the disc is destroyed": [
        R("STAGING", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("STAGING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as destroyed, NOTE auto-deprecated: last "
                  "in-service disc destroyed at Home")),
        R("BURNING", "BURNING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("BURNING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as destroyed, NOTE auto-deprecated: last "
                  "in-service disc destroyed at Home")),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("BURNED + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as destroyed, NOTE auto-deprecated: last "
                  "in-service disc destroyed at Home")),
        R("VERIFIED + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as destroyed, NOTE auto-deprecated: last "
                  "in-service disc destroyed at Home")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("CONSOLIDATING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as destroyed, NOTE auto-deprecated: last "
                  "in-service disc destroyed at Home")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("DEPRECATED + disc", "DEPRECATED", "Home DESTROYED",
          events="NOTE disc at Home recorded as destroyed"),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          events="NOTE disc at Home recorded as destroyed"),
        R("DESTROYED + disc", "DESTROYED", "Home DESTROYED",
          events="NOTE disc at Home recorded as destroyed"),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
    ],
    "the last disc is marked lost": [
        R("STAGING", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("STAGING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as lost (whereabouts unknown), NOTE "
                  "auto-deprecated: last in-service disc lost at Home")),
        R("BURNING", "BURNING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("BURNING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as lost (whereabouts unknown), NOTE "
                  "auto-deprecated: last in-service disc lost at Home")),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("BURNED + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as lost (whereabouts unknown), NOTE "
                  "auto-deprecated: last in-service disc lost at Home")),
        R("VERIFIED + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as lost (whereabouts unknown), NOTE "
                  "auto-deprecated: last in-service disc lost at Home")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("CONSOLIDATING + disc", "DEPRECATED", "Home DESTROYED",
          events=("NOTE disc at Home recorded as lost (whereabouts unknown), NOTE "
                  "auto-deprecated: last in-service disc lost at Home")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("DEPRECATED + disc", "DEPRECATED", "Home DESTROYED",
          events="NOTE disc at Home recorded as lost (whereabouts unknown)"),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          events="NOTE disc at Home recorded as lost (whereabouts unknown)"),
        R("DESTROYED + disc", "DESTROYED", "Home DESTROYED",
          events="NOTE disc at Home recorded as lost (whereabouts unknown)"),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says="No disc of volume 1 recorded at 'Home'"),
    ],
    "consolidation starts": [
        R("STAGING", "STAGING",
          rc="1",
          says="Volume <L> is STAGING: only VERIFIED volumes can be consolidated.",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says="Volume <L> is STAGING: only VERIFIED volumes can be consolidated."),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says="Volume <L> is STAGING: only VERIFIED volumes can be consolidated."),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          rc="1",
          says="Volume <L> is STAGING: only VERIFIED volumes can be consolidated."),
        R("BURNING", "BURNING",
          rc="1",
          says="Volume <L> is BURNING: only VERIFIED volumes can be consolidated.",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says="Volume <L> is BURNING: only VERIFIED volumes can be consolidated."),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says="Volume <L> is BURNING: only VERIFIED volumes can be consolidated."),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says="Volume <L> is BURNED: verify its disc first ('lcsas verify <L> --disc')."),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="1",
          says="Volume <L> is BURNED: verify its disc first ('lcsas verify <L> --disc')."),
        R("VERIFIED + disc", "CONSOLIDATING", "Home ACTIVE",
          events=("NOTE VERIFIED → CONSOLIDATING, NOTE consolidation: packs staged into burn "
                  "session <S>")),
        R("VERIFIED", "CONSOLIDATING",
          events=("NOTE VERIFIED → CONSOLIDATING, NOTE consolidation: packs staged into burn "
                  "session <S>")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          rc="1",
          says=("Volume <L> is CONSOLIDATING: it is already being consolidated: burn that "
                "session and run 'consolida…")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Volume <L> is CONSOLIDATING: it is already being consolidated: burn that "
                "session and run 'consolida…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says="Volume <L> is DEPRECATED: only VERIFIED volumes can be consolidated."),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says="Volume <L> is DEPRECATED: only VERIFIED volumes can be consolidated."),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says="Volume <L> is DEPRECATED: only VERIFIED volumes can be consolidated."),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says="Volume <L> is DESTROYED: only VERIFIED volumes can be consolidated."),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says="Volume <L> is DESTROYED: only VERIFIED volumes can be consolidated."),
    ],
    "consolidation finishes (packs on another volume too)": [
        R("STAGING", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("BURNING", "BURNING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …")),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNED; verify its disc first ('lcsas verify "
                "<L> --disc'). Nothi…")),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNED; verify its disc first ('lcsas verify "
                "<L> --disc'). Nothi…")),
        R("VERIFIED + disc", "DEPRECATED", "Home ACTIVE",
          events="NOTE VERIFIED → DEPRECATED"),
        R("VERIFIED", "DEPRECATED",
          events="NOTE VERIFIED → DEPRECATED"),
        R("CONSOLIDATING + disc", "DEPRECATED", "Home ACTIVE",
          events="NOTE CONSOLIDATING → DEPRECATED"),
        R("CONSOLIDATING", "DEPRECATED",
          events="NOTE CONSOLIDATING → DEPRECATED"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DESTROYED; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated…")),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DESTROYED; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated…")),
    ],
    "deprecate by hand (packs on no other volume)": [
        R("STAGING", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is STAGING; it was never burned, so it is not "
                "deprecated; delete it…")),
        R("BURNING", "BURNING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNING; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated. …")),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNED; verify its disc first ('lcsas verify "
                "<L> --disc'). Nothi…")),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is BURNED; verify its disc first ('lcsas verify "
                "<L> --disc'). Nothi…")),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: 3 of its pack(s) are on no other in-service "
                "volume; burn and verify th…")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says=("Cannot deprecate volume <L>: 3 of its pack(s) are on no other in-service "
                "volume; burn and verify th…")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: 3 of its pack(s) are on no other in-service "
                "volume; burn and verify th…")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Cannot deprecate volume <L>: 3 of its pack(s) are on no other in-service "
                "volume; burn and verify th…")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DEPRECATED; it is already deprecated. "
                "Nothing was deprecated.")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DESTROYED; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated…")),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Cannot deprecate volume <L>: it is DESTROYED; only a VERIFIED or CONSOLIDATING "
                "volume is deprecated…")),
    ],
    "consolidation abandoned (its session aborted)": [
        R("STAGING", "STAGING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "VERIFIED", "Home ACTIVE",
          events="NOTE CONSOLIDATING → VERIFIED"),
        R("CONSOLIDATING", "VERIFIED",
          events="NOTE CONSOLIDATING → VERIFIED"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "session abort": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> comes from a catalog rebuilt from "
                "discs, and no disc it…")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is STAGING, but a disc of it passed "
                "verification at Hom…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNING, but it was BURNED when "
                "a burn of it started…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNED, but no disc of it is "
                "recorded. A disc of it …")),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "session abort --never-burned": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "deleted"),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is STAGING, but a disc of it passed "
                "verification at Hom…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNING, but it was BURNED when "
                "a burn of it started…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNED, but no disc of it is "
                "recorded. A disc of it …")),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "session abort --volume": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Refusing to clean or abort volume <L>: <L> comes from a catalog rebuilt from "
                "discs, and no disc it …")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort volume <L>: <L> is STAGING, but a disc of it passed "
                "verification at Home…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          rc="1",
          says="Volume <L> has a recorded disc — a burned disc exists, refusing to abort it."),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort volume <L>: <L> is BURNING, but it was BURNED when a "
                "burn of it started.…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says="Volume <L> has a recorded disc — a burned disc exists, refusing to abort it."),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Volume <L> has status BURNED — only never-burned (STAGING/BURNING) volumes can "
                "be aborted.")),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status BURNED — only never-burned (STAGING/BURNING) volumes can "
                "be aborted.")),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status VERIFIED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says=("Volume <L> has status VERIFIED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status CONSOLIDATING — only never-burned (STAGING/BURNING) "
                "volumes can be aborted.")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Volume <L> has status CONSOLIDATING — only never-burned (STAGING/BURNING) "
                "volumes can be aborted.")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status DESTROYED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Volume <L> has status DESTROYED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
    ],
    "session abort --volume --never-burned": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "deleted"),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort volume <L>: <L> is STAGING, but a disc of it passed "
                "verification at Home…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE",
          rc="1",
          says="Volume <L> has a recorded disc — a burned disc exists, refusing to abort it."),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort volume <L>: <L> is BURNING, but it was BURNED when a "
                "burn of it started.…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE",
          rc="1",
          says="Volume <L> has a recorded disc — a burned disc exists, refusing to abort it."),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Volume <L> has status BURNED — only never-burned (STAGING/BURNING) volumes can "
                "be aborted.")),
        R("BURNED + disc", "BURNED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status BURNED — only never-burned (STAGING/BURNING) volumes can "
                "be aborted.")),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status VERIFIED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("VERIFIED", "VERIFIED",
          rc="1",
          says=("Volume <L> has status VERIFIED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status CONSOLIDATING — only never-burned (STAGING/BURNING) "
                "volumes can be aborted.")),
        R("CONSOLIDATING", "CONSOLIDATING",
          rc="1",
          says=("Volume <L> has status CONSOLIDATING — only never-burned (STAGING/BURNING) "
                "volumes can be aborted.")),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DEPRECATED", "DEPRECATED",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED",
          rc="1",
          says=("Volume <L> has status DEPRECATED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE",
          rc="1",
          says=("Volume <L> has status DESTROYED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
        R("DESTROYED", "DESTROYED",
          rc="1",
          says=("Volume <L> has status DESTROYED — only never-burned (STAGING/BURNING) volumes "
                "can be aborted.")),
    ],
    "stage --clean": [
        R("STAGING", "STAGING",
          rc="1",
          says=("Session <S> has 1 volume(s) with no verified disc (<L>) — never burned, or "
                "burned but post-burn ver…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> comes from a catalog rebuilt from "
                "discs, and no disc it…")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is STAGING, but a disc of it passed "
                "verification at Hom…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          rc="1",
          says=("Session <S> has 1 volume(s) with no verified disc (<L>) — never burned, or "
                "burned but post-burn ver…"),
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNING, but it was BURNED when "
                "a burn of it started…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNED, but no disc of it is "
                "recorded. A disc of it …")),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "stage --clean --force": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> comes from a catalog rebuilt from "
                "discs, and no disc it…")),
        R("STAGING, burn evidence", "STAGING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is STAGING, but a disc of it passed "
                "verification at Hom…")),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNING, but it was BURNED when "
                "a burn of it started…")),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED",
          rc="1",
          says=("Refusing to clean or abort session <S>: <L> is BURNED, but no disc of it is "
                "recorded. A disc of it …")),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "catalog reconcile --fix": [
        R("STAGING", "STAGING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          deleted_by="reconcile-waiting abort-volume abort"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "catalog reconcile --fix --include-waiting": [
        R("STAGING", "deleted"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "deleted"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "catalog rebuilt from discs, its own disc among them": [
        R("STAGING", "BURNED"),
        R("STAGING, rebuilt", "BURNED"),
        R("STAGING, burn evidence", "BURNED"),
        R("STAGING + disc", "BURNED", "Home ACTIVE"),
        R("BURNING", "BURNED"),
        R("BURNING, was BURNED", "BURNED"),
        R("BURNING + disc", "BURNED", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "catalog rebuilt from discs, without its own disc": [
        R("STAGING", "STAGING",
          events="NOTE Catalog rebuild: burn state unknown"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          events="NOTE Catalog rebuild: burn state unknown"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
    "an old catalog is brought up to date from a later disc": [
        R("STAGING", "STAGING",
          deleted_by="abort-volume abort"),
        R("STAGING, rebuilt", "STAGING"),
        R("STAGING, burn evidence", "STAGING"),
        R("STAGING + disc", "STAGING", "Home ACTIVE"),
        R("BURNING", "BURNING",
          deleted_by="abort-volume abort"),
        R("BURNING, was BURNED", "BURNING"),
        R("BURNING + disc", "BURNING", "Home ACTIVE"),
        R("BURNED, rebuilt", "BURNED"),
        R("BURNED + disc", "BURNED", "Home ACTIVE"),
        R("VERIFIED + disc", "VERIFIED", "Home ACTIVE"),
        R("VERIFIED", "VERIFIED"),
        R("CONSOLIDATING + disc", "CONSOLIDATING", "Home ACTIVE"),
        R("CONSOLIDATING", "CONSOLIDATING"),
        R("DEPRECATED + disc", "DEPRECATED", "Home ACTIVE"),
        R("DEPRECATED", "DEPRECATED"),
        R("DEPRECATED, lost", "DEPRECATED", "Home DESTROYED"),
        R("DESTROYED + disc", "DESTROYED", "Home ACTIVE"),
        R("DESTROYED", "DESTROYED"),
    ],
}
# fmt: on

ROWS: list[Row] = [Row(event, **cell) for event, cells in TABLE.items()
                   for cell in cells]


def _pairs() -> list[tuple[str, str]]:
    return [(e, s) for e, ev in EVENTS.items() for s in ev.starts]


# ---------------------------------------------------------------------
# Which status writes the table reaches
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    kind: str  # "lifecycle event", "_update_status", "_delete_volume",
    #            "status write"
    path: str  # relative to src/lcsas
    line: int  # the line the call starts on
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


# The methods of ``VolumeLifecycle`` (``db/lifecycle.py``, #892) that only
# read; every other public method is an event that changes or deletes a
# volume.
_LIFECYCLE_READS = frozenset({"burn_evidence", "burn_unknown", "may_burn",
                              "may_delete"})
_LIFECYCLE = "db/lifecycle.py"


@functools.cache
def lifecycle_events() -> frozenset[str]:
    from lcsas.db.lifecycle import VolumeLifecycle

    public = {name for name, value in vars(VolumeLifecycle).items()
              if callable(value) and not name.startswith("_")}
    assert public >= _LIFECYCLE_READS, "a read-only method was renamed"
    events = frozenset(public - _LIFECYCLE_READS)
    assert events, "VolumeLifecycle has no event methods"
    return events


def status_write_sites() -> list[Site]:
    """Every place that changes or deletes a volume's status: calls of the
    ``VolumeLifecycle`` event methods outside ``db/lifecycle.py``; inside
    it, every call of the status graph's writer ``_update_status`` and of
    ``_delete_volume`` (the decision points the #891 ``update_status`` and
    ``delete_volume`` calls were), and every ``execute`` whose SQL writes
    ``volumes.status`` or deletes a volume.  Nothing else writes them
    (``test_volume_status_writers.py``)."""
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            kind = ""
            if rel != _LIFECYCLE:
                if isinstance(func, ast.Attribute) and name in lifecycle_events():
                    kind = "lifecycle event"
            elif name in ("_update_status", "_delete_volume"):
                kind = name
            elif name == "execute" and any(
                isinstance(a, ast.Constant) and isinstance(a.value, str)
                and re.search(r"UPDATE volumes SET status|DELETE FROM volumes ",
                              " ".join(a.value.split()))
                for a in node.args
            ):
                kind = "status write"
            if kind:
                sites.append(Site(kind, rel, node.lineno,
                                  lines[node.lineno - 1].strip()))
    return sorted(sites, key=lambda s: (s.kind, s.path, s.line))


@functools.cache
def _site_lines() -> dict[str, frozenset[int]]:
    by_file: dict[str, set[int]] = {}
    for site in status_write_sites():
        by_file.setdefault(str(SRC / site.path), set()).add(site.line)
    return {path: frozenset(lines) for path, lines in by_file.items()}


@contextlib.contextmanager
def _tracing(hits: dict[tuple[str, int], set[tuple[str, str]]],
             key: tuple[str, str]) -> Iterator[None]:
    """Record, under *key*, which :func:`status_write_sites` run.

    Records nothing while another tracer runs (coverage, a debugger):
    coverage's tracer puts itself back when called from another one, so
    the two cannot share; the reach test then skips."""
    lines = _site_lines()
    previous = sys.gettrace()
    if previous is not None:
        yield
        return

    def _global(frame: Any, event: str, arg: Any) -> Any:
        mine = lines.get(frame.f_code.co_filename)
        if mine is None:
            return None

        def _local(frame: Any, event: str, arg: Any) -> Any:
            if event == "line" and frame.f_lineno in mine:
                hits.setdefault((frame.f_code.co_filename, frame.f_lineno),
                                set()).add(key)
            return _local

        return _local

    sys.settrace(_global)
    try:
        yield
    finally:
        sys.settrace(None)


def site_rows(hits: dict[tuple[str, int], set[tuple[str, str]]],
              ) -> dict[Site, list[tuple[str, str]]]:
    return {
        site: sorted(hits.get((str(SRC / site.path), site.line), set()))
        for site in status_write_sites()
    }


# ---------------------------------------------------------------------
# Running the whole table
# ---------------------------------------------------------------------


def _work_dir(fallback: Callable[[], Path]) -> Path:
    """Where the table runs.  Each row writes and copies small catalogs a
    few times; on tmpfs that takes a third of the time it takes on a disk
    (no fsync), so /dev/shm is used where there is one."""
    import tempfile

    shm = Path("/dev/shm")
    if shm.is_dir():
        with contextlib.suppress(OSError):
            return Path(tempfile.mkdtemp(prefix="lcsas-891-", dir=shm))
    return fallback()


Results = dict[tuple[str, str], tuple[Observed, str]]
Hits = dict[tuple[str, int], set[tuple[str, str]]]


def run_table(base: Path, *, trace: bool = False) -> tuple[Results, Hits]:
    t = build_templates(base)
    results: Results = {}
    hits: Hits = {}
    for key in _pairs():
        results[key] = run_row(
            t, *key, during=(lambda k=key: _tracing(hits, k)) if trace
            else contextlib.nullcontext)
    return results, hits


@pytest.fixture(scope="module")
def table(tmp_path_factory):
    base = _work_dir(lambda: tmp_path_factory.mktemp("lifecycle"))
    try:
        yield run_table(base, trace=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

_STATUSES = ("STAGING", "BURNING", "BURNED", "VERIFIED", "CONSOLIDATING",
             "DEPRECATED", "DESTROYED")


def test_starts_cross_every_status_with_and_without_a_disc():
    have = {(s.status, s.disc == "ACTIVE") for s in STARTS.values()}
    assert have >= {(st, d) for st in _STATUSES for d in (True, False)}
    # #758 and #678: the two states a catalog rebuild leaves.
    assert STARTS["STAGING, rebuilt"].notes == (_REBUILD_NOTE,)
    rebuilt = STARTS["BURNED, rebuilt"]
    assert (rebuilt.disc, rebuilt.notes, rebuilt.events) == (None, (), ())


def test_every_event_and_start_has_exactly_one_row():
    keys = [row.key for row in ROWS]
    assert len(keys) == len(set(keys)), "duplicate rows"
    assert set(keys) == set(_pairs())
    assert list(TABLE) == list(EVENTS), "TABLE lists the events in order"


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r.event} | {r.start}")
def test_row(table, row):
    results, _hits = table
    assert results[row.key] == row.result(), (
        "behaviour changed; if on purpose, print the observed table with "
        "'python -m tests.unit.test_volume_lifecycle_table --observe' and "
        "list the changed rows in the PR")


def test_status_write_sites_are_the_ones_the_table_was_written_for():
    """The 20 ``update_status`` calls, 5 raw status writes and 5 deletions
    the table was written for (#891) became 28 calls of ``VolumeLifecycle``
    events and, inside it, 18 ``_update_status`` and 2 ``_delete_volume``
    calls (events that shared a sequence share one call) and 9 status
    writes (#892).  #906 removed the ISO check's event (2 calls, 1
    ``_update_status``), #907 the dead ``abort_consolidation`` (1 call).
    A new one needs rows that reach it."""
    counts = Counter(site.kind for site in status_write_sites())
    assert counts == {"lifecycle event": 25, "_update_status": 17,
                      "_delete_volume": 2, "status write": 9}


def test_every_status_write_is_reached_by_a_row(table):
    _results, hits = table
    if not hits and sys.gettrace() is not None:
        pytest.skip("another tracer (coverage?) ran; the reach check needs "
                    "its own: run without --cov")
    unreached = [str(site) for site, rows in site_rows(hits).items()
                 if not rows]
    assert unreached == []


def test_markdown_renders_every_row():
    text = markdown()
    for row in ROWS:
        assert f"| {_md(row.start)} | **{row.end}** |" in text


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown() -> str:
    """The table for the owner to read (from the data above; runs
    nothing)."""
    out = ["# Volume lifecycle today (#891)", "",
           "Rendered by `python -m tests.unit.test_volume_lifecycle_table "
           "--markdown` from `tests/unit/test_volume_lifecycle_table.py`.",
           "", "## Start states", "", "| start | status | disc | what |",
           "|---|---|---|---|"]
    for name, st in STARTS.items():
        disc = {"ACTIVE": "in service at Home", "lost": "lost (Home)",
                None: "none"}[st.disc]
        out.append(f"| {_md(name)} | {st.status} | {disc} | {_md(st.what)} |")
    suspects = [row for row in ROWS if row.suspect]
    out += ["", f"## Suspect rows ({len(suspects)})", "",
            "| event | start | end | suspect |", "|---|---|---|---|"]
    out += [f"| {_md(r.event)} | {_md(r.start)} | {r.end} | {_md(r.suspect)} |"
            for r in suspects]
    for event, ev in EVENTS.items():
        out += ["", f"## {event}", "", f"`{_md(ev.command)}`", "",
                "| start | end | discs | events written | rc | owner sees "
                "| deleted by | suspect |",
                "|---|---|---|---|---|---|---|---|"]
        for row in ROWS:
            if row.event == event:
                out.append(
                    f"| {_md(row.start)} | **{row.end}** | {_md(row.discs)} "
                    f"| {_md(row.events)} | {row.rc} | {_md(row.says)} "
                    f"| {row.deleted_by} | {_md(row.suspect)} |")
    out += ["", "`deleted by`: " + "; ".join(
        f"{k} = `{v}`" for k, v in DELETERS.items()) + "."]
    return "\n".join(out) + "\n"


def _q(value: str) -> str:
    """A double-quoted Python string literal."""
    return json.dumps(value, ensure_ascii=False)


def _literal(value: str, indent: int) -> str:
    """*value* as a Python literal that fits in 100 columns."""
    if indent + len(_q(value)) <= 98:
        return _q(value)
    chunks, cur = [], ""
    for word in value.split(" "):
        if cur and indent + 1 + len(_q(cur + " " + word)) > 97:
            chunks.append(cur + " ")
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    chunks.append(cur)
    pad = " " * (indent + 1)
    return "(" + f"\n{pad}".join(_q(c) for c in chunks) + ")"


def table_literal(observed: Results) -> str:
    """The TABLE literal for *observed*, keeping the suspect notes the
    current TABLE has."""
    known = {row.key: row.suspect for row in ROWS}
    out = ["TABLE: dict[str, list[dict[str, str]]] = {"]
    for event, ev in EVENTS.items():
        out.append(f"    {_q(event)}: [")
        for start in ev.starts:
            obs, dels = observed[(event, start)]
            args = [_q(start), _q(obs.end)]
            if obs.discs:
                args.append(_q(obs.discs))
            kwargs = [(name, value) for name, value, default in (
                ("events", obs.events, ""), ("rc", obs.rc, "0"),
                ("says", obs.says, ""), ("deleted_by", dels, ""),
                ("suspect", known.get((event, start), ""), ""),
            ) if value != default]
            lines = ["        R(" + ", ".join(args)]
            for name, value in kwargs:
                lines[-1] += ","
                lines.append(
                    f"          {name}={_literal(value, 11 + len(name))}")
            lines[-1] += "),"
            out.extend(lines)
        out.append("    ],")
    out.append("}")
    return "\n".join(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--markdown"]:
        sys.stdout.write(markdown())
    elif args[:1] in (["--observe"], ["--sites"]):
        import tempfile

        base = _work_dir(lambda: Path(tempfile.mkdtemp(prefix="lcsas-891-")))
        try:
            results, hits = run_table(base, trace=args[0] == "--sites")
        finally:
            shutil.rmtree(base, ignore_errors=True)
        if args[0] == "--observe":
            print(table_literal(results))
        else:
            for site, rows in site_rows(hits).items():
                print(f"{site.kind:14} {site}  {site.text}")
                for event, start in rows[:3]:
                    print(f"    {event} | {start}")
                if len(rows) > 3:
                    print(f"    ... and {len(rows) - 3} more rows")
    else:
        sys.exit("usage: python -m tests.unit.test_volume_lifecycle_table "
                 "--markdown | --observe | --sites")
