"""Meta-volume restore: prove the rescue disc is truly self-contained.

This test builds a complete LCSAS data scenario (repos + ISOs),
constructs a meta-volume, then deletes everything except:
  1. The ISOs (data discs)
  2. The meta-volume directory (rescue disc)
  3. The key file

It then runs the meta-volume's ``restore.sh`` using ONLY the bundled
tools — no system-installed rustic, xorriso, or Python — and verifies
that every file is restored byte-for-byte.

Requires: ``rustic`` and ``xorriso`` on PATH (for initial setup only).
"""

from __future__ import annotations

import hashlib
import json
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
from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs

# ── Skip conditions ──────────────────────────────────────────────

requires_rustic = pytest.mark.skipif(
    not shutil.which("rustic"), reason="rustic not installed"
)
requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"), reason="xorriso not installed"
)
pytestmark = [requires_rustic, requires_xorriso]

# ── Deterministic data ──────────────────────────────────────────

RNG_SEED = 20260215
NUM_FILES = 8
FILE_SIZE_RANGE = (512, 4096)


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
    args: list[str],
    repo: Path,
    pw: Path,
    tmpdir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run rustic with optional TMPDIR override.

    If *tmpdir* is provided it is passed as TMPDIR so that rustic writes
    its temporary pack files there instead of the (possibly tiny) /tmp.
    """
    env = None
    if tmpdir is not None:
        env = {**os.environ, "TMPDIR": str(tmpdir)}
    result = subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), *args],
        capture_output=True, text=True, check=True, env=env,
    )
    return result


# ── Stubs for burn pipeline ─────────────────────────────────────

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


# ── Rustic stub ──────────────────────────────────────────────────

class _NoOpRustic:
    def init_repo(self, *a, **kw): pass
    def backup(self, *a, **kw): pass
    def snapshots(self, *a, **kw): return []
    def restore_dry_run(self, *a, **kw): pass
    def restore(self, *a, **kw): pass
    def prune_dry_run(self, *a, **kw): pass


# ═════════════════════════════════════════════════════════════════
#  THE TEST
# ═════════════════════════════════════════════════════════════════

class TestMetaVolumeRestore:
    """Restore using ONLY the meta-volume's bundled tools + ISOs + key."""

    @pytest.fixture(autouse=True)
    def setup_scenario(self, tmp_path: Path):
        """Build data ISOs + meta-volume, then nuke everything else."""
        self.tmp = tmp_path
        self.rng = random.Random(RNG_SEED)

        # ── Paths ────────────────────────────────────────────────
        mirror = tmp_path / "mirror"
        staging = tmp_path / "staging"
        iso_out = tmp_path / "isos"
        db_path = tmp_path / "db" / "catalog.db"
        key_file = tmp_path / "key.txt"

        for d in [mirror, staging, iso_out, db_path.parent]:
            d.mkdir(parents=True, exist_ok=True)

        key_file.write_text("meta-volume-test-password\n")

        # ── Generate test data ───────────────────────────────────
        src_dir = tmp_path / "original"
        self.manifest = _generate_files(src_dir, self.rng, NUM_FILES, "mv")

        # ── Init rustic repo & backup ────────────────────────────
        repo = mirror / "photos"
        _rustic(["init"], repo, key_file, tmpdir=tmp_path)
        _rustic(["backup", "--json", str(src_dir)], repo, key_file, tmpdir=tmp_path)

        # ── Init LCSAS catalog ───────────────────────────────────
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "photos", "Photo Archive", str(repo))

        # ── Scan + burn ISOs ─────────────────────────────────────
        scanned = scan_mirror_packs(repo)
        delta = DeltaAnalyzer(conn, scanned, repo_id="photos")
        delta.register_new_packs()

        from lcsas.db.queries import get_unarchived_packs
        mt = MediaType.TEST_TINY
        repo_configs = {
            "photos": RepositoryConfig(
                name="photos",
                mirror_path=repo,
                password_file=key_file,
            ),
        }
        config = LCSASConfig(
            mirror_base_path=mirror,
            staging_path=staging,
            db_path=db_path,
            default_media_type=mt,
            default_ecc_redundancy_pct=0,
            label_prefix="META",
            metadata_reserve_bytes=50_000,
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
                orchestrator.execute(manifest, iso_output=iso,
                                     skip_burn=True, skip_ecc=True)
                isos.append(iso)
            except ValueError:
                break

        conn.close()

        # ── Build meta-volume ────────────────────────────────────
        meta_dir = tmp_path / "meta_volume"
        meta_builder = MetaVolumeBuilder(meta_dir)
        meta_builder.build()

        # ═══════════════════════════════════════════════════════════
        # NUKE everything except ISOs, meta-volume, and key file
        # ═══════════════════════════════════════════════════════════
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

        # Move back
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

        self.all_isos = sorted(self.iso_dir.glob("*.iso"))

    # ── Assertions ───────────────────────────────────────────────

    def test_environment_is_clean(self):
        """After nuke, only ISOs + meta-volume + key remain."""
        entries = set(os.listdir(self.tmp))
        allowed = {"isos", "meta_volume", "key.txt", "restored"}
        assert entries <= allowed, f"Unexpected: {entries - allowed}"

    def test_meta_volume_has_all_tools(self):
        """Meta-volume contains all required tools."""
        assert (self.meta_dir / "tools" / "bin" / "rustic").is_file()
        assert (self.meta_dir / "tools" / "bin" / "xorriso").is_file()
        assert (self.meta_dir / "tools" / "bin" / "python3").is_file()
        assert (self.meta_dir / "restore.sh").is_file()

    def test_meta_volume_has_source(self):
        """Meta-volume contains LCSAS source code."""
        assert (self.meta_dir / "lcsas" / "src" / "lcsas" / "__init__.py").is_file()

    def test_meta_volume_has_docs(self):
        """Meta-volume contains project documentation."""
        assert (self.meta_dir / "README_RESTORE.md").is_file()
        vi = json.loads((self.meta_dir / "volume_info.json").read_text())
        assert vi["type"] == "meta"

    def test_restore_sh_executes(self):
        """restore.sh restores data using ONLY bundled tools."""
        # Run restore.sh with a pristine environment — no system tools.
        # The script uses absolute paths to its bundled rustic/xorriso,
        # so we only need basic shell utilities on PATH.
        env = {
            "PATH": "/usr/bin:/bin",   # basic shell commands only
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp / "tmp"),
        }
        (self.tmp / "tmp").mkdir(exist_ok=True)

        result = subprocess.run(
            [
                "bash", str(self.meta_dir / "restore.sh"),
                "--key", str(self.key_file),
                "--isos", str(self.iso_dir),
                "--target", str(self.restore_target),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, (
            f"restore.sh failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "Restore complete" in result.stdout

    def test_restored_files_match_originals(self):
        """Every file restored by restore.sh matches the original hash."""
        # First, run the restore
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp / "tmp"),
        }
        (self.tmp / "tmp").mkdir(exist_ok=True)

        subprocess.run(
            [
                "bash", str(self.meta_dir / "restore.sh"),
                "--key", str(self.key_file),
                "--isos", str(self.iso_dir),
                "--target", str(self.restore_target),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

        # Find all .bin files under the restore target
        found: dict[str, Path] = {}
        for root, _dirs, files in os.walk(self.restore_target):
            for f in files:
                if f.endswith(".bin"):
                    found[f] = Path(root) / f

        assert len(found) >= len(self.manifest), (
            f"Expected {len(self.manifest)} files, found {len(found)}"
        )

        for name, expected_hash in self.manifest.items():
            assert name in found, f"Missing file: {name}"
            actual = _sha256_file(found[name])
            assert actual == expected_hash, (
                f"{name}: hash mismatch "
                f"(expected {expected_hash[:16]}…, got {actual[:16]}…)"
            )
