"""Corrupt-disc failover: verify multi-copy resilience.

Scenario:
    1. Create a rustic repo with test data.
    2. Back up and burn to ISOs.
    3. Create a SECOND copy of the same volumes (simulating multi-copy burn).
    4. Corrupt several pack files in copy 1.
    5. Attempt ingest from copy 1 with ``collect_failures=True``.
    6. Verify corrupted packs are detected and reported.
    7. Re-ingest the failed packs from copy 2 (the clean copy).
    8. Restore and verify byte-for-byte correctness.

Requires: ``rustic`` and ``xorriso`` on PATH.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
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
from lcsas.restore.executor import PackCorruptionError, RestoreExecutor

# ── Skip conditions ──────────────────────────────────────────────

requires_rustic = pytest.mark.skipif(
    not shutil.which("rustic"), reason="rustic not installed"
)
requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"), reason="xorriso not installed"
)
pytestmark = [requires_rustic, requires_xorriso]

# ── Constants ────────────────────────────────────────────────────

RNG_SEED = 20260216
NUM_FILES = 10
FILE_SIZE_RANGE = (512, 8_192)


# ── Helpers ──────────────────────────────────────────────────────

def _generate_files(
    directory: Path,
    rng: random.Random,
    count: int,
    prefix: str = "file",
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
    args: list[str],
    repo: Path,
    pw: Path,
    tmpdir: Path | None = None,
) -> subprocess.CompletedProcess:
    env = None
    if tmpdir is not None:
        env = {**os.environ, "TMPDIR": str(tmpdir)}
    return subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), *args],
        capture_output=True, text=True, check=True, env=env,
    )


class _NoOpDVDisaster:
    def augment_iso(self, *a, **kw):
        pass
    def verify_iso(self, *a, **kw):
        return True
    def repair_iso(self, *a, **kw):
        return True


class _TestXorrisoRunner:
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
    """Stub for RestoreExecutor — only used for prepare_cache/ingest."""
    def init_repo(self, *a, **kw): pass
    def backup(self, *a, **kw): pass
    def snapshots(self, *a, **kw): return []
    def restore_dry_run(self, *a, **kw): pass
    def restore(self, *a, **kw): pass
    def prune_dry_run(self, *a, **kw): pass


def _extract_iso(iso_path: Path, dest: Path) -> Path:
    """Extract ISO contents via xorriso."""
    dest.mkdir(parents=True, exist_ok=True)
    for subpath in ("/data", "/metadata", "/catalog.db", "/volume_info.json"):
        target = dest / subpath.lstrip("/")
        subprocess.run(
            ["xorriso", "-indev", str(iso_path),
             "-osirrox", "on",
             "-extract", subpath, str(target)],
            capture_output=True, text=True,
        )
        # Some subpaths may not exist on all ISOs (e.g. standalone_restorer.py)
        # — that's fine, just skip.
    # Fix read-only permissions from ISO
    for root, dirs, files in os.walk(dest):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)
    return dest


# =========================================================================
# TEST CLASS
# =========================================================================


class TestCorruptDiscFailover:
    """Verify that corrupted packs are detected and recovery falls back
    to a second (clean) copy of the same volume."""

    @pytest.fixture(autouse=True)
    def setup_scenario(self, tmp_path: Path):
        """Create a single-repo scenario, burn to ISOs, then set up
        two copies of the extracted data — one of which gets corrupted."""
        self.tmp = tmp_path
        self.rng = random.Random(RNG_SEED)

        # Directories
        src_dir = tmp_path / "source_data"
        mirror = tmp_path / "mirror"
        staging = tmp_path / "staging"
        iso_out = tmp_path / "isos"
        db_path = tmp_path / "db" / "catalog.db"
        self.key_file = tmp_path / "key.txt"

        for d in [mirror, staging, iso_out, db_path.parent]:
            d.mkdir(parents=True, exist_ok=True)

        # 1. Create key + test data
        self.key_file.write_text("corrupt-disc-test-password\n")
        self.manifest = _generate_files(src_dir, self.rng, NUM_FILES, "f")

        # 2. Init repo + backup
        repo = mirror / "data"
        _rustic(["init"], repo, self.key_file, tmpdir=tmp_path)
        _rustic(["backup", "--json", str(src_dir)],
                repo, self.key_file, tmpdir=tmp_path)

        # 3. Init LCSAS catalog
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "data", "Test Data", str(repo))

        scanned = scan_mirror_packs(repo)
        delta = DeltaAnalyzer(conn, scanned, repo_id="data")
        delta.register_new_packs()

        # 4. Burn to ISOs
        mt = MediaType.TEST_TINY
        repo_configs = {
            "data": RepositoryConfig(
                name="data",
                mirror_path=repo,
                password_file=self.key_file,
            ),
        }
        config = LCSASConfig(
            mirror_base_path=mirror,
            staging_path=staging,
            db_path=db_path,
            default_media_type=mt,
            default_ecc_redundancy_pct=0,
            label_prefix="COPY",
            metadata_reserve_bytes=50_000,
            repositories=repo_configs,
        )
        from lcsas.db.queries import get_unarchived_packs

        xorriso = _TestXorrisoRunner()
        dvdisaster = _NoOpDVDisaster()
        orchestrator = BurnOrchestrator(config, conn, xorriso, dvdisaster)

        self.iso_files: list[Path] = []
        while get_unarchived_packs(conn):
            try:
                manifest = orchestrator.prepare(media_type=mt)
                iso_path = iso_out / f"{manifest.volume_label}.iso"
                orchestrator.execute(
                    manifest,
                    iso_output=iso_path,
                    skip_burn=True,
                    skip_ecc=True,
                )
                self.iso_files.append(iso_path)
            except ValueError:
                break

        conn.close()

        # 5. Extract ISOs into two copies: copy_clean and copy_corrupt
        self.extract_dir = tmp_path / "extracted"
        self.copy_clean = tmp_path / "copy_clean"
        self.copy_corrupt = tmp_path / "copy_corrupt"

        for iso in self.iso_files:
            label = iso.stem
            _extract_iso(iso, self.extract_dir / label)

        # Make two copies of all extracted data
        shutil.copytree(str(self.extract_dir), str(self.copy_clean))
        shutil.copytree(str(self.extract_dir), str(self.copy_corrupt))

        # 6. Corrupt packs in copy_corrupt
        self.corrupted_packs: list[str] = []
        for vol_dir in sorted(self.copy_corrupt.iterdir()):
            data_dir = vol_dir / "data"
            if not data_dir.is_dir():
                continue
            pack_files = sorted(data_dir.rglob("*"))
            pack_files = [f for f in pack_files if f.is_file()]
            # Corrupt up to 2 packs per volume
            to_corrupt = pack_files[:min(2, len(pack_files))]
            for pack_file in to_corrupt:
                sha = pack_file.name
                self.corrupted_packs.append(sha)
                # Flip bytes in the middle of the file
                data = bytearray(pack_file.read_bytes())
                mid = len(data) // 2
                for i in range(min(8, len(data))):
                    data[mid + i] ^= 0xFF
                pack_file.write_bytes(bytes(data))

        assert len(self.corrupted_packs) > 0, (
            "Test setup: no packs to corrupt"
        )

        # 7. Collect ALL required pack SHA-256 hashes
        self.all_pack_shas: list[str] = []
        for vol_dir in sorted(self.extract_dir.iterdir()):
            data_dir = vol_dir / "data"
            if not data_dir.is_dir():
                continue
            for pack_file in data_dir.rglob("*"):
                if pack_file.is_file():
                    sha = pack_file.name
                    if sha not in self.all_pack_shas:
                        self.all_pack_shas.append(sha)

        # 8. Metadata source (from clean copy, most recent volume)
        latest_vol = sorted(self.copy_clean.iterdir())[-1]
        self.metadata_source = latest_vol / "metadata" / "data"

        self.restore_dir = tmp_path / "restored"
        self.restore_dir.mkdir()

    # ── Tests ────────────────────────────────────────────────────

    def test_corrupt_packs_detected(self):
        """Ingest from corrupted copy detects SHA-256 mismatches."""
        executor = RestoreExecutor(_NoOpRustic())
        cache = self.restore_dir / "cache_detect"
        executor.prepare_cache(cache, self.metadata_source)

        # Ingest from corrupted copy with collect_failures=True
        total_ingested = 0
        all_failed: list[str] = []
        for vol_dir in sorted(self.copy_corrupt.iterdir()):
            ingested, failed = executor.ingest_volume(
                cache_dir=cache,
                volume_mount=vol_dir,
                required_packs=self.all_pack_shas,
                verify=True,
                collect_failures=True,
            )
            total_ingested += ingested
            all_failed.extend(failed)

        # At least some corrupted packs should have been detected
        assert len(all_failed) > 0, "No corrupted packs detected!"
        for sha in self.corrupted_packs:
            assert sha in all_failed, (
                f"Corrupted pack {sha[:16]}... was not detected"
            )

    def test_corrupt_pack_raises_without_collect(self):
        """Without collect_failures, corruption raises PackCorruptionError."""
        executor = RestoreExecutor(_NoOpRustic())
        cache = self.restore_dir / "cache_raise"
        executor.prepare_cache(cache, self.metadata_source)

        with pytest.raises(PackCorruptionError):
            for vol_dir in sorted(self.copy_corrupt.iterdir()):
                executor.ingest_volume(
                    cache_dir=cache,
                    volume_mount=vol_dir,
                    required_packs=self.all_pack_shas,
                    verify=True,
                    collect_failures=False,
                )

    def test_failover_to_clean_copy_restores_all(self):
        """Corrupted packs from copy 1 are recovered from copy 2."""
        executor = RestoreExecutor(_NoOpRustic())
        cache = self.restore_dir / "cache_failover"
        executor.prepare_cache(cache, self.metadata_source)

        # Phase 1: Ingest from corrupted copy
        failed_packs: list[str] = []
        for vol_dir in sorted(self.copy_corrupt.iterdir()):
            ingested, failed = executor.ingest_volume(
                cache_dir=cache,
                volume_mount=vol_dir,
                required_packs=self.all_pack_shas,
                verify=True,
                collect_failures=True,
            )
            failed_packs.extend(failed)

        assert len(failed_packs) > 0, "Expected some failed packs"

        # Phase 2: Re-ingest ONLY the failed packs from clean copy
        for vol_dir in sorted(self.copy_clean.iterdir()):
            recovered, still_failed = executor.ingest_volume(
                cache_dir=cache,
                volume_mount=vol_dir,
                required_packs=failed_packs,
                verify=True,
                collect_failures=True,
            )

        # All packs should now be available
        cache_data = cache / "data"
        cached_packs = set()
        for f in cache_data.rglob("*"):
            if f.is_file():
                cached_packs.add(f.name)

        for sha in self.all_pack_shas:
            assert sha in cached_packs, (
                f"Pack {sha[:16]}... still missing after failover"
            )

        # Phase 3: Actually restore and verify byte-for-byte
        target = self.restore_dir / "output"
        target.mkdir(parents=True)

        result = subprocess.run(
            ["rustic", "restore", "latest", str(target),
             "-r", str(cache),
             "--password-file", str(self.key_file),
             "--no-cache"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"rustic restore failed (rc={result.returncode}):\n"
            f"stderr: {result.stderr}"
        )

        # Verify every file
        restored_files: dict[str, Path] = {}
        for root, _dirs, files in os.walk(target):
            for f in files:
                if f.endswith(".bin"):
                    restored_files[f] = Path(root) / f

        assert len(restored_files) >= len(self.manifest), (
            f"Expected {len(self.manifest)} files, got {len(restored_files)}"
        )
        for name, expected_hash in self.manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: hash mismatch after failover restore"
            )
