"""The Python reader's disc search, ``DiscSearch`` (#884), tested directly.

Most tier-3 tests drive the search through ``PurePythonRestorer`` or
through its interface (``read_pack_range``, with ``prompt_in`` and
``prompt_out`` in place of stdin and stderr).  The tests here need a
helper of the search itself (the pack-copy helpers, catalog discovery,
the prompt's rendering of each catalog state), so they live in this one
file and nowhere else.  Moved here from ``test_tier3_pack_cache.py`` and
``test_restic_fallback.py``.
"""

from __future__ import annotations

import collections
import errno
import hashlib
import shutil
from pathlib import Path

import pytest

from lcsas.restore import _disc_search as ds
from lcsas.restore import restic_fallback as fb
from lcsas.restore._disc_search import (
    EXIT_READER_QUIT,
    DiscSearch,
    RestoreAbortedError,
)

from .tier3_repo_builder import PASSWORD, build_repo


def _search(
    tmp_path: Path, roots: list[Path] | None = None, **kw: object,
) -> tuple[DiscSearch, list[str]]:
    """A search over *roots* (default: an empty ``repo/data``) whose
    prompt text goes to the returned list."""
    repo_data = tmp_path / "repo" / "data"
    repo_data.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    kw.setdefault("prompt_out", out.append)
    search = DiscSearch(
        roots if roots is not None else [repo_data],
        repo_data=repo_data, log=lambda _msg: None,
        **kw,  # type: ignore[arg-type]
    )
    return search, out


# ── Pack cache helpers (from test_tier3_pack_cache.py) ───────────


def test_drain_fits_keeps_a_margin() -> None:
    gib = 1 << 30
    assert ds._drain_fits(free=10 * gib, total=100 * gib, need=8 * gib)
    assert not ds._drain_fits(free=10 * gib, total=100 * gib, need=9 * gib + 1)
    # 5% of a small drive.
    assert ds._drain_fits(free=200, total=1000, need=150)
    assert not ds._drain_fits(free=200, total=1000, need=151)
    assert not ds._drain_fits(free=100, total=1000, need=101)


def test_a_damaged_pack_is_not_kept_in_the_cache(tmp_path: Path) -> None:
    fs = ds._DiscFs()
    src = tmp_path / "pack"
    src.write_bytes(b"not what the id says")
    dst = tmp_path / "cache" / "data" / "ab" / ("ab" * 32)
    assert fs.copy_checked(src, dst, "ab" * 32, lambda _n: None) == (
        "mismatch", None,
    )
    assert not dst.exists()
    assert not dst.with_name(dst.name + ".part").exists()
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    dst = tmp_path / "cache" / "data" / good[:2] / good
    assert fs.copy_checked(src, dst, good, lambda _n: None) == ("ok", None)
    assert dst.read_bytes() == src.read_bytes()


