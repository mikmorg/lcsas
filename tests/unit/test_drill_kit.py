"""The release-drill kit (tests/e2e/drill_kit): the parts that need no tools.

Pins that the corpus is deterministic for a seed and holds the items
restores get wrong (#834), that the Windows profile holds only names NTFS
can store, that the drill config splits only ``photos`` (2-of-3, in its own
table), that the kit refuses a workdir inside the public checkout, and that
verify_restore.py finds each set however deep the restore nested it and
catches a missing, changed, mistyped, re-moded, re-linked, unlinked or
out-of-date item.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.config.settings import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT = REPO_ROOT / "tests" / "e2e" / "drill_kit"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"drill_{name}", KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kit = _load("build_kit")
corpus = kit.corpus          # the module build_kit uses (tests/e2e/drill_kit/corpus.py)

_SMALL = (("photos", 2, 2, 1024), ("documents", 2, 1, 2048))
_SMALL_ADDED = (("photos", 1, 512), ("documents", 1, 256))
_SMALL_BIG = ("photos", "videos/home movie (test data).bin", 4096)


@pytest.fixture
def small(monkeypatch) -> None:
    monkeypatch.setattr(corpus, "CORPUS", _SMALL)
    monkeypatch.setattr(corpus, "ADDED", _SMALL_ADDED)
    monkeypatch.setattr(corpus, "BIG_FILE", _SMALL_BIG)


def _rm(path: Path) -> None:
    """Remove a tree that has a read-only folder in it."""
    for p in [path, *path.rglob("*")]:
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o755)
    shutil.rmtree(path)


def _both_states(root: Path, seed: str, profile: str):
    corpus.build_corpus(root, seed, profile)
    first = corpus.scan_sets(root)
    corpus.change_corpus(root, seed)
    return first, corpus.make_manifest(root, first, profile=profile, seed=seed)


@pytest.fixture
def tree(tmp_path: Path, small):
    """A folder for a corpus, cleaned up even with its read-only folder."""
    root = tmp_path / "corpus"
    yield root
    if root.exists():
        _rm(root)


# --- the corpus -------------------------------------------------------------


def test_corpus_is_deterministic_for_a_seed(tree: Path) -> None:
    _, a = _both_states(tree / "a", "seed-1", "posix")
    _, b = _both_states(tree / "b", "seed-1", "posix")
    _, c = _both_states(tree / "c", "seed-2", "posix")
    assert a["sets"] == b["sets"]
    assert a["sets"] != c["sets"]
    assert sorted(a["sets"]) == ["documents", "photos"]
    assert b"TEST DATA" in (tree / "a" / "photos" / "album-notes-01.txt").read_bytes()


def test_full_corpus_is_one_cd700_disc_per_session() -> None:
    """Two backups, two burn sessions, one CD700 data disc each: two data
    discs in all, as the owner decided for the drill archive."""
    from lcsas.config.media import MediaType

    per_disc = MediaType.CD700.usable_bytes - 100 * 1_048_576  # default reserve
    first = sum(n * size for _, _, n, size in corpus.CORPUS) + corpus.BIG_FILE[2]
    added = sum(n * size for _, n, size in corpus.ADDED)
    margin = 32 * 1_048_576                  # text files, pack headers, slack
    assert per_disc / 2 < first < per_disc - margin
    assert 50 * 1_048_576 < added < per_disc - margin
    assert corpus.BIG_FILE[2] > 100 * 1_000_000          # #834: above 100 MB


def test_corpus_holds_each_kind_of_item(tree: Path) -> None:
    first, manifest = _both_states(tree, "s", "windows")
    for name, s in manifest["sets"].items():
        entries = s["entries"]
        stem = "album-notes" if name == "photos" else "letter"
        assert {e["type"] for e in entries.values()} == {"file", "dir"}, name
        assert entries["empty file.txt"]["size"] == 0
        assert entries["empty folder/also empty"]["type"] == "dir"
        assert not any(p.startswith("empty folder/also empty/") for p in entries)
        assert entries["read-only folder"]["mode"] == 0o555
        assert entries["read-only folder/read-only file.txt"]["mode"] == 0o444
        assert entries["private (mode 600).txt"]["mode"] == 0o640
        assert max(p.count("/") for p in entries) >= 20       # the deep tree
        pair = [p for p, e in entries.items() if "hardlink" in e]
        assert sorted(pair) == ["hardlinks/original.txt",
                                "hardlinks/second name for the same file.txt"]
        assert any(not p.isascii() for p in entries)
        # The second backup edits, deletes, renames and adds.
        assert sorted(s["absent"]) == [f"{stem}-02.txt", "renamed/old name.txt"]
        edited = f"{stem}-01.txt"
        assert entries[edited]["sha256"] != first[name][edited]["sha256"]
        assert "added after the first backup.txt" in entries
        assert "renamed/new name.txt" in entries
    assert corpus.BIG_FILE[1] in manifest["sets"]["photos"]["entries"]


def test_windows_profile_names_are_ntfs_safe(tree: Path) -> None:
    """#834: the sets the Windows drills restore (both of them) hold only
    names NTFS can store, and no path too long for Windows tier 1 (#619)."""
    first, manifest = _both_states(tree, "s", "windows")
    checked = 0
    for name, s in manifest["sets"].items():
        paths = set(s["entries"]) | set(first[name])
        for rel in paths:
            for part in rel.split("/"):
                assert corpus.is_ntfs_safe(part), (name, rel)
                checked += 1
            assert len(f"{name}/{rel}") <= corpus.WINDOWS_MAX_SET_PATH, rel
        # No two names in one folder that differ only by case.
        folded: dict[tuple[str, str], str] = {}
        for rel in paths:
            parent, _, leaf = rel.rpartition("/")
            key = (parent.casefold(), leaf.casefold())
            assert folded.setdefault(key, rel) == rel, (rel, folded[key])
    assert checked > 100
    longest = max(len(f"photos/{p}") for p in manifest["sets"]["photos"]["entries"])
    assert longest > corpus.WINDOWS_MAX_SET_PATH - 40   # it is a long path


def test_posix_profile_holds_what_windows_cannot(tree: Path) -> None:
    _, manifest = _both_states(tree, "s", "posix")
    for name, s in manifest["sets"].items():
        entries = s["entries"]
        names = {os.fsencode(p.rpartition("/")[2]) for p in entries}
        for raw in corpus.POSIX_NAMES:
            assert raw in names, raw
            # case.txt and CASE.txt are each fine; NTFS cannot hold both.
            if raw.lower() != b"case.txt":
                assert not corpus.is_ntfs_safe(os.fsdecode(raw)), raw
        assert {b"case.txt", b"CASE.txt"} <= names
        assert corpus.NON_UTF8_NAME in names
        links = {p: e["target"] for p, e in entries.items() if e["type"] == "symlink"}
        assert links["links/a/up"] == "../b/f"                # up and across
        assert links["links/absolute"].startswith("/")
        assert not (tree / name / "links" / links["links/dangling"]).exists()
        assert max(len(f"{name}/{p}") for p in entries) > corpus.POSIX_MIN_LONG_PATH
    # The manifest keeps a byte that is not UTF-8 through JSON.
    assert json.loads(json.dumps(manifest, ensure_ascii=True)) == manifest


@pytest.mark.parametrize("name", ["a.txt", "café ☀.txt", "it's & 100%.txt",
                                  "CONSOLE.txt", "x.CON", "日本語"])
def test_is_ntfs_safe_accepts(name: str) -> None:
    assert corpus.is_ntfs_safe(name)


@pytest.mark.parametrize("name", ["", ".", "..", "a:b", 'a"b', "a\\b", "a|b",
                                  "a*b", "a?b", "a<b", "a>b", "tab\there",
                                  "dot.", "space ", "CON", "con.txt", "NUL",
                                  "COM1.log", "lpt9", "caf\udce9"])
def test_is_ntfs_safe_refuses(name: str) -> None:
    assert not corpus.is_ntfs_safe(name)


def test_scan_tree_reads_what_is_on_disk(tmp_path: Path) -> None:
    """The manifest comes from the tree (lstat; links not followed)."""
    d = tmp_path / "set"
    (d / "sub").mkdir(parents=True)
    (d / "f").write_bytes(b"x")
    (d / "f").chmod(0o600)
    (d / "sub").chmod(0o700)
    os.link(d / "f", d / "g")
    (d / "l").symlink_to("sub")
    entries = corpus.scan_tree(d)
    assert entries["f"]["mode"] == 0o600
    assert entries["f"]["hardlink"] == entries["g"]["hardlink"]
    assert entries["sub"] == {"type": "dir", "mode": 0o700}
    assert entries["l"] == {"type": "symlink", "target": "sub"}


# --- the kit ----------------------------------------------------------------


def test_drill_config_splits_only_photos(tmp_path: Path) -> None:
    config = load_config(kit.write_config(tmp_path))
    assert config.repo_split("photos") == (True, 2, 3)
    assert config.repo_split("documents")[0] is False
    assert config.key_split is False
    assert config.label_prefix == "LCSAS"
    assert config.default_media_type.name == "CD700"


def test_refuses_a_workdir_inside_the_checkout(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(KIT / "build_kit.py"),
         "--workdir", str(REPO_ROOT / "build" / "drill-kit-should-not-exist")],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "outside the checkout" in proc.stderr
    assert not (REPO_ROOT / "build" / "drill-kit-should-not-exist").exists()


# --- verify_restore.py --------------------------------------------------------


@pytest.fixture
def restored(tmp_path: Path, tree: Path):
    """(manifest, restored folder): the posix corpus copied with cp -a
    (modes, links and hardlinks kept), nested deeper than the manifest."""
    _, manifest = _both_states(tree, "s", "posix")
    corpus.write_manifest(tmp_path / "manifest.json", manifest)
    target = tmp_path / "restored" / "snapshot"
    target.mkdir(parents=True)
    for name in manifest["sets"]:
        subprocess.run(["cp", "-a", str(tree / name), str(target / name)],
                       check=True)
    yield tmp_path / "manifest.json", tmp_path / "restored"
    _rm(tmp_path / "restored")


def _count(manifest: Path, repo: str | None = None) -> int:
    sets = json.loads(manifest.read_text())["sets"]
    return sum(len(s["entries"]) + len(s["absent"])
               for n, s in sets.items() if repo in (None, n))


def _verify(manifest: Path, restored: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(KIT / "verify_restore.py"), str(manifest),
         str(restored), *extra],
        capture_output=True, text=True,
    )


