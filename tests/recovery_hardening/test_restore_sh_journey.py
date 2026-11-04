"""Hardening (#605 #606 #607 #608 #609 #617): restore.sh's journey.

Drill #476 restored on a one-drive computer and met, in order:

* F5 (#605): the backup sets were read off the META disc, whose copy of
  the snapshots was older than the newest data disc's, and a set made
  after the META disc was not offered at all.  Discovery was first-wins
  by scan order.  Now restore.sh asks for the NEWEST data disc first (on
  a terminal), ranks every copy of a set by its disc's label (the year,
  then the number) and keeps the tools folder's copy fresh from the
  newest disc (C2).
* F4 (#606): the set list showed folder names, which are UUIDs on real
  discs (every fixture used id == name, so no gate saw one).  Now it
  shows the marker's name (metadata/<id>/LCSAS_REPO.txt), with the split
  note; LCSAS_REPO and the typed answer match the id or the name.  Names
  are untrusted text off a disc.
* F6 (#607): a split password had to be rebuilt by hand before the
  restore.  Now the script asks for exactly K share cards, checks each
  one alone with lcsas-keyshare, and combines them (C7).
* F7 (#608): an empty or wrong password ended the run.  Now it asks
  again; Q quits with 77.
* Another set: after a restore that succeeds it asks "Restore another
  backup set from these discs?" before the tools folder goes.

Every fixture uses UUID set ids and names that differ from them.  Most
runs go through ``subprocess.run`` (traced for the shell-coverage gate)
with LCSAS_INTERACTIVE=on standing in for a terminal; the runs where a
disc must appear between two answers use a Popen session, and the
terminal-only behaviour uses a pty.  Both mirror conftest's trace
rewrite.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import termios
import textwrap
import threading
import time
from pathlib import Path

import pytest

from .conftest import _scaled_timeout

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
RESTORE_AUTO_SH = REPO_ROOT / "recovery" / "scripts" / "restore_auto.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"
KEYSHARE_REAL = REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-keyshare"

# Invented ids and names (the repository is public).  The ids sort so
# that DOCS is listed first; the names differ from the ids.
DOCS_ID = "1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b"
PHOTOS_ID = "9f3b2c1d-7e4a-4b8c-a1d2-3e4f5a6b7c8d"
LATE_ID = "c0ffee00-1234-4abc-9def-0123456789ab"
DOCS = "documents"
PHOTOS = "family photos"
GOOD_PW = "not-a-real-password-2718"

needs_keyshare = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and platform.machine() == "x86_64"
         and KEYSHARE_REAL.is_file()),
    reason="the committed lcsas-keyshare is an x86_64 Linux binary",
)

# The stub tier 1.  --help answers the pre-flight.  --list-snapshots is
# the password check: 0 for GOOD_PW, else 77, as tier 1 does.  A restore
# run logs its --repo, --target and password and exits 77 on a wrong
# password, else $STUB_RC (default 0) after writing restored.txt.
STUB = textwrap.dedent("""\
    #!/bin/sh
    [ "${1:-}" = "--help" ] && exit 0
    pw="" prev="" target="" repo="" list=0
    for a in "$@"; do
        case "$prev" in
            --password-file) pw="$(cat "$a")" ;;
            --target) target="$a" ;;
            --repo) repo="$a" ;;
        esac
        [ "$a" = "--list-snapshots" ] && list=1
        prev="$a"
    done
    if [ "$list" = 1 ]; then
        printf 'CHECK %s\\n' "$repo" >> "$STUB_LOG"
        [ "$pw" = "$GOOD_PW" ] && exit 0
        exit 77
    fi
    printf 'RUN repo=%s target=%s pw=%s\\n' "$repo" "$target" "$pw" >> "$STUB_LOG"
    [ "$pw" = "$GOOD_PW" ] || exit 77
    printf 'restored\\n' > "$target/restored.txt"
    exit "${STUB_RC:-0}"
