"""Shared LCSAS e2e fixture builders (INFRA-01).

The rustic-repo and ISO-mastering primitives used to live only inside
``tests/e2e/cdemu_blind_restore/setup.py``, entangled with the cdemu /
tmux / blind-agent machinery and requiring root.  The Windows-journey
workflow (`.github/workflows/windows-e2e.yml`) needs the *same* builders
on a plain ubuntu runner with no cdemu, so they are extracted here as a
single source of truth.

Nothing in this module needs root, cdemu, or any privileged setup — it
only shells out to ``rustic`` and ``xorriso`` and uses the in-tree LCSAS
burn pipeline.  ``setup.py`` imports these primitives; the Windows
fixture job calls :func:`build_windows_fixture` directly.

stdlib-only at runtime, matching the project's zero-dependency rule.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The backup set build_windows_fixture makes.  Its name has a space, which
# name_cmd keeps; the macOS journey restores it.
WINDOWS_SET_NAME = "Family photos"


def _ensure_src_on_path() -> None:
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


# ---------------------------------------------------------------------------
# Backup sets, registered the way an owner does
# ---------------------------------------------------------------------------


def add_repo(db_path: Path, name: str, mirror: Path) -> str:
    """Register the backup set at *mirror* as *name* with ``lcsas repo
    add``, as an owner does, and return the id it was given.

    ``repo add`` gives every set a fresh UUID, and every disc keeps the
    set's metadata in ``metadata/<id>/``.  The fixtures used to register
    ``repo_id == name``, so no gate ever saw an id that differs from the
    name, and drill #476 found the set menu showing UUIDs (#606, #617).
    The CLI log line goes to stderr, so a caller's stdout stays its own.
    """
    _ensure_src_on_path()
    from lcsas.cli.main import main as lcsas_main

    with contextlib.redirect_stdout(sys.stderr):
        rc = lcsas_main(["--db", str(db_path), "repo", "add", name, str(mirror)])
    if rc != 0:
        raise RuntimeError(f"lcsas repo add {name!r} {mirror} exited {rc}")
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT repo_id FROM repositories WHERE name = ?", (name,)
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"{len(rows)} catalog rows are named {name!r}")
    repo_id = str(rows[0][0])
    uuid.UUID(repo_id)  # ValueError if repo add stops making UUIDs
    if repo_id == name:
        raise RuntimeError(f"repo add gave {name!r} its own name as its id")
    return repo_id


def split_password(
    workdir: Path,
    *,
    db_path: Path,
    name: str,
    mirror: Path,
    password_file: Path,
    threshold: int,
    shares: int,
    cards_dir: Path,
) -> tuple[int, list[Path]]:
    """Split the password of the backup set *name* into *shares* share
    cards, any *threshold* of which rebuild it, as an owner does: the
    set's config says ``key_split = true`` with its K and N, and ``lcsas
    key split`` checks that the password opens the set's keys, records the
    split in the catalog (key_escrow, under the set's id) and writes the
    printable cards.  The burn's drift guard needs that record, and the
    set markers on the discs take ``split=K-of-N`` and the split id from it
    (C1).

    Returns the split id (the cards' "Split ID") and the printed cards,
    copied into *cards_dir*, card 1 first.  The cards are invented test
    data, made fresh with every fixture: never commit one.  The command's
    lines go to stderr, as in :func:`add_repo`; it prints no share words.
    """
    _ensure_src_on_path()
    from lcsas.cli.main import main as lcsas_main
    from lcsas.db.key_escrow import get_split_for_repo
    from lcsas.keyshare import extract_mnemonic, share_identifier

    # The owner's lcsas.toml, cut down to what `key split` reads.  JSON
    # string quoting is valid TOML for these plain paths and names.
    slug = mirror.name
    cfg = workdir / f"split-{slug}.toml"
    cfg.write_text("\n".join([
        "[paths]",
        f"mirror_base = {json.dumps(str(mirror.parent))}",
        f"staging = {json.dumps(str(workdir / 'staging'))}",
        f"database = {json.dumps(str(db_path))}",
        f"[repos.{json.dumps(name)}]",
        f"mirror_path = {json.dumps(str(mirror))}",
        f"password_file = {json.dumps(str(password_file))}",
        "key_split = true",
        f"key_threshold = {threshold}",
        f"key_shares = {shares}",
    ]) + "\n", encoding="utf-8")
    out = workdir / "keyshares" / slug
    with contextlib.redirect_stdout(sys.stderr):
        rc = lcsas_main([
            "--config", str(cfg), "key", "split", "--repo", name,
            "--out", str(out),
        ])
    if rc != 0:
        raise RuntimeError(f"lcsas key split --repo {name!r} exited {rc}")
    printed = sorted(
        out.glob("*-share-*-card.txt"),
        key=lambda p: int(p.name.rsplit("-share-", 1)[1].split("-", 1)[0]),
    )
    if len(printed) != shares:
        raise RuntimeError(f"key split wrote {len(printed)} cards, not {shares}")
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for card in printed:
        dst = cards_dir / card.name
        shutil.copy2(card, dst)
        cards.append(dst)
    split_id = share_identifier(
        extract_mnemonic(cards[0].read_text(encoding="utf-8"))
    )
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row  # as the catalog code reads rows
        repo_id = conn.execute(
            "SELECT repo_id FROM repositories WHERE name = ?", (name,)
        ).fetchone()[0]
        record = get_split_for_repo(conn, str(repo_id))
    if record is None or (record.threshold, record.shares, record.slip39_id) != (
        threshold, shares, split_id,
    ):
        raise RuntimeError(f"key split recorded {record!r} for {name!r}")
    return split_id, cards


def _is_uuid(text: str) -> bool:
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def set_marker_problems(
    metadata_dir: Path, names: Iterable[str], *, require_all: bool,
) -> list[str]:
    """What is wrong with the backup set folders in *metadata_dir* (a
    disc's ``metadata/``), measured against an owner's discs (#617).

    Every folder must be named by a UUID id, never by a set's name.  Its
    ``LCSAS_REPO.txt`` marker must give that id and one of *names*, and no
    two folders may give the same name.  With *require_all*, every one of
    *names* must have a folder: the META disc carries every set, a data
    disc only the sets it holds packs of.  An empty list means all is well.
    """
    _ensure_src_on_path()
    from lcsas.staging.repo_marker import read_repo_marker

    wanted = set(names)
    problems: list[str] = []
    found: dict[str, str] = {}
    folders = (
        sorted(p for p in metadata_dir.iterdir() if p.is_dir())
        if metadata_dir.is_dir() else []
    )
    if not folders:
        problems.append(f"{metadata_dir}: no backup set folders")
    for folder in folders:
        if not _is_uuid(folder.name):
            problems.append(
                f"{folder}: the folder is not named by a UUID id"
                + (" (it is the set's name)" if folder.name in wanted else "")
            )
            continue
        marker = read_repo_marker(folder)
        if marker is None:
            problems.append(f"{folder}: no LCSAS_REPO.txt whose id= is the folder")
            continue
        name = marker.get("name", "")
        if name not in wanted:
            problems.append(f"{folder}: the marker names {name!r}, no set of this archive")
        elif name in found:
            problems.append(f"{folder}: {name!r} also has the folder {found[name]}")
        else:
            found[name] = folder.name
    if require_all:
        missing = sorted(wanted - set(found))
        if missing:
            problems.append(f"{metadata_dir}: no folder for {', '.join(missing)}")
    return problems


def restore_set_span_problem(sets_path: Path, *, minimum: int = 2) -> str | None:
    """Why the restored set's packs do not lie on at least *minimum* data
    discs with no one disc holding them all, or None if they do.

    Reads ``restore_discs`` in a fixture's ``sets.json``.  windows-e2e
    restores with every disc mounted and checks the tree byte for byte;
    that proves the restore read more than one disc only when the set it
    restored needs more than one.  The other sets' packs share the discs,
    so the count must be of the restored set's packs alone.
    """
    sets = json.loads(sets_path.read_text(encoding="utf-8"))
    discs: dict[str, list[str]] = sets.get("restore_discs") or {}
    every = {sha for packs in discs.values() for sha in packs}
    if not every:
        return f"{sets_path}: no disc holds a pack of the restored set"
    holding = sorted(label for label, packs in discs.items() if packs)
    if len(holding) < minimum:
        return (
            f"the restored set's {len(every)} packs lie on {len(holding)} "
            f"data disc(s) ({', '.join(holding)}); need >= {minimum}"
        )
    for label in holding:
        if set(discs[label]) >= every:
            return f"{label} alone holds all {len(every)} packs of the restored set"
    return None


# ---------------------------------------------------------------------------
# SHA-256 tree manifest
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree_manifest(root: Path) -> dict[str, str]:
    """Map every regular file under *root* to its SHA-256, keyed by the
    POSIX-style path relative to *root* (stable across OSes)."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            manifest[path.relative_to(root).as_posix()] = sha256_file(path)
    return manifest


# ---------------------------------------------------------------------------
# Synthetic source tree
# ---------------------------------------------------------------------------


def generate_source_tree(
    root: Path, *, count: int, size_bytes: int
) -> dict[str, str]:
    """Create *count* incompressible files of *size_bytes* under *root*
    and return their SHA-256 manifest (keyed by relative POSIX path)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for i in range(count):
        path = root / f"file_{i:03d}.bin"
        with open(path, "wb") as f:
            remaining = size_bytes
            while remaining > 0:
                chunk = os.urandom(min(1 << 20, remaining))
                f.write(chunk)
                remaining -= len(chunk)
    return sha256_tree_manifest(root)


# ---------------------------------------------------------------------------
# Rustic repo
# ---------------------------------------------------------------------------


def init_rustic_repo(
    *,
    mirror: Path,
    password_file: Path,
    source_tree: Path,
    small_packs: bool = True,
) -> None:
    """Initialise a rustic repo at *mirror*, configure small packs for a
    realistic disc count, and back up *source_tree* into it.

    *password_file* must already exist (caller owns its lifecycle /
    permissions).  This is the exact rustic invocation sequence the cdemu
    fixture used, extracted verbatim so both fixtures stay in lockstep.
    """
    mirror.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "RUSTIC_REPOSITORY": str(mirror),
        "RUSTIC_PASSWORD_FILE": str(password_file),
    }
    subprocess.run(["rustic", "init"], check=True, env=env)
    if small_packs:
        subprocess.run(
            [
                "rustic", "config",
                "--set-datapack-size", "256KiB",
                "--set-datapack-size-limit", "512KiB",
                "--set-treepack-size", "128KiB",
                "--set-treepack-size-limit", "256KiB",
            ],
            check=True, env=env,
        )
    subprocess.run(["rustic", "backup", str(source_tree)], check=True, env=env)


# ---------------------------------------------------------------------------
# ISO mastering
# ---------------------------------------------------------------------------


def master_iso(stage: Path, iso_path: Path, *, volume_label: str) -> Path:
    """Master *stage* into *iso_path* with xorriso (Rock Ridge + Joliet).

    The ``.iso`` extension on *iso_path* is load-bearing for the Windows
    consumer: PowerShell ``Mount-DiskImage`` requires it.
    """
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "xorriso", "-as", "mkisofs",
            "-V", volume_label,
            "-R", "-J",
            "-o", str(iso_path),
            str(stage),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return iso_path


def build_meta_iso(
    *,
    meta_stage: Path,
    iso_out: Path,
    catalog_db: Path | None = None,
    allow_no_dvdisaster_source: bool = False,
) -> Path:
    """Build a production meta tree with :class:`MetaVolumeBuilder` and
    master it to ``<iso_out>/LCSAS_META.iso``.

    *allow_no_dvdisaster_source* lets a fixture build proceed without the
    ~600 MB upstream recovery cache (`make fetch-recovery`): the meta tree
    still carries restore.bat + the committed tier-1 binaries, which is all
    the Windows-journey gate needs.  The cdemu fixture leaves it False (its
    host has the full cache and exercises the complete disc).
    """
    _ensure_src_on_path()
    from lcsas.meta.builder import MetaVolumeBuilder

    if meta_stage.exists():
        shutil.rmtree(meta_stage)
    meta_stage.mkdir(parents=True)
    MetaVolumeBuilder(
        meta_stage,
        catalog_db_path=catalog_db,
        allow_no_dvdisaster_source=allow_no_dvdisaster_source,
    ).build()
    return master_iso(
        meta_stage, iso_out / "LCSAS_META.iso", volume_label="LCSAS_META"
    )


# ---------------------------------------------------------------------------
# Self-contained Windows fixture
# ---------------------------------------------------------------------------

# windows-e2e's backup sets.  The restored one has an accented letter,
# which name_cmd turns into "_", so name_cmd differs from the name (C1);
# the second set puts the menu on screen, as on the drill's two-set
# archive (#606, #617).  (\u00ed is a small i with an acute accent.)
WINDOWS_E2E_SET_NAME = "Fotos da fam\u00edlia"
WINDOWS_E2E_OTHER_SET_NAMES = ("Work documents",)
# windows-e2e's second set has its password split 2-of-3, as the drill's
# archive has one set split and one not (#607, F6).  Its cards go into
# the fixture artifact, and a leg types two of them on real Windows.
WINDOWS_E2E_SPLIT_SETS: dict[str, tuple[int, int]] = {"Work documents": (2, 3)}
# windows-e2e's size (#517): 8 x 512 KiB of incompressible data does not
# fit one TEST_TINY disc, so the restored set spans several.  The workflow
# and tests/integration/test_e2e_fixture_sets.py build this same shape.
WINDOWS_E2E_FILE_COUNT = 8
WINDOWS_E2E_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class FixtureSet:
    """A backup set in a fixture: its id, its name and the name restore.bat
    shows (``name_cmd``, C1).  A set built with its files also names its
    manifest, and a split set its K and N, its split id and its printed
    share cards (paths relative to the fixture folder)."""

    repo_id: str
    name: str
    name_cmd: str
    manifest: str = ""
    split: tuple[int, int] | None = None
    split_id: int | None = None
    cards: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        got: dict[str, object] = {
            "id": self.repo_id, "name": self.name, "name_cmd": self.name_cmd,
            # As the set marker says it (C1).  The share words stay in the
            # card files: the fixture job prints sets.json.
            "split": f"{self.split[0]}-of-{self.split[1]}" if self.split else "none",
        }
        if self.manifest:
            got["manifest"] = self.manifest
        if self.split:
            got["split_id"] = f"{self.split_id:05d}" if self.split_id is not None else ""
            got["cards"] = list(self.cards)
        return got


@dataclass(frozen=True)
class WindowsFixture:
    """Artifacts produced for the windows-e2e workflow's `fixture` job."""

    meta_iso: Path
    data_isos: list[Path]
    manifest_path: Path
    password_file: Path
    source_root: Path
    restore_set: FixtureSet
    other_sets: tuple[FixtureSet, ...]
    sets_path: Path


def build_windows_fixture(
    workdir: Path,
    *,
    file_count: int = 12,
    file_bytes: int = 64 * 1024,
    meta_metadata: bool = False,
    set_name: str = WINDOWS_SET_NAME,
    other_set_names: tuple[str, ...] = (),
    split_sets: Mapping[str, tuple[int, int]] | None = None,
) -> WindowsFixture:
    """Build a complete TEST_TINY fixture for the Windows restore-journey
    gate, with NO root/cdemu/privileged steps.

    Produces, under *workdir*:
      * ``iso_out/LCSAS_META.iso``       — bootable meta tree + tier-1 .exe
      * ``iso_out/<label>.iso`` × N      — data discs (TEST_TINY-sized)
      * ``manifest.json``                — {relative posix path: sha256} of
                                           the source tree the heir restores
      * ``repo.pw``                      — fixture-only repo password (the
                                           workflow treats this as a per-run
                                           secret; never a real credential)
      * ``sets.json``                    — the backup sets: ``restore`` (the
                                           one ``manifest.json`` describes)
                                           and ``others``, each with its id,
                                           name and name_cmd; and
                                           ``restore_discs``, {data disc
                                           label: the restored set's pack
                                           ids on it} (ASCII JSON)
      * ``manifest-<folder>.json``       — the same as ``manifest.json``,
                                           for each other set
      * ``cards/``                       — the printed share cards of each
                                           set in *split_sets*

    Every set is registered with ``lcsas repo add`` (:func:`add_repo`), so
    its id is a UUID and its ``metadata/<id>/`` folder on the discs is not
    its name (#617).  *set_name* names the set that is restored.  Each of
    *other_set_names* adds a small set with its own password, which
    nobody restores: two sets or more put restore.bat's set menu on
    screen.  The config key, the catalog name and the marker's ``name=``
    are the same text for every set.  Folder names stay ASCII (``win``,
    ``other1``, ...): the restored tree mirrors the source path, and
    Windows tier 1's non-ASCII path handling is its own issue (#619).

    Keep *file_count* small (Defender real-time scanning slows many-small-
    file restores on the windows runner) but >1 so the restore exercises a
    real tree.

    *meta_metadata* also copies the backup set's metadata (keys, index,
    snapshots, config) onto the meta ISO, as a real ``lcsas meta build``
    does: the builder reads it from the catalog.  windows-e2e's one-drive
    leg needs it (#617): with only the meta disc in the drive at launch,
    that copy is the only place restore.bat can find the set.  Off by
    default, so the other users of this fixture keep their meta ISO.

    *split_sets* maps a set's name to the K and N its password is split
    into, as an owner splits it (:func:`split_password`): the set's config
    says so, and the split is recorded in the catalog before the burn, so
    every disc's marker for the set says ``split=K-of-N`` (#607).
    """
    _ensure_src_on_path()
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_unarchived_packs
    from lcsas.db.schema import create_all
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs
    from lcsas.staging.metadata import MIN_HOLOGRAPHIC_RESERVE_BYTES
    from lcsas.staging.repo_marker import cmd_name

    sources = workdir / "sources"
    mirror_base = workdir / "mirror"
    staging = workdir / "staging"
    iso_out = workdir / "iso_out"
    meta_stage = workdir / "meta_stage"
    db_path = workdir / "catalog.db"
    pw_file = workdir / "repo.pw"

    for d in (sources, mirror_base, staging, iso_out):
        d.mkdir(parents=True, exist_ok=True)

    # (folder name, set name, file count, file size, password file).  The
    # other sets are small: they exist to be listed, not restored.
    plan = [("win", set_name, file_count, file_bytes, pw_file)]
    for i, other in enumerate(other_set_names, start=1):
        plan.append((f"other{i}", other, 2, 32 * 1024, workdir / f"other{i}.pw"))
    if len({name for _, name, _, _, _ in plan}) != len(plan):
        raise ValueError(f"backup set names must differ: {plan!r}")
    splits = dict(split_sets or {})
    unknown = set(splits) - {name for _, name, _, _, _ in plan}
    if unknown:
        raise ValueError(f"split_sets names no set of this fixture: {sorted(unknown)}")

    # 1. synthetic source trees + each set's manifest
    # 2. rustic repos (fixture-only passwords)
    manifests: dict[str, dict[str, str]] = {}
    for slug, _name, count, size, pw in plan:
        manifests[slug] = generate_source_tree(
            sources / slug, count=count, size_bytes=size,
        )
        pw.write_text(os.urandom(16).hex())
        pw.chmod(0o600)
        init_rustic_repo(
            mirror=mirror_base / slug, password_file=pw,
            source_tree=sources / slug,
        )

    # 3. catalog: each set registered as an owner does (repo add), then
    #    its packs
    if db_path.exists():
        db_path.unlink()
    conn = get_connection(db_path)
    create_all(conn)
    conn.commit()
    sets: list[FixtureSet] = []
    for slug, name, _count, _size, pw in plan:
        repo_id = add_repo(db_path, name, mirror_base / slug)
        # 3b. a split set: its password into share cards, recorded in the
        #     catalog before anything is burned (#607)
        split = splits.get(name)
        split_id: int | None = None
        cards: tuple[str, ...] = ()
        if split is not None:
            split_id, card_paths = split_password(
                workdir, db_path=db_path, name=name,
                mirror=mirror_base / slug, password_file=pw,
                threshold=split[0], shares=split[1],
                cards_dir=workdir / "cards",
            )
            cards = tuple(p.relative_to(workdir).as_posix() for p in card_paths)
        manifest_name = "manifest.json" if slug == "win" else f"manifest-{slug}.json"
        sets.append(FixtureSet(
            repo_id, name, cmd_name(name, repo_id), manifest=manifest_name,
            split=split, split_id=split_id, cards=cards,
        ))
        scanned = scan_mirror_packs(mirror_base / slug).packs
        DeltaAnalyzer(conn, scanned, repo_id=repo_id).register_new_packs()
        conn.commit()

    # 4. burn pipeline → data ISOs (no ECC; the gate exercises the restore
    #    path, not RS03 repair — that has its own opt-in suite).  Each
    #    further set adds its index/snapshots/keys to every disc, as in the
    #    blind fixture's reserve.  A split set's config says so, as the
    #    owner's lcsas.toml does; the burn's drift guard checks it against
    #    the recorded split.
    repos = {
        name: RepositoryConfig(
            name=name, mirror_path=mirror_base / slug, password_file=pw,
            key_split=True if name in splits else None,
            key_threshold=splits[name][0] if name in splits else None,
            key_shares=splits[name][1] if name in splits else None,
        )
        for slug, name, _count, _size, pw in plan
    }
    config = LCSASConfig(
        mirror_base_path=mirror_base,
        staging_path=staging,
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="LCSAS",
        metadata_reserve_bytes=(
            MIN_HOLOGRAPHIC_RESERVE_BYTES + 150_000 * (len(plan) - 1)
        ),
        repositories=repos,
    )

    class _NoOpEcc:
        def augment_iso(self, iso_path: Path, redundancy_pct: int = 15) -> None:
            pass

        def verify_iso(self, iso_path: Path) -> bool:
            return True

        def repair_iso(self, iso_path: Path) -> bool:
            return True

    orchestrator = BurnOrchestrator(
        config, conn, SubprocessXorrisoRunner(), _NoOpEcc()
    )
    data_isos: list[Path] = []
    while get_unarchived_packs(conn):
        prepared = orchestrator.prepare(media_type=MediaType.TEST_TINY)
        iso_path = iso_out / f"{prepared.volume_label}.iso"
        orchestrator.execute(prepared, iso_output=iso_path, skip_burn=True)
        data_isos.append(iso_path)
    # Where the restored set's packs went, by disc label.  The other sets'
    # packs share the discs, so counting every pack on a disc proves
    # nothing about the restored set (restore_set_span_problem).
    restore_discs: dict[str, list[str]] = {}
    for label, sha in conn.execute(
        """
        SELECT v.label, p.sha256
        FROM packs p
        JOIN volume_packs vp ON p.pack_id = vp.pack_id
        JOIN volumes v ON vp.volume_id = v.volume_id
        WHERE p.repo_id = ? AND p.is_pruned = 0
        ORDER BY v.label, p.sha256
        """,
        (sets[0].repo_id,),
    ):
        restore_discs.setdefault(str(label), []).append(str(sha))
    conn.close()

    # 5. meta ISO (carries the tier-1 lcsas-restore.exe + a repo to discover).
    #    Tolerate a missing upstream recovery cache so the gate runs on a
    #    bare ubuntu runner without `make fetch-recovery`'s ~600 MB download.
    meta_iso = build_meta_iso(
        meta_stage=meta_stage,
        iso_out=iso_out,
        catalog_db=db_path if meta_metadata else None,
        allow_no_dvdisaster_source=True,
    )
    # The split reaches the META disc's markers, which restore.bat reads
    # to ask for the cards; tests/integration/test_e2e_fixture_sets.py
    # checks the data discs' too.
    if meta_metadata:
        from lcsas.staging.repo_marker import read_repo_marker

        for s in sets:
            marker = read_repo_marker(meta_stage / "metadata" / s.repo_id) or {}
            want = s.as_json()
            got = (marker.get("split"), marker.get("split_id", ""))
            if got != (want["split"], want.get("split_id", "")):
                raise RuntimeError(
                    f"the META disc's marker for {s.name!r} says split={got[0]!r} "
                    f"split_id={got[1]!r}, not {want['split']!r} "
                    f"{want.get('split_id', '')!r}"
                )

    # 6. manifest each set's source tree (manifest.json is the restored
    #    set's), and say which set is which
    for (slug, *_rest), fixture_set in zip(plan, sets, strict=True):
        (workdir / fixture_set.manifest).write_text(
            json.dumps(manifests[slug], indent=2, sort_keys=True)
        )
    manifest_path = workdir / "manifest.json"
    sets_path = workdir / "sets.json"
    sets_path.write_text(json.dumps({
        "restore": sets[0].as_json(),
        "others": [s.as_json() for s in sets[1:]],
        "restore_discs": restore_discs,
    }, indent=2) + "\n")

    return WindowsFixture(
        meta_iso=meta_iso,
        data_isos=data_isos,
        manifest_path=manifest_path,
        password_file=pw_file,
        source_root=sources / "win",
        restore_set=sets[0],
        other_sets=tuple(sets[1:]),
        sets_path=sets_path,
    )


def _main(argv: list[str]) -> int:
    """CLI entry for the windows-e2e `fixture` job:

        python tests/e2e/fixture_lib.py <workdir>

    Builds the Windows fixture and prints the produced artifact paths as
    JSON to stdout so the workflow step can pick them up.
    """
    if len(argv) != 2:
        print("usage: fixture_lib.py <workdir>", file=sys.stderr)
        return 2
    workdir = Path(argv[1]).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    fx = build_windows_fixture(workdir)
    print(json.dumps({
        "meta_iso": str(fx.meta_iso),
        "data_isos": [str(p) for p in fx.data_isos],
        "manifest": str(fx.manifest_path),
        "password_file": str(fx.password_file),
        "sets": str(fx.sets_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
