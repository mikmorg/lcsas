"""Hardening (#609, #610, #615, #616): restore.sh's tools folder.

A restore that stops -- a wrong password, q at the disc prompt (tier 1
exit 75), Ctrl-C -- used to throw away everything: the copy of the
binaries made off the meta disc, and a pack cache under
``${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>`` that no later run could reuse
and no run ever removed.  Carrying on meant the meta disc again, the
copy again and every disc again.  And the EXIT/INT/TERM handler returned
0 on Ctrl-C, so the script carried on (and could exit 0).

Now the run keeps its files in one tools folder ($LCSAS_TOOLS_DIR,
default $HOME/lcsas-restore-tools): the pack cache, a copy of the
catalog, a copy of the set's metadata and resume.sh, which carries the
restore on without the meta disc and never holds the password.  The
folder goes after a restore that succeeds and stays after anything
else.  Ctrl-C exits 130 and TERM 143, keeping it.

The first run of each test is traced (``sh restore.sh`` in place, with
LCSAS_RELOCATED standing in for the relocated copy where needed); the
resume.sh run and the real relocation's re-exec run under /bin/sh.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import textwrap
import time
from pathlib import Path

from .conftest import _scaled_timeout

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

# Invented: a set's folder is named by its id; its name is different.
SET_ID = "9f3b2c1d-7e4a-4b8c-a1d2-3e4f5a6b7c8d"
SET_NAME = "family photos"
SECRET = "not-a-real-password-3141"

# The stub tier 1: prints its argv and pack cache; exits with the rc in
# $STUB_ONCE_RC_FILE (then deletes it), else $STUB_RC, else 0.  With
# $STUB_SLEEP_FLAG it touches that file and sleeps.  On success it
# writes restored.txt into --target, as a restore would.  restore.sh's
# `--help` pre-flight (#223) is answered without touching any of that.
STUB = textwrap.dedent("""\
    #!/bin/sh
    [ "${1:-}" = "--help" ] && exit 0
    target=""
    prev=""
    for a in "$@"; do
        printf 'ARG: %s\\n' "$a"
        [ "$prev" = "--target" ] && target="$a"
        prev="$a"
    done
    printf 'CACHE: %s\\n' "${LCSAS_PACK_CACHE_DIR-<unset>}"
    if [ -n "${STUB_SLEEP_FLAG:-}" ]; then
        : > "$STUB_SLEEP_FLAG"
        sleep 30
    fi
    rc="${STUB_RC:-0}"
    if [ -f "${STUB_ONCE_RC_FILE:-/nonexistent}" ]; then
        rc="$(cat "$STUB_ONCE_RC_FILE")"
        rm -f "$STUB_ONCE_RC_FILE"
    fi
    [ "$rc" = 0 ] && printf 'restored\\n' > "$target/restored.txt"
    exit "$rc"
