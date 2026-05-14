"""Pure-Python fallback restore: prove the fallback works with real rustic repos.

This test creates REAL rustic repositories (using the rustic binary),
burns them to ISOs through the full LCSAS pipeline, then restores the
data using ONLY the pure-Python fallback — proving that the
``PurePythonRestorer`` can handle real rustic-encrypted data, not just
synthetic test vectors.

Restore alternatives tested across the integration suite:
  1. System ``rustic`` binary   → test_disc_only_restore.py
  2. Meta-volume restore.sh     → test_meta_volume_restore.py
  3. Pure-Python fallback        → THIS FILE

Requires: ``rustic`` and ``xorriso`` on PATH (for initial data setup).
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs
from lcsas.restore.executor import RestoreExecutor
from lcsas.restore.restic_fallback import PurePythonRestorer

# ── Skip conditions ──────────────────────────────────────────────

_RESTIC_BIN = shutil.which("rustic") or shutil.which("restic") or ""
# rustic uses positional <destination>; restic uses --target <destination>
_RESTIC_IS_RUSTIC = os.path.basename(_RESTIC_BIN).startswith("rustic") if _RESTIC_BIN else False


def _restore_cmd(
    binary: str,
    snapshot: str,
    target: Path,
    repo: Path,
    password_file: Path,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build a rustic/restic restore command handling their different syntaxes.

    rustic: restore <snapshot> <destination> [flags]
    restic: restore <snapshot> [flags] --target <destination>
    """
    flags = extra_flags or []
    if os.path.basename(binary).startswith("rustic"):
        return [binary, "restore", snapshot, str(target),
                "-r", str(repo), "--password-file", str(password_file), *flags]
    return [binary, "restore", snapshot,
            "-r", str(repo), "--password-file", str(password_file), *flags,
            "--target", str(target)]


requires_restic_binary = pytest.mark.skipif(
    not _RESTIC_BIN,
    reason="neither rustic nor restic installed",
)
requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"), reason="xorriso not installed"
)
pytestmark = [requires_restic_binary, requires_xorriso]

# ── Deterministic test data ─────────────────────────────────────

RNG_SEED = 20260221
NUM_INITIAL_FILES = 8
NUM_INCREMENTAL_FILES = 4
FILE_SIZE_RANGE = (512, 4_096)


def _generate_files(
    directory: Path,
    rng: random.Random,
    count: int,
    prefix: str = "file",
) -> dict[str, str]:
    """Create deterministic random files.  Returns {name: sha256}."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for i in range(count):
        size = rng.randint(*FILE_SIZE_RANGE)
        data = rng.randbytes(size)
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


def _restic_cmd(
    args: list[str],
    repo: Path,
    password_file: Path,
    tmpdir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run a rustic/restic command (whichever is available)."""
    cmd = [_RESTIC_BIN, "-r", str(repo), "--password-file", str(password_file), *args]
    env = None
    if tmpdir is not None:
        env = {**os.environ, "TMPDIR": str(tmpdir)}
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )
    return result


# ── Stubs for burn pipeline ─────────────────────────────────────

class _NoOpDVDisaster:
    def augment_iso(self, *a, **kw):
        pass

    def verify_iso(self, *a, **kw):
        return True

    def repair_iso(self, *a, **kw):
        return True


class _TestXorrisoRunner:
    """Real ISO creation via xorriso, no physical burns."""

    def create_iso(self, source_dir: Path, output_iso: Path, volume_label: str, **_kw) -> Path:
        subprocess.run(
            ["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long",
             "-iso-level", "3", "-V", volume_label,
             "-o", str(output_iso), str(source_dir)],
            capture_output=True, text=True, check=True,
        )
        return output_iso

    def burn_iso(self, *a, **kw):
        pass

    def verify_disc(self, *a, **kw):
        return True


class _NoOpRustic:
    """Stub for RestoreExecutor (only prepare_cache is used)."""

    def init_repo(self, *a, **kw): pass
    def backup(self, *a, **kw): pass
    def snapshots(self, *a, **kw): return []
    def restore_dry_run(self, *a, **kw): pass
    def restore(self, *a, **kw): pass
    def prune_dry_run(self, *a, **kw): pass


# =========================================================================
# THE TEST
# =========================================================================


