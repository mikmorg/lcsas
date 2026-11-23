"""#948: the owner's restore commands against the real rustic.

``restore plan``, ``restore exec``, ``restore from-disc`` and ``volume
impact --snapshots`` learned a snapshot's packs from ``rustic restore
--dry-run --json``, which rustic 0.11.2 rejects; every test replaced that
call with a fake.  These tests cross the real seam: real rustic
repositories, real ISOs mastered by xorriso during ``lcsas stage``, the
discs read back from ``xorriso -osirrox`` extracts (read-only files, as on
a mounted disc), and every command through ``lcsas.cli.main.main``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from lcsas.cli.main import main
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

#: The extracted discs are read-only only for an ordinary user: root
#: writes anywhere, so these tests would prove nothing as root.
_ordinary_user = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores read-only file modes",
)

_BIG = 700_000  # two of these do not fit on one TEST_TINY disc


def _packs(mirror: Path) -> set[str]:
    return {p.name for p in (mirror / "data").rglob("*") if p.is_file()}


def _extract_iso(iso: Path, dest: Path) -> Path:
    """Extract *iso* the way an owner without root reads a disc image."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(iso),
         "-extract", "/", str(dest)],
        capture_output=True, text=True, check=True,
    )
    return dest


def _iso_of(inst: ScratchInstall, label: str) -> Path:
    tree = inst.disc_trees()[label]
    iso = tree.parent / f"{label}.iso"
    assert iso.is_file(), iso
    return iso


def _disc_packs(disc: Path) -> set[str]:
    return {p.name for p in (disc / "data").rglob("*") if p.is_file()}


def _digests(folder: Path) -> dict[str, str]:
    """File name -> sha256 of every ``.bin`` file under *folder*."""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.rglob("*.bin") if p.is_file()
    }


class _World:
    """A two-disc archive of set ``alpha`` (plus a small set ``beta``).

    Three backups of ``alpha``: a small file, then two big ones.  Each
    big data pack fills most of a TEST_TINY disc, so they land on
    different discs; ``new_packs[k]`` are the packs backup *k* wrote.
    """

    def __init__(self, root: Path) -> None:
        inst = ScratchInstall(root / "install", repos=("alpha", "beta"))
        self.snaps: list[str] = []
        self.new_packs: list[set[str]] = []
        for name, size in (("first.bin", 5000), ("second.bin", _BIG),
                           ("third.bin", _BIG)):
            before = _packs(inst.mirror("alpha"))
            old = inst.mirror_snapshots("alpha")
            inst.backup("alpha", name, size=size)
            self.new_packs.append(_packs(inst.mirror("alpha")) - before)
            (snap,) = inst.mirror_snapshots("alpha") - old
            self.snaps.append(snap)
        inst.backup("beta", "small.bin", size=5000)
        inst.add_repos()
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        for session in inst.sessions():
            inst.mark_burned(session)
        self.inst = inst
        self.labels = inst.labels()
        assert len(self.labels) == 2, self.labels
        # Every disc as the owner reads it: an xorriso extract.
        self.discs = root / "discs"
        for label in self.labels:
            _extract_iso(_iso_of(inst, label), self.discs / label)


def _make_writable(root: Path) -> None:
    """Extracted discs are read-only; let pytest remove them."""
    for p in [root, *root.rglob("*")]:
        if p.is_dir() and not p.is_symlink():
            os.chmod(p, p.stat().st_mode | 0o700)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_World]:
    root = tmp_path_factory.mktemp("owner-restore")
    yield _World(root)
    _make_writable(root)


@pytest.fixture(autouse=True)
def _writable_after(tmp_path: Path) -> Iterator[None]:
    yield
    _make_writable(tmp_path)


@_ordinary_user
def test_extracted_disc_is_read_only(world: _World) -> None:
    """The extract keeps the disc's read-only modes, as a mount does."""
    disc = world.discs / world.labels[0]
    assert not os.access(disc / "catalog.db", os.W_OK)


