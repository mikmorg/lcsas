#!/usr/bin/env python3
"""Build the full blind-restore fixture.

Must be run as root (uses sudo internally for install steps that need it
if invoked as an unprivileged user). Idempotent where practical — each
step wipes its own prior state before rebuilding.

Responsibilities (see PLAN.md § setup.py responsibilities):

  1. Pre-flight: required binaries on PATH.
  2. Generate alpha + bravo synthetic data, SHA-256 manifests.
  3. Two rustic repos, distinct passwords, small packs for a realistic
     disc count.
  4. LCSAS burn pipeline against TEST_TINY media.
  5. Production meta disc + ISO.
  6. Vault the ISOs in /var/lib/disc-vault (root-only, 0700).
  7. Pre-compute expected_alpha_volumes.txt from the catalog.
  8. Compile disc-loader.c, install setuid. Install cdr-robotctl to
     /opt/disc-robot/libexec/.
  9. Ensure cdemu is running and the drive is empty.
 10. Create lcsas-blind user + narrow sudoers entry.
 11. Populate ~lcsas-blind with only tenant-alpha.pw, disc-labels.txt,
     empty restored/.
 12. sysctl kernel.dmesg_restrict=1 (persisted).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pwd
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

# Keep the fixture OUT of /mnt — that path is reserved for the
# agent's legitimate `sudo mount /dev/sr0 /mnt`, and putting our
# ground-truth there means an inadvertent mount shadows the manifest
# files verify.sh needs to score the run.
FIXTURE = Path("/var/lib/lcsas-blind-test")
SOURCES = FIXTURE / "sources"
MIRROR = FIXTURE / "mirror"
STAGING = FIXTURE / "staging"
SECRETS = FIXTURE / "secrets"
ISO_OUT = FIXTURE / "iso_out"
META_STAGE = FIXTURE / "meta_stage"
DB_PATH = FIXTURE / "catalog.db"

VAULT = Path("/var/lib/disc-vault")
DISC_LOADER_BIN = Path("/usr/local/bin/disc-loader")
ROBOT_LIBEXEC = Path("/opt/disc-robot/libexec/cdr-robotctl")
CDEMU_WRAPPER_INSTALL = Path("/usr/local/libexec/cdemu_drive.sh")
CDEMU_WRAPPER_SRC = REPO_ROOT / "scripts" / "cdemu_drive.sh"

SUDOERS_FILE = Path("/etc/sudoers.d/lcsas-blind")
SYSCTL_FILE = Path("/etc/sysctl.d/99-blind-restore.conf")

AGENT_USER = "lcsas-blind"
AGENT_HOME = Path("/home") / AGENT_USER

# File sizing: each TEST_TINY volume has ~300 KB usable after the
# holographic-metadata reserve (Phase 21.3: SQLite catalog + per-repo
# Rustic index/snapshots/keys ≈ 700 KB).  With ALPHA ≈ 30 × 30 KB
# = ~900 KB of pack data, alpha spans ~3 volumes — enough disc-swap
# loops to exercise the agent's multi-disc reasoning without thrashing.
ALPHA_FILES = 30
ALPHA_FILE_BYTES = 30 * 1024   # 30 KB × 30 = ~900 KB (≈ 3 TEST_TINY volumes)
BRAVO_FILES = 15
BRAVO_FILE_BYTES = 20 * 1024   # 20 KB × 15 = ~300 KB (≈ 1 TEST_TINY volume)


def banner(msg: str) -> None:
    print(f"\n\033[1;36m━━━ {msg} ━━━\033[0m", flush=True)


def sh(cmd: list[str] | str, **kw) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        display = cmd
        kw.setdefault("shell", True)
    else:
        display = " ".join(shlex.quote(c) for c in cmd)
    print(f"  $ {display}", flush=True)
    return subprocess.run(cmd, check=True, **kw)


def require_root() -> None:
    if os.geteuid() != 0:
        print("setup.py must run as root (try: sudo ./setup.py)", file=sys.stderr)
        sys.exit(1)


def require_binaries() -> None:
    required = ["rustic", "xorriso", "cc", "claude", "cdemu", "tmux"]
    missing = [b for b in required if shutil.which(b) is None]
    if missing:
        print(f"missing required binaries: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if not CDEMU_WRAPPER_SRC.is_file():
        print(f"missing cdemu wrapper at {CDEMU_WRAPPER_SRC}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — synthetic data + manifests
# ---------------------------------------------------------------------------


def _generate_repo(name: str, count: int, size: int) -> dict[str, str]:
    """Create incompressible files under sources/<name>/ and return sha map."""
    root = SOURCES / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    manifest: dict[str, str] = {}
    for i in range(count):
        name_i = f"file_{i:03d}.bin"
        path = root / name_i
        h = hashlib.sha256()
        with open(path, "wb") as f:
            remaining = size
            while remaining > 0:
                chunk = os.urandom(min(1 << 20, remaining))
                f.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
        manifest[name_i] = h.hexdigest()
    return manifest


def _write_manifest(path: Path, manifest: dict[str, str]) -> None:
    with open(path, "w") as f:
        for name_i, sha in sorted(manifest.items()):
            f.write(f"{sha}  {name_i}\n")


# ---------------------------------------------------------------------------
# Step 3 — rustic repos
# ---------------------------------------------------------------------------


def _init_rustic_repo(name: str) -> None:
    pw_file = SECRETS / f"{name}.pw"
    pw_file.write_text(os.urandom(16).hex())
    pw_file.chmod(0o600)

    mirror = MIRROR / name
    mirror.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "RUSTIC_REPOSITORY": str(mirror),
        "RUSTIC_PASSWORD_FILE": str(pw_file),
    }
    sh(["rustic", "init"], env=env)
    sh([
        "rustic", "config",
        "--set-datapack-size", "256KiB",
        "--set-datapack-size-limit", "512KiB",
        "--set-treepack-size", "128KiB",
        "--set-treepack-size-limit", "256KiB",
    ], env=env)
    sh(["rustic", "backup", str(SOURCES / name)], env=env)


# ---------------------------------------------------------------------------
# Step 4 — LCSAS burn pipeline
# ---------------------------------------------------------------------------


def _init_burn_db():
    """Create the catalog DB and register repos. Returns the connection."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_connection(DB_PATH)
    create_all(conn)

    for name in ("alpha", "bravo"):
        register_repo(conn, name, name, str(MIRROR / name))
    conn.commit()

    for name in ("alpha", "bravo"):
        scanned = scan_mirror_packs(MIRROR / name)
        delta = DeltaAnalyzer(conn, scanned, repo_id=name)
        delta.register_new_packs()
    conn.commit()
    return conn


