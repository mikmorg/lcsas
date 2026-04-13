#!/usr/bin/env python3
"""Manual single-drive restore smoke test.

Builds a tiny synthetic repository, runs the LCSAS burn pipeline against
it to produce a handful of small ISO files, builds the production
meta volume + ISO, and then drives the production ``restore.sh`` in
single-drive mode against a cdemu-backed virtual drive — swapping discs
by hand between phases.

This exists to gate the blind-restore acceptance test: if this smoke
test cannot complete the restore with the unmodified production meta
disc, the blind rig won't either.

Run as:  python3 scripts/smoke_single_drive.py
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
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
WORK_DIR = ROOT / "work"
PW_FILE = ROOT / "smoke.pw"
CDEMU = Path(__file__).parent / "cdemu_drive.sh"

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
              META_STAGE, RESTORE_TARGET, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)


def generate_source() -> dict[str, str]:
    """Create a handful of random files and return {relpath: sha256}."""
    manifest: dict[str, str] = {}
    for i in range(40):
        name = f"file_{i:02d}.bin"
        blob = os.urandom(1_000_000)  # 1 MB each → ~40 MB total
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
        "--set-datapack-size", "2MiB",
        "--set-datapack-size-limit", "3MiB",
        "--set-treepack-size", "1MiB",
        "--set-treepack-size-limit", "2MiB"], env=env)
    sh(["rustic", "backup", str(SRC_DIR)], env=env)


def run_lcsas_burn() -> list[Path]:
    """Use the LCSAS Python API to scan and burn to ISOs."""
    # Add src/ to path so we can import lcsas
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.db.queries import get_unarchived_packs
    from lcsas.db.repos import register_repo
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
        default_media_type=MediaType.TEST_SMALL,
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

    iso_files: list[Path] = []
    vol_n = 0
    while get_unarchived_packs(conn):
        vol_n += 1
        print(f"  preparing volume {vol_n}...")
        try:
            manifest = orchestrator.prepare(media_type=MediaType.TEST_SMALL)
        except ValueError as e:
            print(f"  prepare stopped: {e}")
            break
        iso_path = ISO_OUT / f"{manifest.volume_label}.iso"
        orchestrator.execute(
            manifest, iso_output=iso_path, skip_burn=True, skip_ecc=True,
        )
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
    print(f"  meta volume staged at {META_STAGE}")

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


def run_restore(iso_files: list[Path]) -> int:
    """Spawn restore.sh under expect-style driving so each disc-swap
    prompt triggers a cdemu swap to the next volume.

    Rather than write an expect script, we drive restore.sh manually
    from Python: it reads the pick list, we watch stderr for the
    prompt, cdemu-swap to the named disc, send newline.
    """
    # Start cdemu if needed
    cdemu("start")
    with contextlib.suppress(Exception):
        cdemu("unload")
    cdemu("load", str(META_ISO))
    time.sleep(1)

    restore_sh = META_STAGE / "restore.sh"
    assert restore_sh.is_file(), f"missing {restore_sh}"

    iso_by_label: dict[str, Path] = {f.stem: f for f in iso_files}
    iso_by_label["LCSAS_META"] = META_ISO

    env = {**os.environ, "LD_LIBRARY_PATH": str(META_STAGE / "tools" / "lib")}
    cmd = [
        "bash", str(restore_sh),
        "--key", str(PW_FILE),
        "--target", str(RESTORE_TARGET),
        "--repo", REPO_NAME,
        "--work-dir", str(WORK_DIR),
    ]
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")

    import pty, select, re

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, env)

    prompt_re = re.compile(rb"PLEASE INSERT DISC: (\S*)")
    buf = b""
    last_loaded = "LCSAS_META"
    exit_code = None
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 0.5)
        except InterruptedError:
            continue
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            buf += chunk
            m = prompt_re.search(buf)
            while m:
                want = m.group(1).decode().strip()
                buf = buf[m.end():]
                if not want:
                    want = iso_files[-1].stem
                iso = iso_by_label.get(want)
                if iso is None:
                    print(f"\n[smoke] unknown disc label {want!r}; aborting")
                    os.write(fd, b"skip\n")
                    break
                print(f"\n[smoke] swapping to {want}")
                cdemu("unload")
                time.sleep(0.3)
                cdemu("load", str(iso))
                time.sleep(0.6)
                last_loaded = want
                os.write(fd, b"\n")
                m = prompt_re.search(buf)
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if waited_pid == pid:
            exit_code = os.waitstatus_to_exitcode(status)
            break

    if exit_code is None:
        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
    return exit_code


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

    banner("4. build production meta volume + ISO")
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