""")


def _marker(set_id: str, name: str, split: str = "none", split_id: str = "") -> str:
    return (
        "# LCSAS backup set marker. Restore works without this file.\n"
        f"format=1\nid={set_id}\nname={name}\nname_cmd=x\n"
        f"split={split}\nsplit_id={split_id}\n"
    )


def _set(root: Path, set_id: str, name: str | None, *, tag: str = "meta",
         split: str = "none", split_id: str = "") -> Path:
    """metadata/<set_id>/ under *root*: keys/, index/ and snapshots/, each
    holding a file named after *tag*, and the marker unless *name* is None."""
    repo = root / "metadata" / set_id
    for sub in ("keys", "index", "snapshots"):
        (repo / sub).mkdir(parents=True, exist_ok=True)
        (repo / sub / f"{sub}-{tag}").write_text(tag)
    if name is not None:
        (repo / "LCSAS_REPO.txt").write_text(_marker(set_id, name, split, split_id))
    return repo


def _meta(root: Path, *, keyshare: bool | str = False) -> Path:
    """A META disc: recovery/ with the stub tier 1 and the sentinel
    recovery/scripts/restore.sh (the disc is in the drive while it is
    there).  *keyshare*: True copies the real lcsas-keyshare; a string is
    a log file for a wrapper that records each file's mode first."""
    recovery = root / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    (bin_dir / "lcsas-restore").write_text(STUB)
    (bin_dir / "lcsas-restore").chmod(0o755)
    if keyshare:
        real = bin_dir / "lcsas-keyshare.real"
        shutil.copy2(KEYSHARE_REAL, real)
        real.chmod(0o755)
        log = keyshare if isinstance(keyshare, str) else "/dev/null"
        (bin_dir / "lcsas-keyshare").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            for a in "$@"; do
                [ -f "$a" ] || continue
                printf '%s %s\\n' "$(stat -c %a "$a")" \\
                    "$(stat -c %a "$(dirname "$a")")" >> '{log}'
            done
            exec '{real}' "$@"
        """))
        (bin_dir / "lcsas-keyshare").chmod(0o755)
    (recovery / "scripts").mkdir()
    (recovery / "scripts" / "restore.sh").write_text("# the META disc is in\n")
    return recovery


def _data_disc(media: Path, dirname: str, label: str | None, *,
               json_label: bool = False, catalog_mtime: int | None = None) -> Path:
    """A data disc root under *media*, labelled in VOLUME_INFO.txt (or in
    volume_info.json), with an optional catalog.db of a given mtime."""
    root = media / dirname
    root.mkdir(parents=True, exist_ok=True)
    if label is not None and not json_label:
        (root / "VOLUME_INFO.txt").write_text(
            "ABOUT THIS DISC\n===============\n\n"
            f"  Label:         {label}\r\n  Prepared on:   2026-01-01\n"
        )
    if label is not None and json_label:
        (root / "volume_info.json").write_text(f'{{"type": "data", "label": "{label}"}}\n')
    if catalog_mtime is not None:
        (root / "catalog.db").write_text("not a real catalog")
        os.utime(root / "catalog.db", (catalog_mtime, catalog_mtime))
    return root


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": str(media),
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_TOOLS_DIR": str(tmp_path / "tools"),
        "TMPDIR": str(tmpdir),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "GOOD_PW": GOOD_PW,
        **extra,
    }
    for var in ("LCSAS_PASSWORD", "LCSAS_PWFILE", "LCSAS_REPO", "LCSAS_RELOCATED",
                "LCSAS_META_DISC", "LCSAS_NO_RELOCATE", "LCSAS_TIER_FALLBACK",
                "LCSAS_INTERACTIVE", "LCSAS_NEWEST_DISC_PROMPT",
                "LCSAS_ANOTHER_SET", "LCSAS_SHARES_DIR", "LCSAS_PACK_CACHE_DIR"):
        if var not in extra:
            env.pop(var, None)
    return env


def _run(recovery: Path, target: Path, env: dict[str, str], stdin: str = "",
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )


def _log(tmp_path: Path) -> list[str]:
    log = tmp_path / "stub.log"
    return log.read_text().splitlines() if log.exists() else []


def _runs(tmp_path: Path) -> list[str]:
    return [ln for ln in _log(tmp_path) if ln.startswith("RUN ")]


def _checks(tmp_path: Path) -> list[str]:
    return [ln for ln in _log(tmp_path) if ln.startswith("CHECK ")]


def _menu(stderr: str) -> list[str]:
    return [ln.strip() for ln in stderr.splitlines() if ln.startswith("  ") and ") " in ln[:8]]


def _traced(env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """conftest's trace rewrite, for the Popen and pty runs."""
    env = dict(env)
    if os.environ.get("LCSAS_TRACE_VIA_BASH") and os.environ.get("LCSAS_SHELL_TRACE"):
        env["LCSAS_SHELL_TRACE"] = os.environ["LCSAS_SHELL_TRACE"]
        return "bash", env
    return "sh", env


class _Session:
    """restore.sh on a pipe, answered one prompt at a time."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        interp, env = _traced(env)
        self.proc = subprocess.Popen(
            [interp, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env,
        )
        self._buf = bytearray()
        self._mark = 0
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for chunk in iter(lambda: self.proc.stdout.read1(4096), b""):  # type: ignore[union-attr]
            with self._lock:
                self._buf += chunk

    @property
    def text(self) -> str:
        with self._lock:
            return self._buf.decode(errors="replace")

    def expect(self, needle: str, timeout: float = 20) -> str:
        deadline = time.monotonic() + _scaled_timeout(timeout)
        while time.monotonic() < deadline:
            text = self.text
            i = text.find(needle, self._mark)
            if i >= 0:
                self._mark = i + len(needle)
                return text
            if self.proc.poll() is not None and not self._reader.is_alive():
                break
            time.sleep(0.05)
        raise AssertionError(f"never saw {needle!r}; output:\n{self.text}")

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.encode())
        self.proc.stdin.flush()

    def finish(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=_scaled_timeout(30))
        self._reader.join(timeout=_scaled_timeout(10))
        return rc


def _shares(secret: bytes, k: int, n: int) -> tuple[list[str], str]:
    """SLIP-39 shares of *secret* (K of N) and their Split ID."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from lcsas.keyshare import share_identifier, split_secret
    from lcsas.keyshare.codec import encode_master_secret

    mnemonics = split_secret(encode_master_secret(secret), k, n)
    return mnemonics, f"{share_identifier(mnemonics[0]):05d}"


def _card(mnemonic: str) -> str:
    """A printed share card: prose around the words."""
    return f"SHARE CARD\nRepository: invented\n\n{mnemonic}\n\nKeep this card safe.\n"


# ── F4: names, not ids (#606) ────────────────────────────────────────


def _two_sets(tmp_path: Path, **photos: str) -> Path:
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, DOCS)
    _set(tmp_path / "meta", PHOTOS_ID, PHOTOS, **photos)
    return recovery


def test_the_set_list_shows_names_and_the_split_not_ids(tmp_path: Path) -> None:
    """The menu shows each set's name, with the split note; the UUIDs
    (the folder names) are not what the reader picks from."""
    recovery = _two_sets(tmp_path, split="2-of-3", split_id="04321")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="1\n")
    assert res.returncode == 0, res.stderr
    assert _menu(res.stderr) == [
        f"1) {DOCS}",
        f"2) {PHOTOS} (password split: any 2 of 3 share cards)",
    ], res.stderr
    assert f"repo={tmp_path}/meta/metadata/{DOCS_ID} " in _runs(tmp_path)[0]
    assert f"[restore.sh] backup set: {DOCS}" in res.stderr


