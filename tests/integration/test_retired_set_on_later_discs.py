"""#959: a retired backup set is named on every later disc and on the
estate card, with the disc to start from.

A retired repository's metadata is not on discs staged after the
retirement (#442: only live repositories' metadata is copied, and the
mirror may be gone).  The newest disc and the estate card said nothing
about it, so an heir starting from the newest disc never learned the set
existed.  Owner decision (2026-11-23): say it, do not carry it.

Real rustic, real xorriso, every owner command through ``main()``.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def test_retired_set_is_named_with_its_last_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = ScratchInstall(tmp_path / "x")
    for name in inst.repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    (last,) = inst.labels()

    # alpha's mirror is gone for good: retire it, drop it from the config.
    shutil.rmtree(inst.mirror("alpha"))
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("repo", "retire", "alpha") == 0
    sentence = re.compile(
        rf"alpha: retired after \d{{4}}-\d\d-\d\d\.  ?Its backups are on the "
        rf"discs up to {last}\.")
    assert sentence.search(" ".join(caplog.text.split())), caplog.text
    text = inst.config.read_text()
    inst.config.write_text(re.sub(r"\[repos\.alpha\]\n(?:[^\[\n].*\n)*", "", text))

    inst.backup("beta", "f2.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    newest = inst.labels()[-1]
    assert newest != last
    tree = inst.disc_trees()[newest]
    for name in ("VOLUME_INFO.txt", "START_HERE.txt"):
        flat = " ".join((tree / name).read_text().split())
        assert sentence.search(flat), (name, flat)
        assert 'use that disc as the "newest data disc"' in flat, name
    info = json.loads((tree / "volume_info.json").read_text())
    assert info["retired_backup_sets"] == [
        {"name": "alpha", "last_label": last}], info

    caplog.clear()
    out = tmp_path / "estate.txt"
    assert inst.lcsas("estate", "card", "--output", str(out)) == 0
    flat = " ".join(out.read_text().split())
    assert sentence.search(flat), flat


def _set_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines()
            if ln and not ln.startswith("#")]


def test_retired_sets_file_on_data_discs_and_the_meta_disc(tmp_path: Path) -> None:
    """#992: stage writes metadata/RETIRED_SETS.txt on every data disc,
    and the META disc build writes it on the META disc: one
    ``id|label|date|name`` line per retired set, which restore.sh and
    restore.bat read (a disc whose sets are all retired is then not
    another archive's)."""
    from lcsas.meta.builder import MetaVolumeBuilder

    inst = ScratchInstall(tmp_path / "x")
    for name in inst.repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    (first,) = inst.labels()
    # Before any retirement: the file is there, with no set in it.
    listing = inst.disc_trees()[first] / "metadata" / "RETIRED_SETS.txt"
    assert listing.is_file()
    assert listing.read_text().startswith("# RETIRED_SETS.txt")
    assert _set_lines(listing) == []
    alpha = inst.repo_id("alpha")

    shutil.rmtree(inst.mirror("alpha"))
    assert inst.lcsas("repo", "retire", "alpha") == 0
    text = inst.config.read_text()
    inst.config.write_text(re.sub(r"\[repos\.alpha\]\n(?:[^\[\n].*\n)*", "", text))
    inst.backup("beta", "f2.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    newest = inst.labels()[-1]
    line = re.compile(rf"{re.escape(alpha)}\|{first}\|\d{{4}}-\d\d-\d\d\|alpha")
    lines = _set_lines(inst.disc_trees()[newest] / "metadata" / "RETIRED_SETS.txt")
    assert len(lines) == 1 and line.fullmatch(lines[0]), lines

    meta = tmp_path / "meta"
    MetaVolumeBuilder(meta, catalog_db_path=inst.db,
                      allow_no_dvdisaster_source=True).build()
    assert not (meta / "metadata" / alpha).exists()
    lines = _set_lines(meta / "metadata" / "RETIRED_SETS.txt")
    assert len(lines) == 1 and line.fullmatch(lines[0]), lines
