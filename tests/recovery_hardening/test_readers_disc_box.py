"""The disc box of both readers (#966).

- Item 8: with a single mount folder that is itself a disc (it has
  ``data/``), "Currently searching" listed the disc's own folders
  (data, metadata, ...) as places searched.  It lists the disc once.

Real rustic makes the backup set; its packs are taken away so the
reader asks for a disc.  Tier 1 is the built ``lcsas-restore``
(``LCSAS_RESTORE_BIN``, else ``recovery/build``, else the committed
binary) and the Python reader the generated ``python_reader.py``.
(The box text for a pack the catalog has no record of is pinned in
``recovery/tests/test_disc_locator.c`` and ``_disc_search.py``.)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.restore_contract import Skip as ContractSkip
from tests.contract.restore_contract import tier1_binary, tier3_script

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.skipif(not sys.platform.startswith("linux"),
                       reason="the committed tier-1 binary is Linux"),
]


def _reader(reader: str, work: Path) -> list[str]:
    try:
        if reader == "tier1":
            return [str(tier1_binary())]
        return [sys.executable, "-I", str(tier3_script(work))]
    except ContractSkip as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("reader", ("tier1", "tier3"))
def test_a_disc_mounted_at_the_mount_folder_is_one_disc(
        reader: str, tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text("test-password\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a file\n")
    repo = tmp_path / "set"
    for args in (["init"], ["backup", str(src)]):
        subprocess.run(["rustic", "-r", str(repo), "--no-cache",
                        "--password-file", str(pw), *args],
                       capture_output=True, check=True, timeout=120)
    for pack in (repo / "data").rglob("*"):
        if pack.is_file():
            pack.unlink()
    disc = tmp_path / "disc"
    for sub in ("data/00", "metadata/someset", "extras"):
        (disc / sub).mkdir(parents=True)
    cmd = [*_reader(reader, tmp_path), "--repo", str(repo),
           "--password-file", str(pw), "--restore-folder", str(tmp_path / "out"),
           "--interactive", "on"]
    if reader == "tier1":
        cmd += ["--mount-parent", str(disc), "--auto-mount", "off"]
    else:
        cmd += ["--mount-point", str(disc)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}
    res = subprocess.run(cmd, input="q\n", capture_output=True, text=True,
                         env=env, timeout=300)
    out = res.stdout + res.stderr
    box = out.split("Currently searching:", 1)
    assert len(box) == 2, out
    searched = box[1].split("Insert the right disc", 1)[0]
    listed = [line.strip("| ").strip() for line in searched.splitlines()
              if line.startswith("|   ")]
    assert any(item.endswith("/disc") for item in listed), searched
    assert not any(item.endswith(("/disc/data", "/disc/metadata", "/disc/extras"))
                   for item in listed), searched
