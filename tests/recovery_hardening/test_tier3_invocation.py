"""Hardening test #4: restore.sh tier-3 invocation flag correctness.

`recovery/scripts/restore.sh` cascades through three recovery tiers.
When tier 1 (prebuilt lcsas-restore) and tier 2 (rustic-static) are
both absent, it `exec`s the bundled `standalone_restorer.py` as the
last-resort restorer.  The blind run that exposed the fragility had a
broken tier-3 invocation:

    exec "$PYBIN" "$PYREST" "$REPO" "$TARGET" --password-file "$PWFILE"

…but `standalone_restorer.py`'s CLI parser (defined in
`src/lcsas/restore/standalone_builder.py:_cli_main`) requires
`--repo`, `--target`, `--password-file` as named flags, not
positional.  Tier 3 silently errored out without restoring anything
and the agent in the blind run was forced to improvise around it.

This test pins the corrected invocation: standalone_restorer.py must
be called with named flags only.

What it catches:
  - Reverting the tier-3 fix back to positional args.
  - Adding a new arg to standalone_restorer.py without plumbing it
    through restore.sh.
  - Renaming a flag on one side without updating the other.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _make_minimal_repo(root: Path) -> Path:
    repo = root / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def _install_python_stub(bin_dir: Path) -> Path:
    """A `python3` shim that captures argv to a file and exits 0.

    We put it on a private PATH so restore.sh's tier-3 fall-through
    picks it up instead of the system python3.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "argv.log"
    stub = bin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        # Capture argv to {log} so the test can inspect what
        # restore.sh actually invoked us with.
        : > {log}
        for a in "$@"; do
            printf '%s\\n' "$a" >> {log}
        done
        exit 0
    """))
    stub.chmod(0o755)
    return log


def test_tier3_invokes_with_named_flags(tmp_path: Path) -> None:
    """No tier-1/tier-2 binary → restore.sh execs standalone_restorer.py
    with --repo / --target / --password-file."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    # restore.sh's pattern-1 invocation requires bin/ to exist in the
    # recovery root; an empty bin/ dir is enough — no binaries inside
    # forces the tier cascade past 1 and 2 into tier 3.
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    repo = _make_minimal_repo(recovery)
    # Place a fake standalone_restorer.py next to recovery/ so the
    # tier-3 search finds it (one of the candidate paths is
    # "$RECOVERY/../standalone_restorer.py").
    fake_restorer = recovery.parent / "standalone_restorer.py"
    fake_restorer.write_text("# placeholder for tier-3\n")

    # Capture argv through a stub python3 on PATH.
    pybin_dir = tmp_path / "stubbin"
    argv_log = _install_python_stub(pybin_dir)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        # Force the script down the tier-3 path: no tier-1 or tier-2
        # binary is installed at recovery/bin/<target>/, so it falls
        # through to Python.
    }

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh exited {res.returncode}; stderr:\n{res.stderr}"
    )
    assert argv_log.is_file(), "tier 3 was not reached"
    args = argv_log.read_text().splitlines()

    # First arg should be the script path itself.
    assert args[0].endswith("standalone_restorer.py"), (
        f"python3 was not invoked with standalone_restorer.py first; "
        f"argv: {args}"
    )

    # Required flag-style args.
    assert "--repo" in args, f"--repo missing from tier-3 invocation: {args}"
    assert args[args.index("--repo") + 1] == str(repo), (
        f"--repo arg points to wrong path: {args}"
    )
    assert "--target" in args, f"--target missing: {args}"
    assert args[args.index("--target") + 1] == str(target), args
    assert "--password-file" in args, f"--password-file missing: {args}"

    # Positional REPO / TARGET (the legacy broken form) must NOT
    # appear after the script name.  Any non-flag arg whose immediate
    # predecessor isn't a flag is a stray positional.
    after_script = args[1:]
    positional = [
        a for i, a in enumerate(after_script)
        if not a.startswith("--")
        and not (i > 0 and after_script[i - 1].startswith("--"))
    ]
    assert not positional, (
        f"tier-3 invocation has stray positional args {positional}; "
        f"restore.sh has reverted to the broken positional UX."
    )
    # Specifically the values used as positionals in the broken form:
    assert str(repo) not in positional, (
        "repo appears as a positional arg — tier-3 invocation is broken."
    )
    assert str(target) not in positional, (
        "target appears as a positional arg — tier-3 invocation is broken."
    )


def test_tier3_passes_snapshot_when_not_latest(tmp_path: Path) -> None:
    """A non-`latest` snapshot ID should propagate via --snapshot."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_minimal_repo(recovery)
    fake_restorer = recovery.parent / "standalone_restorer.py"
    fake_restorer.write_text("# placeholder for tier-3\n")

    pybin_dir = tmp_path / "stubbin"
    argv_log = _install_python_stub(pybin_dir)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target),
         "deadbeefcafe1234"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, res.stderr
    args = argv_log.read_text().splitlines()
    assert "--snapshot" in args
    assert args[args.index("--snapshot") + 1] == "deadbeefcafe1234"


def test_tier3_omits_snapshot_when_latest(tmp_path: Path) -> None:
    """For `latest` (the default), --snapshot should be omitted so the
    restorer falls back to its own latest-resolution logic."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_minimal_repo(recovery)
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")

    pybin_dir = tmp_path / "stubbin"
    argv_log = _install_python_stub(pybin_dir)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, res.stderr
    args = argv_log.read_text().splitlines()
    assert "--snapshot" not in args, (
        f"--snapshot leaked into tier-3 invocation for latest; argv: {args}"
    )


def test_standalone_restorer_cli_uses_named_flags() -> None:
    """Sanity: the standalone_restorer.py CLI parser actually does
    require these flags.  If a future commit makes them positional,
    the tier-3 tests above would still pass but production would
    break — so we also pin the parser spec."""
    builder = REPO_ROOT / "src" / "lcsas" / "restore" / "standalone_builder.py"
    src = builder.read_text()
    for flag in ("--repo", "--password-file", "--target"):
        assert re.search(
            rf'add_argument\(\s*[\'"]{re.escape(flag)}[\'"]', src
        ), (
            f"standalone_restorer.py's argparse spec does not register "
            f"{flag} — restore.sh's tier-3 fallback will break."
        )