def test_a_pack_that_cannot_be_copied_is_tried_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One unreadable pack (EIO) and one with bytes that do not match
    its id: each is tried once, the other packs are still copied, and
    the restore completes from the disc."""
    repo = build_repo(
        tmp_path, {f"f{i}.txt": f"file {i}\n".encode() for i in range(6)},
        pack_per_blob=True,
    )
    disc = tmp_path / "disc"
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    packs = sorted(p for p in (disc / "data").rglob("*") if p.is_file())
    unreadable, padded = packs[0].name, packs[1]
    # Bytes after the last blob: every blob still reads, the hash does not.
    with open(padded, "ab") as f:
        f.write(b"\0")

    tries: collections.Counter[str] = collections.Counter()
    real_copy = ds._DiscFs._copy

    def copy(
        fs: ds._DiscFs, src: Path, dst: Path, pack_id: str, tick: object,
    ) -> bool:
        tries[pack_id] += 1
        if pack_id == unreadable:
            raise OSError(errno.EIO, "Input/output error")
        return real_copy(fs, src, dst, pack_id, tick)  # type: ignore[arg-type]

    monkeypatch.setattr(ds._DiscFs, "_copy", copy)
    cache = tmp_path / "cache"
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", disc],
        interactive=False, pack_cache_dir=cache,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    for i in range(6):
        assert (tmp_path / "out" / f"f{i}.txt").read_bytes() == f"file {i}\n".encode()
    assert tries[unreadable] == 1
    assert tries[padded.name] == 1
    cached = {p.name for p in (cache / "data").rglob("*") if p.is_file()}
    assert cached == {p.name for p in packs} - {unreadable, padded.name}
    err = capsys.readouterr().err
    assert err.count("does not match its id") == 1
    assert err.count("Input/output error") == 1


def test_a_cache_that_cannot_be_made_is_off(tmp_path: Path) -> None:
    """The search's side of test_tier3_pack_cache's
    test_a_cache_that_cannot_be_made_is_turned_off: no cache, one log
    line."""
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    logged: list[str] = []
    search = DiscSearch(
        [tmp_path], pack_cache=blocker / "cache",
        repo_data=tmp_path / "data", log=logged.append,
    )
    assert search._pack_cache is None
    assert len(logged) == 1
    assert logged[0].startswith("Cannot create the pack cache")


# ── Catalog discovery (from test_restic_fallback.py) ─────────────


class TestDiscoverCatalogDedup:
    def test_duplicate_search_paths_deduped(self, tmp_path: Path) -> None:
        # The same root listed twice: the second pass's candidates are
        # already in `seen` -> the dedup `continue` fires.  No
        # catalog.db anywhere -> None.
        d = tmp_path / "mnt"
        d.mkdir()
        search, _out = _search(tmp_path, [d, d])
        assert search._discover_catalog() is None

    def test_finds_catalog_in_search_path(self, tmp_path: Path) -> None:
        d = tmp_path / "mnt"
        d.mkdir()
        (d / "catalog.db").write_bytes(b"SQLite")
        search, _out = _search(tmp_path, [d])
        assert search._discover_catalog() == d / "catalog.db"


# ── The prompt's catalog states (from test_restic_fallback.py) ───


class TestSwapPromptCatalogStates:
    def test_hit_but_no_volume_mapping_line(self, tmp_path: Path) -> None:
        # Catalog knows the pack but lists no disc for it ->
        # the dedicated prompt line.
        search, out = _search(tmp_path)
        search._lookup_volume_labels = (  # type: ignore[method-assign]
            lambda pid: ([], search._CATALOG_HIT_NO_VOLS)
        )
        search._print_swap_prompt("a" * 32)
        assert "lists no disc for it" in "".join(out)

    def test_miss_says_how_to_pick_an_older_snapshot(
        self, tmp_path: Path,
    ) -> None:
        # #657 (tier-1 parity): a pack the catalog never saw may belong
        # to a snapshot newer than every disc.
        search, out = _search(tmp_path)
        search._lookup_volume_labels = (  # type: ignore[method-assign]
            lambda pid: ([], search._CATALOG_MISS)
        )
        search._print_swap_prompt("a" * 32)
        box = "".join(out).split("\n")
        i = box.index(
            "| (catalog has no record of this pack ID)                  |"
        )
        assert box[i + 1:i + 5] == [
            "| If the NEWEST data disc has already been read, this pack |",
            "| was never burned: the snapshot is newer than the discs.  |",
            "| Type 'q', then list the snapshots (--list-snapshots)     |",
            "| and restore an older one with --snapshot <id>.           |",
        ]

    def test_hit_does_not_mention_older_snapshots(self, tmp_path: Path) -> None:
        search, out = _search(tmp_path)
        search._lookup_volume_labels = (  # type: ignore[method-assign]
            lambda pid: (["LCSAS_BD25_2026_0001"], "hit")
        )
        search._print_swap_prompt("a" * 32)
        assert "--list-snapshots" not in "".join(out)

    @pytest.mark.parametrize(
        "lookup",
        [
            None,
            ([], "hit-no-vols"),
            ([], "miss"),
            (["LCSAS_CD700_2026_0002", "L" * 56 + "0042"], "hit"),
        ],
    )
    def test_every_box_line_is_60_columns(
        self, tmp_path: Path, lookup: tuple[list[str], str] | None,
    ) -> None:
        # rc6 (#476 drill, F10): the "Pack ... is required" line was 65
        # columns in a 60-column box, the same text as tier 1's.  Every
        # line from the top border to the bottom one must be 60 wide, a
        # long label or path included (cut from the left, keeping its end).
        search, out = _search(tmp_path, [tmp_path / ("m" * 80)])
        search._lookup_volume_labels = (  # type: ignore[method-assign]
            lambda pid: lookup
        )
        search._print_swap_prompt("ab" * 32)
        err = "".join(out)
        box = err[err.index("+---"): err.rindex("+") + 1].split("\n")
        assert len(box) >= 8, err
        for line in box:
            assert len(line) == 60, f"{len(line)} columns: {line!r}"
        assert "| Pack abababababababab... is required for the next file.  |" in box
        if lookup is not None and lookup[0]:
            assert any(line.endswith("LLL0042 |") for line in box), box


# ── The prompt loop, through read_pack_range (from test_restic_fallback.py,
#    TestInteractiveFindPackPath) ──────────────────────────────────


class TestInteractiveReadPackRange:
    def test_eof_on_stdin_gives_up(self, tmp_path: Path) -> None:
        # End of input -> "" -> the whole restore stops (#658: was
        # FileNotFoundError, which failed only this file).
        d = tmp_path / "mnt"
        d.mkdir()
        search, out = _search(tmp_path, [d], interactive=True,
                              prompt_in=lambda: "")
        with pytest.raises(RestoreAbortedError) as ei:
            search.read_pack_range("deadbeef" * 8, 0, 1)
        assert ei.value.exit_code == EXIT_READER_QUIT == 75
        assert not isinstance(ei.value, OSError)
        assert "input ended" in "".join(out)

    def test_q_aborts(self, tmp_path: Path) -> None:
        # 'q' -> abort message + the whole restore stops (#658).
        d = tmp_path / "mnt"
        d.mkdir()
        search, out = _search(tmp_path, [d], interactive=True,
                              prompt_in=lambda: "q\n")
        with pytest.raises(RestoreAbortedError) as ei:
            search.read_pack_range("deadbeef" * 8, 0, 1)
        assert ei.value.exit_code == EXIT_READER_QUIT
        assert "stopped at the disc prompt" in str(ei.value)
        assert "aborted by user" in "".join(out)

    def test_retries_exhausted_still_fails_only_the_file(
        self, tmp_path: Path,
    ) -> None:
        # ENTER every time, never found: FileNotFoundError, a per-file
        # failure, as before.  The number of prompts is bounded.
        d = tmp_path / "mnt"
        d.mkdir()
        search, out = _search(tmp_path, [d], interactive=True,
                              prompt_in=lambda: "\n")
        with pytest.raises(FileNotFoundError, match="Pack file not found"):
            search.read_pack_range("deadbeef" * 8, 0, 1)
        prompts = "".join(out).count("is required for the next file")
        assert prompts == DiscSearch._DISC_SWAP_MAX_RETRIES

    def test_found_after_retry(self, tmp_path: Path) -> None:
        # The first ENTER finds nothing ("still not found"), then the
        # pack appears and its bytes are returned.
        d = tmp_path / "mnt"
        d.mkdir()
        pack_id = "deadbeef" * 8
        presses: list[int] = []

        def enter() -> str:
            presses.append(1)
            if len(presses) == 2:  # the disc goes in before the 2nd ENTER
                (d / pack_id).write_bytes(b"0123456789")
            return "\n"

        search, out = _search(tmp_path, [d], interactive=True,
                              prompt_in=enter)
        assert search.read_pack_range(pack_id, 2, 3) == b"234"
        err = "".join(out)
        assert "still not found" in err
        assert "continuing" in err
        assert len(presses) == 2