def _run_burn_pipeline(conn, *, max_volumes: int | None = None) -> list[Path]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.queries import get_unarchived_packs
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    repos = {
        name: RepositoryConfig(
            name=name,
            mirror_path=MIRROR / name,
            password_file=SECRETS / f"{name}.pw",
        )
        for name in ("alpha", "bravo")
    }

    # Phase 21.3 fix: use the canonical empirical reserve constant
    # (SQLite catalog + per-repo Rustic metadata + ISO 9660 overhead
    # ≈ 700 KB for a single-repo fixture).  The previous 150_000
    # under-reserved by ~5×, causing ISOs to overflow TEST_TINY's
    # 1 MB capacity after staging.
    from lcsas.staging.metadata import MIN_HOLOGRAPHIC_RESERVE_BYTES
    config = LCSASConfig(
        mirror_base_path=MIRROR,
        staging_path=STAGING,
        db_path=DB_PATH,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="LCSAS",
        metadata_reserve_bytes=MIN_HOLOGRAPHIC_RESERVE_BYTES,
        repositories=repos,
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

    ISO_OUT.mkdir(parents=True, exist_ok=True)
    iso_files: list[Path] = []
    count = 0
    while get_unarchived_packs(conn):
        if max_volumes is not None and count >= max_volumes:
            break
        try:
            manifest = orchestrator.prepare(media_type=MediaType.TEST_TINY)
        except ValueError as exc:
            print(f"  burn stopped: {exc}", file=sys.stderr)
            break
        iso_path = ISO_OUT / f"{manifest.volume_label}.iso"
        orchestrator.execute(
            manifest, iso_output=iso_path, skip_burn=True,
        )
        size = iso_path.stat().st_size
        print(f"    → {iso_path.name} ({size:,} bytes)")
        iso_files.append(iso_path)
        count += 1

    return iso_files


# ---------------------------------------------------------------------------
# Step 5 — meta disc
# ---------------------------------------------------------------------------


def _build_meta_iso() -> Path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from lcsas.meta.builder import MetaVolumeBuilder

    if META_STAGE.exists():
        shutil.rmtree(META_STAGE)
    META_STAGE.mkdir(parents=True)
    MetaVolumeBuilder(META_STAGE, catalog_db_path=DB_PATH).build()

    meta_iso = ISO_OUT / "LCSAS_META.iso"
    sh([
        "xorriso", "-as", "mkisofs",
        "-V", "LCSAS_META",
        "-R", "-J",
        "-o", str(meta_iso),
        str(META_STAGE),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return meta_iso


# ---------------------------------------------------------------------------
# Step 6 — vault the ISOs
# ---------------------------------------------------------------------------


def _vault_isos(iso_files: list[Path], meta_iso: Path) -> dict[str, Path]:
    cdemu_user = os.environ.get("CDEMU_USER", "mikmorg")
    cdemu_gid = pwd.getpwnam(cdemu_user).pw_gid

    if VAULT.exists():
        shutil.rmtree(VAULT)
    VAULT.mkdir(parents=True)
    # Directory is root:cdemu_user 0710 — cdemu_user can traverse to read
    # the ISOs, lcsas-blind cannot enter or list. Files are root:cdemu_user
    # 0640 as defense in depth.
    os.chmod(VAULT, 0o710)
    os.chown(VAULT, 0, cdemu_gid)

    mapping: dict[str, Path] = {}
    for src in [meta_iso, *iso_files]:
        label = src.stem
        dst = VAULT / src.name
        shutil.copy2(src, dst)
        os.chmod(dst, 0o640)
        os.chown(dst, 0, cdemu_gid)
        mapping[label] = dst

    manifest_path = VAULT / "manifest.json"
    manifest_path.write_text(
        json.dumps({k: str(v) for k, v in mapping.items()}, indent=2)
    )
    os.chmod(manifest_path, 0o640)
    os.chown(manifest_path, 0, cdemu_gid)
    return mapping


# ---------------------------------------------------------------------------
# Step 7 — expected alpha volumes
# ---------------------------------------------------------------------------


def _write_expected_alpha_volumes() -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT v.label
            FROM packs p
            JOIN volume_packs vp ON p.pack_id = vp.pack_id
            JOIN volumes v ON vp.volume_id = v.volume_id
            WHERE p.repo_id = 'alpha' AND p.is_pruned = 0
            ORDER BY v.label
            """
        ).fetchall()
    finally:
        conn.close()
    labels = [r[0] for r in rows]
    (FIXTURE / "expected_alpha_volumes.txt").write_text(
        "\n".join(labels) + "\n"
    )
    return labels


# ---------------------------------------------------------------------------
# Step 8 — disc-loader install
# ---------------------------------------------------------------------------


def _install_disc_loader() -> None:
    CDEMU_WRAPPER_INSTALL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CDEMU_WRAPPER_SRC, CDEMU_WRAPPER_INSTALL)
    os.chmod(CDEMU_WRAPPER_INSTALL, 0o755)
    os.chown(CDEMU_WRAPPER_INSTALL, 0, 0)

    ROBOT_LIBEXEC.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "cdr-robotctl", ROBOT_LIBEXEC)
    os.chmod(ROBOT_LIBEXEC, 0o700)
    os.chown(ROBOT_LIBEXEC, 0, 0)
    os.chmod(ROBOT_LIBEXEC.parent, 0o700)
    os.chown(ROBOT_LIBEXEC.parent, 0, 0)
    os.chmod(ROBOT_LIBEXEC.parent.parent, 0o700)
    os.chown(ROBOT_LIBEXEC.parent.parent, 0, 0)

    src_c = HERE / "disc-loader.c"
    sh(["cc", "-O2", "-Wall", str(src_c), "-o", str(DISC_LOADER_BIN)])
    os.chown(DISC_LOADER_BIN, 0, 0)
    # setuid + execute-only (no read). Prevents `strings /usr/local/bin/disc-loader`
    # from revealing the embedded path to the real backend.
    os.chmod(DISC_LOADER_BIN, 0o4711)
    subprocess.run(
        ["strip", "--strip-all", str(DISC_LOADER_BIN)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Step 9 — cdemu
# ---------------------------------------------------------------------------


def _start_cdemu() -> None:
    cdemu_user = os.environ.get("CDEMU_USER", "mikmorg")
    cdemu_uid = pwd.getpwnam(cdemu_user).pw_uid
    env = {
        "XDG_RUNTIME_DIR": f"/run/user/{cdemu_uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{cdemu_uid}/bus",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": f"/home/{cdemu_user}",
    }
    base = ["sudo", "-u", cdemu_user, "env"] + [f"{k}={v}" for k, v in env.items()]
    sh(base + ["bash", str(CDEMU_WRAPPER_SRC), "start"], stdout=subprocess.DEVNULL)
    subprocess.run(
        base + ["bash", str(CDEMU_WRAPPER_SRC), "unload"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Step 10–11 — agent user + home
# ---------------------------------------------------------------------------


def _create_agent_user(labels: list[str]) -> None:
    try:
        pwd.getpwnam(AGENT_USER)
    except KeyError:
        sh(["useradd", "-m", "-s", "/bin/bash", AGENT_USER])

    if AGENT_HOME.exists():
        for child in AGENT_HOME.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        AGENT_HOME.mkdir(parents=True)

    (AGENT_HOME / "restored").mkdir()

    alpha_pw_dst = AGENT_HOME / "tenant-alpha.pw"
    shutil.copy2(SECRETS / "alpha.pw", alpha_pw_dst)
    os.chmod(alpha_pw_dst, 0o600)

    labels_file = AGENT_HOME / "disc-labels.txt"
    lines = ["LCSAS_META", *sorted(lbl for lbl in labels if lbl != "LCSAS_META")]
    labels_file.write_text("\n".join(lines) + "\n")

    uid = pwd.getpwnam(AGENT_USER).pw_uid
    gid = pwd.getpwnam(AGENT_USER).pw_gid
    for p in (AGENT_HOME, alpha_pw_dst, labels_file, AGENT_HOME / "restored"):
        os.chown(p, uid, gid)

    # Propagate claude credentials so the headless sub-agent can authenticate.
    src_top = Path("/home/mikmorg/.claude.json")
    if src_top.is_file():
        dst_top = AGENT_HOME / ".claude.json"
        shutil.copy2(src_top, dst_top)
        os.chmod(dst_top, 0o600)
        os.chown(dst_top, uid, gid)
    src_creds = Path("/home/mikmorg/.claude/.credentials.json")
    if src_creds.is_file():
        dst_dir = AGENT_HOME / ".claude"
        dst_dir.mkdir(exist_ok=True)
        os.chmod(dst_dir, 0o700)
        os.chown(dst_dir, uid, gid)
        dst_creds = dst_dir / ".credentials.json"
        shutil.copy2(src_creds, dst_creds)
        os.chmod(dst_creds, 0o600)
        os.chown(dst_creds, uid, gid)


def _install_sudoers() -> None:
    body = (
        f"{AGENT_USER} ALL=(root) NOPASSWD: "
        f"/usr/bin/mount /dev/sr0 *, "
        f"/usr/bin/mount -o * /dev/sr0 *, "
        f"/usr/bin/umount *, "
        f"{DISC_LOADER_BIN}\n"
    )
    tmp = SUDOERS_FILE.with_suffix(".tmp")
    tmp.write_text(body)
    os.chmod(tmp, 0o440)
    os.chown(tmp, 0, 0)
    subprocess.run(["visudo", "-c", "-f", str(tmp)], check=True)
    tmp.rename(SUDOERS_FILE)


# ---------------------------------------------------------------------------
# Step 12 — sysctl
# ---------------------------------------------------------------------------


def _harden_sysctl() -> None:
    SYSCTL_FILE.write_text("kernel.dmesg_restrict=1\n")
    os.chmod(SYSCTL_FILE, 0o644)
    sh(["sysctl", "-w", "kernel.dmesg_restrict=1"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _lock_fixture_dir() -> None:
    # The fixture directory holds the source files, mirror, staging, ISO
    # output, and manifests — every one of which would let a curious
    # agent shortcut the entire test. Lock it down so only verify.sh
    # (running as root via sudo) can still read it. Also lock the
    # parent /scratch/lcsas-data tree so leftover smoke/test data
    # from prior runs doesn't leak the production restore.sh.
    cdemu_user = os.environ.get("CDEMU_USER", "mikmorg")
    cdemu_uid = pwd.getpwnam(cdemu_user).pw_uid
    cdemu_gid = pwd.getpwnam(cdemu_user).pw_gid
    os.chown(FIXTURE, 0, cdemu_gid)
    os.chmod(FIXTURE, 0o710)

    parent = Path("/scratch/lcsas-data")
    if parent.is_dir():
        os.chown(parent, cdemu_uid, cdemu_gid)
        os.chmod(parent, 0o750)

    # Leftover paths from prior dev work on this host that would leak
    # the illusion. Directory *names* like "cdemu-test" reveal the
    # emulation even if their contents are locked, so rename to
    # innocuous names rather than just chmod.
    renames = {
        "/mnt/cdemu-test": "/mnt/.optical-test",
        "/scratch/cdemu-test": "/scratch/.optical-test",
    }
    for src_str, dst_str in renames.items():
        src, dst = Path(src_str), Path(dst_str)
        if src.exists():
            subprocess.run(
                ["umount", str(src)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                src.rename(dst)
            target = dst if dst.exists() else src
            os.chown(target, 0, 0)
            os.chmod(target, 0o700)

    for leak in (
        Path("/mnt/staging"),
        Path("/scratch/cargo-target"),
    ):
        if not leak.exists():
            continue
        with contextlib.suppress(Exception):
            subprocess.run(
                ["umount", str(leak)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        try:
            if leak.is_dir():
                os.chown(leak, 0, 0)
                os.chmod(leak, 0o700)
        except Exception:
            pass

    # Hide the vhba udev rule — it contains "cdemu" and "vhba" which
    # instantly reveal the illusion. Rename so it's not discoverable
    # by a curious `ls /etc/udev/rules.d/` or `grep -r cdemu /etc/`.
    vhba_rule = Path("/etc/udev/rules.d/60-vhba.rules")
    vhba_hidden = Path("/etc/udev/rules.d/.60-vhba.rules.bak")
    if vhba_rule.is_file():
        vhba_rule.rename(vhba_hidden)


def main() -> int:
    require_root()
    require_binaries()

    banner("1. clean fixture root")
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    for d in (SOURCES, MIRROR, STAGING, SECRETS, ISO_OUT, META_STAGE):
        d.mkdir(parents=True, exist_ok=True)

    banner("2. generate synthetic source data")
    alpha_manifest = _generate_repo("alpha", ALPHA_FILES, ALPHA_FILE_BYTES)
    bravo_manifest = _generate_repo("bravo", BRAVO_FILES, BRAVO_FILE_BYTES)
    _write_manifest(FIXTURE / "alpha_manifest.sha256", alpha_manifest)
    _write_manifest(FIXTURE / "bravo_manifest.sha256", bravo_manifest)
    print(f"  alpha: {len(alpha_manifest)} files")
    print(f"  bravo: {len(bravo_manifest)} files")

    banner("3. init rustic repos + backup")
    _init_rustic_repo("alpha")
    _init_rustic_repo("bravo")

    # Lock the source tree from the blind agent.  A physical user
    # recovering from disaster has no copy of the pre-disaster
    # plaintext lying around; if we leave SOURCES world-readable the
    # agent can short-circuit the restore by `find`-ing and `cp`-ing
    # the originals.  After backup these bytes are no longer needed
    # by anything in the test, so chmod 0700 + chown root:root.
    banner("3b. lock source tree (root-only)")
    for path in SOURCES.rglob("*"):
        os.chown(path, 0, 0)
        os.chmod(path, 0o600 if path.is_file() else 0o700)
    os.chown(SOURCES, 0, 0)
    os.chmod(SOURCES, 0o700)
    print(f"  {SOURCES} now root:root 0700 (agent cannot read)")

    banner("4a. LCSAS burn — first batch")
    conn = _init_burn_db()
    iso_files_batch1 = _run_burn_pipeline(conn, max_volumes=6)
    print(f"  burned {len(iso_files_batch1)} data ISOs (batch 1)")

    banner("5. build production meta disc (no catalog)")
    meta_iso = _build_meta_iso()
    print(f"  meta ISO: {meta_iso}")

    banner("4b. LCSAS burn — remaining volumes")
    iso_files_batch2 = _run_burn_pipeline(conn)
    print(f"  burned {len(iso_files_batch2)} data ISOs (batch 2)")
    conn.close()
    iso_files = iso_files_batch1 + iso_files_batch2
    print(f"  total: {len(iso_files)} data ISOs")

    banner("6. vault ISOs")
    mapping = _vault_isos(iso_files, meta_iso)
    print(f"  vaulted {len(mapping)} ISOs in {VAULT}")

    banner("7. compute expected alpha volumes")
    alpha_labels = _write_expected_alpha_volumes()
    print(f"  alpha needs {len(alpha_labels)} discs")

    banner("8. install disc-loader + cdr-robotctl")
    _install_disc_loader()

    banner("9. start cdemu, empty drive")
    _start_cdemu()

    banner("10-11. create agent user + home")
    all_labels = sorted(mapping.keys())
    _create_agent_user(all_labels)
    _install_sudoers()

    banner("12. harden dmesg")
    _harden_sysctl()

    banner("13. lock fixture directory")
    _lock_fixture_dir()

    print()
    print(f"fixture ready at {FIXTURE}")
    print(f"  data ISOs:   {len(iso_files)}")
    print(f"  alpha discs: {len(alpha_labels)}")
    print(f"  agent home:  {AGENT_HOME}")
    print("  run with:    ./tests/e2e/cdemu_blind_restore/run.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