def test_verify_restore_passes_a_nested_restore(restored) -> None:
    manifest, root = restored
    proc = _verify(manifest, root)
    assert proc.returncode == 0, proc.stdout
    n = _count(manifest)
    assert f"{n}/{n} items restored intact" in proc.stdout


def test_verify_restore_catches_missing_and_changed(restored) -> None:
    manifest, root = restored
    (root / "snapshot" / "photos" / "scan-002.dat").unlink()
    (root / "snapshot" / "documents" / "letter-01.txt").write_text("changed")
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    assert "MISSING  photos/scan-002.dat" in proc.stdout
    assert "DIFFERS  documents/letter-01.txt" in proc.stdout
    one = _verify(manifest, root, "--repo", "photos")
    assert "MISSING  photos/scan-002.dat" in one.stdout
    assert "documents/" not in one.stdout


def test_verify_restore_lists_extra_files_without_failing(restored) -> None:
    manifest, root = restored
    (root / ".lcsas-restore-marker").write_text("marker")
    (root / "snapshot" / "photos" / "stray.dat").write_text("stray")
    proc = _verify(manifest, root)
    assert proc.returncode == 0, proc.stdout
    assert "EXTRA    .lcsas-restore-marker" in proc.stdout
    assert "EXTRA    snapshot/photos/stray.dat" in proc.stdout
    n = _count(manifest)
    assert f"{n}/{n} items restored intact" in proc.stdout