@pytest.mark.parametrize("answer", [PHOTOS, PHOTOS_ID, "2"])
def test_a_set_is_chosen_by_its_name_its_id_or_its_number(tmp_path: Path, answer: str) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin=f"{answer}\n")
    assert res.returncode == 0, res.stderr
    assert f"repo={tmp_path}/meta/metadata/{PHOTOS_ID} " in _runs(tmp_path)[0]


@pytest.mark.parametrize("value", [PHOTOS, PHOTOS_ID])
def test_lcsas_repo_matches_the_name_or_the_id(tmp_path: Path, value: str) -> None:
    """resume.sh and restore_auto.sh pass the id; a person passes the name."""
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=value))
    assert res.returncode == 0, res.stderr
    assert "Multiple repositories" not in res.stderr
    assert f"repo={tmp_path}/meta/metadata/{PHOTOS_ID} " in _runs(tmp_path)[0]


def test_a_hostile_name_is_printed_and_matched_never_run(tmp_path: Path) -> None:
    """A name is untrusted text off a disc: $( ), backticks, a glob and a
    leading dash are printed as they are and matched literally; control
    characters never reach the terminal."""
    pwned = tmp_path / "pwned"
    hostile = f"-n $(touch {pwned}1) `touch {pwned}2` *"
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, DOCS)
    _set(tmp_path / "meta", PHOTOS_ID, hostile + "\x1b[31m")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="1\n")
    assert res.returncode == 0, res.stderr
    # Listed by name: "-" sorts before "d".
    assert _menu(res.stderr) == [f"1) {hostile}[31m", f"2) {DOCS}"], res.stderr
    assert f"repo={tmp_path}/meta/metadata/{PHOTOS_ID} " in _runs(tmp_path)[0]
    assert "\x1b" not in res.stderr
    assert not list(tmp_path.glob("pwned*")), "a name was run as a command"

    # LCSAS_REPO matches it literally; "*" matches no set.
    res = _run(recovery, tmp_path / "out2",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=hostile + "[31m"))
    assert res.returncode == 0, res.stderr
    assert f"repo={tmp_path}/meta/metadata/{PHOTOS_ID} " in _runs(tmp_path)[-1]
    res = _run(recovery, tmp_path / "out3",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO="*"))
    assert res.returncode == 1
    assert "not among available repositories" in res.stderr
    assert not list(tmp_path.glob("pwned*"))


def test_no_marker_or_a_marker_for_another_folder_shows_the_id(tmp_path: Path) -> None:
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, None)
    photos = _set(tmp_path / "meta", PHOTOS_ID, None)
    (photos / "LCSAS_REPO.txt").write_text(_marker(DOCS_ID, "copied from elsewhere"))
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="1\n")
    assert res.returncode == 0, res.stderr
    assert _menu(res.stderr) == [f"1) {DOCS_ID}", f"2) {PHOTOS_ID}"]
    assert "backup set:" not in res.stderr


def test_a_name_two_sets_share_is_refused(tmp_path: Path) -> None:
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, PHOTOS)
    _set(tmp_path / "meta", PHOTOS_ID, PHOTOS)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin=f"{PHOTOS}\n")
    assert res.returncode == 1
    assert f"More than one backup set is called {PHOTOS}" in res.stderr
    assert not _runs(tmp_path)


def test_the_name_comes_from_another_copy_when_a_disc_has_no_marker(tmp_path: Path) -> None:
    """A data disc made before the marker existed: the set is read from
    it, but its name comes from the META disc's copy."""
    recovery = _two_sets(tmp_path, split="2-of-3")
    disc = _data_disc(tmp_path / "media", "DISC", "LCSAS_TEST_TINY_2026_0001")
    _set(disc, DOCS_ID, None, tag="disc")
    _set(disc, PHOTOS_ID, None, tag="disc")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="2\n")
    assert res.returncode == 0, res.stderr
    assert _menu(res.stderr) == [
        f"1) {DOCS}", f"2) {PHOTOS} (password split: any 2 of 3 share cards)",
    ], res.stderr
    assert f"repo={disc}/metadata/{PHOTOS_ID} " in _runs(tmp_path)[0]


def test_a_wrong_answer_is_asked_again_and_q_quits(tmp_path: Path) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_INTERACTIVE="on"),
               stdin="holidays\nq\n")
    assert res.returncode == 65, res.stderr
    assert "no repository named holidays" in res.stderr
    assert "Type its number, its name or its id" in res.stderr
    assert res.stderr.count("Choose a repository") == 2
    # No answer at all: the input ended.
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="")
    assert res.returncode == 1
    assert "no repository chosen" in res.stderr


# ── F5: the newest data disc's copy (#605, C2) ───────────────────────


@pytest.mark.parametrize(("older", "newer"), [
    ("LCSAS_TEST_TINY_2026_0001", "LCSAS_TEST_TINY_2026_0002"),
    ("LCSAS_CD700_2025_0009", "LCSAS_CD700_2026_0001"),
])
def test_the_newest_label_wins_not_the_scan_order(tmp_path: Path, older: str, newer: str) -> None:
    """The copy on the disc with the newest label is used, whatever the
    order the discs are found in (the older disc sorts first here), and
    whatever the catalogs' mtimes say; the META copy is the fallback."""
    recovery = _two_sets(tmp_path)
    old = _data_disc(tmp_path / "media", "AAA", older, catalog_mtime=2_000_000_000)
    new = _data_disc(tmp_path / "media", "ZZZ", newer, catalog_mtime=1_000_000_000)
    for disc, tag in ((old, "old"), (new, "new")):
        _set(disc, DOCS_ID, DOCS, tag=tag)
        _set(disc, PHOTOS_ID, PHOTOS, tag=tag)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS))
    assert res.returncode == 0, res.stderr
    assert f"repo={new}/metadata/{DOCS_ID} " in _runs(tmp_path)[0], res.stderr


