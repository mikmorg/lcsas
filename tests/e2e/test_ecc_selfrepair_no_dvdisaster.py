"""Always-on RS03 self-repair with ONLY the in-house tool [FMT-01].

The headline gate for FMT-01: prove that a corrupted RS03-augmented disc
image is repaired byte-identically using NOTHING but the bundled LCSAS
tooling (``lcsas-ecc``) -- no ``dvdisaster``, no ``ddrescue`` -- even when
a ``dvdisaster`` on PATH actively fails (exits 127).

Unlike ``tests/integration/test_ecc_repair.py`` (which augments with the
real dvdisaster and pads to a ~700 MB full medium, hence opt-in + slow),
this test builds a TINY unpadded augmented image with the in-repo fixture
generator (``recovery/tests/ecc_make_fixture.c``, which reuses the
shipped rs03 encoder/decoder), so it runs in well under a second and ships
in the default suite.

Flow:
  1. compile ecc_make_fixture + lcsas-ecc from source with cc;
  2. generate a small RS03 image; snapshot its data region;
  3. overwrite a data sector (simulated bit-rot) -> verify reports damage;
  4. put a dvdisaster shim that exits 127 first on PATH;
  5. run ``lcsas-ecc fix`` -> assert it repairs the data byte-identically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
ECC_DIR = RECOVERY / "src" / "lcsas-ecc"
FIXTURE_SRC = RECOVERY / "tests" / "ecc_make_fixture.c"

SECTOR = 2048

_CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")

pytestmark = pytest.mark.skipif(
    _CC is None,
    reason="needs a C compiler (cc/gcc/clang) to build the ecc fixture + tool",
)


def _compile(out: Path, *sources: Path, includes: tuple[Path, ...] = ()) -> None:
    cmd = [
        _CC, "-std=c89", "-O1", "-D_POSIX_C_SOURCE=200809L",
        "-D_FILE_OFFSET_BITS=64", "-Wno-long-long",
    ]
    for inc in includes:
        cmd += ["-I", str(inc)]
    cmd += [str(s) for s in sources]
    cmd += ["-o", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"compile failed:\n{res.stderr}"


@pytest.fixture(scope="module")
def ecc_tools(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build (ecc_make_fixture, lcsas-ecc) once for the module."""
    bdir = tmp_path_factory.mktemp("ecc_tools")
    gf = ECC_DIR / "gf256.c"
    rs = ECC_DIR / "rs03.c"
    main = ECC_DIR / "main.c"

    fixture = bdir / "ecc_make_fixture"
    _compile(fixture, FIXTURE_SRC, gf, rs, includes=(ECC_DIR,))

    ecc = bdir / "lcsas-ecc"
    _compile(ecc, main, gf, rs, includes=(ECC_DIR,))
    return fixture, ecc


def _make_fixture(fixture_bin: Path, out: Path) -> None:
    res = subprocess.run([str(fixture_bin), str(out)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"fixture gen failed:\n{res.stderr}"
    assert out.is_file() and out.stat().st_size > 0


def _dvdisaster_127_shim(dirpath: Path) -> dict[str, str]:
    """Return an env whose PATH has a dvdisaster that always exits 127."""
    dirpath.mkdir(parents=True, exist_ok=True)
    shim = dirpath / "dvdisaster"
    shim.write_text("#!/bin/sh\necho 'dvdisaster: shimmed (exit 127)' >&2\nexit 127\n")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{dirpath}{os.pathsep}{env.get('PATH', '')}"
    return env


def test_selfrepair_byte_identical_without_dvdisaster(
    ecc_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    fixture_bin, ecc = ecc_tools
    img = tmp_path / "disc.img"
    _make_fixture(fixture_bin, img)

    env = _dvdisaster_127_shim(tmp_path / "shimbin")
    # Sanity: the shim really fails, so any success below is lcsas-ecc's.
    assert subprocess.run(["dvdisaster"], env=env,
                          capture_output=True).returncode == 127

    original = img.read_bytes()

    # A clean image verifies OK (exit 0).
    r = subprocess.run([str(ecc), "verify", str(img)], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"clean image should verify: {r.stderr}"

    # Corrupt one data sector (sector 5 is well inside the data region).
    data = bytearray(original)
    base = 5 * SECTOR
    for i in range(base, base + 64):
        data[i] ^= 0xFF
    img.write_bytes(bytes(data))
    assert img.read_bytes() != original

    # verify must now report damage (exit 1), NOT crash / NOT pass.
    r = subprocess.run([str(ecc), "verify", str(img)], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 1, (
        f"corrupted image must report damage (exit 1); got {r.returncode}\n"
        f"{r.stdout}\n{r.stderr}"
    )

    # Repair in place using ONLY lcsas-ecc (dvdisaster on PATH exits 127).
    r = subprocess.run([str(ecc), "fix", str(img)], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"lcsas-ecc fix should fully repair; got {r.returncode}\n"
        f"{r.stdout}\n{r.stderr}"
    )

    # The repaired image is byte-identical to the original.
    assert img.read_bytes() == original, "repair was not byte-identical"

    # And it verifies clean again.
    r = subprocess.run([str(ecc), "verify", str(img)], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"repaired image should verify clean: {r.stderr}"


def test_fix_to_out_leaves_source_untouched(
    ecc_tools: tuple[Path, Path], tmp_path: Path
) -> None:
    """`lcsas-ecc fix --out F` writes the repair to F, not in place."""
    fixture_bin, ecc = ecc_tools
    img = tmp_path / "disc.img"
    _make_fixture(fixture_bin, img)
    original = img.read_bytes()

    data = bytearray(original)
    base = 7 * SECTOR
    for i in range(base, base + 64):
        data[i] ^= 0xAA
    corrupted = bytes(data)
    img.write_bytes(corrupted)

    out = tmp_path / "repaired.img"
    r = subprocess.run([str(ecc), "fix", str(img), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"fix --out failed: {r.stderr}"
    # Source unchanged; output is the byte-identical repair.
    assert img.read_bytes() == corrupted, "source image must be untouched"
    assert out.read_bytes() == original, "out image must be the clean repair"
