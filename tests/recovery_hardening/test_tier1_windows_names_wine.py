"""#619: Windows tier 1 restores every name Windows can hold, and only those.

A backup made on Linux can hold names that Windows tier 1 got wrong:
non-ASCII names came out garbled (the narrow file calls read the UTF-8
bytes in the ANSI code page), a name holding ``:`` went into an NTFS
alternate data stream, a name holding ``..\\`` passed the path check, a
device name (``CON.txt``) or a trailing dot or space was written to
something else than the file, and a path over 260 characters failed.

This backs a tree holding each case up with real ``rustic`` on Linux,
restores it with the Windows ``lcsas-restore.exe`` under wine, and
checks, on the host side:

* every name Windows can hold (accents, other scripts, an emoji, a
  folder and a file with such names, a path over 260 characters) is
  restored byte for byte, with its content;
* every name Windows cannot hold is not restored under any other name:
  it is named in a ``NOT RESTORED`` line and a row of
  ``RESTORE_FAILURES.txt``, and tier 1 exits 4 (#872);
* nothing is written outside the restore folder;
* the restore folder, the password file, the pack cache folder
  (``LCSAS_PACK_CACHE_DIR``) and a disc catalog (``--catalog``) may
  have non-ASCII names too: the arguments and the environment are read
  as Unicode.

Wine does not hold Windows' MAX_PATH limit everywhere, so the long path
passing here does not prove the ``\\\\?\\`` prefix on its own:
``test_long_path_prefix_helper`` below checks the prefix itself (built
with zig, when zig is installed).  No test runs these names on real
Windows yet (the windows-e2e journey uses plain names).

Skips when wine, rustic or the Windows binary is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import build_rustic_repo
from tests.recovery_hardening._wine_helpers import stop_wineserver

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_EXE = Path(os.environ["LCSAS_RESTORE_BIN"]) if os.environ.get(
    "LCSAS_RESTORE_BIN"
) else REPO_ROOT / "recovery" / "bin" / "x86_64-windows" / "lcsas-restore.exe"

_BIN_OK = RESTORE_EXE.is_file() and RESTORE_EXE.suffix == ".exe"
_WINE_OK = shutil.which("wine") is not None
_RUSTIC_OK = shutil.which("rustic") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_BIN_OK and _WINE_OK and _RUSTIC_OK),
        reason=(f"needs wine, rustic and a Windows {RESTORE_EXE.name} "
                f"(wine={_WINE_OK}, rustic={_RUSTIC_OK}, exe={_BIN_OK})"),
    ),
]

PASSWORD = "test-password"

# Names Windows can hold: they must come back byte for byte.
GOOD_FILES = (
    "plain.txt",
    "café crème, résumé – naïve.txt",
    "Ångström Øre Ünïcödé.txt",
    "emoji 📷🎉 photo.txt",
    "日本語 ファイル.txt",
    "Ελληνικά и кириллица.txt",
    "it's (copy) [1] {x} & 100% #1 !.txt",
    ".hidden",
    "CONSOLE.txt",       # starts like CON, but is not a device name
    "COM12",
    "my con.txt",
)
GOOD_DIR = "Ünïcödé folder 目录"
GOOD_INNER = "ñandú 🦤.txt"

# Names Windows cannot hold: each one is refused and listed, never
# written under another name.
BAD_NAMES = (
    "a:b.txt",           # would be stream "b.txt" of a file "a"
    "C:evil.txt",        # a drive prefix
    "..\\evil.txt",      # '..\' would climb out of the folder
    "sub\\dir.txt",
    "CON.txt",           # device names, with or without an extension
    "nul",
    "Com1.log",
    "LPT9",
    "trailing dot.",
    "trailing space ",
    'star* "quote" <angle> pipe|?.txt',
)

# The long path: six 50-character folders put it well over 260
# characters once the restore folder is in front of it.
LONG_DIRS = [f"long folder {i} " + "x" * 38 for i in range(1, 7)]
LONG_LEAF = "file at the end of a long path ü.txt"


def _win(path: Path) -> str:
    """The Windows spelling of a host path (wine maps Z: to /)."""
    return "Z:" + str(path).replace("/", "\\")


def _content(name: str) -> bytes:
    return ("content of " + name + "\n").encode("utf-8") * 3


def _make_source(src: Path) -> None:
    for name in GOOD_FILES + BAD_NAMES:
        (src / name).write_bytes(_content(name))
    (src / GOOD_DIR).mkdir()
    (src / GOOD_DIR / GOOD_INNER).write_bytes(_content(GOOD_INNER))
    deep = src.joinpath(*LONG_DIRS)
    deep.mkdir(parents=True)
    (deep / LONG_LEAF).write_bytes(_content(LONG_LEAF))


def _make_catalog(path: Path) -> None:
    """A minimal disc catalog: one disc holding one 2 MiB pack."""
    import sqlite3

    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE schema_version(version INTEGER);"
        "INSERT INTO schema_version VALUES(10);"
        "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
        " size_bytes INTEGER, repo_id TEXT);"
        "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
        " uuid TEXT, media_type TEXT, status TEXT);"
        "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);"
        "INSERT INTO volumes VALUES(1, 'LCSAS_TEST_2026_0001', 'u1',"
        " 'CD700', 'VERIFIED');"
        "INSERT INTO packs VALUES(1, '" + "ab" * 32 + "', 2097152, 'r');"
        "INSERT INTO volume_packs VALUES(1, 1);"
    )
    db.commit()
    db.close()


def _wine_env(prefix: Path) -> dict[str, str]:
    # A UTF-8 locale: wine converts host file names with it.
    return {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all",
            "DISPLAY": "", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


@pytest.fixture(scope="module")
def restored(tmp_path_factory):
    """Back the source up, restore it once under wine; yield what the
    tests look at."""
    base = tmp_path_factory.mktemp("win_names")
    src = base / "src"
    src.mkdir()
    _make_source(src)
    # A non-ASCII folder for the password file, the restore and the
    # pack cache: the arguments and LCSAS_PACK_CACHE_DIR must be read
    # as Unicode, not in the ANSI code page.
    home = base / "hôme 用户"
    home.mkdir()
    pw = home / "pässword.txt"
    pw.write_text(PASSWORD + "\n")
    repo = base / "repo"
    build_rustic_repo(src, repo, pw)
    # A disc catalog in that folder too: sqlite takes UTF-8 file names
    # on Windows, which tier 1 now passes it.
    catalog = home / "catalögue.db"
    _make_catalog(catalog)
    target = home / "réstored 目标"
    cache = home / "cäche"
    exe = base / "lcsas-restore.exe"
    shutil.copy2(RESTORE_EXE, exe)

    prefix = base / "prefix"
    env = _wine_env(prefix)
    subprocess.run(["wine", "wineboot", "--init"], env=env,
                   capture_output=True, timeout=300, check=False)
    try:
        res = subprocess.run(
            ["wine", str(exe), "--repo", _win(repo),
             "--password-file", _win(pw), "--restore-folder", _win(target),
             "--interactive", "off"],
            env={**env, "LCSAS_PACK_CACHE_DIR": _win(cache)},
            capture_output=True, timeout=600,
        )
        cat = subprocess.run(
            ["wine", str(exe), "--list-pending-packs",
             "--catalog", _win(catalog), "--repo", _win(home),
             "--password-file", "NUL"],
            env=env, capture_output=True, timeout=300,
        )
    finally:
        stop_wineserver(prefix)
    yield {
        "cat_rc": cat.returncode,
        "cat_out": (cat.stdout + cat.stderr).decode("utf-8", "replace"),
        "base": base, "src": src, "target": target, "cache": cache,
        "rc": res.returncode,
        "err": res.stderr.decode("utf-8", "replace"),
        "out": res.stdout.decode("utf-8", "replace"),
    }


def _root(w) -> Path:
    """The restored copy of the source folder: tier 1 restores the
    snapshot's absolute path under the target."""
    return w["target"].joinpath(*w["src"].parts[1:])