def test_restore_plan_prints_a_pick_list(
    world: _World, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        rc = world.inst.lcsas("restore", "plan", "--repo", "alpha", "latest")
    assert rc == 0, caplog.text
    # The latest snapshot of alpha needs packs from both discs.
    for label in world.labels:
        assert label in caplog.text, caplog.text
    assert "Total:" in caplog.text


def test_restore_exec_volume_dir_restores_byte_identical(
    world: _World, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = world.inst
    out = tmp_path / "restored"
    with caplog.at_level(logging.INFO):
        rc = inst.lcsas(
            "restore", "exec", "--repo", "alpha",
            "--password-file", str(inst.key("alpha")),
            "--volume-dir", str(world.discs),
            world.snaps[-1][:8], str(out),
        )
    assert rc == 0, caplog.text
    want = _digests(inst.data("alpha"))
    assert set(want) == {"first.bin", "second.bin", "third.bin"}
    assert _digests(out) == want


def test_volume_impact_names_the_affected_snapshots(
    world: _World, caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing the disc without the first backup's packs loses the
    snapshots that need what is on it, and not the first snapshot."""
    inst = world.inst
    (label,) = [
        lb for lb in world.labels
        if not (_disc_packs(world.discs / lb) & world.new_packs[0])
    ]
    on_disc = _disc_packs(world.discs / label)
    # Ground truth from the mirror: snapshot k needs the packs backup k
    # wrote and the data packs of the earlier big files (trees are new
    # in every backup; nothing of backup 1 is on this disc).
    big = {
        p.name for p in (inst.mirror("alpha") / "data").rglob("*")
        if p.is_file() and p.stat().st_size > _BIG // 2
    }
    affected = []
    for k, snap in enumerate(world.snaps):
        needs = set(world.new_packs[k])
        for earlier in world.new_packs[:k]:
            needs |= earlier & big
        if needs & on_disc:
            affected.append(snap)
    assert affected and world.snaps[0] not in affected

    with caplog.at_level(logging.INFO):
        rc = inst.lcsas("volume", "impact", "--snapshots", label)
    assert rc == 0, caplog.text
    assert (f"{len(affected)} snapshot(s) would become unrestorable"
            in caplog.text), caplog.text
    for snap in world.snaps:
        assert (snap in caplog.text) == (snap in affected), (snap, caplog.text)
    assert "cannot tell" not in caplog.text


def _from_disc(*argv: str) -> int:
    # No --config: the command works from the disc alone.
    return main(["restore", "from-disc", *argv])


@_ordinary_user
def test_from_disc_two_discs(
    world: _World, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Disc 1 in the drive, disc 2 only under --volume-dir: the reader
    finds the packs of both, as an ordinary user, from read-only files."""
    inst = world.inst
    first, second = world.labels
    drive = tmp_path / "drive"
    _extract_iso(_iso_of(inst, first), drive)
    others = tmp_path / "others"
    _extract_iso(_iso_of(inst, second), others / second)
    alpha = _packs(inst.mirror("alpha"))
    on_first, on_second = _disc_packs(drive), _disc_packs(others / second)
    assert alpha & on_first and alpha & on_second
    assert alpha <= on_first | on_second

    out = tmp_path / "restored"
    with caplog.at_level(logging.INFO):
        rc = _from_disc(
            str(drive), str(out), "--password-file", str(inst.key("alpha")),
            "--repo", "alpha", "--volume-dir", str(others),
        )
    assert rc == 0, caplog.text
    assert _digests(out) == _digests(inst.data("alpha"))


@_ordinary_user
def test_from_disc_single_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = ScratchInstall(tmp_path / "install", repos=("solo",))
    inst.backup("solo", "one.bin", size=20_000)
    inst.backup("solo", "two.bin", size=20_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (label,) = inst.labels()
    disc = _extract_iso(_iso_of(inst, label), tmp_path / "disc")
    assert not os.access(disc / "catalog.db", os.W_OK)

    out = tmp_path / "restored"
    with caplog.at_level(logging.INFO):
        rc = _from_disc(str(disc), str(out),
                        "--password-file", str(inst.key("solo")))
    assert rc == 0, caplog.text
    want = _digests(inst.data("solo"))
    assert set(want) == {"one.bin", "two.bin"}
    assert _digests(out) == want

