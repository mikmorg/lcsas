"""Every release's disc catalog, read by the newest readers (#961).

``tests/fixtures/catalogs/`` holds the disc catalog each release made
(``<release>.db.gz``, from a real scratch install) and synthetic
next-version ones made from it (``<release>-<change>.db.gz``): what a v11
may do and every older reader must live with.  See ``make_catalogs.py``
there; at each release, add that release's catalog.

Over each catalog this runs:

- the committed x86_64 tier-1 binary (``LCSAS_RESTORE_BIN`` overrides),
  on the backup set ``alpha`` with every pack missing: the version it
  reads (``[catalog] schema vN``) and the discs its first disc prompt
  names;
- the Python reader (tier 3, the ``python_reader.py`` a META disc
  carries), the same way;
- ``lcsas catalog rebuild`` on a disc carrying the catalog: the volumes
  it merges, or the refusal naming the disc.

No rustic, no xorriso: the repository is the disc's own metadata of the
set (config, keys, index, snapshots), committed beside the catalogs.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.contract.disc_catalog import named_discs

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux")
         and platform.machine() in ("x86_64", "AMD64")),
    reason="runs the committed x86_64 Linux tier-1 binary")

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "catalogs"
COMMITTED_TIER1 = ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore"

#: Rebuild's words for a disc it refuses because the catalog is newer.
NEWER = "written by a newer LCSAS (schema v11)"


@dataclass(frozen=True)
class Expect:
    version: int
    #: None: rebuild merges every volume; else the refusal names the disc
    #: and this value (table.column holds 'value').
    refused_for: str | None = None


#: What each catalog of a release is, by the part of its name after the
#: release.  A new kind of next-version change needs its row here.
KINDS: dict[str, Expect] = {
    "": Expect(10),
    "migrated": Expect(10),
    "next-row": Expect(11),
    "next-columns": Expect(11),
    "next-table-index": Expect(11),
    "next-media-type": Expect(11),
    "next-volume-status": Expect(11, "volumes.status holds 'SHELVED'"),
    "next-disc-status": Expect(11, "volume_copies.status holds 'LENT'"),
    "next-event-type": Expect(11, "volume_events.event_type holds 'RELABELLED'"),
}


def _releases() -> dict[str, dict[str, object]]:
    return {p.stem: json.loads(p.read_text())
            for p in sorted(FIXTURES.glob("*.json"))}


def _catalogs() -> list[tuple[str, str, str]]:
    """(file name, release, kind) of every committed catalog."""
    found = []
    for path in sorted(FIXTURES.glob("*.db.gz")):
        name = path.name[:-len(".db.gz")]
        release = next(r for r in sorted(_releases(), key=len, reverse=True)
                       if name == r or name.startswith(r + "-"))
        found.append((path.name, release, name[len(release) + 1:]))
    return found


CATALOGS = _catalogs()


def test_every_release_has_its_catalogs() -> None:
    """Each release's catalog and every next-version kind are there, and
    no catalog is of a kind this test does not know."""
    assert _releases(), "no release catalog committed"
    for release in _releases():
        kinds = {k for _, r, k in CATALOGS if r == release}
        assert kinds == set(KINDS), (release, sorted(kinds ^ set(KINDS)))


def _unpack(name: str, dst: Path) -> Path:
    with gzip.open(FIXTURES / name, "rb") as f:
        dst.write_bytes(f.read())
    return dst


def _tier1() -> Path:
    env_bin = os.environ.get("LCSAS_RESTORE_BIN")
    found = Path(env_bin) if env_bin else COMMITTED_TIER1
    assert found.is_file(), f"no tier-1 binary at {found}"
    return found


def _repo(work: Path, release: dict[str, object]) -> tuple[Path, Path]:
    """A copy of the set's disc metadata with no packs, and its password."""
    repo = work / "repo" / str(release["repo_id"])
    shutil.copytree(FIXTURES / "repo" / str(release["repo_id"]), repo)
    (repo / "data").mkdir()
    return repo, FIXTURES / "repo" / "password"


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}


def _run_reader(reader: str, catalog: Path, release: dict[str, object],
                work: Path) -> subprocess.CompletedProcess[str]:
    repo, pwfile = _repo(work, release)
    snapshot = str(release["snapshots"][0])  # type: ignore[index]
    if reader == "tier1":
        empty = work / "no-discs"
        empty.mkdir()
        cmd = [str(_tier1()), "--repo", str(repo), "--password-file",
               str(pwfile), "--mount-parent", str(empty),
               "--interactive", "on", "--auto-mount", "off", "--verbose"]
    else:
        from tests.contract.restore_contract import tier3_script

        cmd = [sys.executable, "-I", str(tier3_script(work.parent / "t3")),
               "--repo", str(repo), "--password-file", str(pwfile),
               "--interactive", "on"]
    cmd += ["--catalog", str(catalog), "--snapshot", snapshot,
            "--restore-folder", str(work / "target")]
    return subprocess.run(cmd, input="q\n", capture_output=True, text=True,
                          env=_env(), timeout=300)


def _first_disc(release: dict[str, object]) -> tuple[str, ...]:
    """The discs holding the first snapshot's tree: the first session's
    one volume, the oldest label."""
    return (min(release["labels"]),)  # type: ignore[type-var]


@pytest.mark.parametrize(("name", "release", "kind"), CATALOGS)
@pytest.mark.parametrize("reader", ["tier1", "tier3"])
def test_readers_name_the_disc(tmp_path: Path, reader: str, name: str,
                               release: str, kind: str) -> None:
    """Both readers read the catalog and name the disc holding the pack
    they want first; tier 1 reads the catalog's version as its highest
    row."""
    info = _releases()[release]
    catalog = _unpack(name, tmp_path / "catalog.db")
    res = _run_reader(reader, catalog, info, tmp_path / "w")
    output = res.stdout + res.stderr
    assert named_discs(output) == _first_disc(info), output
    if reader == "tier1":
        assert f"[catalog] schema v{KINDS[kind].version}\n" in res.stderr, (
            res.stderr)
        assert "newer than this recovery binary" not in res.stderr, res.stderr


def _labels(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return sorted(str(r[0]) for r in conn.execute("SELECT label FROM volumes"))
    finally:
        conn.close()


@pytest.mark.parametrize(("name", "release", "kind"), CATALOGS)
def test_catalog_rebuild_reads_the_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str,
    release: str, kind: str,
) -> None:
    """``catalog rebuild`` merges every volume the catalog knows, or,
    for a value this LCSAS does not know, refuses the disc by its label
    and says it is newer."""
    from lcsas.cli.main import main

    info = _releases()[release]
    disc = tmp_path / "disc"
    disc.mkdir()
    _unpack(name, disc / "catalog.db")
    (disc / "volume_info.json").write_text(json.dumps(info["disc"]))
    out = tmp_path / "rebuilt.db"
    with caplog.at_level(logging.INFO):
        rc = main(["catalog", "rebuild", "--output", str(out), str(disc)])
    want = KINDS[kind]
    label = info["disc"]["label"]  # type: ignore[index]
    if want.refused_for is None:
        assert rc == 0, caplog.text
        assert _labels(out) == info["labels"], caplog.text
        if want.version > 10:
            assert "written by a newer LCSAS" in caplog.text, caplog.text
    else:
        assert rc != 0, caplog.text
        refusal = [r.getMessage() for r in caplog.records
                   if NEWER in r.getMessage()]
        assert refusal, caplog.text
        assert f"disc {label}" in refusal[0], refusal
        assert want.refused_for in refusal[0], refusal
        assert not out.exists() or _labels(out) == [], caplog.text