def test_label_from_volume_info_json_and_unlabelled_discs_rank_lower(tmp_path: Path) -> None:
    recovery = _two_sets(tmp_path)
    plain = _data_disc(tmp_path / "media", "AAA", None, catalog_mtime=2_000_000_000)
    labelled = _data_disc(tmp_path / "media", "BBB", "LCSAS_TEST_TINY_2026_0001",
                          json_label=True)
    _set(plain, DOCS_ID, DOCS, tag="plain")
    _set(labelled, DOCS_ID, DOCS, tag="labelled")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS))
    assert res.returncode == 0, res.stderr
    assert f"repo={labelled}/metadata/{DOCS_ID} " in _runs(tmp_path)[0]


def test_sets_only_on_the_meta_disc_or_only_on_a_data_disc_are_offered(tmp_path: Path) -> None:
    """A set made after the META disc is on the data discs only; a set on
    the META disc only keeps its META copy (the fallback)."""
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, DOCS)
    disc = _data_disc(tmp_path / "media", "DISC", "LCSAS_TEST_TINY_2026_0003")
    _set(disc, LATE_ID, "added later", tag="disc")
    # A META disc found under the mount dirs (not the one we run from)
    # ranks below every data disc.
    other_meta = tmp_path / "media" / "OLDMETA"
    _set(other_meta, LATE_ID, "added later", tag="oldmeta")
    (other_meta / "volume_info.json").write_text('{"type": "meta"}\n')
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="1\n")
    assert res.returncode == 0, res.stderr
    # Best copy first: the data disc's sets, then the META-only ones.
    assert _menu(res.stderr) == ["1) added later", f"2) {DOCS}"], res.stderr
    assert f"repo={disc}/metadata/{LATE_ID} " in _runs(tmp_path)[0]
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW), stdin="2\n")
    assert f"repo={tmp_path}/meta/metadata/{DOCS_ID} " in _runs(tmp_path)[-1]


def test_the_tools_copy_from_a_newer_disc_beats_an_older_disc(tmp_path: Path) -> None:
    """The next set (or a resumed run) with an older disc in the drive
    uses the tools folder's copy, refreshed from the newest disc."""
    recovery = _two_sets(tmp_path)
    tools = tmp_path / "tools"
    copy = _set(tools, DOCS_ID, DOCS, tag="tools")
    (tools / ".lcsas-restore-tools").write_text("")
    (tools / "metadata" / ".from").mkdir()
    (tools / "metadata" / ".from" / DOCS_ID).write_text("LCSAS_TEST_TINY_2026_0002\n")
    old = _data_disc(tmp_path / "media", "OLD", "LCSAS_TEST_TINY_2026_0001")
    _set(old, DOCS_ID, DOCS, tag="old")
    _set(old, PHOTOS_ID, PHOTOS, tag="old")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS, STUB_RC="1"))
    assert res.returncode == 1, res.stderr
    assert f"repo={copy} " in _runs(tmp_path)[0], res.stderr
    # ...and the older disc's copy never replaces it.
    assert (copy / "index" / "index-tools").is_file()
    assert not (copy / "index" / "index-old").exists()
    # Without the tag (a META copy), the older disc's copy is better.
    (tools / "metadata" / ".from" / DOCS_ID).unlink()
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS, STUB_RC="1"))
    assert f"repo={old}/metadata/{DOCS_ID} " in _runs(tmp_path)[-1], res.stderr


def _single_drive(tmp_path: Path) -> tuple[Path, Path]:
    """The META disc as relocated: LCSAS_RELOCATED names it, its sentinel
    says it is in the drive, and no data disc is mounted yet."""
    meta = tmp_path / "meta"
    recovery = _meta(meta)
    _set(meta, DOCS_ID, DOCS)
    _set(meta, PHOTOS_ID, PHOTOS)
    return meta, recovery


def test_the_newest_disc_step_waits_for_a_labelled_data_disc(tmp_path: Path) -> None:
    """C7: the box, then ENTER rescans: the META disc still in, nothing
    mounted, a disc with no label, and at last the newest data disc,
    whose full label is printed.  Its copy of every set it holds
    replaces the META disc's index/ and snapshots/ in the tools folder,
    keys/ are added, and a set it does not hold keeps the META copy."""
    meta, recovery = _single_drive(tmp_path)
    media = tmp_path / "media"
    env = _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_RELOCATED=str(meta),
               LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS, STUB_RC="1")
    s = _Session([str(RESTORE_SH), str(recovery), str(tmp_path / "out"), "latest"], env)
    s.expect("put in your NEWEST data disc")
    s.expect("ENTER, S or Q: ")
    s.send("\n")
    s.expect("The META disc is still in the drive")
    (meta / "recovery" / "scripts" / "restore.sh").unlink()   # ejected
    s.expect("ENTER, S or Q: ")
    s.send("\n")
    s.expect("No data disc found yet")
    plain = media / "PLAIN"
    _set(plain, DOCS_ID, DOCS, tag="plain")
    s.expect("ENTER, S or Q: ")
    s.send("\n")
    s.expect("is not an LCSAS data disc")
    shutil.rmtree(plain)
    old = _data_disc(media, "LCSAS_TEST_TINY_2026", "LCSAS_TEST_TINY_2026_0001")
    _set(old, DOCS_ID, DOCS, tag="old")
    new = _data_disc(media, "LCSAS_TEST_TINY_20261", "LCSAS_TEST_TINY_2026_0002")
    _set(new, DOCS_ID, DOCS, tag="new")
    s.expect("ENTER, S or Q: ")
    s.send("\n")
    s.expect("Found the data disc labelled LCSAS_TEST_TINY_2026_0002")
    s.expect("copying the backup sets' metadata from the disc labelled")
    rc = s.finish()
    assert rc == 1, s.text
    assert f"repo={new}/metadata/{DOCS_ID} " in _runs(tmp_path)[0], s.text

    copied = tmp_path / "tools" / "metadata"
    assert sorted(p.name for p in (copied / DOCS_ID / "index").iterdir()) == ["index-new"]
    assert sorted(p.name for p in (copied / DOCS_ID / "snapshots").iterdir()) == ["snapshots-new"]
    assert sorted(p.name for p in (copied / DOCS_ID / "keys").iterdir()) == ["keys-new"]
    assert (copied / ".from" / DOCS_ID).read_text().strip() == "LCSAS_TEST_TINY_2026_0002"
    assert not (copied / PHOTOS_ID).exists(), "no data disc holds that set"
    assert "LCSAS_NEWEST_DISC_PROMPT=0" in (tmp_path / "tools" / "resume.sh").read_text()


