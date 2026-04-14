"""Disc-only restore: the ultimate disaster-recovery validation.

Given ONLY the burned ISOs and the encryption key file, this test
restores an entire multi-repo collection — including data from
incremental backups — and verifies every file byte-for-byte against
the originals.

If anything beyond the discs and the key file is required, the core
purpose of LCSAS has been critically missed.

Scenario
--------
  1. Create two rustic repositories (``family``, ``work``).
  2. Back up initial data into both repos (snapshot S1).
  3. Scan packs → burn to ISOs (Disc 1 etc.).
  4. ADD new files to ``family``, MODIFY files in ``work``.
  5. Back up incrementally (snapshot S2).
  6. Scan new packs → burn to ISOs (Disc N+1 etc.).
  7. **DELETE everything** except the ISOs and ``key.txt``.
  8. Extract ISOs with ``xorriso``.
  9. Reconstruct each repo cache from on-disc metadata + packs.
 10. ``rustic restore latest`` from the reconstructed cache.
 11. Verify every restored file matches the originals.

Requires: ``rustic`` and ``xorriso`` on PATH.
"""

from __future__ import annotations

import hashlib
import json
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

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

requires_rustic = pytest.mark.skipif(
    not shutil.which("rustic"),
    reason="rustic not installed",
)
requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"),
    reason="xorriso not installed",
)
pytestmark = [requires_rustic, requires_xorriso]

# ---------------------------------------------------------------------------
# Deterministic test data
# ---------------------------------------------------------------------------

RNG_SEED = 20260214
NUM_INITIAL_FILES = 12
NUM_INCREMENTAL_FILES = 6
FILE_SIZE_RANGE = (512, 8_192)


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


