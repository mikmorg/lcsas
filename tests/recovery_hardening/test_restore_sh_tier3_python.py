"""Hardening test (#714, #706): which Python tier 3 runs, and its exit 75.

#714: the CPython bundled for Linux (python-build-standalone, musl) is
dynamically linked against the musl loader (/lib/ld-musl-<arch>.so.1).
Stock Debian, Ubuntu and Fedora do not have it, so there the bundled
interpreter is executable but fails with "not found" (exit 127).
restore.sh used to pick it on `[ -x ]` alone and never tried the host
python3.  Now a candidate counts only if it runs a Python 3.8+.

The lab VM has the musl loader, so these tests never rely on the real
bundled binary failing: the "bundled" interpreter is a stub whose `#!`
names a loader that does not exist (the same execve ENOENT the real
binary hits on a glibc host), or one that runs but is too old.

#706: tier 3 exits 75 when the reader types q at its disc prompt (or
the input ends), the code tier 1 uses for the same stop.  restore.sh
treats it as a pause, not a failure.  The tier-3 stub here exits 75
itself, so this does not depend on #706's reader change.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

# A `#!` line naming a musl loader that is not installed: execve fails
# with ENOENT and the shell reports "not found", exit 127 -- exactly
# what the real bundled CPython does on a glibc-only host.
NO_LOADER = "#!/nonexistent/lib/ld-musl-x86_64.so.1\n"
# A Python that starts but is too old (e.g. 2.7 or 3.7): the probe's
# `sys.exit(sys.version_info < (3, 8))` exits 1.
TOO_OLD = "#!/bin/sh\nexit 1\n"


def _fixture(tmp_path: Path, bundled: str | None) -> Path:
    """recovery/ with a repo, no tier-1/2 binaries, the tier-3 script,
    and (optionally) a bundled python3 with the given body."""
    recovery = tmp_path / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (tmp_path / "standalone_restorer.py").write_text("# stub\n")
    if bundled is not None:
        py = bin_dir / "python" / "bin" / "python3"
        py.parent.mkdir(parents=True)
        py.write_text(bundled)
        py.chmod(0o755)
    return recovery


def _host_python(stub_dir: Path, rc: int, log: Path) -> Path:
    """A host `python3` that passes the probe, then records its argv and
    exits `rc` (writing a line to stderr when rc is not 0)."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in -c) exit 0 ;; esac
        printf '%s\\n' "$@" > {log}
        echo HOST_PYTHON_RAN
        if [ {rc} -ne 0 ]; then
            echo '[lcsas-restore] stopped at the disc prompt.' >&2
        fi
        exit {rc}
    """))
    stub.chmod(0o755)
    return stub


def _broken_host_pythons(stub_dir: Path) -> None:
    """python3 and python on PATH that cannot run either.  They exit 127
    from a shell rather than use NO_LOADER: when exec of a command found
    on PATH fails with ENOENT, the shell goes on searching PATH and
    would find the host's real /usr/bin/python3."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name in ("python3", "python"):
        p = stub_dir / name
        p.write_text("#!/bin/sh\nexit 127\n")
        p.chmod(0o755)


def _run(recovery: Path, tmp_path: Path, stub_dir: Path,
         **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        # /usr/local/bin (where a host restic/rustic usually lives) is
        # left out: a stock tool would be tier 2b and run before tier 3.
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LCSAS_TARGET": HOST_TARGET,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        **extra,
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored"),
         "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=30,
    )


@pytest.mark.parametrize("bundled", [NO_LOADER, TOO_OLD],
                         ids=["no-musl-loader", "too-old"])
def test_bundled_python_that_cannot_run_falls_back_to_host(
        tmp_path: Path, bundled: str) -> None:
    recovery = _fixture(tmp_path, bundled)
    stub_dir = tmp_path / "stubbin"
    log = tmp_path / "host-argv.log"
    _host_python(stub_dir, 0, log)

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 0, (
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}")
    assert "HOST_PYTHON_RAN" in res.stdout, res.stdout
    assert log.read_text().splitlines()[0].endswith("standalone_restorer.py")
    assert ("[tier 3] the bundled Python cannot run on this host "
            "(missing musl loader?); using the host python3") in res.stderr, (
        res.stderr)
    assert "falling back to Python (python3 + " in res.stderr, res.stderr


def test_bundled_python_that_runs_is_still_preferred(tmp_path: Path) -> None:
    """A bundled interpreter that passes the probe is used, not the host's."""
    recovery = _fixture(tmp_path, textwrap.dedent("""\
        #!/bin/sh
        case "$1" in -c) exit 0 ;; esac
        echo BUNDLED_PYTHON_RAN
        exit 0
    """))
    stub_dir = tmp_path / "stubbin"
    _host_python(stub_dir, 0, tmp_path / "host-argv.log")

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 0, res.stderr
    assert "BUNDLED_PYTHON_RAN" in res.stdout, res.stdout
    assert "HOST_PYTHON_RAN" not in res.stdout, res.stdout
    assert "cannot run on this host" not in res.stderr, res.stderr


