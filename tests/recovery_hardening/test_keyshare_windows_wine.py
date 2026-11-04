"""rc6: the committed Windows ``lcsas-keyshare.exe`` under wine.

restore.bat drives this binary to rebuild a split password from share
cards, so its Windows build must keep the contract the Linux one is held
to (``tests/integration/test_keyshare_binary_cards.py``):

  * the password goes to stdout as exact bytes -- stdout is switched to
    binary, so a newline byte in a password is not turned into CR LF;
  * more than K cards rebuild it (the cards say "any K");
  * exit 3 names a mistyped word, exit 1 means "cannot combine".

Skips without wine or the committed .exe (``LCSAS_KEYSHARE_EXE``
overrides it).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lcsas.cli.main import main as cli_main
from tests.recovery_hardening._wine_helpers import stop_wineserver

REPO_ROOT = Path(__file__).resolve().parents[2]
KEYSHARE_EXE = Path(os.environ["LCSAS_KEYSHARE_EXE"]) if os.environ.get(
    "LCSAS_KEYSHARE_EXE"
) else REPO_ROOT / "recovery" / "bin" / "x86_64-windows" / "lcsas-keyshare.exe"

_OK = KEYSHARE_EXE.is_file() and shutil.which("wine") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _OK, reason=f"needs wine and {KEYSHARE_EXE}"),
]

# A newline and a CR inside: a text-mode stdout would add or mangle bytes.
PASSWORD = b"line one\nline two\r\x01end"

WINEPREFIX = os.environ.get("LCSAS_WINEPREFIX") or os.path.join(
    tempfile.gettempdir(), "lcsas-wine-prefix"
)
ENV = {**os.environ, "WINEPREFIX": WINEPREFIX, "WINEDEBUG": "-all", "DISPLAY": ""}


@pytest.fixture(scope="module")
def cards(tmp_path_factory) -> list[Path]:
    base = tmp_path_factory.mktemp("ks_wine")
    pw = base / "pw"
    pw.write_bytes(PASSWORD)
    out = base / "shares"
    assert cli_main([
        "key", "split", "--repo", "alpha", "--threshold", "2", "--shares", "4",
        "--password-file", str(pw), "--out", str(out),
    ]) == 0
    os.makedirs(WINEPREFIX, exist_ok=True)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["wine", "wineboot", "--init"], env=ENV,
                       capture_output=True, timeout=180)
    shutil.copy2(KEYSHARE_EXE, base / "lcsas-keyshare.exe")
    yield sorted(out.glob("alpha-share-*-card.txt"))
    stop_wineserver(WINEPREFIX)


def _run(cards_dir: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["wine", str(cards_dir.parent.parent / "lcsas-keyshare.exe"), *args],
        env=ENV, capture_output=True, timeout=120, check=False)


def test_password_bytes_exact_from_more_than_k_cards(cards) -> None:
    proc = _run(cards[0], str(cards[3]), str(cards[0]), str(cards[2]))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == PASSWORD


def test_typo_is_exit_3_and_too_few_is_exit_1(cards, tmp_path) -> None:
    words = [ln for ln in cards[1].read_text().splitlines()
             if ln.startswith("  ") and len(ln.split()) >= 20][0].split()
    words[2] = "qqqq"
    bad = tmp_path / "typed.txt"
    bad.write_text(" ".join(words) + "\n")
    proc = _run(cards[0], str(bad))
    assert proc.returncode == 3, proc.stderr
    assert b"word 3 'qqqq' is not a share word" in proc.stderr
    assert _run(cards[0], str(cards[1])).returncode == 1
