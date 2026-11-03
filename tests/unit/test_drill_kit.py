"""The release-drill kit (tests/e2e/drill_kit): the parts that need no tools.

Pins that the corpus is deterministic for a seed, that the drill config
splits only ``photos`` (2-of-3, in its own table), that the kit refuses a
workdir inside the public checkout, and that verify_restore.py matches files
by their ``<repo>/<file>`` tail and catches a missing or changed file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.config.settings import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT = REPO_ROOT / "tests" / "e2e" / "drill_kit"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"drill_{name}", KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kit = _load("build_kit")

_SMALL = (("photos", 1, 2, 1024), ("documents", 1, 1, 2048))


def test_corpus_is_deterministic_for_a_seed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kit, "CORPUS", _SMALL)
    a = kit.build_corpus(tmp_path / "a", "seed-1")
    b = kit.build_corpus(tmp_path / "b", "seed-1")
    c = kit.build_corpus(tmp_path / "c", "seed-2")
    assert a == b
    assert a != c
    assert sorted(a) == [
        "documents/document-001.dat", "documents/letter-01.txt",
        "photos/album-notes-01.txt", "photos/scan-001.dat", "photos/scan-002.dat",
    ]
    assert b"TEST DATA" in (tmp_path / "a" / "photos" / "album-notes-01.txt").read_bytes()


def test_full_corpus_fills_two_cd700_discs() -> None:
    from lcsas.config.media import MediaType

    per_disc = MediaType.CD700.usable_bytes - 100 * 1_048_576  # default reserve
    total = sum(n * size for _, _, n, size in kit.CORPUS)
    assert per_disc < total < 2 * per_disc


def test_drill_config_splits_only_photos(tmp_path: Path) -> None:
    config = load_config(kit.write_config(tmp_path))
    assert config.repo_split("photos") == (True, 2, 3)
    assert config.repo_split("documents")[0] is False
    assert config.key_split is False
    assert config.label_prefix == "LCSAS"
    assert config.default_media_type.name == "CD700"


def test_refuses_a_workdir_inside_the_checkout(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(KIT / "build_kit.py"),
         "--workdir", str(REPO_ROOT / "build" / "drill-kit-should-not-exist")],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "outside the checkout" in proc.stderr
    assert not (REPO_ROOT / "build" / "drill-kit-should-not-exist").exists()


@pytest.fixture
def restored(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setattr(kit, "CORPUS", _SMALL)
    manifest = kit.build_corpus(tmp_path / "corpus", "s")
    kit.write_manifest(tmp_path / "manifest.sha256", manifest)
    # A restore that nests the files deeper than the manifest does.
    target = tmp_path / "restored" / "snapshot"
    target.parent.mkdir()
    import shutil
    shutil.copytree(tmp_path / "corpus", target)
    return tmp_path / "manifest.sha256", tmp_path / "restored"


def _verify(manifest: Path, restored: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(KIT / "verify_restore.py"), str(manifest),
         str(restored), *extra],
        capture_output=True, text=True,
    )


def test_verify_restore_passes_a_nested_restore(restored) -> None:
    proc = _verify(*restored)
    assert proc.returncode == 0, proc.stdout
    assert "5/5 files restored intact" in proc.stdout


def test_verify_restore_catches_missing_and_changed(restored) -> None:
    manifest, root = restored
    (root / "snapshot" / "photos" / "scan-002.dat").unlink()
    (root / "snapshot" / "documents" / "letter-01.txt").write_text("changed")
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    assert "MISSING  photos/scan-002.dat" in proc.stdout
    assert "DIFFERS  documents/letter-01.txt" in proc.stdout
    one = _verify(manifest, root, "--repo", "photos")
    assert "MISSING  photos/scan-002.dat" in one.stdout
    assert "documents/" not in one.stdout