def test_exits_4_and_names_each_refused_item(restored) -> None:
    w = restored
    err = w["err"]
    assert w["rc"] == 4, f"rc={w['rc']}\n{err}\n{w['out']}"
    assert "finished with errors" in err, err
    assert "restore complete" not in err, err
    assert err.count("NOT RESTORED: ") == len(BAD_NAMES), err
    assert f"{len(BAD_NAMES)} item(s) were NOT restored" in err, err


def test_good_names_restore_byte_for_byte(restored) -> None:
    root = _root(restored)
    have = set(os.listdir(os.fsencode(root)))
    for name in GOOD_FILES:
        raw = name.encode("utf-8")
        assert raw in have, (name, sorted(have))
        assert (root / name).read_bytes() == _content(name), name
    inner = root / GOOD_DIR / GOOD_INNER
    assert inner.read_bytes() == _content(GOOD_INNER)
    assert os.listdir(os.fsencode(root / GOOD_DIR)) == [
        GOOD_INNER.encode("utf-8")]


def test_long_path_restores(restored) -> None:
    root = _root(restored)
    leaf = root.joinpath(*LONG_DIRS, LONG_LEAF)
    win_len = len(_win(leaf))
    assert win_len > 300, win_len          # the case is really long
    assert leaf.read_bytes() == _content(LONG_LEAF)