def test_the_merge_unions_keys_and_keeps_meta_only_sets(tmp_path: Path) -> None:
    """Relocated, the tools folder is META_ROOT: it holds the META disc's
    copies, and the newest disc refreshes the sets it holds."""
    tools = tmp_path / "tools"
    recovery = _meta(tools)
    (tools / ".lcsas-restore-tools").write_text("")
    _set(tools, DOCS_ID, DOCS, tag="meta")
    _set(tools, PHOTOS_ID, PHOTOS, tag="meta")
    new = _data_disc(tmp_path / "media", "NEW", "LCSAS_TEST_TINY_2026_0002")
    _set(new, DOCS_ID, DOCS, tag="new")
    (tools / "recovery" / "scripts" / "restore.sh").unlink()
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_RELOCATED=str(tmp_path / "gone-meta"),
                    LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=PHOTOS, STUB_RC="1"))
    assert res.returncode == 1, res.stderr
    docs = tools / "metadata" / DOCS_ID
    assert sorted(p.name for p in (docs / "keys").iterdir()) == ["keys-meta", "keys-new"]
    assert sorted(p.name for p in (docs / "index").iterdir()) == ["index-new"]
    photos = tools / "metadata" / PHOTOS_ID
    assert sorted(p.name for p in (photos / "index").iterdir()) == ["index-meta"]
    assert f"repo={photos} " in _runs(tmp_path)[0]
    # A second run with the same disc copies nothing again.
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_RELOCATED=str(tmp_path / "gone-meta"),
                    LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=PHOTOS, STUB_RC="1"))
    assert "copying the backup sets' metadata" not in res.stderr


@pytest.mark.parametrize(("answer", "rc", "said"), [
    ("s\n", 0, "Skipped: using the copies on the META disc."),
    ("S\n", 0, "Skipped: using the copies on the META disc."),
    ("q\n", 65, "Stopped at your request."),
    ("later\nQ\n", 65, "Press ENTER once the disc is in, or type S or Q."),
    ("", 65, "Stopped at your request."),
])
def test_the_newest_disc_step_skip_and_quit(
    tmp_path: Path, answer: str, rc: int, said: str,
) -> None:
    meta, recovery = _single_drive(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_RELOCATED=str(meta),
                    LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS_ID),
               stdin=answer)
    assert res.returncode == rc, res.stderr
    assert said in res.stderr
    if rc == 0:
        assert f"repo={meta}/metadata/{DOCS_ID} " in _runs(tmp_path)[0]
    else:
        assert not _runs(tmp_path)


@pytest.mark.parametrize("case", ["pipe", "off", "prompt-off", "disc-in", "no-meta"])
def test_the_newest_disc_step_is_asked_only_when_it_can_be(tmp_path: Path, case: str) -> None:
    """Not for a pipe (the blind harness's restore_auto.sh, automation),
    not with LCSAS_INTERACTIVE=off or LCSAS_NEWEST_DISC_PROMPT=0, not
    when a data disc is already mounted, and not without a META disc."""
    meta, recovery = _single_drive(tmp_path)
    extra = {"LCSAS_INTERACTIVE": "on", "LCSAS_RELOCATED": str(meta)}
    if case == "pipe":
        del extra["LCSAS_INTERACTIVE"]
    elif case == "off":
        extra["LCSAS_INTERACTIVE"] = "off"
    elif case == "prompt-off":
        extra["LCSAS_NEWEST_DISC_PROMPT"] = "0"
    elif case == "disc-in":
        _set(_data_disc(tmp_path / "media", "D", "LCSAS_TEST_TINY_2026_0001"),
             DOCS_ID, DOCS, tag="disc")
    else:
        del extra["LCSAS_RELOCATED"]
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS_ID, **extra))
    assert res.returncode == 0, res.stderr
    assert "NEWEST data disc" not in res.stderr


def _pty_run(argv: list[str], env: dict[str, str], steps: list[tuple[str, bytes]],
             ) -> tuple[int, str, int]:
    """Run on a pty that is the child's controlling terminal; for each
    (needle, data) wait for needle, then write data (b"\\x03" is
    Ctrl-C).  Returns (rc, output, the terminal's lflag at the end)."""
    import fcntl
    import pty
    import select

    interp, env = _traced(env)
    master, slave = pty.openpty()

    def _ctty() -> None:  # in the child, after setsid
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([interp, *argv], stdin=slave, stdout=slave, stderr=slave,
                            env=env, start_new_session=True, preexec_fn=_ctty)
    out = bytearray()
    deadline = time.monotonic() + _scaled_timeout(30)

    def pump() -> None:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            with contextlib.suppress(OSError):
                out.extend(os.read(master, 4096))

    try:
        for needle, data in steps:
            while needle.encode() not in out:
                exited = proc.poll() is not None
                pump()
                if needle.encode() in out:
                    break
                assert not exited and time.monotonic() < deadline, (
                    f"never saw {needle!r}:\n{out.decode(errors='replace')}")
            os.write(master, data)
        while proc.poll() is None:
            assert time.monotonic() < deadline, out.decode(errors="replace")
            pump()
        pump()
        lflag = termios.tcgetattr(slave)[3]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=_scaled_timeout(10))
        os.close(master)
        os.close(slave)
    return proc.returncode, out.decode(errors="replace"), lflag


