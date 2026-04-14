"""Interactive single-drive restore via PTY + loop devices.

Builds a LCSAS scenario (repo → backup → burn → meta volume), then
drives the production ``restore.sh`` interactively through a
pseudoterminal.  Disc swaps are simulated by re-attaching ISOs to a
Linux loop device, so no cdemu or physical hardware is needed.

Requires: root (for losetup + mount), ``rustic`` and ``xorriso`` on PATH.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pty
import random
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs

# ── Skip conditions ──────────────────────────────────────────────

pytestmark = [
    pytest.mark.skipif(
        os.geteuid() != 0,
        reason="requires root (loop devices + mount)",
    ),
    pytest.mark.skipif(
        not shutil.which("rustic"), reason="rustic not installed"
    ),
    pytest.mark.skipif(
        not shutil.which("xorriso"), reason="xorriso not installed"
    ),
]

# ── Deterministic data ──────────────────────────────────────────

RNG_SEED = 20260413
NUM_FILES = 40
FILE_SIZE_RANGE = (200_000, 500_000)  # ~14 MB total → forces multi-disc on TEST_SMALL

# Total timeout for the restore subprocess (seconds).
RESTORE_TIMEOUT = 300


def _generate_files(
    directory: Path, rng: random.Random, count: int, prefix: str = "f"
) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for i in range(count):
        data = rng.randbytes(rng.randint(*FILE_SIZE_RANGE))
        name = f"{prefix}_{i:04d}.bin"
        (directory / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    return manifest


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rustic(
    args: list[str], repo: Path, pw: Path, tmpdir: Path | None = None
) -> subprocess.CompletedProcess:
    env = None
    if tmpdir is not None:
        env = {**os.environ, "TMPDIR": str(tmpdir)}
    return subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), *args],
        capture_output=True, text=True, check=True, env=env,
    )


# ── Stubs for the burn pipeline ─────────────────────────────────

class _NoOpDVDisaster:
    def augment_iso(self, *a, **kw): pass
    def verify_iso(self, *a, **kw): return True
    def repair_iso(self, *a, **kw): return True


class _TestXorrisoRunner:
    def create_iso(self, source_dir, output_iso, volume_label, **_kw):
        subprocess.run(
            ["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long",
             "-iso-level", "3", "-V", volume_label,
             "-o", str(output_iso), str(source_dir)],
            capture_output=True, text=True, check=True,
        )
        return output_iso

    def burn_iso(self, *a, **kw): pass
    def verify_disc(self, *a, **kw): return True


# ═════════════════════════════════════════════════════════════════
#  THE TEST
# ═════════════════════════════════════════════════════════════════

class TestInteractiveRestore:
    """Drive restore.sh interactively via PTY + loop device swaps."""

    # ── Fixture ──────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def setup_scenario(self, tmp_path: Path):
        """Build data ISOs + meta-volume, nuke everything else."""
        self.tmp = tmp_path
        self.rng = random.Random(RNG_SEED)

        mirror = tmp_path / "mirror"
        staging = tmp_path / "staging"
        iso_out = tmp_path / "isos"
        db_path = tmp_path / "db" / "catalog.db"
        key_file = tmp_path / "key.txt"

        for d in [mirror, staging, iso_out, db_path.parent]:
            d.mkdir(parents=True, exist_ok=True)

        key_file.write_text("interactive-restore-test-password\n")

        # ── Generate test data ───────────────────────────────────
        src_dir = tmp_path / "original"
        self.manifest = _generate_files(src_dir, self.rng, NUM_FILES, "ir")

        # ── Rustic init + backup ─────────────────────────────────
        repo = mirror / "photos"
        _rustic(["init"], repo, key_file, tmpdir=tmp_path)
        # Force small packs so the data spans multiple discs.
        _rustic(
            ["config",
             "--set-datapack-size", "1MiB",
             "--set-datapack-size-limit", "2MiB",
             "--set-treepack-size", "512KiB",
             "--set-treepack-size-limit", "1MiB"],
            repo, key_file, tmpdir=tmp_path,
        )
        _rustic(
            ["backup", "--json", str(src_dir)], repo, key_file, tmpdir=tmp_path
        )

        # ── LCSAS catalog ────────────────────────────────────────
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "photos", "photos", str(repo))

        scanned = scan_mirror_packs(repo)
        delta = DeltaAnalyzer(conn, scanned, repo_id="photos")
        delta.register_new_packs()

        from lcsas.db.queries import get_unarchived_packs

        mt = MediaType.TEST_SMALL
        repo_configs = {
            "photos": RepositoryConfig(
                name="photos", mirror_path=repo, password_file=key_file,
            ),
        }
        config = LCSASConfig(
            mirror_base_path=mirror,
            staging_path=staging,
            db_path=db_path,
            default_media_type=mt,
            default_ecc_redundancy_pct=0,
            label_prefix="ITEST",
            metadata_reserve_bytes=1_500_000,
            repositories=repo_configs,
        )

        orchestrator = BurnOrchestrator(
            config, conn, _TestXorrisoRunner(), _NoOpDVDisaster()
        )

        isos: list[Path] = []
        while get_unarchived_packs(conn):
            try:
                manifest = orchestrator.prepare(media_type=mt)
                iso = iso_out / f"{manifest.volume_label}.iso"
                orchestrator.execute(
                    manifest, iso_output=iso, skip_burn=True, skip_ecc=True
                )
                isos.append(iso)
            except ValueError:
                break

        conn.close()

        assert len(isos) >= 2, (
            f"Need multi-disc scenario to test swaps, got {len(isos)} ISOs"
        )

        # ── Build meta-volume ────────────────────────────────────
        meta_dir = tmp_path / "meta_volume"
        meta_builder = MetaVolumeBuilder(meta_dir)
        meta_builder.build()

        # ── Nuke everything except ISOs, meta-volume, key ────────
        safe = tmp_path / "_safe"
        safe.mkdir()
        safe_isos = safe / "isos"
        safe_isos.mkdir()
        for iso in isos:
            shutil.move(str(iso), str(safe_isos / iso.name))
        shutil.move(str(meta_dir), str(safe / "meta_volume"))
        shutil.copy2(str(key_file), str(safe / "key.txt"))

        for entry in tmp_path.iterdir():
            if entry.name == "_safe":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

        self.iso_dir = tmp_path / "isos"
        self.iso_dir.mkdir()
        for iso in safe_isos.iterdir():
            shutil.move(str(iso), str(self.iso_dir / iso.name))
        self.meta_dir = tmp_path / "meta_volume"
        shutil.move(str(safe / "meta_volume"), str(self.meta_dir))
        self.key_file = tmp_path / "key.txt"
        shutil.move(str(safe / "key.txt"), str(self.key_file))
        shutil.rmtree(safe)

        self.restore_target = tmp_path / "restored"
        self.restore_target.mkdir()
        (tmp_path / "work").mkdir()
        (tmp_path / "tmp").mkdir()

        self.all_isos = sorted(self.iso_dir.glob("*.iso"))

    # ── Loop-device helpers ──────────────────────────────────────

    @staticmethod
    def _find_free_loop() -> str:
        """Return the path of an unused loop device."""
        r = subprocess.run(
            ["losetup", "-f"], capture_output=True, text=True, check=True
        )
        return r.stdout.strip()

    @staticmethod
    def _attach_loop(dev: str, iso: Path) -> None:
        subprocess.run(["losetup", dev, str(iso)], check=True)

    @staticmethod
    def _detach_loop(dev: str) -> None:
        subprocess.run(
            ["losetup", "-d", dev], capture_output=True, check=False
        )

    @staticmethod
    def _swap_loop(dev: str, iso: Path) -> None:
        """Detach current backing file (if any) and attach *iso*."""
        subprocess.run(["losetup", "-d", dev], capture_output=True, check=False)
        subprocess.run(["losetup", dev, str(iso)], check=True)

    # ── ISO manipulation ────────────────────────────────────────

    def _prepare_stale_bootstrap_iso(self) -> None:
        """Inject a complete-but-backdated catalog into the oldest ISO.

        After the burn loop, each disc's catalog only knows about
        itself and earlier discs.  To test the organic catalog upgrade
        path we need the *oldest* disc to carry a pick list that spans
        all volumes, but with a stale freshness timestamp so that later
        discs trigger an upgrade.

        Steps:
          1. Mount the freshest (last) data ISO → copy its catalog.db
          2. Backdate ``created_at`` on all volumes by one hour
          3. Extract the oldest ISO, swap in the stale catalog, rebuild
        """
        data_isos = sorted(
            iso for iso in self.all_isos if "META" not in iso.stem
        )
        oldest_iso = data_isos[0]
        freshest_iso = data_isos[-1]

        work = self.tmp / "_iso_patch"
        work.mkdir()
        fresh_mnt = work / "fresh_mnt"
        old_mnt = work / "old_mnt"
        extract = work / "extract"
        fresh_mnt.mkdir()
        old_mnt.mkdir()
        extract.mkdir()

        try:
            # 1. Copy the freshest catalog.
            subprocess.run(
                ["mount", "-o", "ro,loop", str(freshest_iso), str(fresh_mnt)],
                check=True, capture_output=True,
            )
            stale_cat = work / "catalog.db"
            shutil.copy2(str(fresh_mnt / "catalog.db"), str(stale_cat))
            subprocess.run(
                ["umount", str(fresh_mnt)],
                check=True, capture_output=True,
            )

            # 2. Backdate freshness so later discs appear fresher.
            conn = sqlite3.connect(str(stale_cat))
            conn.execute(
                "UPDATE volumes "
                "SET created_at = datetime(created_at, '-1 hour')"
            )
            conn.commit()
            conn.close()

            # 3. Extract oldest ISO, replace catalog, rebuild.
            subprocess.run(
                ["mount", "-o", "ro,loop", str(oldest_iso), str(old_mnt)],
                check=True, capture_output=True,
            )
            # cp -a preserves structure
            subprocess.run(
                ["cp", "-a", str(old_mnt) + "/.", str(extract)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["umount", str(old_mnt)],
                check=True, capture_output=True,
            )

            shutil.copy2(str(stale_cat), str(extract / "catalog.db"))

            vol_label = oldest_iso.stem
            subprocess.run(
                ["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long",
                 "-iso-level", "3", "-V", vol_label,
                 "-o", str(oldest_iso), str(extract)],
                capture_output=True, text=True, check=True,
            )
        finally:
            # Best-effort cleanup.
            subprocess.run(
                ["umount", str(fresh_mnt)],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["umount", str(old_mnt)],
                capture_output=True, check=False,
            )
            shutil.rmtree(work, ignore_errors=True)

    # ── PTY driver ───────────────────────────────────────────────

    def _drive_restore(
        self, bootstrap_oldest: bool = False,
    ) -> tuple[int, str]:
        """Spawn restore.sh under a PTY, respond to disc-swap prompts.

        Returns ``(exit_code, full_output_text)``.

        When *bootstrap_oldest* is True the Phase 1 empty-label prompt
        is answered with the oldest data disc instead of the freshest.
        """
        loop_dev = self._find_free_loop()
        # Don't pre-attach — Phase 1 unmounts/ejects before prompting,
        # so we attach the right ISO when the first prompt arrives.

        env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp / "tmp"),
            "TERM": "dumb",
        }
        cmd = [
            "bash",
            str(self.meta_dir / "restore.sh"),
            "--key", str(self.key_file),
            "--target", str(self.restore_target),
            "--repo", "photos",
            "--drive", loop_dev,
            "--work-dir", str(self.tmp / "work"),
        ]

        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(cmd[0], cmd, env)
            # execvpe doesn't return; if it does, bail.
            os._exit(127)  # noqa: SLF001

        try:
            return self._pty_loop(pid, fd, loop_dev, bootstrap_oldest)
        finally:
            # Ensure child is dead.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            with contextlib.suppress(OSError):
                os.close(fd)
            # Always detach the loop device.
            self._detach_loop(loop_dev)

    def _pty_loop(
        self, pid: int, fd: int, loop_dev: str,
        bootstrap_oldest: bool = False,
    ) -> tuple[int, str]:
        # Labels are UPPER_ALPHA + digits + underscores.  Must NOT
        # match the ║ box-drawing character that follows the %-36s field.
        prompt_re = re.compile(rb"INSERT DISC:\s*([A-Za-z0-9_]*)")

        # Data ISOs only (no META).  Sorted so [-1] = highest-numbered
        # = freshest catalog, used for the Phase-1 empty-label prompt.
        data_isos = sorted(
            iso for iso in self.all_isos if "META" not in iso.stem
        )
        assert data_isos, "no data ISOs found — check burn pipeline output"
        iso_by_label: dict[str, Path] = {iso.stem: iso for iso in self.all_isos}
        bootstrap_iso = data_isos[0] if bootstrap_oldest else data_isos[-1]

        buf = b""
        output_chunks: list[bytes] = []
        deadline = time.monotonic() + RESTORE_TIMEOUT

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                r, _, _ = select.select([fd], [], [], min(remaining, 2.0))
            except InterruptedError:
                continue

            if r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output_chunks.append(chunk)
                buf += chunk

                # Respond to every disc-swap prompt in the buffer.
                while True:
                    m = prompt_re.search(buf)
                    if not m:
                        break
                    want = m.group(1).decode().strip()
                    buf = buf[m.end():]

                    # Phase 1 empty label → pick bootstrap disc.
                    if not want:
                        want = bootstrap_iso.stem

                    iso = iso_by_label.get(want)
                    if iso is None:
                        os.write(fd, b"skip\n")
                        continue

                    self._swap_loop(loop_dev, iso)
                    time.sleep(0.1)
                    os.write(fd, b"\n")

            # Check if child exited.
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if wpid == pid:
                # Drain remaining output from the PTY.
                self._drain(fd, output_chunks)
                full = b"".join(output_chunks).decode(errors="replace")
                return os.waitstatus_to_exitcode(status), full

        # Reached here either by break (EOF / child exit) or timeout.
        full = b"".join(output_chunks).decode(errors="replace")

        # Try to reap the child — it probably already exited.
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return -1, full
        if wpid == pid:
            return os.waitstatus_to_exitcode(status), full

        # Still running → timeout.  Kill and report.
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        return -1, full

    @staticmethod
    def _drain(fd: int, sink: list[bytes], timeout: float = 1.0) -> None:
        """Read any remaining bytes from *fd* after child exits."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
            except (InterruptedError, OSError):
                break
            if not r:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            sink.append(chunk)

    # ── State reset ─────────────────────────────────────────────

    def _reset_restore_state(self) -> None:
        """Clear restore target and work dirs so tests are independent."""
        if self.restore_target.exists():
            shutil.rmtree(self.restore_target)
        self.restore_target.mkdir()
        work = self.tmp / "work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()

    # ── Assertions ───────────────────────────────────────────────

    def _assert_files_restored(self) -> None:
        """Verify every generated file was restored byte-for-byte."""
        found: dict[str, Path] = {}
        for root, _dirs, files in os.walk(self.restore_target):
            for f in files:
                if f.endswith(".bin"):
                    found[f] = Path(root) / f

        assert len(found) >= len(self.manifest), (
            f"Expected {len(self.manifest)} files, found {len(found)}"
        )
        for name, expected_hash in self.manifest.items():
            assert name in found, f"Missing restored file: {name}"
            actual = _sha256_file(found[name])
            assert actual == expected_hash, (
                f"{name}: hash mismatch "
                f"(expected {expected_hash[:16]}…, got {actual[:16]}…)"
            )

    def test_interactive_restore_completes(self):
        """Drive restore.sh interactively and verify restored data."""
        rc, output = self._drive_restore()

        assert rc == 0, (
            f"restore.sh failed (rc={rc}):\n{output}"
        )
        assert "RESTORE COMPLETE" in output, (
            f"Missing 'RESTORE COMPLETE' in output:\n{output[-2000:]}"
        )

        # The whole point: verify that multiple disc swaps occurred.
        phase2_swaps = len(re.findall(
            r"INSERT DISC:\s+ITEST_", output
        ))
        assert phase2_swaps >= 2, (
            f"Expected ≥2 disc-swap prompts in Phase 2, got {phase2_swaps}"
        )

        self._assert_files_restored()

    def test_stale_catalog_triggers_upgrade(self):
        """Bootstrap from oldest disc with stale catalog; verify upgrade."""
        self._reset_restore_state()
        self._prepare_stale_bootstrap_iso()

        rc, output = self._drive_restore(bootstrap_oldest=True)

        assert rc == 0, (
            f"restore.sh failed (rc={rc}):\n{output}"
        )
        assert "RESTORE COMPLETE" in output, (
            f"Missing 'RESTORE COMPLETE' in output:\n{output[-2000:]}"
        )

        # The organic upgrade must have fired at least once.
        assert "Fresher catalog" in output, (
            f"Expected catalog upgrade message in output:\n{output[-2000:]}"
        )

        self._assert_files_restored()
