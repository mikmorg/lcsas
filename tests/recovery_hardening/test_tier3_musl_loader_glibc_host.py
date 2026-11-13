"""Hardening test (#714): the bundled CPython runs on glibc via its loader.

The REAL bundled CPython runs on a glibc host through the REAL musl
loader the meta disc carries.

The Linux CPython bundled for tier 3 (python-build-standalone,
x86_64-unknown-linux-musl) is dynamically linked against musl's loader,
/lib/ld-musl-x86_64.so.1.  Stock Debian, Ubuntu and Fedora lack it, so
there the interpreter fails with "not found" (exit 127).  The lab VM has
the loader installed, which hid this from every drill.  The meta disc
now carries the loader next to the interpreter
(python/lib/ld-musl-x86_64.so.1, pinned in recovery/UPSTREAM.sha256),
and restore.sh runs `<loader> <python3> ...` when python3 cannot start
on its own.

This test proves it in a `debian:12-slim` container (no musl, no
network): the interpreter fails on its own, runs through the loader
with the modules the reader needs, and restore.sh -- run by Debian's
dash -- picks it through the loader and runs a Python script with it.

Inputs come from the upstream cache that recovery/scripts/fetch_upstream.sh
fills ($LCSAS_RECOVERY_CACHE, default ~/.cache/lcsas/recovery-binaries):
python/x86_64-unknown-linux-musl/python/ and
musl/x86_64-unknown-linux-musl/ld-musl-x86_64.so.1.  Skips cleanly when
docker, the image, the cache or an x86_64 host is missing.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
TARGET = "x86_64-unknown-linux-musl"
LOADER = "ld-musl-x86_64.so.1"
IMAGE = "debian:12-slim"


def _cache_root() -> Path:
    env = os.environ.get("LCSAS_RECOVERY_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "lcsas" / "recovery-binaries"


@pytest.fixture(scope="module")
def inputs() -> tuple[Path, Path]:
    """(python tree, loader) from the upstream cache, or skip."""
    if platform.machine() not in ("x86_64", "AMD64"):
        pytest.skip("needs an x86_64 host (the x86_64 CPython + loader)")
    cache = _cache_root()
    py = cache / "python" / TARGET / "python"
    ld = cache / "musl" / TARGET / LOADER
    if not (py / "bin" / "python3").is_file():
        pytest.skip(f"no bundled CPython in the upstream cache ({py}); "
                    "run recovery/scripts/fetch_upstream.sh")
    if not ld.is_file():
        pytest.skip(f"no musl loader in the upstream cache ({ld}); "
                    "run recovery/scripts/fetch_upstream.sh")
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    probe = subprocess.run(["docker", "info"], capture_output=True,
                           text=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip("docker daemon not usable here")
    have = subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True, text=True, timeout=60)
    if have.returncode != 0:
        pull = subprocess.run(["docker", "pull", "-q", IMAGE],
                              capture_output=True, text=True, timeout=600)
        if pull.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}: {pull.stderr.strip()}")
    return py.resolve(), ld.resolve()


def _docker(py: Path, ld: Path, script: str,
            extra_mounts: tuple[tuple[Path, str], ...] = ()
            ) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "run", "--rm", "--network", "none",
           "-v", f"{py}:/py:ro", "-v", f"{ld}:/ld/{LOADER}:ro"]
    for src, dst in extra_mounts:
        cmd += ["-v", f"{src}:{dst}:ro"]
    cmd += [IMAGE, "sh", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def test_bundled_cpython_needs_and_runs_through_the_bundled_loader(
        inputs: tuple[Path, Path]) -> None:
    py, ld = inputs
    res = _docker(py, ld, """
        set -u
        if ls /lib/ld-musl-* >/dev/null 2>&1; then
            echo HOST_HAS_MUSL; exit 3
        fi
        /py/bin/python3 -c pass 2>/dev/null
        echo "direct_rc=$?"
        /ld/ld-musl-x86_64.so.1 /py/bin/python3 -c '
import sys, ssl, sqlite3, zlib, hashlib, lzma, bz2, json, hmac, base64
print("via_loader", sys.version.split()[0], sys.prefix, ssl.OPENSSL_VERSION)
print("sqlite", sqlite3.sqlite_version)
'
        echo "loader_rc=$?"
    """)
    print(res.stdout, res.stderr)
    assert "HOST_HAS_MUSL" not in res.stdout, "the image has a musl loader"
    # On its own the interpreter cannot start: the shell says "not found".
    assert "direct_rc=127" in res.stdout, res.stdout + res.stderr
    assert "loader_rc=0" in res.stdout, res.stdout + res.stderr
    assert "via_loader 3.12." in res.stdout, res.stdout
    # The prefix is the interpreter's tree, not the loader's folder, so
    # the stdlib and lib-dynload are found.
    assert " /py OpenSSL" in res.stdout, res.stdout


def test_restore_sh_picks_the_bundled_loader_on_a_glibc_host(
        inputs: tuple[Path, Path], tmp_path: Path) -> None:
    """restore.sh under Debian's dash: no tier 1/2/2b, no host python3,
    the bundled python3 cannot start on its own -- tier 3 runs it through
    the bundled loader and the reader script runs."""
    py, ld = inputs
    fixture = tmp_path / "meta"
    (fixture / "recovery" / "scripts").mkdir(parents=True)
    shutil.copy2(RESTORE_SH, fixture / "recovery" / "scripts" / "restore.sh")
    repo = fixture / "recovery" / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    # A stand-in reader: real Python, run by the real interpreter.
    (fixture / "standalone_restorer.py").write_text(
        "import sys, ssl, sqlite3, zlib\n"
        "print('READER_RAN', sys.version.split()[0], sys.executable)\n"
    )
    bin_dir = f"/meta/recovery/bin/{TARGET}"
    res = _docker(py, ld, f"""
        set -u
        cp -R /ro-meta /meta
        mkdir -p {bin_dir}
        cp -R /py {bin_dir}/python
        cp /ld/{LOADER} {bin_dir}/python/lib/{LOADER}
        command -v python3 && {{ echo HOST_HAS_PYTHON; exit 3; }}
        printf 'pw\\n' | LCSAS_TARGET={TARGET} LCSAS_MOUNT_DIRS= \\
            LCSAS_ALLOW_NO_PACK_SEARCH=1 HOME=/tmp \\
            sh /meta/recovery/scripts/restore.sh /meta/recovery \\
               /tmp/restored latest
        echo "restore_rc=$?"
    """, extra_mounts=((fixture, "/ro-meta"),))
    print(res.stdout, res.stderr)
    assert "HOST_HAS_PYTHON" not in res.stdout, res.stdout
    assert "restore_rc=0" in res.stdout, res.stdout + res.stderr
    assert "READER_RAN 3.12." in res.stdout, res.stdout + res.stderr
    assert (
        "[tier 3] the bundled Python cannot start on its own here "
        "(no musl loader on this host); running it through the bundled "
        f"loader {bin_dir}/python/lib/{LOADER}") in res.stderr, res.stderr
