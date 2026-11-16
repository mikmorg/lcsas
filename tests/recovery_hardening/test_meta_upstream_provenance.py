"""test_meta_upstream_provenance.py -- bundled upstream bytes come from the pinned archives (#832).

FAILURE MODE CAUGHT
-------------------
`lcsas meta build` re-hashed each cached upstream archive against
recovery/UPSTREAM.sha256 (#372), then copied rustic, restic, CPython and
the musl loader from the folder fetch_upstream.sh unpacks beside the
archive.  Nothing tied that folder to the archive: an altered or stale
unpacked binary went onto the disc while the archive still matched.

WHAT THIS GATE ASSERTS
----------------------
  * The disc gets the archive's bytes, whatever the unpacked copy holds.
  * A cache whose unpacked copy differs from its archive fails the build,
    naming the file.
  * An unpacked copy with no pinned archive bundles nothing.
  * An archive member that would escape its folder fails the build.
  * `lcsas meta verify --strict` compares the bundled files with what the
    pinned archives unpack to, and says which archives it could not check.

Synthetic archives only (tests/fixtures/upstream_cache.py); no download.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tarfile
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.meta.builder import MetaBuildError, MetaVolumeBuilder
from lcsas.meta.required_contents import APPROVED_TARGETS
from lcsas.meta.upstream import check_bundled_upstream
from tests.fixtures.upstream_cache import add_upstream

LINUX = "x86_64-unknown-linux-musl"


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the upstream bundler; return the disc's recovery/ folder."""
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "cache"))
    recovery_dst = tmp_path / "meta" / "recovery"
    recovery_dst.mkdir(parents=True, exist_ok=True)
    MetaVolumeBuilder(
        tmp_path / "meta", recovery_dir=tmp_path / "pins",
    )._bundle_upstream_binaries(recovery_dst)
    return recovery_dst


def _disc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bundled disc recovery/ that carries the pins, as a real one does."""
    recovery_dst = _bundle(tmp_path, monkeypatch)
    (recovery_dst / "UPSTREAM.sha256").write_bytes(
        (tmp_path / "pins" / "UPSTREAM.sha256").read_bytes()
    )
    return recovery_dst


def test_bundles_the_archive_bytes_not_the_unpacked_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no unpacked copy at all (fetch --no-extract) the disc still
    gets every file, straight from the archives."""
    add_upstream(tmp_path / "cache", tmp_path / "pins", LINUX,
                 restic=True, musl=True, extract=False,
                 rustic_body=b"\x7fELF the pinned rustic")
    bin_dir = _bundle(tmp_path, monkeypatch) / "bin" / LINUX
    assert (bin_dir / "rustic-static").read_bytes() == b"\x7fELF the pinned rustic"
    assert os.access(bin_dir / "rustic-static", os.X_OK)
    assert (bin_dir / "restic").read_bytes() == b"\x7fELF fake restic"
    assert (bin_dir / "python" / "bin" / "python3").is_file()
    assert os.readlink(bin_dir / "python" / "bin" / "python") == "python3"
    assert (bin_dir / "python" / "lib" / "ld-musl-x86_64.so.1").is_file()
    # The rustic archive's README and config/ stay off the disc, as before.
    assert sorted(p.name for p in bin_dir.iterdir()) == [
        "python", "restic", "rustic-static",
    ]


@pytest.mark.parametrize(
    ("category", "changed"),
    [
        ("rustic", "rustic/x86_64-unknown-linux-musl/rustic"),
        ("python", "python/x86_64-unknown-linux-musl/python/bin/python3"),
        ("restic", "restic/x86_64-unknown-linux-musl/restic"),
        ("musl", "musl/x86_64-unknown-linux-musl/ld-musl-x86_64.so.1"),
    ],
)
def test_changed_unpacked_copy_fails_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    category: str, changed: str,
) -> None:
    """The acceptance case of #832: archive intact, unpacked binary
    altered -> the build stops and names the altered file."""
    cache = tmp_path / "cache"
    add_upstream(cache, tmp_path / "pins", LINUX, restic=True, musl=True)
    (cache / changed).write_bytes(b"altered after the fetch")
    with pytest.raises(MetaBuildError) as exc:
        _bundle(tmp_path, monkeypatch)
    assert str(cache / changed) in str(exc.value)
    assert "#832" in str(exc.value)


