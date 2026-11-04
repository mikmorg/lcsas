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


@dataclass(frozen=True)
class FixtureSet:
    """A backup set in a fixture: its id, its name and the name restore.bat
    shows (``name_cmd``, C1)."""

    repo_id: str
    name: str
    name_cmd: str

    def as_json(self) -> dict[str, str]:
        return {"id": self.repo_id, "name": self.name, "name_cmd": self.name_cmd}


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
                                           name and name_cmd (ASCII JSON)

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

    # 1. synthetic source trees + the restored set's manifest
    # 2. rustic repos (fixture-only passwords)
    manifest: dict[str, str] = {}
    for slug, _name, count, size, pw in plan:
        tree = generate_source_tree(sources / slug, count=count, size_bytes=size)
        if slug == "win":
            manifest = tree
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
    for slug, name, _count, _size, _pw in plan:
        repo_id = add_repo(db_path, name, mirror_base / slug)
        sets.append(FixtureSet(repo_id, name, cmd_name(name, repo_id)))
        scanned = scan_mirror_packs(mirror_base / slug).packs
        DeltaAnalyzer(conn, scanned, repo_id=repo_id).register_new_packs()
        conn.commit()

    # 4. burn pipeline → data ISOs (no ECC; the gate exercises the restore
    #    path, not RS03 repair — that has its own opt-in suite).  Each
    #    further set adds its index/snapshots/keys to every disc, as in the
    #    blind fixture's reserve.
    repos = {
        name: RepositoryConfig(
            name=name, mirror_path=mirror_base / slug, password_file=pw
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

    # 6. manifest the restorable source tree, and say which set it is
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    sets_path = workdir / "sets.json"
    sets_path.write_text(json.dumps({
        "restore": sets[0].as_json(),
        "others": [s.as_json() for s in sets[1:]],
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
