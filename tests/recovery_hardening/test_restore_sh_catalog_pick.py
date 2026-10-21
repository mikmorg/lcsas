"""restore.sh: which catalog.db it picks, and from where (#517 gaps).

``catalog_consider`` keeps the freshest ``catalog.db`` by mtime.  The
catalog feeds tier 1's disc-swap hints (``--catalog``) and the free-space
preflight.  These tests pin, with every disc under a test-controlled
``LCSAS_MOUNT_DIRS``:

* discs are found where repo discovery and the pack search find them: a
  child of a mount dir (``/run/media/$USER/<label>``-style) and a disc
  mounted AT the mount dir itself (``mount /dev/sr0 /mnt``).  The scan
  used its own fixed list (/Volumes/* /media/$USER/* /media/* /mnt/*), so
  both were missed on the hosts that use them;
* with three discs mounted, the newest catalog wins and all three are
  pack sources; with only one of several mounted, only it is used;
* on an mtime tie the first catalog in scan order is kept.

Stub tier-1 binary: prints its argv (``ARG: ...``) and exits 0.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _recovery(tmp_path: Path) -> Path:
    recovery = tmp_path / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "lcsas-restore"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
        exit 0
    """))
    stub.chmod(0o755)
    repo = recovery / "metadata" / "alpha"
    for sub in ("keys", "index", "snapshots"):
        (repo / sub).mkdir(parents=True)
    (repo / "keys" / "stub_key").write_text("stub")
    return recovery


def _disc(root: Path, mtime: int) -> Path:
    """A data disc at *root*: data/ plus a catalog.db stamped *mtime*."""
    (root / "data").mkdir(parents=True)
    catalog = root / "catalog.db"
    catalog.write_bytes(b"catalog of " + root.name.encode())
    os.utime(catalog, (mtime, mtime))
    return catalog


def _run(
    recovery: Path, target: Path, mount_dirs: list[Path],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": ":".join(str(d) for d in mount_dirs),
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
    }
    for var in ("LCSAS_REPO", "LCSAS_PWFILE", "LCSAS_PACK_CACHE_DIR"):
        env.pop(var, None)
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub\n", capture_output=True, text=True, env=env, timeout=30,
    )
    args = [ln.removeprefix("ARG: ") for ln in res.stdout.splitlines() if ln.startswith("ARG: ")]
    return res, args


def _values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def test_catalog_on_a_disc_under_a_mount_dir_child_is_used(tmp_path: Path) -> None:
    """/run/media/$USER/<label>-style: the mount dir holds the disc dirs."""
    recovery = _recovery(tmp_path)
    run_media = tmp_path / "run-media-user"
    catalog = _disc(run_media / "LCSAS_BD25_2026_0001", 1_700_000_000)

    res, args = _run(recovery, tmp_path / "restored", [run_media])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(catalog)], (args, res.stderr)


def test_catalog_on_a_disc_mounted_at_the_mount_dir_itself_is_used(tmp_path: Path) -> None:
    """`sudo mount /dev/sr0 /mnt`: catalog.db sits directly in the mount dir."""
    recovery = _recovery(tmp_path)
    mnt = tmp_path / "mnt"
    catalog = _disc(mnt, 1_700_000_000)

    res, args = _run(recovery, tmp_path / "restored", [mnt])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(catalog)], (args, res.stderr)
    assert _values(args, "--pack-search") == [str(mnt)], args


def test_three_mounted_discs_newest_catalog_wins_and_all_are_searched(tmp_path: Path) -> None:
    recovery = _recovery(tmp_path)
    media = tmp_path / "media"
    _disc(media / "LCSAS_0001", 1_700_000_000)
    newest = _disc(media / "LCSAS_0002", 1_700_000_300)
    _disc(media / "LCSAS_0003", 1_700_000_100)

    res, args = _run(recovery, tmp_path / "restored", [media])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(newest)], args
    assert _values(args, "--pack-search") == [
        str(media / "LCSAS_0001"), str(media / "LCSAS_0002"), str(media / "LCSAS_0003"),
    ], args


def test_partial_mount_uses_only_the_disc_that_is_mounted(tmp_path: Path) -> None:
    """One disc of a set mounted (the others not yet inserted)."""
    recovery = _recovery(tmp_path)
    media = tmp_path / "media"
    only = _disc(media / "LCSAS_0002", 1_700_000_000)

    res, args = _run(recovery, tmp_path / "restored", [media])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(only)], args
    assert _values(args, "--pack-search") == [str(media / "LCSAS_0002")], args


def test_mtime_tie_keeps_the_first_catalog_in_scan_order(tmp_path: Path) -> None:
    """`catalog_consider` replaces the pick only on a strictly newer mtime,
    so equal mtimes keep the first one scanned (mount dirs in
    LCSAS_MOUNT_DIRS order, discs in glob order).  mtime is the only
    freshness signal the script has; a disc with a skewed clock can still
    win or lose wrongly (docs/workflows/restore-bare-metal.md)."""
    recovery = _recovery(tmp_path)
    first_dir, second_dir = tmp_path / "a-media", tmp_path / "b-media"
    first = _disc(first_dir / "LCSAS_0009", 1_700_000_000)
    _disc(second_dir / "LCSAS_0001", 1_700_000_000)

    res, args = _run(recovery, tmp_path / "restored", [first_dir, second_dir])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(first)], args


def test_local_recovery_catalog_is_kept_unless_a_disc_is_newer(tmp_path: Path) -> None:
    """The recovery tree's own catalog.db is considered first (last resort)."""
    recovery = _recovery(tmp_path)
    local = recovery / "catalog.db"
    local.write_bytes(b"local")
    os.utime(local, (1_700_000_500, 1_700_000_500))
    media = tmp_path / "media"
    _disc(media / "LCSAS_0001", 1_700_000_000)

    res, args = _run(recovery, tmp_path / "restored", [media])

    assert res.returncode == 0, res.stderr
    assert _values(args, "--catalog") == [str(local)], args
