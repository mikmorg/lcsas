#!/usr/bin/env python3
"""Manual single-drive restore smoke test.

Builds a tiny synthetic repository, runs the LCSAS burn pipeline against
it to produce a handful of small ISO images, builds the production
meta disc + ISO image, and then drives the production ``restore.sh`` in
single-drive mode against a cdemu-backed virtual drive: each disc it asks
for is loaded and mounted where restore.sh looks for discs (#756).

This exists to gate the blind-restore acceptance test: if this smoke
test cannot complete the restore with the unmodified production meta
disc, the blind rig won't either.

Run as:  sudo python3 scripts/smoke_single_drive.py  (root: mount, cdemu)
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/mnt/lcsas-data/smoke")
SRC_DIR = ROOT / "source"
MIRROR_DIR = ROOT / "mirror/smoke"
STAGING_DIR = ROOT / "staging"
ISO_OUT = ROOT / "iso_out"
DB_PATH = ROOT / "catalog.db"
META_STAGE = ROOT / "meta_stage"
META_ISO = ISO_OUT / "LCSAS_META.iso"
RESTORE_TARGET = ROOT / "restored"
# restore.sh's work folder is its tools folder (LCSAS_TOOLS_DIR); it looks
# for discs in MOUNT_DIR (LCSAS_MOUNT_DIRS), where the drive is mounted.
TOOLS_DIR = ROOT / "lcsas-restore-tools"
MOUNT_DIR = ROOT / "mnt"
PW_FILE = ROOT / "smoke.pw"
CDEMU = Path(__file__).parent / "cdemu_drive.sh"
# The repository root, for tests.e2e.restore_sh_driver (and src/ for lcsas).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_NAME = "smoke"
REPO_PASSWORD = "smoke-test-password"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def banner(msg: str) -> None:
    print(f"\n\033[1;36m━━━ {msg} ━━━\033[0m")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def clean_root() -> None:
    if ROOT.exists():
        # use sudo in case prior runs left root-owned files (mounts, restore output)
        subprocess.run(["sudo", "rm", "-rf", str(ROOT)], check=True)
    ROOT.mkdir(parents=True)
    for d in (SRC_DIR, MIRROR_DIR.parent, STAGING_DIR, ISO_OUT,
              META_STAGE, RESTORE_TARGET, MOUNT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def generate_source() -> dict[str, str]:
    """Create a handful of random files and return {relpath: sha256}.

    Sized for TEST_TINY media (1 MB) — each file is ~100 KB so several
    packs are produced, exercising the multi-disc swap loop without any
    individual pack exceeding the volume capacity.
    """
    manifest: dict[str, str] = {}
    for i in range(40):
        name = f"file_{i:02d}.bin"
        blob = os.urandom(100_000)  # 100 KB each → ~4 MB total
        (SRC_DIR / name).write_bytes(blob)
        manifest[name] = hashlib.sha256(blob).hexdigest()
    print(f"  generated {len(manifest)} files in {SRC_DIR}")
    return manifest


def init_rustic_and_backup() -> None:
    PW_FILE.write_text(REPO_PASSWORD)
    PW_FILE.chmod(0o600)
    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "RUSTIC_REPOSITORY": str(MIRROR_DIR),
           "RUSTIC_PASSWORD_FILE": str(PW_FILE)}
    sh(["rustic", "init"], env=env)
    sh(["rustic", "config",
        "--set-datapack-size", "200KiB",
        "--set-datapack-size-limit", "300KiB",
        "--set-treepack-size", "100KiB",
        "--set-treepack-size-limit", "200KiB"], env=env)
    sh(["rustic", "backup", str(SRC_DIR)], env=env)


def run_lcsas_burn() -> list[Path]:
    """Use the LCSAS Python API to scan and burn to ISOs."""
    # Add src/ to path so we can import lcsas
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(DB_PATH)
    create_all(conn)

    register_repo(conn, REPO_NAME, REPO_NAME, str(MIRROR_DIR))
    conn.commit()

    scanned = scan_mirror_packs(MIRROR_DIR)
    delta = DeltaAnalyzer(conn, scanned, repo_id=REPO_NAME)
    delta.register_new_packs()
    conn.commit()

    unarchived = get_unarchived_packs(conn)
    print(f"  {len(unarchived)} packs to archive")

    config = LCSASConfig(
        mirror_base_path=MIRROR_DIR.parent,
        staging_path=STAGING_DIR,
        db_path=DB_PATH,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="LCSAS",
        metadata_reserve_bytes=500_000,
        repositories={
            REPO_NAME: RepositoryConfig(
                name=REPO_NAME,
                mirror_path=MIRROR_DIR,
                password_file=PW_FILE,
            ),
        },
    )

    class NoOpEcc:
        def augment_iso(self, iso_path, redundancy_pct=15):
            pass
        def verify_iso(self, iso_path):
            return True
        def repair_iso(self, iso_path):
            return True

    orchestrator = BurnOrchestrator(
        config, conn, SubprocessXorrisoRunner(), NoOpEcc()
    )

    # The owner's flow (#699 removed prepare/execute): stage every ISO in
    # one burn session, then finish it with skip_burn (no drive).
    iso_files: list[Path] = []
    try:
        result = orchestrator.stage(media_type=MediaType.TEST_TINY)
        orchestrator.burn_session(
            result.session_id, skip_burn=True, interactive=False,
        )
    except ValueError as e:
        print(f"  stage stopped: {e}")
        result = None
    ISO_OUT.mkdir(parents=True, exist_ok=True)
    for staged in (result.iso_paths if result else []):
        iso_path = ISO_OUT / staged.name
        shutil.move(str(staged), iso_path)
        iso_files.append(iso_path)
        print(f"    → {iso_path.name} ({iso_path.stat().st_size:,} bytes)")

    conn.close()
    print(f"  produced {len(iso_files)} data ISOs")
    return iso_files


def build_meta_disc() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from lcsas.meta.builder import MetaVolumeBuilder

    builder = MetaVolumeBuilder(META_STAGE)
    builder.build()
    print(f"  meta disc staged at {META_STAGE}")

    sh([
        "xorriso", "-as", "mkisofs",
        "-V", "LCSAS_META",
        "-R", "-J",
        "-o", str(META_ISO),
        str(META_STAGE),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  meta ISO: {META_ISO}")


# ---------------------------------------------------------------------------
# Restore via cdemu-backed /dev/sr0
# ---------------------------------------------------------------------------


def cdemu(*args: str) -> None:
    sh(["bash", str(CDEMU), *args], stdout=subprocess.DEVNULL)


def cdemu_device() -> str:
    out = subprocess.run(["bash", str(CDEMU), "device"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return out or "/dev/sr0"


def eject(drive: Path) -> None:
    if os.path.ismount(drive):
        subprocess.run(["umount", str(drive)], check=False)
    with contextlib.suppress(Exception):
        cdemu("unload")


def insert(iso: Path, device: str, drive: Path) -> None:
    """Put *iso* in the cdemu drive and mount it where restore.sh looks."""
    eject(drive)
    time.sleep(0.3)
    cdemu("load", str(iso))
    time.sleep(0.6)
    drive.mkdir(parents=True, exist_ok=True)
    sh(["mount", "-o", "ro", device, str(drive)])


def run_restore(iso_files: list[Path]) -> int:
    """Run the META disc's restore.sh under a pseudoterminal and answer it
    as an heir with one drive would (#756).

    restore.sh finds discs where they are mounted (LCSAS_MOUNT_DIRS):
    each disc is loaded into the cdemu drive and mounted at
    MOUNT_DIR/sr0.  Step 0 asks for the newest data disc; tier 1 then
    names each disc it needs, which is loaded, then ENTER.  The command
    line and the prompts come from tests/e2e/restore_sh_driver.py.
    """
    from tests.e2e.restore_sh_driver import (
        ENTER,
        QUIT,
        build_restore_cmd,
        build_restore_env,
        drive_restore,
    )

    cdemu("start")
    device = cdemu_device()
    drive = MOUNT_DIR / "sr0"
    eject(drive)

    restore_sh = META_STAGE / "restore.sh"
    assert restore_sh.is_file(), f"missing {restore_sh}"

    data_isos = sorted(iso_files, key=lambda p: p.stem)
    by_label = {f.stem: f for f in data_isos}
    newest = data_isos[-1]

    def answer(prompt, n):
        if prompt.kind == "newest":
            print(f"\n[smoke] inserting the newest data disc {newest.stem}")
            insert(newest, device, drive)
            return ENTER
        if prompt.kind == "ram":
            return b"y\n"
        if prompt.kind != "disc":
            return None  # N: no other backup set
        want = next((by_label[lab] for lab in prompt.labels
                     if lab in by_label), None)
        if want is None and prompt.labels:
            print(f"\n[smoke] unknown disc labels {prompt.labels}; quitting")
            return QUIT
        want = want or newest
        print(f"\n[smoke] swapping to {want.stem}")
        insert(want, device, drive)
        return ENTER

    cmd = build_restore_cmd(META_STAGE, key=PW_FILE,
                            target=RESTORE_TARGET, repo=REPO_NAME)
    env = build_restore_env(
        home=ROOT, tools_dir=TOOLS_DIR, mount_dir=MOUNT_DIR,
        meta_disc=META_STAGE, tmpdir=ROOT / "tmp",
        path=os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
    )
    (ROOT / "tmp").mkdir(exist_ok=True)
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")

    def echo(chunk: bytes) -> None:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    try:
        rc, _out = drive_restore(cmd, env, answer, timeout=900, echo=echo)
    finally:
        eject(drive)
    return rc


def verify_restore(manifest: dict[str, str]) -> bool:
    """Compare every source file's SHA-256 against the restored copy."""
    restored_root = RESTORE_TARGET / REPO_NAME
    # rustic restores absolute-ish paths; find the source dir inside.
    matches = list(restored_root.rglob("file_00.bin"))
    if not matches:
        print(f"  no restored files found under {restored_root}")
        return False
    restored_base = matches[0].parent
    ok = True
    for name, sha in manifest.items():
        p = restored_base / name
        if not p.is_file():
            print(f"  MISSING: {name}")
            ok = False
            continue
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != sha:
            print(f"  HASH MISMATCH: {name}")
            ok = False
    return ok


def main() -> int:
    banner("1. clean + generate source")
    clean_root()
    manifest = generate_source()

    banner("2. rustic init + backup")
    init_rustic_and_backup()

    banner("3. LCSAS burn → ISOs")
    iso_files = run_lcsas_burn()
    if len(iso_files) < 2:
        print("\n[smoke] WARNING: only 1 disc — swap loop not exercised")

    banner("4. build production meta disc + ISO")
    build_meta_disc()

    banner("5. drive restore.sh via cdemu")
    rc = run_restore(iso_files)
    print(f"\n  restore.sh exit code: {rc}")
    if rc != 0:
        return rc

    banner("6. verify restored files")
    ok = verify_restore(manifest)
    print("\n" + ("  ✅ SMOKE TEST PASSED" if ok else "  ❌ SMOKE TEST FAILED"))
    with contextlib.suppress(Exception):
        cdemu("unload")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
