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

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_src_on_path() -> None:
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


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


@dataclass(frozen=True)
class WindowsFixture:
    """Artifacts produced for the windows-e2e workflow's `fixture` job."""

    meta_iso: Path
    data_isos: list[Path]
    manifest_path: Path
    password_file: Path
    source_root: Path


def build_windows_fixture(
    workdir: Path,
    *,
    file_count: int = 12,
    file_bytes: int = 64 * 1024,
) -> WindowsFixture:
    """Build a complete single-tenant TEST_TINY fixture for the Windows
    restore-journey gate, with NO root/cdemu/privileged steps.

    Produces, under *workdir*:
      * ``iso_out/LCSAS_META.iso``       — bootable meta tree + tier-1 .exe
      * ``iso_out/<label>.iso`` × N      — data discs (TEST_TINY-sized)
      * ``manifest.json``                — {relative posix path: sha256} of
                                           the source tree the heir restores
      * ``repo.pw``                      — fixture-only repo password (the
                                           workflow treats this as a per-run
                                           secret; never a real credential)

    Keep *file_count* small (Defender real-time scanning slows many-small-
    file restores on the windows runner) but >1 so the restore exercises a
    real tree.
    """
    _ensure_src_on_path()
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_unarchived_packs
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs
    from lcsas.staging.metadata import MIN_HOLOGRAPHIC_RESERVE_BYTES

    tenant = "win"
    sources = workdir / "sources"
    mirror_base = workdir / "mirror"
    mirror = mirror_base / tenant
    staging = workdir / "staging"
    iso_out = workdir / "iso_out"
    meta_stage = workdir / "meta_stage"
    db_path = workdir / "catalog.db"
    pw_file = workdir / "repo.pw"

    for d in (sources, mirror_base, staging, iso_out):
        d.mkdir(parents=True, exist_ok=True)

    # 1. synthetic source tree + manifest
    src_tree = sources / tenant
    manifest = generate_source_tree(
        src_tree, count=file_count, size_bytes=file_bytes
    )

    # 2. rustic repo (fixture-only password)
    pw_file.write_text(os.urandom(16).hex())
    pw_file.chmod(0o600)
    init_rustic_repo(
        mirror=mirror, password_file=pw_file, source_tree=src_tree
    )

    # 3. catalog + pack registration
    if db_path.exists():
        db_path.unlink()
    conn = get_connection(db_path)
    create_all(conn)
    register_repo(conn, tenant, tenant, str(mirror))
    conn.commit()
    scanned = scan_mirror_packs(mirror).packs
    DeltaAnalyzer(conn, scanned, repo_id=tenant).register_new_packs()
    conn.commit()

    # 4. burn pipeline → data ISOs (no ECC; the gate exercises the restore
    #    path, not RS03 repair — that has its own opt-in suite).
    repos = {
        tenant: RepositoryConfig(
            name=tenant, mirror_path=mirror, password_file=pw_file
        )
    }
    config = LCSASConfig(
        mirror_base_path=mirror_base,
        staging_path=staging,
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="LCSAS",
        metadata_reserve_bytes=MIN_HOLOGRAPHIC_RESERVE_BYTES,
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
        allow_no_dvdisaster_source=True,
    )

    # 6. manifest the restorable source tree
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    return WindowsFixture(
        meta_iso=meta_iso,
        data_isos=data_isos,
        manifest_path=manifest_path,
        password_file=pw_file,
        source_root=src_tree,
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
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