@pytest.mark.parametrize("case", ["missing", "empty", "wrong_repo",
                                  "wrong_folder", "empty_manifest",
                                  "old_manifest"])
def test_verify_restore_refuses_when_nothing_is_compared(
    restored, tmp_path: Path, case: str,
) -> None:
    """#833: a mistyped or empty folder, or a manifest/--repo that selects
    nothing, used to print "0/0 files restored intact" and exit 0."""
    manifest, root = restored
    extra: tuple[str, ...] = ()
    if case == "missing":
        root = tmp_path / "no-such-folder"
    elif case == "empty":
        root = tmp_path / "empty"
        root.mkdir()
    elif case == "wrong_repo":
        extra = ("--repo", "nosuchrepo")
    elif case == "wrong_folder":
        root = tmp_path / "other"
        (root / "a").mkdir(parents=True)
        (root / "a" / "unrelated.txt").write_text("x")
    elif case == "empty_manifest":
        manifest = tmp_path / "empty.json"
        manifest.write_text(json.dumps({"format": corpus.MANIFEST_FORMAT,
                                        "sets": {"photos": {"entries": {}}}}))
    elif case == "old_manifest":
        # A kit built before #834 wrote "<sha256>  <repo>/<file>" lines.
        manifest = tmp_path / "manifest.sha256"
        manifest.write_text(f"{'0' * 64}  photos/scan-001.dat\n")
    proc = _verify(manifest, root, *extra)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "ERROR:" in proc.stderr
    assert "not a pass" in proc.stderr
    assert "0/0" not in proc.stdout