""")


def _meta_disc(root: Path, script: bool = False) -> Path:
    """A meta-disc-shaped tree: recovery/ with the stub tier 1, and
    metadata/<id>/ at the ROOT with the set marker.  With *script*, the
    real restore.sh at the root and at recovery/scripts/ (the sentinel
    that says the disc is still in the drive)."""
    recovery = root / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    (bin_dir / "lcsas-restore").write_text(STUB)
    (bin_dir / "lcsas-restore").chmod(0o755)
    (recovery / "scripts").mkdir()
    if script:
        (root / "restore.sh").write_text(RESTORE_SH.read_text())
        (recovery / "scripts" / "restore.sh").write_text(RESTORE_SH.read_text())
    repo = root / "metadata" / SET_ID
    for sub in ("keys", "index", "snapshots"):
        (repo / sub).mkdir(parents=True)
        (repo / sub / f"{sub}-from-meta").write_text(sub)
    (repo / "LCSAS_REPO.txt").write_text(
        f"format=1\nid={SET_ID}\nname={SET_NAME}\nname_cmd=family photos\n"
        "split=none\nsplit_id=\n"
    )
    return recovery


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_PASSWORD": SECRET,
        "TMPDIR": str(_tmpdir(tmp_path)),
        **extra,
    }
    for var in ("LCSAS_PACK_CACHE_DIR", "LCSAS_PWFILE", "LCSAS_REPO",
                "LCSAS_RELOCATED", "LCSAS_META_DISC", "LCSAS_NO_RELOCATE",
                "LCSAS_TIER_FALLBACK", "LCSAS_INTERACTIVE"):
        if var not in extra:
            env.pop(var, None)
    return env


def _tmpdir(tmp_path: Path) -> Path:
    d = tmp_path / "tmpdir"
    d.mkdir(exist_ok=True)
    return d


def _run(recovery: Path, target: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
        timeout=30,
    )


def _arg(stdout: str, flag: str) -> str | None:
    args = [ln[5:] for ln in stdout.splitlines() if ln.startswith("ARG: ")]
    for i, a in enumerate(args[:-1]):
        if a == flag:
            return args[i + 1]
    return None


def _cache(stdout: str) -> str:
    return next(ln[7:] for ln in stdout.splitlines() if ln.startswith("CACHE: "))


# ── Success and failure ──────────────────────────────────────────────


def test_success_uses_the_tools_pack_cache_and_removes_the_folder(tmp_path: Path) -> None:
    """#616: the pack cache is <tools>/pack-cache, not a per-pid folder
    under $TMPDIR; after a success the tools folder is gone and nothing
    is left in $TMPDIR."""
    recovery = _meta_disc(tmp_path / "disc")
    tools = tmp_path / "tools"
    res = _run(recovery, tmp_path / "out", _env(tmp_path, LCSAS_TOOLS_DIR=str(tools)))
    assert res.returncode == 0, res.stderr
    assert _cache(res.stdout) == f"{tools}/pack-cache"
    assert not tools.exists(), "a successful restore must remove the tools folder"
    assert sorted(p.name for p in _tmpdir(tmp_path).iterdir()) == [], (
        "nothing may be left under $TMPDIR (no lcsas-pack-cache.<pid>)"
    )


def test_failure_keeps_the_tools_folder_and_writes_resume_sh(tmp_path: Path) -> None:
    """#609: any exit but success keeps the folder; resume.sh (mode 700)
    names the set by id and the target by its absolute path, and NEVER
    holds the password.  The set's metadata is copied in."""
    recovery = _meta_disc(tmp_path / "disc")
    tools = tmp_path / "tools"
    target = tmp_path / "out"
    res = _run(recovery, target, _env(tmp_path, LCSAS_TOOLS_DIR=str(tools), STUB_RC="1"))
    assert res.returncode == 1, res.stderr

    resume = tools / "resume.sh"
    assert resume.is_file(), res.stderr
    assert stat.S_IMODE(resume.stat().st_mode) == 0o700
    text = resume.read_text()
    assert SECRET not in text
    assert f"LCSAS_REPO='{SET_ID}'" in text, text
    assert f"LCSAS_TOOLS_DIR='{tools}'" in text, text
    assert f"'{target}'" in text, text
    assert "exit code 1" in res.stderr and f"sh {tools}/resume.sh" in res.stderr, res.stderr
    copied = tools / "metadata" / SET_ID
    assert (copied / "LCSAS_REPO.txt").is_file()
    assert (copied / "index" / "index-from-meta").is_file()