def test_on_a_terminal_the_step_is_asked_and_the_password_is_hidden(tmp_path: Path) -> None:
    """A real terminal (LCSAS_INTERACTIVE unset = auto): the newest disc
    step is asked; Ctrl-C at the password prompt exits 130 with the
    terminal's echo back on; a typed password is not echoed and is
    checked before tier 1 runs."""
    meta, recovery = _single_drive(tmp_path)
    argv = [str(RESTORE_SH), str(recovery), str(tmp_path / "out"), "latest"]
    rc, out, lflag = _pty_run(
        argv, _env(tmp_path, LCSAS_RELOCATED=str(meta), LCSAS_REPO=DOCS),
        [("ENTER, S or Q: ", b"s\n"), ("Password: ", b"\x03")],
    )
    assert rc == 130, out
    assert "put in your NEWEST data disc" in out
    assert lflag & termios.ECHO, "Ctrl-C at the password prompt left echo off"

    rc, out, lflag = _pty_run(
        argv, _env(tmp_path, LCSAS_RELOCATED=str(meta), LCSAS_REPO=DOCS,
                   LCSAS_NEWEST_DISC_PROMPT="0", LCSAS_ANOTHER_SET="0"),
        [("Password: ", f"{GOOD_PW}\n".encode())],
    )
    assert rc == 0, out
    assert GOOD_PW not in out, "the password was echoed"
    assert lflag & termios.ECHO
    assert len(_checks(tmp_path)) == 1


# ── F7: the password (#608) ──────────────────────────────────────────


def test_an_empty_or_wrong_password_is_asked_again(tmp_path: Path) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=DOCS_ID,
                    LCSAS_ANOTHER_SET="0"),
               stdin=f"\nnot-it\n{GOOD_PW}\n")
    assert res.returncode == 0, res.stderr
    assert f"Type the password for {DOCS}" in res.stderr
    assert "No password typed." in res.stderr
    assert f"That password was not accepted for {DOCS}." in res.stderr
    assert len(_checks(tmp_path)) == 2
    assert _runs(tmp_path) == [
        f"RUN repo={tmp_path}/meta/metadata/{DOCS_ID} target={tmp_path}/out pw={GOOD_PW}",
    ]


@pytest.mark.parametrize("answer", ["q\n", "Q\n", "", "not-it\n"])
def test_q_or_the_end_of_input_at_the_password_exits_77(tmp_path: Path, answer: str) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=DOCS_ID),
               stdin=answer)
    assert res.returncode == 77, res.stderr
    assert "Stopped at your request; no password was given." in res.stderr
    assert not _runs(tmp_path)


def test_a_piped_password_is_not_checked_first_and_77_is_explained(tmp_path: Path) -> None:
    """Not on a terminal nothing can be asked again: no check run, and a
    wrong password reaches tier 1, which says so (the default path used
    to exit 77 with no word from the script)."""
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_REPO=DOCS_ID), stdin="not-it\n")
    assert res.returncode == 77, res.stderr
    assert not _checks(tmp_path)
    assert "[tier 1] wrong password" in res.stderr


def test_a_password_from_the_environment_is_not_asked_again(tmp_path: Path) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=DOCS_ID,
                    LCSAS_PASSWORD="not-it"), stdin=f"{GOOD_PW}\n")
    assert res.returncode == 77, res.stderr
    assert "Password:" not in res.stderr
    assert not _checks(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text(GOOD_PW + "\n")
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=DOCS_ID,
                    LCSAS_PWFILE=str(pwfile), LCSAS_ANOTHER_SET="0"))
    assert res.returncode == 0, res.stderr
    assert "Password:" not in res.stderr


# ── F6: share cards (#607) ───────────────────────────────────────────


def _split_sets(tmp_path: Path, split_id: str, *, keyshare: bool | str = True) -> Path:
    recovery = _meta(tmp_path / "meta", keyshare=keyshare)
    _set(tmp_path / "meta", DOCS_ID, DOCS)
    _set(tmp_path / "meta", PHOTOS_ID, PHOTOS, split="2-of-3", split_id=split_id)
    return recovery


@needs_keyshare
def test_typed_share_cards_rebuild_the_password(tmp_path: Path) -> None:
    """Exactly K cards: a line with no words and a mistyped card are
    asked again (the bad word named, not the temporary file), the same
    card twice is caught, and each card sits alone in a mode 600 file in
    a private folder that is gone afterwards."""
    cards, split_id = _shares(GOOD_PW.encode(), 2, 3)
    words = cards[0].split()
    words[3] = "zzzzq"
    mistyped = " ".join(words)
    ks_log = tmp_path / "ks.log"
    recovery = _split_sets(tmp_path, split_id, keyshare=str(ks_log))
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=PHOTOS,
                    LCSAS_ANOTHER_SET="0"),
               stdin=f"   \n{mistyped}\n{cards[0]}\n{cards[0].upper()}\n{cards[2]}\n")
    assert res.returncode == 0, res.stderr
    assert (f"The password for {PHOTOS} is split into 3 share cards;\n"
            f"you need any 2 of them (Split ID {split_id}).") in res.stderr
    assert "No words typed." in res.stderr
    assert "card 1" in res.stderr and "'zzzzq'" in res.stderr
    assert "share." not in res.stderr, "the temporary file was named"
    assert "That is a card you already typed." in res.stderr
    assert res.stderr.count("Share card 1 of 2") == 3
    assert res.stderr.count("Share card 2 of 2") == 2
    assert "Share card 3" not in res.stderr
    assert _runs(tmp_path) == [
        f"RUN repo={tmp_path}/meta/metadata/{PHOTOS_ID} target={tmp_path}/out pw={GOOD_PW}",
    ]
    modes = set(ks_log.read_text().split("\n")) - {""}
    assert modes == {"600 700"}, modes
    assert list((tmp_path / "tmpdir").iterdir()) == [], "share files were left behind"