def test_no_python_runs_guard_names_the_bundled_one(tmp_path: Path) -> None:
    """No tier 1/2/2b and no Python that runs: the no-recovery-binary
    guard fires (before the password prompt) and says the bundled
    interpreter is there but cannot run."""
    recovery = _fixture(tmp_path, NO_LOADER)
    stub_dir = tmp_path / "stubbin"
    _broken_host_pythons(stub_dir)

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 64, (
        f"rc={res.returncode}\nstderr:{res.stderr}")
    bundled = recovery / "bin" / HOST_TARGET / "python" / "bin" / "python3"
    assert f"tier 3: the bundled Python {bundled} cannot run" in res.stderr, (
        res.stderr)
    assert "no working python3 on PATH" in res.stderr, res.stderr
    assert "Password:" not in res.stderr, res.stderr


def test_no_python_runs_after_tier1_fails(tmp_path: Path) -> None:
    """Tier 1 present but failing (LCSAS_TIER_FALLBACK=1) and no Python
    that runs: tier 3 says it cannot run, and the run fails."""
    recovery = _fixture(tmp_path, NO_LOADER)
    t1 = recovery / "bin" / HOST_TARGET / "lcsas-restore"
    t1.write_text("#!/bin/sh\necho 'FAKE tier 1 crash' >&2\nexit 17\n")
    t1.chmod(0o755)
    stub_dir = tmp_path / "stubbin"
    _broken_host_pythons(stub_dir)

    res = _run(recovery, tmp_path, stub_dir, LCSAS_TIER_FALLBACK="1")

    assert res.returncode == 1, (
        f"rc={res.returncode}\nstderr:{res.stderr}")
    assert "[tier 3] cannot run: the bundled Python" in res.stderr, res.stderr
    assert "ERROR: no recovery method available." in res.stderr, res.stderr
    assert "falling back to Python" not in res.stderr, res.stderr


def test_tier3_exit_75_is_a_stop_not_a_failure(tmp_path: Path) -> None:
    """Tier 3 exits 75 (q at its disc prompt): restore.sh exits 75 too,
    reports no failure, keeps the tools folder and says how to resume."""
    recovery = _fixture(tmp_path, None)
    stub_dir = tmp_path / "stubbin"
    _host_python(stub_dir, 75, tmp_path / "host-argv.log")
    tools = tmp_path / "tools"

    res = _run(recovery, tmp_path, stub_dir, LCSAS_TOOLS_DIR=str(tools))

    assert res.returncode == 75, (
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}")
    assert "HOST_PYTHON_RAN" in res.stdout, res.stdout
    assert "[tier 3] stopped at the disc prompt at your request." in res.stderr, (
        res.stderr)
    assert "[tier 3] FAILED" not in res.stderr, res.stderr
    assert "captured stderr" not in res.stderr, res.stderr
    assert (tools / "resume.sh").is_file()
    assert f"sh {tools}/resume.sh" in res.stderr, res.stderr


# ── #714 step 2: the musl loader the disc carries ─────────────────────
#
# The meta disc ships musl's loader at python/lib/ld-musl-<arch>.so.1
# (recovery/UPSTREAM.sha256, musl/).  When the bundled interpreter cannot
# start on its own, restore.sh runs it as `<loader> <python3> ...`
# before it falls back to the host python3.  The "loader" here is a stub
# that stands in for musl's: it runs the program named as its first
# argument (here: records it) -- or fails, for the case where even the
# loader cannot help.  The fixture sits under a folder with a space, as
# a mounted disc label can.

LOADER_NAME = "ld-musl-x86_64.so.1"