def test_set_metadata_from_a_data_disc_replaces_the_older_copy(tmp_path: Path) -> None:
    """C2 for the set being restored: its index/ and snapshots/ in the
    tools folder come from the data disc it was read from (the older
    copy goes), and keys/ are added, so a resumed run with that disc out
    of the drive sees the same snapshots."""
    recovery = _meta_disc(tmp_path / "disc")
    tools = tmp_path / "tools"
    old = tools / "metadata" / SET_ID
    for sub in ("keys", "index", "snapshots"):
        (old / sub).mkdir(parents=True)
        (old / sub / f"{sub}-old").write_text("old")
    (tools / ".lcsas-restore-tools").write_text("")
    media = tmp_path / "media"
    data = media / "LCSAS_TEST_TINY_2026_0002" / "metadata" / SET_ID
    for sub in ("keys", "index", "snapshots"):
        (data / sub).mkdir(parents=True)
        (data / sub / f"{sub}-new").write_text("new")
    (data.parent.parent / "data").mkdir()

    res = _run(recovery, tmp_path / "out", _env(
        tmp_path, LCSAS_TOOLS_DIR=str(tools), LCSAS_MOUNT_DIRS=str(media), STUB_RC="1",
    ))
    assert res.returncode == 1, res.stderr
    assert _arg(res.stdout, "--repo") == str(data)
    assert sorted(p.name for p in (old / "keys").iterdir()) == ["keys-new", "keys-old"]
    assert sorted(p.name for p in (old / "index").iterdir()) == ["index-new"]
    assert sorted(p.name for p in (old / "snapshots").iterdir()) == ["snapshots-new"]


def test_no_home_keeps_the_run_files_in_a_temporary_folder(tmp_path: Path) -> None:
    """With no $HOME (and no LCSAS_TOOLS_DIR) there is no tools folder:
    the pack cache and catalog copy go to a temporary folder that is
    removed whatever the outcome."""
    recovery = _meta_disc(tmp_path / "disc")
    env = _env(tmp_path, STUB_RC="1")
    env.pop("HOME", None)
    env.pop("LCSAS_TOOLS_DIR", None)
    res = _run(recovery, tmp_path / "out", env)
    assert res.returncode == 1, res.stderr
    assert _cache(res.stdout).startswith(f"{_tmpdir(tmp_path)}/lcsas-restore-run.")
    assert sorted(p.name for p in _tmpdir(tmp_path).iterdir()) == []
    assert "resume.sh" not in res.stderr


def test_a_folder_with_other_files_is_never_used(tmp_path: Path) -> None:
    """LCSAS_TOOLS_DIR pointing at a folder that holds other files (say
    $HOME itself) is refused, so it is never emptied or deleted."""
    recovery = _meta_disc(tmp_path / "disc")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "letter.txt").write_text("keep me")
    res = _run(recovery, tmp_path / "out", _env(tmp_path, LCSAS_TOOLS_DIR=str(mine)))
    assert res.returncode == 0, res.stderr
    assert f"cannot use {mine} as the tools folder; using a temporary one" in res.stderr
    assert sorted(p.name for p in mine.iterdir()) == ["letter.txt"]


def test_a_run_that_stops_before_it_starts_leaves_no_empty_folder(tmp_path: Path) -> None:
    """No set found: nothing in the tools folder is worth keeping, so the
    folder this run made goes.  One that holds a copy of the binaries
    (a relocated run) stays for the next run."""
    empty = tmp_path / "empty" / "recovery"
    (empty / "bin" / HOST_TARGET).mkdir(parents=True)
    (empty / "bin" / HOST_TARGET / "lcsas-restore").write_text(STUB)
    (empty / "bin" / HOST_TARGET / "lcsas-restore").chmod(0o755)
    tools = tmp_path / "tools"
    res = _run(empty, tmp_path / "out", _env(tmp_path, LCSAS_TOOLS_DIR=str(tools)))
    assert res.returncode == 1, res.stderr
    assert "no restic repository found" in res.stderr, res.stderr
    assert not tools.exists()

    (tools / "recovery").mkdir(parents=True)
    (tools / ".lcsas-restore-tools").write_text("")
    res = _run(empty, tmp_path / "out", _env(tmp_path, LCSAS_TOOLS_DIR=str(tools)))
    assert res.returncode == 1, res.stderr
    assert f"the tools copied from the disc stay in {tools} for the next run" in res.stderr
    assert (tools / "recovery").is_dir()