def test_refused_names_are_listed_and_not_written(restored) -> None:
    w = restored
    root = _root(w)
    rows = (w["target"] / "RESTORE_FAILURES.txt").read_text(
        encoding="utf-8").splitlines()
    body = rows[rows.index("path\treason\tblob_id") + 1:]
    reasons = {r.split("\t")[0].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]:
               r.split("\t")[1] for r in body if r}
    assert len(body) == len(BAD_NAMES), rows
    for row in body:
        assert row.split("\t")[1].startswith("name refused: Windows"), row
    joined = "\n".join(body)
    for name in ("a:b.txt", "C:evil.txt", "CON.txt", "nul", "Com1.log",
                 "LPT9", "trailing dot.", "trailing space "):
        assert name in joined, (name, joined)
    assert "keeps this name for a device" in reasons.get("CON.txt", ""), reasons
    assert "ends in a dot or a space" in reasons.get("trailing dot.", ""), reasons
    # Exactly the good names (and the folders) are in the restored copy:
    # nothing written under another name ("a" from "a:b.txt", "CON",
    # "trailing dot" without its dot, "evil.txt" from "..\evil.txt").
    want = {n.encode("utf-8") for n in GOOD_FILES} | {
        GOOD_DIR.encode("utf-8"), LONG_DIRS[0].encode("utf-8")}
    assert set(os.listdir(os.fsencode(root))) == want


def test_nothing_is_written_outside_the_restore_folder(restored) -> None:
    w = restored
    base = w["base"]
    allowed = {"src", "repo", "lcsas-restore.exe", "prefix", "hôme 用户"}
    assert set(os.listdir(base)) == allowed, sorted(os.listdir(base))
    home = w["target"].parent
    assert set(os.listdir(home)) <= {
        "pässword.txt", "catalögue.db", "réstored 目标", "cäche"}, sorted(
            os.listdir(home))
    assert not list(base.rglob("evil.txt"))


def test_non_ascii_cache_folder_is_used(restored) -> None:
    """LCSAS_PACK_CACHE_DIR named a non-ASCII folder: tier 1 made that
    folder, not one with a garbled name."""
    assert restored["cache"].is_dir(), sorted(
        os.listdir(restored["target"].parent))