def _rustic(args: list[str], repo: Path, password_file: Path,
            tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    """Run a rustic command.

    If *tmpdir* is provided it is passed as TMPDIR so that rustic writes
    its temporary pack files there instead of the system /tmp (which may
    be a small partition).
    """
    cmd = ["rustic", "-r", str(repo), "--password-file", str(password_file), *args]
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


def _rustic_snapshot_id(result: subprocess.CompletedProcess) -> str:
    """Extract snapshot ID from ``rustic``/``restic`` ``backup --json`` output.

    Handles two formats:
    - rustic: single pretty-printed JSON object with an ``"id"`` field.
    - restic: JSON lines where the summary line contains ``"snapshot_id"``.
    """
    stdout = result.stdout.strip()
    # Rustic format: the entire stdout is a single JSON object
    try:
        obj = json.loads(stdout)
        if "id" in obj:
            return obj["id"]
        if "snapshot_id" in obj:
            return obj["snapshot_id"]
    except json.JSONDecodeError:
        pass
    # Restic format: JSON lines — last object has "snapshot_id"
    for line in reversed(stdout.splitlines()):
        try:
            obj = json.loads(line)
            if "snapshot_id" in obj:
                return obj["snapshot_id"]
            if "id" in obj:
                return obj["id"]
        except json.JSONDecodeError:
            continue
    raise RuntimeError("Could not parse snapshot_id from rustic/restic output")


# ---------------------------------------------------------------------------
# No-op ECC runner (we skip dvdisaster for test media)
# ---------------------------------------------------------------------------

class _NoOpDVDisaster:
    def augment_iso(self, iso_path, redundancy_pct=15):
        pass

    def verify_iso(self, iso_path):
        return True

    def repair_iso(self, iso_path):
        return True


# ---------------------------------------------------------------------------
# No-op xorriso burn (we only create ISOs, never burn physical media)
# ---------------------------------------------------------------------------


class _TestXorrisoRunner:
    """Real ISO creation, but no physical burns."""

    def create_iso(self, source_dir: Path, output_iso: Path, volume_label: str, **_kw) -> Path:
        cmd = [
            "xorriso",
            "-as", "mkisofs",
            "-r", "-J", "-joliet-long", "-iso-level", "3",
            "-V", volume_label,
            "-o", str(output_iso),
            str(source_dir),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return output_iso

    def burn_iso(self, iso_path: Path, device: str = "/dev/sr0") -> None:
        pass  # No physical burn

    def verify_disc(self, device: str = "/dev/sr0") -> bool:
        return True


# =========================================================================
# THE TEST
# =========================================================================


class TestDiscOnlyRestore:
    """Given ONLY ISOs + key file, restore everything and verify."""

    # -----------------------------------------------------------------
    # Fixtures (scoped to this class for isolation)
    # -----------------------------------------------------------------

    @pytest.fixture(autouse=True)
    def setup_world(self, tmp_path: Path):
        """Build the complete pipeline state, then destroy everything
        except ISOs and the key file."""

        self.tmp = tmp_path
        self.rng = random.Random(RNG_SEED)

        # Directory layout
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

        # ── Step 1: Create encryption key ────────────────────────────
        self.key_file.write_text("disc-only-restore-test-password\n")

        # ── Step 2: Generate initial test data ───────────────────────
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

        # ── Step 3: Init rustic repos (the "local mirrors") ────────
        family_repo = self.mirror / "family"
        work_repo = self.mirror / "work"

        _rustic(["init"], family_repo, self.key_file, tmpdir=self.tmp)
        _rustic(["init"], work_repo, self.key_file, tmpdir=self.tmp)

        # ── Step 4: Initial backup ──────────────────────────────────
        r = _rustic(["backup", "--json", str(family_src)],
                     family_repo, self.key_file, tmpdir=self.tmp)
        self.snap_family_1 = _rustic_snapshot_id(r)

        r = _rustic(["backup", "--json", str(work_src)],
                     work_repo, self.key_file, tmpdir=self.tmp)
        self.snap_work_1 = _rustic_snapshot_id(r)

        # ── Step 5: Init LCSAS catalog & register repos ──────────────
        conn = get_connection(self.db_path)
        create_all(conn)
        register_repo(conn, "family", "Family Photos",
                      str(family_repo))
        register_repo(conn, "work", "Work Documents",
                      str(work_repo))

        # ── Step 6: Scan + register + burn round 1 ─────────────────
        self._scan_and_register(conn, {"family": family_repo,
                                        "work": work_repo})
        iso_round_1 = self._burn_all(conn, family_repo, work_repo)

        # ── Step 7: Incremental data ────────────────────────────────
        # Add new files to family
        self.family_manifest.update(
            _generate_files(family_src, self.rng, NUM_INCREMENTAL_FILES,
                            "fam_inc")
        )
        # Modify existing files in work
        work_files = sorted(work_src.iterdir())
        for f in work_files[:3]:
            new_data = self.rng.randbytes(self.rng.randint(*FILE_SIZE_RANGE))
            f.write_bytes(new_data)
            self.work_manifest[f.name] = hashlib.sha256(new_data).hexdigest()

        # ── Step 8: Incremental backup ──────────────────────────────
        r = _rustic(["backup", "--json", str(family_src)],
                     family_repo, self.key_file, tmpdir=self.tmp)
        self.snap_family_2 = _rustic_snapshot_id(r)

        r = _rustic(["backup", "--json", str(work_src)],
                     work_repo, self.key_file, tmpdir=self.tmp)
        self.snap_work_2 = _rustic_snapshot_id(r)

        # ── Step 9: Scan + register + burn round 2 ──────────────────
        self._scan_and_register(conn, {"family": family_repo,
                                        "work": work_repo})
        iso_round_2 = self._burn_all(conn, family_repo, work_repo)

        conn.close()

        self.all_isos = iso_round_1 + iso_round_2

        # ══════════════════════════════════════════════════════════════
        # ██  THE MOMENT OF TRUTH: delete EVERYTHING except ISOs + key
        # ══════════════════════════════════════════════════════════════

        # Move ISOs and key to a safe location
        safe = tmp_path / "_safe"
        safe.mkdir()
        saved_isos = []
        for iso in self.all_isos:
            dest = safe / iso.name
            shutil.move(str(iso), str(dest))
            saved_isos.append(dest)
        saved_key = safe / "key.txt"
        shutil.copy2(str(self.key_file), str(saved_key))

        # Nuke everything in tmp_path except _safe
        for entry in tmp_path.iterdir():
            if entry.name == "_safe":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

        # Move saved items back
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

        # At this point, tmp_path contains ONLY:
        #   key.txt
        #   isos/*.iso
        # Nothing else. No mirror, no DB, no original data, no staging.

        # Directories for the restore phase
        self.extract_dir = tmp_path / "extracted"
        self.restore_dir = tmp_path / "restored"
        self.extract_dir.mkdir()
        self.restore_dir.mkdir()

    # -----------------------------------------------------------------
    # Pipeline helpers (used during setup, before the wipe)
    # -----------------------------------------------------------------

    def _scan_and_register(
        self, conn: sqlite3.Connection, repos: dict[str, Path]
    ) -> None:
        for repo_id, repo_path in repos.items():
            scanned = scan_mirror_packs(repo_path)
            delta = DeltaAnalyzer(conn, scanned, repo_id=repo_id)
            delta.register_new_packs()

    def _burn_all(
        self,
        conn: sqlite3.Connection,
        family_repo: Path,
        work_repo: Path,
    ) -> list[Path]:
        """Burn all unarchived packs to ISOs.  Returns list of ISO paths."""
        from lcsas.db.queries import get_unarchived_packs

        mt = MediaType.TEST_TINY  # 1 MB — forces multi-volume
        repo_configs = {
            "family": RepositoryConfig(
                name="family",
                mirror_path=family_repo,
                password_file=self.key_file,
            ),
            "work": RepositoryConfig(
                name="work",
                mirror_path=work_repo,
                password_file=self.key_file,
            ),
        }
        config = LCSASConfig(
            mirror_base_path=self.mirror,
            staging_path=self.staging,
            db_path=self.db_path,
            default_media_type=mt,
            default_ecc_redundancy_pct=0,
            label_prefix="DISC",
            metadata_reserve_bytes=50_000,
            repositories=repo_configs,
        )

        xorriso = _TestXorrisoRunner()
        dvdisaster = _NoOpDVDisaster()
        orchestrator = BurnOrchestrator(config, conn, xorriso, dvdisaster)

        iso_files: list[Path] = []
        while get_unarchived_packs(conn):
            try:
                manifest = orchestrator.prepare(media_type=mt)
                iso_path = self.iso_out / f"{manifest.volume_label}.iso"
                orchestrator.execute(
                    manifest,
                    iso_output=iso_path,
                    skip_burn=True,
                    skip_ecc=True,
                )
                iso_files.append(iso_path)
            except ValueError:
                break
        return iso_files

    # -----------------------------------------------------------------
    # Extraction helper
    # -----------------------------------------------------------------

    def _extract_iso(self, iso_path: Path) -> Path:
        """Extract an ISO's contents using xorriso.  Returns extract dir."""
        vol_label = iso_path.stem
        dest = self.extract_dir / vol_label
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

    def _build_restore_cache(self, repo_id: str) -> Path:
        """Build a rustic restore cache for one repo using ONLY disc data.

        Uses the metadata from the LAST disc (most up-to-date) and
        packs from ALL discs.  Places packs in the two-level
        ``data/<prefix>/<hash>`` layout that rustic expects.
        """
        cache = self.restore_dir / f"cache_{repo_id}"

        # Find the LAST extracted disc (has the most complete metadata)
        extracted_vols = sorted(
            d for d in self.extract_dir.iterdir() if d.is_dir()
        )
        latest = extracted_vols[-1]
        metadata_src = latest / "metadata" / repo_id

        # Set up cache from disc metadata (index, snapshots, keys, config)
        executor = RestoreExecutor(_NoOpRustic())
        executor.prepare_cache(cache, metadata_src)

        # Ingest packs from ALL extracted discs into two-level layout
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

    # =================================================================
    # ASSERTIONS — the discs + key are all we have
    # =================================================================

    def test_nothing_remains_except_isos_and_key(self):
        """Verify the setup actually destroyed everything."""
        entries = set(os.listdir(self.tmp))
        # Only key.txt, isos/, extracted/, restored/ should exist
        # (extracted/restored created empty for the restore phase)
        allowed = {"key.txt", "isos", "extracted", "restored"}
        assert entries <= allowed, (
            f"Unexpected survivors: {entries - allowed}"
        )
        assert self.key_file.is_file()
        assert len(self.all_isos) >= 2, (
            f"Expected at least 2 ISOs, got {len(self.all_isos)}"
        )

    def test_isos_are_self_describing(self):
        """Every ISO contains volume_info.json and catalog.db."""
        for iso in self.all_isos:
            vol_dir = self._extract_iso(iso)
            assert (vol_dir / "volume_info.json").is_file(), (
                f"{iso.name} missing volume_info.json"
            )
            vi = json.loads((vol_dir / "volume_info.json").read_text())
            assert "uuid" in vi
            assert "label" in vi

            assert (vol_dir / "catalog.db").is_file(), (
                f"{iso.name} missing catalog.db"
            )
            # Catalog should be a valid SQLite database
            cat_conn = sqlite3.connect(str(vol_dir / "catalog.db"))
            tables = {row[0] for row in cat_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            cat_conn.close()
            assert "packs" in tables
            assert "volumes" in tables
            assert "volume_packs" in tables

    def test_every_iso_has_holographic_metadata(self):
        """Every ISO carries full repository metadata for every repo."""
        for iso in self.all_isos:
            vol_dir = self._extract_iso(iso)
            meta = vol_dir / "metadata"
            assert meta.is_dir(), f"{iso.name} missing metadata/"

            for repo_id in ("family", "work"):
                repo_meta = meta / repo_id
                assert repo_meta.is_dir(), (
                    f"{iso.name} missing metadata/{repo_id}/"
                )
                # Must have index, snapshots, keys, config
                assert (repo_meta / "index").is_dir(), (
                    f"{iso.name} missing metadata/{repo_id}/index/"
                )
                assert (repo_meta / "snapshots").is_dir()
                assert (repo_meta / "keys").is_dir()
                assert (repo_meta / "config").is_file()

                # keys/ must contain at least one key file
                key_files = list((repo_meta / "keys").iterdir())
                assert len(key_files) >= 1, (
                    f"{iso.name}: metadata/{repo_id}/keys/ is empty"
                )

    def test_latest_catalog_knows_all_volumes(self):
        """The catalog.db on the last-burned disc lists every volume."""
        # Extract all ISOs first
        for iso in self.all_isos:
            self._extract_iso(iso)

        latest = sorted(self.extract_dir.iterdir())[-1]
        cat_path = latest / "catalog.db"
        conn = sqlite3.connect(str(cat_path))
        conn.row_factory = sqlite3.Row

        volumes = conn.execute("SELECT label FROM volumes ORDER BY label").fetchall()
        vol_labels = {r["label"] for r in volumes}
        conn.close()

        iso_labels = {iso.stem for iso in self.all_isos}
        assert iso_labels <= vol_labels, (
            f"Last catalog missing volumes: {iso_labels - vol_labels}"
        )

    def test_restore_family_from_discs_only(self):
        """Restore the family repo (latest snapshot) from disc data only."""
        # Extract all ISOs
        for iso in self.all_isos:
            self._extract_iso(iso)

        cache = self._build_restore_cache("family")
        target = self.restore_dir / "family_output"
        target.mkdir(parents=True)

        # Restore latest snapshot using rustic against the disc-only cache
        result = subprocess.run(
            ["rustic", "restore", "latest", str(target),
             "-r", str(cache),
             "--password-file", str(self.key_file),
             "--no-cache"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"rustic restore failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # Find the restored files (rustic restores full path tree)
        restored_files = self._find_restored_files(target)

        # Verify every file from the final manifest (initial + incremental)
        assert len(restored_files) >= len(self.family_manifest), (
            f"Expected {len(self.family_manifest)} files, found {len(restored_files)}"
        )
        for name, expected_hash in self.family_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: expected {expected_hash[:16]}..., "
                f"got {actual_hash[:16]}..."
            )

    def test_restore_work_from_discs_only(self):
        """Restore the work repo (latest snapshot) from disc data only."""
        for iso in self.all_isos:
            self._extract_iso(iso)

        cache = self._build_restore_cache("work")
        target = self.restore_dir / "work_output"
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
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        restored_files = self._find_restored_files(target)

        assert len(restored_files) >= len(self.work_manifest)
        for name, expected_hash in self.work_manifest.items():
            assert name in restored_files, f"Missing: {name}"
            actual_hash = _sha256_file(restored_files[name])
            assert actual_hash == expected_hash, (
                f"{name}: expected {expected_hash[:16]}..., "
                f"got {actual_hash[:16]}..."
            )

    def test_restored_data_includes_incremental_changes(self):
        """The restore must include files added/modified in the incremental."""
        for iso in self.all_isos:
            self._extract_iso(iso)

        # Restore family
        cache_fam = self._build_restore_cache("family")
        target_fam = self.restore_dir / "family_inc_check"
        target_fam.mkdir(parents=True)
        result = subprocess.run(
            ["rustic", "restore", "latest", str(target_fam),
             "-r", str(cache_fam),
             "--password-file", str(self.key_file),
             "--no-cache"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"rustic restore failed:\nstderr: {result.stderr}"
        )
        restored_fam = self._find_restored_files(target_fam)

        # Incremental files must be present
        incremental_names = [f"fam_inc_{i:04d}.bin" for i in range(NUM_INCREMENTAL_FILES)]
        for name in incremental_names:
            assert name in restored_fam, (
                f"Incremental file {name} missing from family restore"
            )

        # Restore work
        cache_wrk = self._build_restore_cache("work")
        target_wrk = self.restore_dir / "work_inc_check"
        target_wrk.mkdir(parents=True)
        result = subprocess.run(
            ["rustic", "restore", "latest", str(target_wrk),
             "-r", str(cache_wrk),
             "--password-file", str(self.key_file),
             "--no-cache"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"rustic restore failed:\nstderr: {result.stderr}"
        )
        restored_wrk = self._find_restored_files(target_wrk)

        # Modified work files must have the NEW hashes (not the originals)
        for name, expected_hash in self.work_manifest.items():
            assert name in restored_wrk, f"Missing: {name}"
            actual_hash = _sha256_file(restored_wrk[name])
            assert actual_hash == expected_hash, (
                f"Work file {name} has stale content after incremental "
                f"(expected {expected_hash[:16]}..., got {actual_hash[:16]}...)"
            )

    def test_on_disc_catalog_enables_pick_list(self):
        """The catalog.db on the latest disc can generate a valid pick list
        that maps packs to the correct volumes — without any external DB."""
        for iso in self.all_isos:
            self._extract_iso(iso)

        latest = sorted(self.extract_dir.iterdir())[-1]
        cat_path = latest / "catalog.db"

        conn = sqlite3.connect(str(cat_path))
        conn.row_factory = sqlite3.Row

        # Get all pack SHA-256 hashes known to the catalog
        rows = conn.execute("SELECT sha256 FROM packs WHERE is_pruned = 0").fetchall()
        all_shas = [r["sha256"] for r in rows]
        assert len(all_shas) > 0

        # Use the on-disc catalog to generate a pick list
        from lcsas.db.queries import get_pick_list

        pick = get_pick_list(conn, all_shas)
        found = {p.sha256 for packs in pick.values() for p in packs}

        # Every non-pruned pack should be locatable
        assert found == set(all_shas), (
            f"Pick list from on-disc catalog missing packs: "
            f"{set(all_shas) - found}"
        )

        # Every volume label in the pick list should correspond to an ISO
        iso_labels = {iso.stem for iso in self.all_isos}
        for vol_label in pick:
            assert vol_label in iso_labels, (
                f"Pick list references unknown volume: {vol_label}"
            )

        conn.close()

    def test_packs_span_multiple_discs(self):
        """Verify the test scenario actually uses multiple discs
        (otherwise the test isn't exercising multi-volume restore)."""
        assert len(self.all_isos) >= 2, (
            f"Expected multi-disc scenario, got {len(self.all_isos)} ISOs"
        )

        # Extract and count packs per disc
        packs_per_disc: dict[str, set[str]] = {}
        for iso in self.all_isos:
            vol_dir = self._extract_iso(iso)
            data_dir = vol_dir / "data"
            if data_dir.is_dir():
                packs_per_disc[iso.stem] = {
                    f.name for f in data_dir.rglob("*") if f.is_file()
                }

        # At least 2 discs should have packs
        discs_with_packs = {
            label for label, packs in packs_per_disc.items() if packs
        }
        assert len(discs_with_packs) >= 2, (
            "Packs should span at least 2 discs"
        )

        # Union of all packs across all discs
        all_packs = set()
        for packs in packs_per_disc.values():
            all_packs |= packs
        assert len(all_packs) > 0

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _find_restored_files(self, target: Path) -> dict[str, Path]:
        """Walk the restore target to find .bin files, keyed by name."""
        found: dict[str, Path] = {}
        for root, _dirs, files in os.walk(target):
            for f in files:
                if f.endswith(".bin"):
                    found[f] = Path(root) / f
        return found


class _NoOpRustic:
    """Minimal stub implementing the RusticRunner protocol — only
    used for RestoreExecutor.prepare_cache / ingest_volume which
    never call any rustic methods."""

    def init_repo(self, *a, **kw):
        pass

    def backup(self, *a, **kw):
        pass

    def snapshots(self, *a, **kw):
        return []

    def restore_dry_run(self, *a, **kw):
        pass

    def restore(self, *a, **kw):
        pass

    def prune_dry_run(self, *a, **kw):
        pass