def test_extra_files_in_the_unpacked_copy_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file the archive does not hold (say __pycache__ left by running the
    cached python) neither fails the build nor reaches the disc."""
    cache = tmp_path / "cache"
    add_upstream(cache, tmp_path / "pins", LINUX)
    stray = cache / "python" / LINUX / "python" / "lib" / "__pycache__"
    stray.mkdir()
    (stray / "os.cpython-312.pyc").write_bytes(b"\x00")
    bin_dir = _bundle(tmp_path, monkeypatch) / "bin" / LINUX
    assert not (bin_dir / "python" / "lib" / "__pycache__").exists()


def test_unpacked_copy_without_pinned_archive_bundles_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a pinned, verified archive can put a file on the disc."""
    cache = tmp_path / "cache"
    add_upstream(cache, tmp_path / "pins", LINUX)
    for archive in cache.rglob("*.tar.gz"):
        archive.unlink()
    recovery_dst = _bundle(tmp_path, monkeypatch)
    assert not (recovery_dst / "bin").exists()


def test_member_escaping_its_folder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("python/../../escape")
        body = b"evil"
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    data = buf.getvalue()
    rel = f"python/{LINUX}/cpython-evil.tar.gz"
    (tmp_path / "cache" / rel).parent.mkdir(parents=True)
    (tmp_path / "cache" / rel).write_bytes(data)
    (tmp_path / "pins").mkdir()
    (tmp_path / "pins" / "UPSTREAM.sha256").write_text(
        f"{hashlib.sha256(data).hexdigest()}  {rel}\n"
    )
    with pytest.raises(MetaBuildError, match="unsafe member path"):
        _bundle(tmp_path, monkeypatch)
    assert not (tmp_path / "meta" / "escape").exists()


# ── meta verify --strict ─────────────────────────────────────────────


def test_strict_check_passes_on_a_fresh_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for target in (LINUX, "x86_64-pc-windows-gnu"):
        add_upstream(tmp_path / "cache", tmp_path / "pins", target,
                     restic=True, musl=target == LINUX)
    recovery_dst = _disc(tmp_path, monkeypatch)
    result = check_bundled_upstream(
        recovery_dst, tmp_path / "cache", APPROVED_TARGETS,
    )
    assert result.problems == []
    assert result.unchecked == []
    assert result.checked > 0


def test_strict_check_names_changed_and_extra_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_upstream(tmp_path / "cache", tmp_path / "pins", LINUX)
    recovery_dst = _disc(tmp_path, monkeypatch)
    bin_dir = recovery_dst / "bin" / LINUX
    (bin_dir / "rustic-static").write_bytes(b"swapped on the disc")
    (bin_dir / "python" / "bin" / "planted").write_bytes(b"not upstream")
    result = check_bundled_upstream(
        recovery_dst, tmp_path / "cache", APPROVED_TARGETS,
    )
    joined = "\n".join(result.problems)
    assert f"bin/{LINUX}/rustic-static: differs from rustic/{LINUX}/" in joined
    assert f"bin/{LINUX}/python/bin/planted: not in python/{LINUX}/" in joined
    assert len(result.problems) == 2


def test_strict_check_lists_archives_it_cannot_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_upstream(tmp_path / "cache", tmp_path / "pins", LINUX)
    recovery_dst = _disc(tmp_path, monkeypatch)
    result = check_bundled_upstream(
        recovery_dst, tmp_path / "no-cache", APPROVED_TARGETS,
    )
    assert result.problems == []
    assert result.checked == 0
    assert sorted(r.split("/")[0] for r in result.unchecked) == [
        "python", "rustic",
    ]


def test_meta_verify_strict_reports_upstream_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end through the CLI: a swapped rustic-static on an otherwise
    consistent disc (its MANIFEST.sha256 rewritten to match) fails."""
    add_upstream(tmp_path / "cache", tmp_path / "pins", LINUX)
    recovery_dst = _disc(tmp_path, monkeypatch)
    (recovery_dst / "bin" / LINUX / "rustic-static").write_bytes(b"swapped")
    (recovery_dst / "MANIFEST.sha256").write_text("")
    MetaVolumeBuilder(tmp_path / "meta")._regenerate_recovery_manifest(
        recovery_dst,
    )
    caplog.set_level(logging.INFO)
    assert main(["meta", "verify", str(tmp_path / "meta"), "--strict"]) == 1
    assert f"UPSTREAM bin/{LINUX}/rustic-static: differs from" in caplog.text
    assert "MISMATCH" not in caplog.text  # the manifest itself agrees

    caplog.clear()
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "no-cache"))
    main(["meta", "verify", str(tmp_path / "meta"), "--strict"])
    assert "Not checked against their pinned archives" in caplog.text
    assert "UPSTREAM bin/" not in caplog.text