class TestPurePythonFallbackRestore:
    """Restore real rustic repos using ONLY the pure-Python fallback.

    This proves the PurePythonRestorer handles real rustic-encrypted data:
    - scrypt key derivation with real rustic key files
    - AES-256-CTR decryption of real pack files
    - Poly1305-AES MAC verification with real MACs
    - Index/snapshot parsing of real rustic metadata
    - Optional zstd decompression of real compressed blobs
    - Recursive tree traversal of real directory trees
    """

    @pytest.fixture(autouse=True)
    def setup_world(self, tmp_path: Path):
        """Build repos, burn ISOs, nuke everything except ISOs + key."""
        self.tmp = tmp_path
        self.rng = random.Random(RNG_SEED)

        # ── Paths ────────────────────────────────────────────────
        self.original_data = tmp_path / "original_data"
        self.mirror = tmp_path / "mirror"
        self.staging = tmp_path / "staging"
        self.iso_out = tmp_path / "isos"
        self.db_path = tmp_path / "db" / "catalog.db"
        self.key_file = tmp_path / "key.txt"
        self.extract_dir = tmp_path / "extracted"
        self.restore_dir = tmp_path / "restored"

        for d in [self.mirror, self.staging, self.iso_out,
                  self.db_path.parent, self.extract_dir, self.restore_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # ── Step 1: Create encryption key ────────────────────────
        self.key_file.write_text("pure-python-fallback-test-pw\n")

        # ── Step 2: Generate test data ───────────────────────────
        self.family_manifest: dict[str, str] = {}
        self.work_manifest: dict[str, str] = {}

        family_src = self.original_data / "family"
        work_src = self.original_data / "work"

        self.family_manifest.update(
            _generate_files(family_src, self.rng, NUM_INITIAL_FILES, "fam")
        )
        self.work_manifest.update(
            _generate_files(work_src, self.rng, NUM_INITIAL_FILES, "wrk")
        )

        # ── Step 3: Init rustic repos ────────────────────────────
        family_repo = self.mirror / "family"
        work_repo = self.mirror / "work"

        _restic_cmd(["init"], family_repo, self.key_file, tmpdir=self.tmp)
        _restic_cmd(["init"], work_repo, self.key_file, tmpdir=self.tmp)

        # ── Step 4: Initial backup ───────────────────────────────
        _restic_cmd(["backup", "--json", str(family_src)],
                family_repo, self.key_file, tmpdir=self.tmp)
        _restic_cmd(["backup", "--json", str(work_src)],
                work_repo, self.key_file, tmpdir=self.tmp)

        # ── Step 5: LCSAS catalog + scan + burn ──────────────────
        conn = get_connection(self.db_path)
        create_all(conn)
        register_repo(conn, "family", "Family Photos", str(family_repo))
        register_repo(conn, "work", "Work Docs", str(work_repo))

        self._scan_and_register(conn, {"family": family_repo, "work": work_repo})
        iso_round_1 = self._burn_all(conn, family_repo, work_repo)

        # ── Step 6: Incremental data ─────────────────────────────
        self.family_manifest.update(
            _generate_files(family_src, self.rng, NUM_INCREMENTAL_FILES, "fam_inc")
        )
        work_files = sorted(work_src.iterdir())
        for f in work_files[:2]:
            new_data = self.rng.randbytes(self.rng.randint(*FILE_SIZE_RANGE))
            f.write_bytes(new_data)
            self.work_manifest[f.name] = hashlib.sha256(new_data).hexdigest()

        _restic_cmd(["backup", "--json", str(family_src)],
                family_repo, self.key_file, tmpdir=self.tmp)
        _restic_cmd(["backup", "--json", str(work_src)],
                work_repo, self.key_file, tmpdir=self.tmp)

        self._scan_and_register(conn, {"family": family_repo, "work": work_repo})
        iso_round_2 = self._burn_all(conn, family_repo, work_repo)
        conn.close()

        self.all_isos = iso_round_1 + iso_round_2

        # ══════════════════════════════════════════════════════════
        #  NUKE everything except ISOs + key
        # ══════════════════════════════════════════════════════════
        safe = tmp_path / "_safe"
        safe.mkdir()
        saved_isos = []
        for iso in self.all_isos:
            dest = safe / iso.name
            shutil.move(str(iso), str(dest))
            saved_isos.append(dest)
        saved_key = safe / "key.txt"
        shutil.copy2(str(self.key_file), str(saved_key))

        for entry in tmp_path.iterdir():
            if entry.name == "_safe":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

        self.iso_out.mkdir(parents=True)
        final_isos = []
        for iso in saved_isos:
            dest = self.iso_out / iso.name
            shutil.move(str(iso), str(dest))
            final_isos.append(dest)
        self.key_file = tmp_path / "key.txt"
        shutil.move(str(saved_key), str(self.key_file))
        shutil.rmtree(safe)

        self.all_isos = final_isos
        self.extract_dir = tmp_path / "extracted"
        self.restore_dir = tmp_path / "restored"
        self.extract_dir.mkdir()
        self.restore_dir.mkdir()

    # ── Pipeline helpers ─────────────────────────────────────────

    def _scan_and_register(
        self, conn: sqlite3.Connection, repos: dict[str, Path]
    ) -> None:
        for repo_id, repo_path in repos.items():
            scanned = scan_mirror_packs(repo_path)
            delta = DeltaAnalyzer(conn, scanned, repo_id=repo_id)
            delta.register_new_packs()

    def _burn_all(
        self, conn: sqlite3.Connection,
        family_repo: Path, work_repo: Path,
    ) -> list[Path]:
        from lcsas.db.queries import get_unarchived_packs

        mt = MediaType.TEST_TINY
        config = LCSASConfig(
            mirror_base_path=self.mirror,
            staging_path=self.staging,
            db_path=self.db_path,
            default_media_type=mt,
            default_ecc_redundancy_pct=0,
            label_prefix="PYFALL",
            metadata_reserve_bytes=50_000,
            repositories={
                "family": RepositoryConfig(
                    name="family", mirror_path=family_repo,
                    password_file=self.key_file,
                ),
                "work": RepositoryConfig(
                    name="work", mirror_path=work_repo,
                    password_file=self.key_file,
                ),
            },
        )
        orchestrator = BurnOrchestrator(
            config, conn, _TestXorrisoRunner(), _NoOpDVDisaster()
        )
        iso_files: list[Path] = []
        while get_unarchived_packs(conn):
            try:
                manifest = orchestrator.prepare(media_type=mt)
                iso = self.iso_out / f"{manifest.volume_label}.iso"
                orchestrator.execute(manifest, iso_output=iso,
                                     skip_burn=True)
                iso_files.append(iso)
            except ValueError:
                break
        return iso_files

    # ── ISO extraction ───────────────────────────────────────────

    def _extract_iso(self, iso_path: Path) -> Path:
        """Extract an ISO's contents using xorriso.  Returns extract dir."""
        vol_label = iso_path.stem
        dest = self.extract_dir / vol_label
        if dest.exists():
            return dest
        dest.mkdir(parents=True, exist_ok=True)

        # Extract /data
        subprocess.run(
            ["xorriso", "-indev", str(iso_path),
             "-osirrox", "on",
             "-extract", "/data", str(dest / "data")],
            capture_output=True, text=True, check=True,
        )

        # Extract /metadata
        subprocess.run(
            ["xorriso", "-indev", str(iso_path),
             "-osirrox", "on",
             "-extract", "/metadata", str(dest / "metadata")],
            capture_output=True, text=True, check=True,
        )

        # Extract /catalog.db
        subprocess.run(
            ["xorriso", "-indev", str(iso_path),
             "-osirrox", "on",
             "-extract", "/catalog.db", str(dest / "catalog.db")],
            capture_output=True, text=True, check=True,
        )

        # Extract /volume_info.json
        subprocess.run(
            ["xorriso", "-indev", str(iso_path),
             "-osirrox", "on",
             "-extract", "/volume_info.json",
             str(dest / "volume_info.json")],
            capture_output=True, text=True, check=True,
        )

        # Fix ISO read-only permissions so pytest tmp_path can clean up
        for root, dirs, files in os.walk(dest):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)

        return dest

    def _extract_all(self) -> None:
        for iso in self.all_isos:
            self._extract_iso(iso)

    # ── Restore cache builder ────────────────────────────────────

    def _build_restore_cache(self, repo_id: str) -> Path:
        """Build a restore cache for one repo using ONLY disc data.

        Arranges packs in two-level ``data/<prefix>/<hash>`` layout.
        """
        cache = self.restore_dir / f"cache_{repo_id}"
        extracted_vols = sorted(
            d for d in self.extract_dir.iterdir() if d.is_dir()
        )
        latest = extracted_vols[-1]
        metadata_src = latest / "metadata" / repo_id

        executor = RestoreExecutor(_NoOpRustic())
        executor.prepare_cache(cache, metadata_src)

        cache_data = cache / "data"
        cache_data.mkdir(exist_ok=True)
        for vol_dir in extracted_vols:
            data_dir = vol_dir / "data"
            if not data_dir.is_dir():
                continue
            for pack_file in data_dir.rglob("*"):
                if not pack_file.is_file():
                    continue
                sha = pack_file.name
                prefix_dir = cache_data / sha[:2]
                prefix_dir.mkdir(exist_ok=True)
                dst = prefix_dir / sha
                if not dst.exists():
                    shutil.copy2(str(pack_file), str(dst))
        return cache

    def _find_restored_files(self, target: Path) -> dict[str, Path]:
        """Walk restore target for .bin files, keyed by name."""
        found: dict[str, Path] = {}
        for root, _dirs, files in os.walk(target):
            for f in files:
                if f.endswith(".bin"):
                    found[f] = Path(root) / f
        return found

    # =================================================================
    # TESTS — Pure-Python Fallback Restore
    # =================================================================

    def test_environment_is_clean(self):
        """After nuke, only ISOs + key remain (plus restore dirs)."""
        entries = set(os.listdir(self.tmp))
        allowed = {"key.txt", "isos", "extracted", "restored"}
        assert entries <= allowed, f"Unexpected: {entries - allowed}"

    def test_fallback_verifies_key(self):
        """PurePythonRestorer can verify the key against a real repo."""
        self._extract_all()
        cache = self._build_restore_cache("family")
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        assert restorer.verify_key(), (
            "PurePythonRestorer failed to verify key against real rustic repo"
        )

    def test_fallback_rejects_wrong_password(self):
        """Wrong password is rejected for real rustic key files."""
        self._extract_all()
        cache = self._build_restore_cache("family")
        wrong_key = self.tmp / "wrong.txt"
        wrong_key.write_text("definitely-wrong-password\n")
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=wrong_key,
        )
        assert not restorer.verify_key(), (
            "PurePythonRestorer should reject wrong password"
        )

    def test_fallback_lists_snapshots(self):
        """PurePythonRestorer finds snapshots from real rustic repos."""
        self._extract_all()
        cache = self._build_restore_cache("family")
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        snaps = restorer.list_snapshots()
        assert len(snaps) >= 2, (
            f"Expected ≥2 snapshots (initial + incremental), got {len(snaps)}"
        )

    def test_fallback_repo_info(self):
        """PurePythonRestorer reads repo config and blob index."""
        self._extract_all()
        cache = self._build_restore_cache("family")
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        info = restorer.repo_info()
        assert info["version"] in (1, 2), f"Unexpected version: {info['version']}"
        assert info["snapshots"] >= 2
        assert info["indexed_blobs"] > 0

    def test_fallback_restore_family(self):
        """Full restore of family repo using pure-Python fallback.

        Verifies every file byte-for-byte against the original manifest,
        including files from the incremental backup.
        """
        self._extract_all()
        cache = self._build_restore_cache("family")
        target = self.restore_dir / "family_fallback"

        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        snap = restorer.restore(target=target)
        assert snap.time != "", "Snapshot should have a timestamp"

        restored_files = self._find_restored_files(target)

        assert len(restored_files) >= len(self.family_manifest), (
            f"Expected {len(self.family_manifest)} files, "
            f"found {len(restored_files)}"
        )
        for name, expected_hash in self.family_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: hash mismatch "
                f"(expected {expected_hash[:16]}..., got {actual_hash[:16]}...)"
            )

    def test_fallback_restore_work(self):
        """Full restore of work repo using pure-Python fallback.

        Includes modified files from the incremental backup — verifies
        the LATEST versions are restored.
        """
        self._extract_all()
        cache = self._build_restore_cache("work")
        target = self.restore_dir / "work_fallback"

        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        restorer.restore(target=target)

        restored_files = self._find_restored_files(target)

        assert len(restored_files) >= len(self.work_manifest), (
            f"Expected {len(self.work_manifest)} files, "
            f"found {len(restored_files)}"
        )
        for name, expected_hash in self.work_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: hash mismatch "
                f"(expected {expected_hash[:16]}..., got {actual_hash[:16]}...)"
            )

    def test_fallback_matches_rustic_restore(self):
        """Pure-Python fallback produces identical output to rustic.

        Restores the same repo with both rustic and PurePythonRestorer,
        then compares every file byte-for-byte.
        """
        self._extract_all()
        cache = self._build_restore_cache("family")

        # ── Restore with rustic/restic ────────────────────────────
        rustic_target = self.restore_dir / "family_rustic"
        rustic_target.mkdir(parents=True)
        result = subprocess.run(
            _restore_cmd(
                _RESTIC_BIN, "latest", rustic_target,
                cache, self.key_file, ["--no-cache"]
            ),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"{_RESTIC_BIN} restore failed:\n{result.stderr}"
        )

        # ── Restore with pure-Python fallback ────────────────────
        fallback_target = self.restore_dir / "family_fallback_cmp"
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        restorer.restore(target=fallback_target)

        # ── Compare all files ────────────────────────────────────
        rustic_files = self._find_restored_files(rustic_target)
        fallback_files = self._find_restored_files(fallback_target)

        assert set(rustic_files.keys()) == set(fallback_files.keys()), (
            f"File sets differ:\n"
            f"  rustic only: {set(rustic_files) - set(fallback_files)}\n"
            f"  fallback only: {set(fallback_files) - set(rustic_files)}"
        )

        for name in rustic_files:
            rustic_hash = _sha256_file(rustic_files[name])
            fallback_hash = _sha256_file(fallback_files[name])
            assert rustic_hash == fallback_hash, (
                f"{name}: rustic vs fallback hash mismatch"
            )

    def test_fallback_restore_with_flat_layout(self):
        """PurePythonRestorer works with flat data/ layout (LCSAS disc style).

        Rearranges packs from two-level to flat layout and verifies
        the fallback still restores correctly.
        """
        self._extract_all()
        cache = self._build_restore_cache("family")

        # Flatten data/ from two-level to flat layout
        data_dir = cache / "data"
        pack_files = [p for p in data_dir.rglob("*") if p.is_file()]
        for pf in pack_files:
            flat_path = data_dir / pf.name
            if flat_path != pf:
                shutil.move(str(pf), str(flat_path))

        # Remove empty prefix directories
        import contextlib
        for d in list(data_dir.iterdir()):
            if d.is_dir():
                with contextlib.suppress(OSError):
                    d.rmdir()

        target = self.restore_dir / "family_flat"
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        restorer.restore(target=target)

        restored_files = self._find_restored_files(target)
        for name, expected_hash in self.family_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: hash mismatch in flat layout restore"
            )

    def test_fallback_incremental_files_present(self):
        """Incremental backup files appear in fallback restore."""
        self._extract_all()
        cache = self._build_restore_cache("family")
        target = self.restore_dir / "family_inc_check"

        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        restorer.restore(target=target)

        restored = self._find_restored_files(target)
        inc_names = [f"fam_inc_{i:04d}.bin" for i in range(NUM_INCREMENTAL_FILES)]
        for name in inc_names:
            assert name in restored, (
                f"Incremental file {name} missing from fallback restore"
            )

    def test_restore_executor_with_fallback_pipeline(self):
        """Full pipeline: RestoreExecutor assembles cache, then
        PurePythonRestorer decrypts and extracts.

        This exercises the RestoreExecutor.prepare_cache() →
        ingest_volume() → PurePythonRestorer.restore() path that a
        real disaster-recovery scenario would follow.
        """
        self._extract_all()

        extracted_vols = sorted(
            d for d in self.extract_dir.iterdir() if d.is_dir()
        )
        latest = extracted_vols[-1]
        metadata_src = latest / "metadata" / "family"

        # ── Use RestoreExecutor to build the cache ───────────────
        executor = RestoreExecutor(_NoOpRustic())
        cache = self.restore_dir / "executor_cache"
        executor.prepare_cache(cache, metadata_src)

        # Collect all pack SHA-256s from disc data/ directories (two-level layout)
        all_packs: list[str] = []
        for vol_dir in extracted_vols:
            data_dir = vol_dir / "data"
            if data_dir.is_dir():
                all_packs.extend(
                    f.name for f in data_dir.rglob("*") if f.is_file()
                )

        # Ingest from each volume.  In a real multi-disc recovery any
        # single volume only holds a subset of the packs, so ask the
        # executor to collect (not raise on) "pack not present here";
        # the loop fills the cache by trying every volume in turn.
        for vol_dir in extracted_vols:
            executor.ingest_volume(
                cache_dir=cache,
                volume_mount=vol_dir,
                required_packs=all_packs,
                verify=True,
                collect_failures=True,
            )

        # ── Now restore with PurePythonRestorer ──────────────────
        target = self.restore_dir / "executor_fallback"
        restorer = PurePythonRestorer(
            repo_path=cache,
            password_file=self.key_file,
        )
        restorer.restore(target=target)

        restored_files = self._find_restored_files(target)
        assert len(restored_files) >= len(self.family_manifest)
        for name, expected_hash in self.family_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: hash mismatch in executor+fallback pipeline"
            )
