r"""FMT-01: restore.bat --check-disc ECC repair path, end-to-end under wine.

The Windows counterpart of ``test_restore_sh_ecc_dispatch.py`` (which
drives ``restore.sh --check-disc`` on Linux) and of
``test_ecc_selfrepair_no_dvdisaster.py`` (which proves byte-identical
self-repair with the in-house tool).  Here the *real* Windows recovery
driver ``restore.bat`` runs under wine and dispatches the *real* committed
``lcsas-ecc.exe`` against a tiny real RS03 image:

  * a clean augmented image -> ``--check-disc`` reports "no damage";
  * a damaged image -> ``--check-disc`` (with LCSAS_CHECK_DISC_AUTOFIX=1)
    detects the damage, repairs it through lcsas-ecc.exe, and the image on
    disk is byte-identical to the original.

No dvdisaster needed: the fixture is built by the in-repo generator
(``recovery/tests/ecc_make_fixture.c``), so this runs in well under a
second and ships in the default recovery-hardening suite whenever wine +
a C compiler + the committed lcsas-ecc.exe are present.

Like the wine smoke test, this exercises the .bat *structure* under wine's
incomplete ``cmd``; the authoritative Windows gate remains the
``windows-e2e.yml`` runner.  But the repair itself is real: the bytes are
verified, so a green run means lcsas-ecc.exe genuinely repaired the image
through restore.bat's dispatch.
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
RESTORE_BAT = RECOVERY / "scripts" / "restore.bat"
ECC_EXE = RECOVERY / "bin" / "x86_64-windows" / "lcsas-ecc.exe"

SECTOR = 2048
ARCH = "x86_64-pc-windows-gnu"
TIMEOUT = 120

_CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
_WINE = shutil.which("wine")


@pytest.fixture(scope="module", autouse=True)
def _require_working_wineboot() -> None:
    """Skip these tests if wineboot cannot initialise a fresh prefix within a
    bounded time.  On some hosts wineboot's first-run init hangs indefinitely
    (issue #390); each per-test wine call then burns the full TIMEOUT, wedging
    `make gate`'s shell-coverage phase.  A single bounded probe fails fast to a
    clean skip instead of grinding.
    """
    if not _WINE:
        return  # the module skipif already handles a missing wine
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        env = {**os.environ, "WINEDEBUG": "-all",
               "WINEPREFIX": _td, "DISPLAY": ""}
        try:
            subprocess.run(["wine", "wineboot", "--init"],
                           env=env, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            pytest.skip(
                "wineboot did not initialise a fresh prefix within 90s on "
                "this host (issue #390) — skipping restore.bat wine tests"
            )
        except OSError:
            pytest.skip("wine is not runnable on this host")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE and _CC and RESTORE_BAT.is_file() and ECC_EXE.is_file()
             and FIXTURE_SRC.is_file()),
        reason=(
            "restore.bat ECC wine e2e needs wine, a C compiler, restore.bat, "
            f"the committed {ECC_EXE.name}, and the fixture generator "
            f"(wine={bool(_WINE)} cc={bool(_CC)} bat={RESTORE_BAT.is_file()} "
            f"exe={ECC_EXE.is_file()})"
        ),
    ),
]


def _build_fixture_image(workdir: Path) -> Path:
    """Compile the host fixture generator and emit a tiny RS03 image."""
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = workdir / "ecc_make_fixture"
    cmd = [
        _CC, "-std=c89", "-O1", "-D_POSIX_C_SOURCE=200809L",
        "-D_FILE_OFFSET_BITS=64", "-Wno-long-long", "-I", str(ECC_DIR),
        str(FIXTURE_SRC), str(ECC_DIR / "gf256.c"), str(ECC_DIR / "rs03.c"),
        "-o", str(fixture),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"fixture compile failed:\n{res.stderr}"

    img = workdir / "disc.img"
    res = subprocess.run([str(fixture), str(img)], capture_output=True,
                         text=True)
    assert res.returncode == 0, f"fixture gen failed:\n{res.stderr}"
    assert img.is_file() and img.stat().st_size > 0
    return img


def _init_prefix(prefix: Path) -> dict[str, str]:
    prefix.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WINEDEBUG": "-all",
        "WINEPREFIX": str(prefix),
        "DISPLAY": "",
    }
    subprocess.run(["wine", "cmd", "/c", "ver"], env=env,
                   capture_output=True, text=True, timeout=TIMEOUT)
    return env


def _map_drive(prefix: Path, letter: str, target: Path) -> None:
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    link = dosdevices / f"{letter.lower()}:"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target, target_is_directory=True)


def _make_meta_tree(root: Path) -> None:
    """Meta-disc layout: recovery/scripts/restore.bat + the committed
    Windows lcsas-ecc.exe under recovery/bin/<arch>/."""
    scripts = root / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    bin_arch = root / "recovery" / "bin" / ARCH
    bin_arch.mkdir(parents=True)
    shutil.copy2(ECC_EXE, bin_arch / "lcsas-ecc.exe")


def _run_check_disc(
    env: dict[str, str], meta_letter: str, win_img: str, *, autofix: bool
) -> subprocess.CompletedProcess[str]:
    run_env = {
        **env,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_TARGET": ARCH,
    }
    if autofix:
        run_env["LCSAS_CHECK_DISC_AUTOFIX"] = "1"
    bat = f"{meta_letter.upper()}:\\recovery\\scripts\\restore.bat"
    # Pad stdin so the `pause` on each branch never blocks under wine.
    return subprocess.run(
        ["wine", "cmd", "/c", f"{bat} --check-disc {win_img}"],
        input="\n\n\n\n\n",
        env=run_env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def test_check_disc_clean_image_reports_intact(tmp_path: Path) -> None:
    """A freshly augmented (undamaged) image: restore.bat --check-disc
    dispatches lcsas-ecc.exe verify and reports no damage."""
    img = _build_fixture_image(tmp_path / "build")

    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    shutil.copy2(img, meta / "disc.img")

    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    _map_drive(prefix, "e", meta)

    res = _run_check_disc(env, "e", "E:\\disc.img", autofix=False)
    out = (res.stdout + res.stderr).lower()
    assert "no damage found" in out and "intact" in out, (
        "restore.bat --check-disc did not report a clean image as intact "
        f"under wine; rc={res.returncode}\n--- output ---\n"
        f"{res.stdout + res.stderr}"
    )


def test_check_disc_repairs_damage_byte_identical(tmp_path: Path) -> None:
    """A damaged image: restore.bat --check-disc detects the damage and
    (autofix) repairs it through lcsas-ecc.exe -- and the bytes on disk are
    restored identically to the original augmented image."""
    img = _build_fixture_image(tmp_path / "build")
    original = img.read_bytes()

    # Corrupt one data sector (sector 5 is well inside the data region).
    data = bytearray(original)
    base = 5 * SECTOR
    for i in range(base, base + 64):
        data[i] ^= 0xFF
    img.write_bytes(bytes(data))
    assert img.read_bytes() != original

    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    disc = meta / "disc.img"
    shutil.copy2(img, disc)

    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    _map_drive(prefix, "e", meta)

    res = _run_check_disc(env, "e", "E:\\disc.img", autofix=True)
    out = (res.stdout + res.stderr).lower()
    assert "damage detected" in out, (
        "restore.bat --check-disc did not detect the damage under wine; "
        f"rc={res.returncode}\n--- output ---\n{res.stdout + res.stderr}"
    )
    assert "repair succeeded" in out or "is now intact" in out, (
        "restore.bat --check-disc did not report a successful repair under "
        f"wine; rc={res.returncode}\n--- output ---\n{res.stdout + res.stderr}"
    )
    # Ground truth: the repair really happened through wine -> lcsas-ecc.exe.
    assert disc.read_bytes() == original, (
        "restore.bat --check-disc repair was not byte-identical to the "
        "original augmented image"
    )
