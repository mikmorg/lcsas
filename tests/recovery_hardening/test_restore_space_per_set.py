"""restore.sh sizes the chosen backup set, even without a sqlite3 command (#798).

Before a restore, restore.sh checks the free space against the size of
the chosen set.  With a ``sqlite3`` command it sums the set's packs in the
catalog; without one it asked tier 1 (``--list-pending-packs``), which
counted every pack in the archive.  On an archive with several sets the
estimate was then the whole archive, and a small set on a disk too small
for the archive was refused (exit 1).

Tier 1 now takes ``--repo-id ID`` (its id is the set's folder name under
``metadata/``), so the estimate is the set's own.  When tier 1 counts no
pack of that id (an older disc names its folders by set name), the size
is the whole archive's, and the script says so.  Any size is an
estimate: a short disk asks the reader, when there is one, whether to
carry on; with nobody there, or any answer but y, the run stops.

These tests run the real tier-1 program (``recovery/bin/x86_64``) on a
real catalog, with ``sqlite3`` hidden from PATH.  The sizes are set from
the real free space of the disk the test folder is on: ``alpha`` is
tiny, ``bravo`` twice the free space, so the archive never fits.
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
TIER1 = REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore"
HOST_TARGET = "x86_64-unknown-linux-musl"

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64")
    or not TIER1.is_file(),
    reason="needs Linux x86_64 and the committed tier-1 binary",
)

SIZING = "Need about"
ASK = "Carry on anyway? [y/N]"
STOPPED = "Not enough free space for this restore"
WHOLE = "counts every backup set on these discs"


def _catalog(path: Path, sizes: dict[str, int]) -> None:
    """A real catalog: one pack of the given size per set, each on a
    volume of its own (tier 1 counts packs through volume_packs)."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        for n, (set_id, size) in enumerate(sorted(sizes.items()), start=1):
            register_repo(conn, set_id, set_id, f"/srv/{set_id}")
            pack = register_pack(conn, f"{n:02x}" * 32, size, set_id)
            vol = create_volume(
                conn, label=f"LCSAS_TEST_2026_{n:04d}", uuid=f"uuid-{n}",
                media_type="BD25", capacity_bytes=26_843_545_600,
                status="VERIFIED",
            )
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        conn.commit()
    finally:
        conn.close()


def _fixture(tmp_path: Path, folders: tuple[str, ...] = ("alpha", "bravo"),
             ids: tuple[str, ...] | None = None) -> Path:
    """A recovery tree with the real tier 1, a set folder per name in
    ``folders`` and a catalog whose set ids are ``ids`` (default: the
    folder names).  alpha is 1 MB; bravo twice the free space here."""
    recovery = tmp_path / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    shutil.copy2(TIER1, bin_dir / "lcsas-restore")
    for name in folders:
        repo = recovery / "metadata" / name
        for sub in ("keys", "index", "data", "snapshots"):
            (repo / sub).mkdir(parents=True)
        (repo / "keys" / "stub_key").write_text("stub")
    free = shutil.disk_usage(tmp_path).free
    ids = ids or folders
    _catalog(recovery / "catalog.db",
             {ids[0]: 1024 * 1024, ids[1]: 2 * free})
    return recovery


def _path_without_sqlite3(tmp_path: Path) -> str:
    """A PATH with every command of this one but sqlite3."""
    shim = tmp_path / "path-no-sqlite3"
    shim.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        for entry in os.scandir(d):
            if entry.name == "sqlite3" or (shim / entry.name).exists():
                continue
            if entry.is_file() and os.access(entry.path, os.X_OK):
                (shim / entry.name).symlink_to(entry.path)
    assert shutil.which("sqlite3", path=str(shim)) is None
    return str(shim)


def _run(tmp_path: Path, recovery: Path, set_name: str, *,
         no_sqlite3: bool = True, answer: str | None = None,
         ) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LCSAS_")}
    env.update({
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_BACKUP_SET": set_name,
        "LCSAS_TOOLS_DIR": str(tmp_path / "tools"),
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    })
    if no_sqlite3:
        env["PATH"] = _path_without_sqlite3(tmp_path)
    if answer is not None:
        env["LCSAS_INTERACTIVE"] = "on"
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored")],
        input=(answer + "\n") if answer is not None else "",
        capture_output=True, text=True, env=env, timeout=60,
        start_new_session=True,
    )


@pytest.mark.parametrize("no_sqlite3", [True, False],
                         ids=["no-sqlite3", "sqlite3"])
def test_a_small_set_of_a_big_archive_is_not_refused(
        tmp_path: Path, no_sqlite3: bool) -> None:
    """#798: alpha (1 MB) fits, although the archive (alpha and bravo)
    does not: the run goes on to the password, with or without sqlite3."""
    recovery = _fixture(tmp_path)
    res = _run(tmp_path, recovery, "alpha", no_sqlite3=no_sqlite3)

    assert SIZING not in res.stderr, res.stderr
    assert STOPPED not in res.stderr, res.stderr
    assert "Password:" in res.stderr, (
        f"rc={res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")


def test_a_set_too_big_stops_with_nobody_to_ask(tmp_path: Path) -> None:
    """bravo alone does not fit: with no terminal the run stops before
    the password (exit 1), names the bypass, and does not ask."""
    recovery = _fixture(tmp_path)
    res = _run(tmp_path, recovery, "bravo")

    assert res.returncode == 1, res.stderr
    assert SIZING in res.stderr and "GB" in res.stderr, res.stderr
    assert "estimate from the catalog" in res.stderr, res.stderr
    assert WHOLE not in res.stderr, res.stderr
    assert ASK not in res.stderr, res.stderr
    assert STOPPED in res.stderr and "LCSAS_SKIP_SPACE_CHECK" in res.stderr
    assert "Password:" not in res.stderr, res.stderr


@pytest.mark.parametrize(("answer", "goes_on"), [("y", True), ("n", False),
                                                 ("", False)])
def test_a_short_disk_asks_when_someone_is_there(
        tmp_path: Path, answer: str, goes_on: bool) -> None:
    """The size is an estimate, so a reader is asked: y carries on to
    the password; anything else stops (exit 1), as restore.bat does."""
    recovery = _fixture(tmp_path)
    res = _run(tmp_path, recovery, "bravo", answer=answer)

    assert SIZING in res.stderr and ASK in res.stderr, res.stderr
    if goes_on:
        assert STOPPED not in res.stderr, res.stderr
        assert "Password:" in res.stderr, res.stderr
    else:
        assert res.returncode == 1, res.stderr
        assert STOPPED in res.stderr, res.stderr
        assert "Password:" not in res.stderr, res.stderr


def test_an_id_tier1_does_not_know_sizes_the_whole_archive(
        tmp_path: Path) -> None:
    """An older disc names its set folders by name, not by the id in the
    catalog: tier 1 counts no pack of that id, so the size is the whole
    archive's, and the script says that it counts every set."""
    recovery = _fixture(tmp_path, ids=("id-alpha", "id-bravo"))
    res = _run(tmp_path, recovery, "alpha")

    assert res.returncode == 1, res.stderr
    assert SIZING in res.stderr, res.stderr
    assert WHOLE in res.stderr, res.stderr
    assert STOPPED in res.stderr, res.stderr