@needs_keyshare
def test_cards_from_two_splits_or_another_set_start_again(tmp_path: Path) -> None:
    good, split_id = _shares(GOOD_PW.encode(), 2, 3)
    other, _ = _shares(GOOD_PW.encode(), 2, 3)
    wrong, _ = _shares(b"the-password-of-another-set", 2, 3)
    recovery = _split_sets(tmp_path, split_id)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=PHOTOS_ID,
                    LCSAS_ANOTHER_SET="0"),
               stdin=(f"{good[0]}\n{other[1]}\n"      # two splits
                      f"{wrong[0]}\n{wrong[1]}\n"     # another set's password
                      f"{good[1]}\n{good[2]}\n"))
    assert res.returncode == 0, res.stderr
    assert "These cards could not be combined" in res.stderr
    assert (f"The password rebuilt from these cards was not accepted for {PHOTOS}."
            in res.stderr)
    assert res.stderr.count("Start again with card 1") == 2
    assert _runs(tmp_path)[-1].endswith(f"pw={GOOD_PW}")


@needs_keyshare
@pytest.mark.parametrize(("answer", "rc"), [
    ("P\n" + GOOD_PW + "\n", 0), ("p\n" + GOOD_PW + "\n", 0),
    ("Q\n", 77), ("", 77),
])
def test_p_types_the_password_and_q_quits(tmp_path: Path, answer: str, rc: int) -> None:
    _, split_id = _shares(GOOD_PW.encode(), 2, 3)
    recovery = _split_sets(tmp_path, split_id)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=PHOTOS,
                    LCSAS_ANOTHER_SET="0"), stdin=answer)
    assert res.returncode == rc, res.stderr
    if rc == 0:
        assert "Password: " in res.stderr
        assert _runs(tmp_path)[0].endswith(f"pw={GOOD_PW}")
    else:
        assert not _runs(tmp_path)


@needs_keyshare
def test_lcsas_shares_dir_rebuilds_the_password_with_no_questions(tmp_path: Path) -> None:
    """Printed cards (prose around the words), more than K of them."""
    cards, split_id = _shares(GOOD_PW.encode(), 2, 3)
    shares = tmp_path / "cards"
    shares.mkdir()
    for i, card in enumerate(cards, 1):
        (shares / f"photos-share-{i}-card.txt").write_text(_card(card))
    recovery = _split_sets(tmp_path, split_id)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_REPO=PHOTOS, LCSAS_SHARES_DIR=str(shares)))
    assert res.returncode == 0, res.stderr
    assert "Share card" not in res.stderr and "Password:" not in res.stderr
    assert "from the 3 card files in" in res.stderr
    assert _runs(tmp_path)[0].endswith(f"pw={GOOD_PW}")


@needs_keyshare
@pytest.mark.parametrize("case", ["missing", "empty", "one-card"])
def test_lcsas_shares_dir_that_cannot_rebuild_exits_77(tmp_path: Path, case: str) -> None:
    cards, split_id = _shares(GOOD_PW.encode(), 2, 3)
    shares = tmp_path / "cards"
    if case != "missing":
        shares.mkdir()
    if case == "one-card":
        (shares / "card1.txt").write_text(_card(cards[0]))
    recovery = _split_sets(tmp_path, split_id)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_REPO=PHOTOS, LCSAS_SHARES_DIR=str(shares)))
    assert res.returncode == 77, res.stderr
    assert not _runs(tmp_path)


@needs_keyshare
def test_no_private_folder_for_the_cards_asks_for_the_password(tmp_path: Path) -> None:
    """The cards are never written anywhere but a private temporary
    folder: when none can be made, the password is asked for instead."""
    _, split_id = _shares(GOOD_PW.encode(), 2, 3)
    recovery = _split_sets(tmp_path, split_id)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_REPO=PHOTOS,
                    LCSAS_ANOTHER_SET="0", TMPDIR=str(tmp_path / "no-such-dir")),
               stdin=f"{GOOD_PW}\n")
    assert res.returncode == 0, res.stderr
    assert "Cannot make a private temporary folder" in res.stderr
    assert "Share card 1 of 2" not in res.stderr
    assert _runs(tmp_path)[0].endswith(f"pw={GOOD_PW}")


def test_a_split_set_with_no_combiner_asks_for_the_password(tmp_path: Path) -> None:
    recovery = _split_sets(tmp_path, "01234", keyshare=False)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_REPO=PHOTOS, LCSAS_ANOTHER_SET="0"),
               stdin=f"{GOOD_PW}\n")
    assert res.returncode == 0, res.stderr
    assert "the share\ncombiner (lcsas-keyshare) is missing here" in res.stderr
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_REPO=PHOTOS, LCSAS_SHARES_DIR=str(tmp_path)))
    assert res.returncode == 77
    assert "there is no share combiner here" in res.stderr


# ── Another backup set ───────────────────────────────────────────────