def _loader(recovery: Path, log: Path | None) -> Path:
    """A bundled loader stub.  ``log`` None: a loader that cannot run
    anything (exit 127).  Otherwise it passes the probe, then records the
    program and its argv and exits 0."""
    ld = recovery / "bin" / HOST_TARGET / "python" / "lib" / LOADER_NAME
    ld.parent.mkdir(parents=True, exist_ok=True)
    if log is None:
        ld.write_text("#!/bin/sh\nexit 127\n")
    else:
        ld.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$2" in -c) exit 0 ;; esac
            printf '%s\\n' "$@" > '{log}'
            echo LOADER_RAN
            exit 0
        """))
    ld.chmod(0o755)
    return ld


def _spaced_fixture(tmp_path: Path, bundled: str) -> Path:
    root = tmp_path / "meta disc"
    root.mkdir()
    return _fixture(root, bundled)


def test_bundled_python_runs_through_the_bundled_loader(tmp_path: Path) -> None:
    """The bundled python3 cannot start on its own (no musl loader on the
    host) but the disc's loader runs it: tier 3 uses the loader, says
    so in one line, and never touches the host python3."""
    recovery = _spaced_fixture(tmp_path, NO_LOADER)
    ld_log = tmp_path / "loader-argv.log"
    _loader(recovery, ld_log)
    stub_dir = tmp_path / "stubbin"
    host_log = tmp_path / "host-argv.log"
    _host_python(stub_dir, 0, host_log)

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 0, (
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}")
    assert "LOADER_RAN" in res.stdout, res.stdout
    assert "HOST_PYTHON_RAN" not in res.stdout, res.stdout
    assert not host_log.exists()
    argv = ld_log.read_text().splitlines()
    assert argv[0].endswith(
        f"/recovery/bin/{HOST_TARGET}/python/bin/python3"), argv
    assert argv[1].endswith("standalone_restorer.py"), argv
    assert "--repo" in argv, argv
    line = next(ln for ln in res.stderr.splitlines()
                if "running it through the bundled loader" in ln)
    # (The password prompt, with no newline after it, may precede it.)
    assert (
        "[tier 3] the bundled Python cannot start on its own here "
        "(no musl loader on this host); running it through the bundled "
        "loader ") in line, line
    assert line.endswith(f"/python/lib/{LOADER_NAME}"), line
    assert "using the host" not in res.stderr, res.stderr


def test_bundled_python_that_runs_does_not_use_the_loader(
        tmp_path: Path) -> None:
    """A bundled python3 that starts on its own is run directly: the
    loader is the second choice, not the first."""
    recovery = _spaced_fixture(tmp_path, textwrap.dedent("""\
        #!/bin/sh
        case "$1" in -c) exit 0 ;; esac
        echo BUNDLED_PYTHON_RAN
        exit 0
    """))
    ld_log = tmp_path / "loader-argv.log"
    _loader(recovery, ld_log)
    stub_dir = tmp_path / "stubbin"
    _host_python(stub_dir, 0, tmp_path / "host-argv.log")

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 0, res.stderr
    assert "BUNDLED_PYTHON_RAN" in res.stdout, res.stdout
    assert "LOADER_RAN" not in res.stdout, res.stdout
    assert not ld_log.exists()
    assert "bundled loader" not in res.stderr, res.stderr


def test_loader_that_cannot_help_falls_back_to_host(tmp_path: Path) -> None:
    """Neither the bundled python3 nor the bundled loader runs: the host
    python3 is used, and the line says the loader was tried too."""
    recovery = _spaced_fixture(tmp_path, NO_LOADER)
    _loader(recovery, None)
    stub_dir = tmp_path / "stubbin"
    _host_python(stub_dir, 0, tmp_path / "host-argv.log")

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 0, (
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}")
    assert "HOST_PYTHON_RAN" in res.stdout, res.stdout
    assert ("[tier 3] the bundled Python cannot run on this host "
            "(not even through its bundled musl loader); using the host "
            "python3") in res.stderr, res.stderr


def test_loader_that_cannot_help_and_no_host_python_guard(
        tmp_path: Path) -> None:
    """No tier 1/2/2b, the bundled python3 and loader both fail, no host
    Python: the guard names the bundled interpreter and the loader."""
    recovery = _spaced_fixture(tmp_path, NO_LOADER)
    _loader(recovery, None)
    stub_dir = tmp_path / "stubbin"
    _broken_host_pythons(stub_dir)

    res = _run(recovery, tmp_path, stub_dir)

    assert res.returncode == 64, (
        f"rc={res.returncode}\nstderr:{res.stderr}")
    assert ("(not even through its bundled musl loader), and no working "
            "python3 on PATH") in res.stderr, res.stderr


def test_loader_that_cannot_help_after_tier1_fails(tmp_path: Path) -> None:
    """Tier 1 fails, the bundled python3 and loader both fail, no host
    Python: tier 3 says it cannot run, naming the loader."""
    recovery = _spaced_fixture(tmp_path, NO_LOADER)
    _loader(recovery, None)
    t1 = recovery / "bin" / HOST_TARGET / "lcsas-restore"
    t1.write_text("#!/bin/sh\necho 'FAKE tier 1 crash' >&2\nexit 17\n")
    t1.chmod(0o755)
    stub_dir = tmp_path / "stubbin"
    _broken_host_pythons(stub_dir)

    res = _run(recovery, tmp_path, stub_dir, LCSAS_TIER_FALLBACK="1")

    assert res.returncode == 1, (
        f"rc={res.returncode}\nstderr:{res.stderr}")
    assert ("[tier 3] (not even through its bundled musl loader), and "
            "there is no working python3 on PATH.") in res.stderr, res.stderr
    assert "falling back to Python" not in res.stderr, res.stderr
