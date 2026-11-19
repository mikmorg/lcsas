"""#602 addendum: the restore pass does not read the trees a second time.

The file-count pass (for the "N/M files" progress line) reads every
tree blob before the restore pass walks the same trees.  On a
multi-disc archive each extra read can be an extra disc prompt.  The
count pass now keeps the trees it read, up to a size cap, and the
restore pass uses them.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import pytest

from lcsas.restore import restic_fallback as fb

from .tier3_repo_builder import PASSWORD, build_repo

_TREE = {
    "a.txt": b"alpha\n",
    "d1": {"b.txt": b"bravo\n", "d2": {"c.txt": b"charlie\n"}},
    "d3": {"e.txt": b"echo\n"},
}
_N_TREES = 4  # root, d1, d1/d2, d3


def _restore_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[fb.PurePythonRestorer, collections.Counter[str]]:
    repo = build_repo(tmp_path, _TREE)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    reads: collections.Counter[str] = collections.Counter()
    real = fb.PurePythonRestorer._decode_blob

    def spy(self: fb.PurePythonRestorer, blob_id: str, loc: Any,
            plaintext: bytes) -> bytes:
        if loc.blob_type == "tree":
            reads[blob_id] += 1
        return real(self, blob_id, loc, plaintext)

    monkeypatch.setattr(fb.PurePythonRestorer, "_decode_blob", spy)
    target = tmp_path / "out"
    restorer.restore(target)
    assert restorer.failures == 0
    assert (target / "d1" / "d2" / "c.txt").read_bytes() == b"charlie\n"
    assert (target / "d3" / "e.txt").read_bytes() == b"echo\n"
    return restorer, reads


def test_each_tree_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LCSAS_PROGRESS", raising=False)
    restorer, reads = _restore_counting(tmp_path, monkeypatch)
    assert restorer._progress_total_files == 4  # the count pass ran
    assert len(reads) == _N_TREES
    assert set(reads.values()) == {1}
    assert restorer._tree_cache == {}  # emptied when restore() returns
    assert restorer._tree_cache_bytes == 0


def test_over_the_cap_trees_are_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LCSAS_PROGRESS", raising=False)
    monkeypatch.setattr(fb, "_TREE_CACHE_MAX_BYTES", 0)
    _restorer, reads = _restore_counting(tmp_path, monkeypatch)
    assert len(reads) == _N_TREES
    assert set(reads.values()) == {2}


def test_no_count_pass_without_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCSAS_PROGRESS", "0")
    _restorer, reads = _restore_counting(tmp_path, monkeypatch)
    assert set(reads.values()) == {1}


def test_cache_is_emptied_when_the_restore_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop at the disc prompt (#658) also empties the cache."""
    monkeypatch.delenv("LCSAS_PROGRESS", raising=False)
    repo = build_repo(tmp_path, _TREE)
    # The reader answers 'q' at the disc prompt.
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=True,
                                     prompt_in=lambda: "q\n",
                                     prompt_out=lambda _text: None)

    real_count = restorer._count_files

    def count_then_stop(tree_id: str) -> int:
        n = real_count(tree_id)
        assert restorer._tree_cache  # the count pass filled it
        # Every tree is cached, so the next pack read is a file's data:
        # take the disc out, and that read stops at the disc prompt.
        (repo / "data").rename(tmp_path / "shelf")
        return n

    monkeypatch.setattr(restorer, "_count_files", count_then_stop)
    with pytest.raises(fb.RestoreAbortedError):
        restorer.restore(tmp_path / "out")
    assert restorer._tree_cache == {}
