"""Interactive single-drive restore via PTY + loop mounts.

Builds a LCSAS scenario (repo → backup → burn → meta volume), then
drives the production ``restore.sh`` interactively through a
pseudoterminal, with the interface it has since #689 (#756): the
flags ``--key``/``--target``/``--repo``, the tools folder as its work
folder, discs found where they are mounted (``LCSAS_MOUNT_DIRS``),
step 0's newest-disc question and tier 1's disc questions.  A disc
swap is an unmount and a read-only loop mount of the next ISO at the
same place, so no cdemu or physical hardware is needed.  The command
line, the prompts and the answers come from
``tests/e2e/restore_sh_driver.py``, which a unit test checks against
``restore.sh`` and tier 1's source on every run.

Requires: root (for losetup + mount), ``rustic`` and ``xorriso`` on PATH.
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
from lcsas.db.schema import create_all
from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs
from tests.e2e.fixture_lib import add_repo
from tests.e2e.restore_sh_driver import (
    ENTER,
    EXIT_DISC_PROMPT_QUIT,
    QUIT,
    TIER1_DONE,
    Prompt,
    build_restore_cmd,
    build_restore_env,
    drive_restore,
)

# ── Skip conditions ──────────────────────────────────────────────

pytestmark = [
    pytest.mark.integration,
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
FILE_SIZE_RANGE = (12_000, 30_000)  # ~840 KB total → forces multi-disc on TEST_TINY

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
        # Force small packs so the data spans multiple discs on TEST_TINY (1 MB).
        _rustic(
            ["config",
             "--set-datapack-size", "256KiB",
             "--set-datapack-size-limit", "512KiB",
             "--set-treepack-size", "128KiB",
             "--set-treepack-size-limit", "256KiB"],
            repo, key_file, tmpdir=tmp_path,
        )
        _rustic(
            ["backup", "--json", str(src_dir)], repo, key_file, tmpdir=tmp_path
        )

        # ── LCSAS catalog ────────────────────────────────────────
        conn = get_connection(db_path)
        create_all(conn)
        conn.commit()
        # As an owner does (#617): the set's id is a UUID, its name
        # "photos", which the restore names it by (--repo photos).
        repo_id = add_repo(db_path, "photos", repo)

        scanned = scan_mirror_packs(repo).packs
        delta = DeltaAnalyzer(conn, scanned, repo_id=repo_id)
        delta.register_new_packs()

        from tests.e2e.fixture_lib import burn_to_isos

        mt = MediaType.TEST_TINY
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
            metadata_reserve_bytes=150_000,
            repositories=repo_configs,
        )

        orchestrator = BurnOrchestrator(
            config, conn, _TestXorrisoRunner(), _NoOpDVDisaster()
        )

        isos = burn_to_isos(orchestrator, iso_out, media_type=mt)

        conn.close()

        assert len(isos) >= 2, (
            f"Need multi-disc scenario to test swaps, got {len(isos)} ISOs"
        )

        # ── Build meta-volume ────────────────────────────────────
        meta_dir = tmp_path / "meta_volume"
        meta_builder = MetaVolumeBuilder(
            meta_dir, allow_no_dvdisaster_source=True
        )
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
        (tmp_path / "tmp").mkdir()
        # Discs are mounted in here; the tools folder is restore.sh's
        # work folder (LCSAS_TOOLS_DIR).
        self.mount_dir = tmp_path / "mnt"
        self.mount_dir.mkdir()
        self.tools_dir = tmp_path / "lcsas-restore-tools"

        self.all_isos = sorted(self.iso_dir.glob("*.iso"))

    # ── The drive: one mount point, discs swapped by mount ──────
    #
    # restore.sh has no drive flag (#689, #756): it finds the discs
    # mounted in LCSAS_MOUNT_DIRS.  The one "drive" is the folder
    # <tmp>/mnt/drive; putting a disc in is a read-only loop mount of its
    # ISO there, taking it out is the unmount.

    @property
    def _drive(self) -> Path:
        return self.mount_dir / "drive"

    def _eject(self) -> None:
        if os.path.ismount(self._drive):
            subprocess.run(["umount", str(self._drive)],
                           capture_output=True, check=False)

    def _insert(self, iso: Path) -> None:
        self._eject()
        self._drive.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["mount", "-o", "ro,loop", str(iso), str(self._drive)],
            check=True, capture_output=True,
        )

    def _data_isos(self) -> list[Path]:
        return sorted(iso for iso in self.all_isos if "META" not in iso.stem)

    # ── PTY driver ───────────────────────────────────────────────

    def _drive_restore(
        self,
        bootstrap_oldest: bool = False,
        on_prompt: None | callable = None,
    ) -> tuple[int, str, list[Prompt]]:
        """Run the META disc's restore.sh under a PTY and answer it.

        Returns ``(exit_code, output, prompts)``.

        Step 0 ("put in your NEWEST data disc") is answered with the
        newest data disc, or the oldest one when *bootstrap_oldest*.  At
        each tier 1 disc question the first disc it names is put in, then
        ENTER; when it names none (its catalog does not know the pack
        yet), the newest data disc is put in.

        *on_prompt*, if given, is called as ``on_prompt(prompt, n)`` for
        every disc question of tier 1 (n counts all questions from 1)
        before that.  It may return:

        * ``None``: the default above.
        * ``"quit"``: type ``q`` (tier 1 stops; restore.sh exits 75).
        * ``"eject"``: take the disc out, then ENTER (an empty drive).
        * a ``Path``: put that ISO in instead, then ENTER.
        """
        data_isos = self._data_isos()
        assert data_isos, "no data ISOs found — check burn pipeline output"
        by_label = {iso.stem: iso for iso in data_isos}
        newest = data_isos[-1]
        bootstrap = data_isos[0] if bootstrap_oldest else newest
        prompts: list[Prompt] = []

        def answer(prompt: Prompt, n: int) -> bytes | None:
            prompts.append(prompt)
            if prompt.kind == "newest":
                self._insert(bootstrap)
                return ENTER
            if prompt.kind == "ram":
                return b"y\n"  # a tmpfs pytest temp folder is fine here
            if prompt.kind != "disc":
                return None  # N: no other backup set
            action = on_prompt(prompt, n) if on_prompt else None
            if action == "quit":
                return QUIT
            if action == "eject":
                self._eject()
                return ENTER
            if isinstance(action, Path):
                self._insert(action)
                return ENTER
            want = next((by_label[lab] for lab in prompt.labels
                         if lab in by_label), newest)
            self._insert(want)
            return ENTER

        self._eject()  # only the META disc is "in" at the start
        cmd = build_restore_cmd(
            self.meta_dir, key=self.key_file,
            target=self.restore_target, repo="photos",
        )
        env = build_restore_env(
            home=self.tmp, tools_dir=self.tools_dir,
            mount_dir=self.mount_dir, meta_disc=self.meta_dir,
            tmpdir=self.tmp / "tmp",
        )
        try:
            rc, out = drive_restore(cmd, env, answer,
                                    timeout=RESTORE_TIMEOUT)
        finally:
            self._eject()
        return rc, out, prompts

    # ── State reset ─────────────────────────────────────────────

    def _reset_restore_state(self) -> None:
        """Clear the restore target and tools folder between runs."""
        if self.restore_target.exists():
            shutil.rmtree(self.restore_target)
        self.restore_target.mkdir()
        if self.tools_dir.exists():
            shutil.rmtree(self.tools_dir)

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
        rc, output, prompts = self._drive_restore()

        assert rc == 0, f"restore.sh failed (rc={rc}):\n{output}"
        assert TIER1_DONE.decode() in output, (
            f"Missing tier 1's 'restore complete':\n{output[-2000:]}"
        )
        # Step 0 asked for the newest data disc first.
        assert prompts and prompts[0].kind == "newest"
        # The whole point: tier 1 asked for other discs in the one drive.
        named = [p for p in prompts if p.kind == "disc" and p.labels]
        assert len(named) >= 1, (
            f"Expected tier 1 to ask for another disc by label:\n{output[-2000:]}"
        )
        assert all(lab.startswith("ITEST_") for p in named for lab in p.labels)

        self._assert_files_restored()

    def test_stale_catalog_triggers_upgrade(self):
        """Start from the OLDEST data disc: its catalog is older than the
        others.  Tier 1 reads the newer catalog off a later disc as it is
        put in (the freshest catalog.db wins), and the restore completes."""
        self._reset_restore_state()

        rc, output, prompts = self._drive_restore(bootstrap_oldest=True)

        assert rc == 0, f"restore.sh failed (rc={rc}):\n{output}"
        assert TIER1_DONE.decode() in output, (
            f"Missing tier 1's 'restore complete':\n{output[-2000:]}"
        )
        assert any(p.kind == "disc" for p in prompts), (
            f"Expected tier 1 to ask for a later disc:\n{output[-2000:]}"
        )
        self._assert_files_restored()

    def test_wrong_disc_reprompts(self):
        """The wrong disc in the drive: tier 1 asks for the same disc again."""
        self._reset_restore_state()
        data_isos = self._data_isos()
        asked: list[list[str]] = []

        def on_prompt(prompt: Prompt, n: int):
            asked.append(prompt.labels)
            if len(asked) == 1 and prompt.labels:
                return next(iso for iso in data_isos
                            if iso.stem not in prompt.labels)
            return None

        rc, output, _ = self._drive_restore(on_prompt=on_prompt)

        assert rc == 0, f"restore.sh failed (rc={rc}):\n{output}"
        assert len(asked) >= 2 and asked[1] == asked[0], (
            f"Expected the same disc asked for again:\n{output[-2000:]}"
        )
        assert TIER1_DONE.decode() in output
        self._assert_files_restored()

    def test_skip_disc_reports_incomplete(self):
        """``q`` at tier 1's disc question stops the restore: exit 75,
        nothing claims it completed, and how to carry on is printed.
        (Before #689 this typed ``skip``; nothing accepts that now.)"""
        self._reset_restore_state()

        def on_prompt(prompt: Prompt, n: int):
            return "quit"

        rc, output, prompts = self._drive_restore(on_prompt=on_prompt)

        assert any(p.kind == "disc" for p in prompts), (
            f"tier 1 never asked for a disc:\n{output[-2000:]}"
        )
        assert rc == EXIT_DISC_PROMPT_QUIT, (
            f"Expected exit {EXIT_DISC_PROMPT_QUIT} after q, got rc={rc}:\n"
            f"{output[-2000:]}"
        )
        assert TIER1_DONE.decode() not in output
        assert "stopped at the disc prompt at your request" in output
        assert "The restore stopped before it finished" in output

    def test_unreadable_drive_reprompts(self):
        """An empty drive at a disc question: tier 1 asks again, and the
        restore completes once the disc is in."""
        self._reset_restore_state()
        asked: list[list[str]] = []

        def on_prompt(prompt: Prompt, n: int):
            asked.append(prompt.labels)
            return "eject" if len(asked) == 1 else None

        rc, output, _ = self._drive_restore(on_prompt=on_prompt)

        assert rc == 0, f"restore.sh failed (rc={rc}):\n{output}"
        assert len(asked) >= 2 and asked[1] == asked[0], (
            f"Expected the same disc asked for again:\n{output[-2000:]}"
        )
        assert TIER1_DONE.decode() in output
        self._assert_files_restored()