# ── Ctrl-C and TERM (#616) ───────────────────────────────────────────


def _traced(argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Popen bypasses the conftest's sh->bash rewrite; mirror it so these
    runs count toward `make shell-coverage`."""
    trace = os.environ.get("LCSAS_SHELL_TRACE")
    if trace and os.environ.get("LCSAS_TRACE_VIA_BASH"):
        return ["bash", *argv[1:]], {**env, "LCSAS_SHELL_TRACE": trace}
    return argv, env


def _interrupt(tmp_path: Path, sig: signal.Signals) -> tuple[int, str, Path]:
    recovery = _meta_disc(tmp_path / "disc")
    tools = tmp_path / "tools"
    flag = tmp_path / "tier1-started"
    argv, env = _traced(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "out"), "latest"],
        _env(tmp_path, LCSAS_TOOLS_DIR=str(tools), STUB_SLEEP_FLAG=str(flag)),
    )
    proc = subprocess.Popen(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + _scaled_timeout(20)
        while not flag.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "tier 1 never started"
            time.sleep(0.05)
        # The terminal's Ctrl-C reaches the whole foreground group.
        os.killpg(proc.pid, sig)
        _, err = proc.communicate(timeout=_scaled_timeout(20))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return proc.returncode, err, tools


def test_ctrl_c_exits_130_and_keeps_the_tools_folder(tmp_path: Path) -> None:
    rc, err, tools = _interrupt(tmp_path, signal.SIGINT)
    assert rc == 130, err
    assert (tools / "resume.sh").is_file(), err
    assert "exit code 130" in err and f"sh {tools}/resume.sh" in err, err


def test_term_exits_143_and_keeps_the_tools_folder(tmp_path: Path) -> None:
    rc, err, tools = _interrupt(tmp_path, signal.SIGTERM)
    assert rc == 143, err
    assert (tools / "resume.sh").is_file(), err


# ── resume.sh after q, with the meta disc gone (#609, #615) ──────────


def test_resume_sh_finishes_after_q_with_the_meta_disc_gone(tmp_path: Path) -> None:
    """The whole journey: run off a (writable stand-in for the) meta disc,
    which relocates into the tools folder; the reader types q at the
    disc prompt (tier 1 exits 75); the meta disc is taken away; resume.sh
    finishes the restore from the tools folder alone, and the folder is
    removed after that success."""
    meta = tmp_path / "LCSAS_META"
    _meta_disc(meta, script=True)
    tools = tmp_path / "tools"
    target = tmp_path / "restored"
    once = tmp_path / "rc-once"
    once.write_text("75")
    env = _env(
        tmp_path, LCSAS_TOOLS_DIR=str(tools), LCSAS_META_DISC=str(meta),
        LCSAS_MOUNT_DIRS=str(tmp_path / "mounts"), STUB_ONCE_RC_FILE=str(once),
    )
    first = subprocess.run(
        ["sh", str(meta / "restore.sh"), str(target)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
        timeout=60,
    )
    assert first.returncode == 75, first.stdout + first.stderr
    assert f"copied recovery binaries to {tools}" in first.stderr, first.stderr
    assert f"sh {tools}/resume.sh" in first.stderr, first.stderr
    assert SECRET not in (tools / "resume.sh").read_text()

    _rmtree(meta)  # the reader takes the META disc out
    second = subprocess.run(
        ["sh", str(tools / "resume.sh")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        env=_env(tmp_path), timeout=60,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert (target / "restored.txt").read_text() == "restored\n"
    assert _arg(second.stdout, "--repo") == f"{tools}/metadata/{SET_ID}"
    assert _arg(second.stdout, "--target") == str(target)
    assert "Choose a repository" not in second.stderr
    assert not tools.exists(), "the tools folder must go after the resumed success"


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path)
