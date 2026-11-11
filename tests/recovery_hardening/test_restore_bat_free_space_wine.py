r"""restore.bat's free-space check, under wine (#660).

restore.bat had no free-space check: a restore too big for the drive ran
out of space part-way, after discs had been swapped.  It now asks
lcsas-restore.exe --list-pending-packs for the size, reads each drive's
free space from `dir`, and stops before the password and the first disc
when a drive is short.  The folder and the pack cache need about that
size each, so when both are on one drive that drive needs both.

The sizes here are set from the real free space of the drive that holds
the wine prefix: a catalog of 0.6 x that space fits once, but not twice.
Every drive letter used here is a folder on that same disk, so a
different letter means a different drive only to restore.bat, which is
what the test is about.  A sparse file stands for packs an earlier run
left in the pack cache: `dir` reports its full size, and it takes no
space.

Wine's cmd is not real cmd (see test_restore_bat_wine_smoke.py).  The
real-Windows run is the windows-e2e job in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.recovery_hardening._wine_helpers import stop_and_remove_prefix
from tests.recovery_hardening.test_restore_bat_wine_smoke import (
    _harden_prefix_drives,
    _init_prefix,
    _make_meta_tree,
    _map_drive,
    _run_bat,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
RESTORE_BAT = RECOVERY / "scripts" / "restore.bat"
RESTORE_EXE = RECOVERY / "bin" / "x86_64-windows" / "lcsas-restore.exe"

_WINE_OK = shutil.which("wine") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE_OK and RESTORE_BAT.is_file() and RESTORE_EXE.is_file()),
        reason=(
            f"restore.bat free-space wine test requires wine "
            f"(present={_WINE_OK}), {RESTORE_BAT} and {RESTORE_EXE}"
        ),
    ),
]

SIZING = "Need about"
SAME_DRIVE = "on the same drive"


@dataclass
class _Wine:
    prefix: Path
    env: dict[str, str]

    @property
    def drive_c(self) -> Path:
        return self.prefix / "drive_c"


@pytest.fixture(scope="module")
def wine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Wine]:
    """One wine prefix for the module (a fresh one is about 1.3 GB)."""
    prefix = tmp_path_factory.mktemp("freespace") / "wineprefix"
    try:
        env = _init_prefix(prefix)
    except subprocess.TimeoutExpired:
        pytest.skip("wine could not start a fresh prefix on this host (#390)")
    env = {k: v for k, v in env.items()
           if not k.startswith("LCSAS_") and k.upper() != "TARGET"}
    try:
        yield _Wine(prefix=prefix, env=env)
    finally:
        stop_and_remove_prefix(prefix)


@pytest.fixture
def meta(wine: _Wine, tmp_path: Path) -> Iterator[Path]:
    """E: is a META tree, and nothing is left on C: by an earlier test:
    no folder, no pack cache, no tools folder (its catalog copy would be
    used again).  The META tree sits beside the prefix, on its disk."""
    _harden_prefix_drives(wine.prefix)
    for tools in wine.drive_c.glob("users/*/AppData/Local/LCSAS-restore"):
        shutil.rmtree(tools, ignore_errors=True)
    root = wine.prefix.parent / f"meta-{tmp_path.name}"
    _make_meta_tree(root)
    _map_drive(wine.prefix, "e", root)
    yield root
    shutil.rmtree(root, ignore_errors=True)
    for name in ("restored", "cache660"):
        shutil.rmtree(wine.drive_c / name, ignore_errors=True)


def _catalog(meta: Path, size_bytes: int) -> None:
    """A real catalog whose one pack is ``size_bytes``, where restore.bat
    looks for one (recovery\\catalog.db)."""
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_multidisc  # sibling fixture builder

    test_multidisc._build_catalog(
        meta / "recovery" / "catalog.db",
        [("ab" * 32, size_bytes)], [],
        "LCSAS_TEST_2026_0001", "LCSAS_TEST_2026_0002")


def _free(wine: _Wine) -> int:
    return shutil.disk_usage(wine.drive_c).free


def _run(wine: _Wine, target: str, **extra: str) -> tuple[int, str]:
    res = _run_bat(
        wine.env, "e",
        stdin_data=target + "\n",
        extra_env={"LCSAS_PW": "not-used", "LCSAS_PACK_CACHE_DIR": "C:\\cache660",
                   **extra},
    )
    return res.returncode, res.stdout + res.stderr


def _sizing_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if SIZING in ln]


def test_same_drive_needs_the_folder_and_the_cache(wine: _Wine, meta: Path) -> None:
    """#660: the folder and the pack cache both on C: need twice the
    size.  0.6 x the free space fits once but not twice: the run stops
    before the password and the first disc, with exit code 1."""
    _catalog(meta, int(_free(wine) * 0.6))
    rc, out = _run(wine, "C:\\restored")

    assert rc == 1, f"rc={rc}\n--- output ---\n{out}"
    lines = _sizing_lines(out)
    assert len(lines) == 1 and "C:\\restored" in lines[0], out
    assert SAME_DRIVE in out and "C:\\cache660" in out, out
    assert "LCSAS_SKIP_SPACE_CHECK" in out, out
    assert "[tier 1]" not in out, out


def test_two_drives_each_need_their_own_share(wine: _Wine, meta: Path) -> None:
    """#660: the folder on E: and the pack cache on C: need 0.6 x the
    free space each, and each drive has it: no stop, tier 1 runs."""
    _catalog(meta, int(_free(wine) * 0.6))
    _rc, out = _run(wine, "E:\\restored")

    assert _sizing_lines(out) == [], out
    assert "[tier 1] running" in out, out


def test_two_drives_both_too_small(wine: _Wine, meta: Path) -> None:
    """A catalog of twice the free space: each drive is named, the
    folder's and the cache's, and nothing runs."""
    _catalog(meta, _free(wine) * 2)
    rc, out = _run(wine, "E:\\restored")

    assert rc == 1, f"rc={rc}\n--- output ---\n{out}"
    lines = _sizing_lines(out)
    assert len(lines) == 2, out
    assert "E:\\restored" in lines[0] and "C:\\cache660" in lines[1], out
    assert SAME_DRIVE not in out, out
    assert "GB" in lines[0], out
    assert "[tier 1]" not in out, out


def test_packs_already_in_the_cache_count(wine: _Wine, meta: Path) -> None:
    """A restore that carries on: the cache already holds most of the
    packs (a sparse file stands for them), so the cache needs only the
    rest and the same drive holds both."""
    free = _free(wine)
    _catalog(meta, int(free * 0.6))
    cache = wine.drive_c / "cache660" / "ab"
    cache.mkdir(parents=True)
    with open(cache / ("ab" * 32), "wb") as fh:
        fh.truncate(int(free * 0.55))
    _rc, out = _run(wine, "C:\\restored")

    assert _sizing_lines(out) == [], out
    assert "[tier 1] running" in out, out


def test_skip_space_check(wine: _Wine, meta: Path) -> None:
    """LCSAS_SKIP_SPACE_CHECK=1 skips the check: tier 1 runs."""
    _catalog(meta, _free(wine) * 2)
    _rc, out = _run(wine, "E:\\restored", LCSAS_SKIP_SPACE_CHECK="1")

    assert _sizing_lines(out) == [], out
    assert "[tier 1] running" in out, out


def test_no_catalog_no_check(wine: _Wine, meta: Path) -> None:
    """With no catalog there is no size, and a missing size never stops
    a restore."""
    assert not (meta / "recovery" / "catalog.db").exists()
    _rc, out = _run(wine, "E:\\restored")

    assert _sizing_lines(out) == [], out
    assert "[tier 1] running" in out, out


def test_harness_uses_one_disk(wine: _Wine, meta: Path) -> None:
    """The sizes above hold only while C: and E: are folders on the same
    disk (see the module docstring)."""
    assert os.stat(wine.drive_c).st_dev == os.stat(meta).st_dev
