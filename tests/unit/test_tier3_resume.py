"""#729: tier 3 keeps the files an earlier run finished.

A file already at its path with the node's size and mtime is kept and
none of its data is read, as tier 1 and rustic do.  The mtime may be up
to 2 seconds off: FAT stores times in 2-second steps (#787).  A file
that an interrupted run left only as ``.lcsas-partial``, or one with
another size or mtime, is restored again.  ``resume=False``
(``--no-resume``) restores everything again.
"""

from __future__ import annotations

import collections
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from lcsas.restore import restic_fallback as fb

from .tier3_repo_builder import PASSWORD, build_repo

_TREE = {
    "a.txt": b"alpha\n",
    "d1": {"b.txt": b"bravo\n" * 3, "d2": {"c.txt": b"charlie\n"}},
    "e.txt": [],  # an empty file: no blob
}
_MTIME = fb._parse_timestamp("2026-01-15T10:00:00.000000000Z")


def _restore(
    repo: Path, target: Path, monkeypatch: pytest.MonkeyPatch,
    *, resume: bool = True,
) -> tuple[fb.PurePythonRestorer, collections.Counter[str]]:
    """Restore *repo* into *target*; count the data blobs read, by blob."""
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, interactive=False, resume=resume,
    )
    reads: collections.Counter[str] = collections.Counter()
    real = fb.PurePythonRestorer._decode_blob

    def spy(self: fb.PurePythonRestorer, blob_id: str, loc: Any,
            plaintext: bytes) -> bytes:
        if loc.blob_type == "data":
            reads[blob_id] += 1
        return real(self, blob_id, loc, plaintext)

    monkeypatch.setattr(fb.PurePythonRestorer, "_decode_blob", spy)
    restorer.restore(target)
    monkeypatch.undo()
    assert restorer.failures == 0
    assert (target / "a.txt").read_bytes() == b"alpha\n"
    assert (target / "d1" / "b.txt").read_bytes() == b"bravo\n" * 3
    assert (target / "d1" / "d2" / "c.txt").read_bytes() == b"charlie\n"
    assert (target / "e.txt").read_bytes() == b""
    return restorer, reads


def _blob(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    repo = build_repo(tmp_path, _TREE)
    target = tmp_path / "out"
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert len(reads) == 3  # the empty file has no blob
    return repo, target


def test_a_second_run_reads_no_data(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, target = restored
    capsys.readouterr()
    restorer, reads = _restore(repo, target, monkeypatch)
    assert not reads
    assert restorer._resumed == 4
    err = capsys.readouterr().err
    assert "4 file(s) were already restored by an earlier run" in err
    assert "Restore complete." in err


def test_an_interrupted_file_is_restored_again(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run stopped in this file: only its partial file is there."""
    repo, target = restored
    final = target / "d1" / "b.txt"
    partial = final.with_name("b.txt.lcsas-partial")
    final.unlink()
    partial.write_bytes(b"bra")
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert set(reads) == {_blob(b"bravo\n" * 3)}
    assert not partial.exists()


@pytest.mark.parametrize(
    ("content", "mtime_offset"),
    [
        (b"other size\n", 0.0),       # wrong size, right mtime
        (b"BRAVO\n" * 3, 10.0),       # right size, mtime 10 s off
        (b"BRAVO\n" * 3, -3.0),       # right size, mtime 3 s early
    ],
)
def test_a_wrong_size_or_mtime_is_restored_again(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    content: bytes, mtime_offset: float,
) -> None:
    repo, target = restored
    path = target / "d1" / "b.txt"
    path.write_bytes(content)
    os.utime(path, (_MTIME, _MTIME + mtime_offset))
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert set(reads) == {_blob(b"bravo\n" * 3)}


@pytest.mark.parametrize("mtime_offset", [-2.0, -1.0, 1.5, 2.0])
def test_an_mtime_within_two_seconds_is_kept(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    mtime_offset: float,
) -> None:
    """FAT (#787): the time is stored in 2-second steps."""
    repo, target = restored
    path = target / "d1" / "b.txt"
    os.utime(path, (_MTIME, _MTIME + mtime_offset))
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert not reads


def test_a_link_in_place_of_the_file_is_not_taken_for_it(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, target = restored
    path = target / "a.txt"
    elsewhere = target.parent / "elsewhere.txt"
    elsewhere.write_bytes(b"alpha\n")
    os.utime(elsewhere, (_MTIME, _MTIME))
    path.unlink()
    os.symlink(elsewhere, path)
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert set(reads) == {_blob(b"alpha\n")}
    assert not path.is_symlink()


def test_a_stale_partial_beside_a_finished_file_is_removed(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, target = restored
    partial = target / "a.txt.lcsas-partial"
    partial.write_bytes(b"left by a stopped run")
    _restorer, reads = _restore(repo, target, monkeypatch)
    assert not reads
    assert not partial.exists()


def test_no_resume_restores_everything_again(
    restored: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, target = restored
    restorer, reads = _restore(repo, target, monkeypatch, resume=False)
    assert len(reads) == 3
    assert restorer._resumed == 0


@pytest.mark.parametrize(
    "node",
    [
        {"mtime": "2026-01-15T10:00:00Z"},                    # no size
        {"size": 6},                                          # no mtime
        {"size": "6", "mtime": "2026-01-15T10:00:00Z"},       # size not int
        {"size": True, "mtime": "2026-01-15T10:00:00Z"},      # a bool
        {"size": 6, "mtime": "yesterday"},                    # bad mtime
    ],
)
def test_a_node_without_a_usable_size_or_mtime_is_never_skipped(
    tmp_path: Path, node: dict[str, Any],
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"alpha\n")
    os.utime(path, (_MTIME, _MTIME))
    assert not fb._already_restored(node, path)
    assert fb._already_restored(
        {"size": 6, "mtime": "2026-01-15T10:00:00Z"}, path
    )