def test_verify_restore_counts_every_missing_file(restored) -> None:
    manifest, root = restored
    names = root / "snapshot" / "photos" / "names"
    files = list(names.iterdir())
    for f in files:
        f.unlink()
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    assert proc.stdout.count("MISSING  photos/names/") == len(files)
    n = _count(manifest)
    assert f"{n - len(files)}/{n} items restored intact" in proc.stdout


def test_verify_restore_compares_names_as_bytes(restored) -> None:
    """A name that came back with other bytes is MISSING, and the byte that
    is not UTF-8 is shown as \\xe9."""
    manifest, root = restored
    names = os.fsencode(root / "snapshot" / "photos" / "names")
    os.rename(os.path.join(names, corpus.NON_UTF8_NAME),
              os.path.join(names, b"latin-1 caf\xc3\xa9.txt"))
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    assert "MISSING  photos/names/latin-1 caf\\xe9.txt" in proc.stdout
    assert "EXTRA    snapshot/photos/names/latin-1 café.txt" in proc.stdout


def test_verify_restore_checks_modes_links_and_hardlinks(restored) -> None:
    manifest, root = restored
    photos = root / "snapshot" / "photos"
    (photos / "read-only folder").chmod(0o755)
    (photos / "links" / "a" / "up").unlink()
    (photos / "links" / "a" / "up").symlink_to("../../b/f")
    (photos / "links" / "absolute").unlink()          # as tier 3 skips it
    second = photos / "hardlinks" / "second name for the same file.txt"
    data = second.read_bytes()
    second.unlink()
    second.write_bytes(data)
    second.chmod(0o644)
    (photos / "empty folder" / "also empty").rmdir()
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    out = proc.stdout
    assert "MODE     photos/read-only folder  (0755, expected 0555)" in out
    assert ("LINK     photos/links/a/up  "
            "(points to '../../b/f', expected '../b/f')") in out
    assert "MISSING  photos/links/absolute" in out
    assert "NOT LINKED photos/hardlinks/original.txt" in out
    assert "MISSING  photos/empty folder/also empty" in out

    meta_off = _verify(manifest, root, "--no-metadata")
    assert "MODE" not in meta_off.stdout
    assert "LINK" not in meta_off.stdout
    assert "MISSING  photos/links/absolute" not in meta_off.stdout
    assert "MISSING  photos/empty folder/also empty" in meta_off.stdout
    assert "Not checked (--no-metadata): modes, hardlinks and" in meta_off.stdout


def test_verify_restore_catches_a_wrong_type(restored) -> None:
    manifest, root = restored
    f = root / "snapshot" / "documents" / "empty file.txt"
    f.unlink()
    f.mkdir()
    proc = _verify(manifest, root)
    assert proc.returncode == 1
    assert "TYPE     documents/empty file.txt  (expected a file)" in proc.stdout


def test_verify_restore_fails_a_restore_of_the_older_snapshot(
    tmp_path: Path, tree: Path,
) -> None:
    """The first backup's state, restored, is not "latest": the deleted and
    renamed files are OLD, the edited one DIFFERS."""
    corpus.build_corpus(tree, "s", "windows")
    first = corpus.scan_sets(tree)
    older = tmp_path / "restored"
    older.mkdir()
    subprocess.run(["cp", "-a", str(tree / "photos"), str(older / "photos")],
                   check=True)
    corpus.change_corpus(tree, "s")
    corpus.write_manifest(tmp_path / "m.json", corpus.make_manifest(
        tree, first, profile="windows", seed="s"))
    proc = _verify(tmp_path / "m.json", older, "--repo", "photos")
    _rm(older)
    assert proc.returncode == 1
    out = proc.stdout
    assert "OLD      photos/album-notes-02.txt" in out
    assert "OLD      photos/renamed/old name.txt" in out
    assert "DIFFERS  photos/album-notes-01.txt" in out
    assert "MODE     photos/private (mode 600).txt  (0600, expected 0640)" in out
    assert "MISSING  photos/added after the first backup.txt" in out
