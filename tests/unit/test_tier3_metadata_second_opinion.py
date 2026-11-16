"""#793: key, index, snapshot and config files get a pure-AES second attempt.

Data blobs carry a content hash, so a native AES that turns wrong after
its start-up self-test is caught there and the blob is decrypted again
with the pure-Python AES.  Key, index, snapshot and config files have no
content hash: a wrong native result shows only as a file that will not
parse.  These tests put a backend in place that returns wrong plaintext
for the whole run (key file included) or for one kind of file, and show
the restore still completes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback as fb
from lcsas.restore._aes_fast import AesCtrBackend, FastAesCtr
from lcsas.restore._aes_pure import aes_ctr

from .test_restic_fallback import _encrypt_with_master
from .tier3_repo_builder import PASSWORD, build_repo


class _Liar(AesCtrBackend):
    """Right AES, then one byte flipped when *wrong(plaintext)* is true."""

    name = "liar"
    min_bytes = 0

    def __init__(self, wrong: Callable[[bytes], bool]) -> None:
        self._wrong = wrong
        self.lies = 0

    def describe(self) -> str:
        return "a lying backend"

    def ctr(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        out = aes_ctr(key, iv, data)
        if self._wrong(out):
            self.lies += 1
            out = bytes([out[0] ^ 0x5A]) + out[1:]
        return out


def _install(
    monkeypatch: pytest.MonkeyPatch, wrong: Callable[[bytes], bool],
) -> tuple[FastAesCtr, _Liar, list[str]]:
    lines: list[str] = []
    monkeypatch.setattr(fb, "_log", lines.append)
    liar = _Liar(wrong)
    aes = FastAesCtr(log=lines.append)
    aes._backend = liar
    # monkeypatch puts the module's _AES_CTR back after the test.
    monkeypatch.setattr(fb, "_AES_CTR", aes)
    return aes, liar, lines


def _repo(tmp_path: Path) -> Path:
    return build_repo(tmp_path, {
        "a.txt": b"alpha\n",
        "dir": {"b.txt": b"bravo\n"},
    })


def _restore_ok(repo: Path, target: Path) -> fb.PurePythonRestorer:
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    restorer.restore(target)
    assert restorer.failures == 0
    assert (target / "a.txt").read_bytes() == b"alpha\n"
    assert (target / "dir" / "b.txt").read_bytes() == b"bravo\n"
    return restorer


def test_wrong_for_everything_from_the_key_file_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong from the first decryption (the key file): restore completes."""
    repo = _repo(tmp_path)
    aes, liar, lines = _install(monkeypatch, lambda _out: True)
    _restore_ok(repo, tmp_path / "out")
    assert liar.lies == 1  # switched off after the key file
    assert aes.backend.name == "pure"
    assert any("gave wrong output for key file" in ln for ln in lines), lines


@pytest.mark.parametrize(("marker", "what"), [
    (b'"packs"', "index/"),
    (b'"hostname"', "snapshots/"),
])
def test_wrong_for_one_metadata_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: bytes, what: str,
) -> None:
    repo = _repo(tmp_path)
    aes, liar, lines = _install(monkeypatch, lambda out: marker in out)
    _restore_ok(repo, tmp_path / "out")
    assert liar.lies == 1
    assert aes.backend.name == "pure"
    said = [ln for ln in lines if "gave wrong output for" in ln]
    assert len(said) == 1 and what in said[0], lines


def test_wrong_for_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    aes, _liar, _lines = _install(
        monkeypatch, lambda out: b"chunker_polynomial" in out)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    assert restorer.repo_info()["repository_id"] == "tier3-test"
    assert aes.backend.name == "pure"


def test_a_damaged_file_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A right backend and a file that really is bad: the parse error is
    raised, and the native AES is not blamed."""
    repo = _repo(tmp_path)
    for f in (repo / "index").iterdir():
        f.write_bytes(_encrypt_with_master(b"{not json"))
    aes, liar, _lines = _install(monkeypatch, lambda _out: False)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    with pytest.raises(json.JSONDecodeError):
        restorer.restore(tmp_path / "out")
    assert aes.backend is liar


def test_a_damaged_file_and_a_wrong_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both attempts fail to parse: the first error is raised, and the
    native AES is not switched off (the pure result did not parse either)."""
    repo = _repo(tmp_path)
    for f in (repo / "index").iterdir():
        f.write_bytes(_encrypt_with_master(b'{not json "packs"'))
    aes, liar, _lines = _install(monkeypatch, lambda out: b'"packs"' in out)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    with pytest.raises(ValueError):
        restorer.restore(tmp_path / "out")
    assert liar.lies == 1
    assert aes.backend is liar


def test_pure_backend_gets_no_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the pure AES in use there is nothing to compare against."""
    repo = _repo(tmp_path)
    for f in (repo / "snapshots").iterdir():
        f.write_bytes(_encrypt_with_master(b"\xff\xfe"))
    aes = FastAesCtr()
    aes._backend = AesCtrBackend()
    monkeypatch.setattr(fb, "_AES_CTR", aes)
    calls: list[bool] = []
    real = fb._decrypt_authenticated

    def spy(*args: object, pure: bool = False) -> bytes:
        calls.append(pure)
        return real(*args, pure=pure)  # type: ignore[arg-type]

    monkeypatch.setattr(fb, "_decrypt_authenticated", spy)
    restorer = fb.PurePythonRestorer(repo, password=PASSWORD,
                                     interactive=False)
    with pytest.raises(ValueError):
        restorer.restore(tmp_path / "out")
    assert True not in calls