def test_another_set_restores_the_next_set_then_removes_the_tools(tmp_path: Path) -> None:
    """Yes: the script starts again at the set list (the same tools
    folder, no META disc, the password asked again) into the folder
    given ("~/" is the home folder); no: the tools folder goes."""
    recovery = _two_sets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_INTERACTIVE="on", HOME=str(home)),
               stdin=f"1\n{GOOD_PW}\ny\nsecond\n~/second\n2\n{GOOD_PW}\nn\n")
    assert res.returncode == 0, res.stderr
    assert res.stderr.count("Restore another backup set from these discs? [y/N]") == 2
    assert "Type the full path of the folder" in res.stderr
    assert "starting again for the next backup set" in res.stderr
    assert _runs(tmp_path) == [
        f"RUN repo={tmp_path}/meta/metadata/{DOCS_ID} target={tmp_path}/out pw={GOOD_PW}",
        f"RUN repo={tmp_path}/meta/metadata/{PHOTOS_ID} target={home}/second pw={GOOD_PW}",
    ]
    assert not tools.exists(), "the tools folder must go after the last set"
    assert list((tmp_path / "tmpdir").iterdir()) == []


@pytest.mark.parametrize("folder", ["", "~"])
def test_another_set_enter_keeps_the_folder_and_tilde_is_home(tmp_path: Path, folder: str) -> None:
    recovery = _two_sets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    res = _run(recovery, tmp_path / "out",
               # The home folder holds the session log by then.
               _env(tmp_path, LCSAS_INTERACTIVE="on", HOME=str(home),
                    LCSAS_FORCE_NONEMPTY_TARGET="1"),
               stdin=f"1\n{GOOD_PW}\ny\n{folder}\n2\n{GOOD_PW}\nn\n")
    assert res.returncode == 0, res.stderr
    second = tmp_path / "out" if folder == "" else home
    assert _runs(tmp_path)[1] == (
        f"RUN repo={tmp_path}/meta/metadata/{PHOTOS_ID} target={second} pw={GOOD_PW}")


@pytest.mark.parametrize(("extra", "answer"), [
    ({"LCSAS_INTERACTIVE": "on"}, "n\n"),
    ({"LCSAS_INTERACTIVE": "on"}, "\n"),
    ({"LCSAS_INTERACTIVE": "on"}, ""),
    ({"LCSAS_INTERACTIVE": "on", "LCSAS_ANOTHER_SET": "0"}, "y\n"),
    ({}, "y\n"),
])
def test_another_set_no_or_not_asked(tmp_path: Path, extra: dict[str, str], answer: str) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=DOCS, **extra),
               stdin=answer)
    assert res.returncode == 0, res.stderr
    asked = "Restore another backup set" in res.stderr
    assert asked == (extra == {"LCSAS_INTERACTIVE": "on"}), res.stderr
    assert len(_runs(tmp_path)) == 1
    assert not (tmp_path / "tools").exists()


def test_another_set_is_not_asked_with_one_set(tmp_path: Path) -> None:
    recovery = _meta(tmp_path / "meta")
    _set(tmp_path / "meta", DOCS_ID, DOCS)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_INTERACTIVE="on"),
               stdin="y\n")
    assert res.returncode == 0, res.stderr
    assert "Restore another backup set" not in res.stderr


def test_restore_auto_turns_the_questions_off(tmp_path: Path) -> None:
    """restore_auto.sh restores every set itself: even with
    LCSAS_INTERACTIVE=on and a META disc named, its restore.sh runs get
    no newest-disc step and no "another set?" question."""
    meta = tmp_path / "meta"
    recovery = _meta(meta)
    (recovery / "scripts" / "restore.sh").write_text(RESTORE_SH.read_text())
    for set_id, name in ((DOCS_ID, DOCS), (PHOTOS_ID, PHOTOS)):
        _set(meta, set_id, name)
        (recovery / "repos" / set_id / "keys").mkdir(parents=True)  # its list
    pwfile = tmp_path / "pw"
    pwfile.write_text(GOOD_PW + "\n")
    env = _env(tmp_path, LCSAS_INTERACTIVE="on", LCSAS_PWFILE=str(pwfile),
               LCSAS_RELOCATED=str(meta))
    res = subprocess.run(
        ["sh", str(RESTORE_AUTO_SH), str(recovery), str(tmp_path / "out")],
        input="y\n", capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "Restore another backup set" not in res.stderr
    assert "NEWEST data disc" not in res.stderr
    assert sorted(_runs(tmp_path)) == [
        f"RUN repo={meta}/metadata/{DOCS_ID} target={tmp_path}/out/{DOCS_ID} pw={GOOD_PW}",
        f"RUN repo={meta}/metadata/{PHOTOS_ID} target={tmp_path}/out/{PHOTOS_ID} pw={GOOD_PW}",
    ]


def test_resume_sh_skips_the_step_and_names_the_set_by_id(tmp_path: Path) -> None:
    recovery = _two_sets(tmp_path)
    res = _run(recovery, tmp_path / "out",
               _env(tmp_path, LCSAS_PASSWORD=GOOD_PW, LCSAS_REPO=PHOTOS,
                    LCSAS_ANOTHER_SET="0", STUB_RC="1"))
    assert res.returncode == 1, res.stderr
    text = (tmp_path / "tools" / "resume.sh").read_text()
    assert f"LCSAS_REPO='{PHOTOS_ID}'" in text
    assert "LCSAS_NEWEST_DISC_PROMPT=0; export LCSAS_NEWEST_DISC_PROMPT" in text
    assert "LCSAS_ANOTHER_SET='0'; export LCSAS_ANOTHER_SET" in text
    assert stat.S_IMODE((tmp_path / "tools" / "resume.sh").stat().st_mode) == 0o700