def _zig() -> list[str] | None:
    try:
        subprocess.run(["python3", "-m", "ziglang", "version"],
                       capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return ["python3", "-m", "ziglang", "cc"]


HELPER_C = r"""
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "lcsas_io.h"
#include "posix_compat.h"
#include <stdio.h>
#include <string.h>
#include <wchar.h>

static int fails = 0;

static void
check(const char *label, const char *in, const wchar_t *want)
{
    wchar_t *got = lcsas_w_path(in);
    if (!got || wcscmp(got, want) != 0) {
        fprintf(stderr, "FAIL %s\n", label);
        fails++;
    }
    free(got);
}

int main(int argc, char **argv)
{
    char lng[400];
    wchar_t wlng[420];
    int i;
    (void)argc; (void)argv;
    /* Short paths are used as they are, slashes and all. */
    check("short", "C:\\a/b.txt", L"C:\\a/b.txt");
    check("short utf8", "C:\\caf\xc3\xa9", L"C:\\caf\x00e9");
    check("prefixed", "\\\\?\\C:\\x", L"\\\\?\\C:\\x");
    /* A long one gets \\?\, backslashes only, and "." / ".." resolved. */
    strcpy(lng, "C:\\r/");
    for (i = 0; i < 30; i++) strcat(lng, "abcdefgh/");
    strcat(lng, "./x/../f.txt");
    wcscpy(wlng, L"\\\\?\\C:\\r\\");
    for (i = 0; i < 30; i++) wcscat(wlng, L"abcdefgh\\");
    wcscat(wlng, L"f.txt");
    check("long", lng, wlng);
    /* A share: \\?\UNC\server\share\... */
    strcpy(lng, "\\\\srv\\share\\");
    for (i = 0; i < 30; i++) strcat(lng, "abcdefgh\\");
    wcscpy(wlng, L"\\\\?\\UNC\\srv\\share\\");
    for (i = 0; i < 30; i++) wcscat(wlng, L"abcdefgh\\");
    check("unc", lng, wlng);
    /* Not UTF-8: refused, not read as ANSI. */
    if (lcsas_w_path("caf\xe9") != NULL) {
        fprintf(stderr, "FAIL latin1 accepted\n");
        fails++;
    }
    /* A file at a long path can be made, written and read back. */
    {
        char dir[600];
        int fd;
        struct stat st;
        sprintf(dir, "%s", "lp");
        for (i = 0; i < 6; i++) strcat(dir, "/folder-name-of-fifty-characters-xxxxxxxxxxxxxxxxx");
        if (lcsas_mkdir_p(dir) != 0) { fprintf(stderr, "FAIL mkdir_p\n"); fails++; }
        strcat(dir, "/f\xc3\xa9.txt");
        fd = lcsas_create_file(dir, 0644);
        if (fd < 0 || lcsas_write_exact(fd, "hi", 2) != 0) {
            fprintf(stderr, "FAIL create long\n");
            fails++;
        }
        if (fd >= 0) close(fd);
        if (stat(dir, &st) != 0 || st.st_size != 2) {
            fprintf(stderr, "FAIL stat long\n");
            fails++;
        }
        if (lcsas_entry_kind(dir) != LCSAS_ENTRY_FILE) {
            fprintf(stderr, "FAIL entry_kind long\n");
            fails++;
        }
    }
    if (fails == 0) printf("helper: OK\n");
    return fails ? 1 : 0;
}
"""


def test_long_path_prefix_helper(tmp_path: Path) -> None:
    """lcsas_w_path (lcsas_io.c), built for Windows with zig and run
    under wine: a long path gets the \\\\?\\ prefix with backslashes
    only and "." and ".." resolved, a share becomes \\\\?\\UNC\\, a short
    path is left as it is, a name that is not UTF-8 is refused; and a
    file at a 300-character path is made, written and found."""
    zig = _zig()
    if zig is None:
        pytest.skip("zig (python3 -m ziglang) not installed")
    rdir = REPO_ROOT / "recovery"
    (tmp_path / "helper.c").write_text(HELPER_C)
    exe = tmp_path / "helper.exe"
    subprocess.run(
        [*zig, "-target", "x86_64-windows-gnu", "-std=c89",
         "-D_FILE_OFFSET_BITS=64", "-Wno-long-long",
         f"-I{rdir / 'src' / 'lcsas-restore'}",
         str(tmp_path / "helper.c"),
         str(rdir / "src" / "lcsas-restore" / "lcsas_io.c"),
         "-o", str(exe)],
        check=True, capture_output=True, timeout=600,
    )
    prefix = tmp_path / "prefix"
    env = _wine_env(prefix)
    try:
        res = subprocess.run(["wine", str(exe)], env=env, cwd=tmp_path,
                             capture_output=True, timeout=300)
    finally:
        stop_wineserver(prefix)
    out = (res.stdout + res.stderr).decode("utf-8", "replace")
    assert res.returncode == 0, out
    assert "helper: OK" in out, out
    deep = tmp_path / "lp"
    leaf = deep.joinpath(
        *["folder-name-of-fifty-characters-xxxxxxxxxxxxxxxxx"] * 6, "fé.txt")
    assert leaf.read_bytes() == b"hi"


def test_catalog_in_a_non_ascii_folder_is_read(restored) -> None:
    """--catalog names a file in a non-ASCII folder: sqlite opens it
    (it takes UTF-8 names on Windows, which tier 1 now passes)."""
    out = restored["cat_out"]
    assert restored["cat_rc"] == 0, out
    assert "Total: 1 pack," in out, out
    assert "LCSAS_TEST_2026_0001" in out, out
