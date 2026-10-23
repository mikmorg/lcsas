"""Hardening (#517): with nowhere to relocate to, restore.sh warns and goes on.

relocate_to_ram tries $TMPDIR, $XDG_RUNTIME_DIR, /tmp, /run, /var/tmp
and $HOME.  When none of them takes a copy, it prints "cannot find a
writable dir to relocate to; continuing from <disc> (drive will be
held)" and returns 1, and the caller's `|| true` keeps `set -eu` from
ending the script there.  The restore then runs from the disc.  A host
with a second drive can still finish it, and so can any restore whose
packs need no disc swap.  A single-drive host can't eject the meta disc
at the swap prompt; the warning is all it gets.  Hard-failing instead
would take the restore away from the hosts that can finish it.

No test reached this branch before.  A CI host always has a writable
/tmp, so a stub mktemp on PATH refuses the relocation's
`mktemp -d <dir>/lcsas-restore.XXXXXX` in every candidate and passes
every other mktemp call (the password file, tier 3) to the real one.
The run must:

  1. print the warning, naming the disc, and no "copied recovery
     binaries" line;
  2. not stop there: tier 1 runs, from the disc, and the script exits 0;
  3. still pass --meta-disc, and run tier 1 with its cwd off the disc,
     so the script's own file is the only thing holding the drive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

STUB = textwrap.dedent("""\
    #!/bin/sh
    printf 'TIER1: %s\\n' "$0"
    printf 'CWD: %s\\n' "$(pwd)"
    for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
    exit 0
""")


def _meta_disc(tmp_path: Path) -> Path:
    meta = tmp_path / "disc"
    (meta / "recovery" / "scripts").mkdir(parents=True)
    (meta / "restore.sh").write_text(RESTORE_SH.read_text())
    host = meta / "recovery" / "bin" / HOST_TARGET
    host.mkdir(parents=True)
    (host / "lcsas-restore").write_text(STUB)
    (host / "lcsas-restore").chmod(0o755)
    repo = meta / "metadata" / "alpha"
    for sub in ("keys", "index", "data"):
        (repo / sub).mkdir(parents=True)
    return meta


def _refusing_mktemp(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH dir whose mktemp refuses relocation dirs, and its call log."""
    real = shutil.which("mktemp")
    assert real, "no mktemp on PATH"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "mktemp.log"
    (stubs / "mktemp").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$*" in
            *lcsas-restore.*) printf '%s\\n' "$*" >> '{log}'; exit 1 ;;
        esac
        exec '{real}' "$@"
    """))
    (stubs / "mktemp").chmod(0o755)
    return stubs, log


def test_no_scratch_dir_warns_and_restores_from_the_disc(tmp_path: Path) -> None:
    meta = _meta_disc(tmp_path)
    stubs, log = _refusing_mktemp(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        # Names the disc: the trigger lcsas-init uses, and one that works
        # as root too (a 0555 dir doesn't stop root writing to it).
        "LCSAS_META_DISC": str(meta),
        "LCSAS_MOUNT_DIRS": str(meta),
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_TARGET": HOST_TARGET,
    }
    for k in ("LCSAS_RELOCATED", "LCSAS_NO_RELOCATE"):
        env.pop(k, None)
    res = subprocess.run(
        ["sh", str(meta / "restore.sh"), str(tmp_path / "out")],
        input="pw\n", capture_output=True, text=True, env=env, timeout=60,
        cwd=meta,
    )
    out = res.stdout + res.stderr

    tried = log.read_text().splitlines() if log.exists() else []
    assert any(t.startswith(f"-d {tmp_path}/lcsas-restore.") for t in tried), (
        f"the stub mktemp never refused $TMPDIR, so the test didn't reach the "
        f"no-scratch branch; it saw: {tried}\n{out}"
    )
    assert (
        "cannot find a writable dir to relocate to; "
        f"continuing from {meta} (drive will be held)"
    ) in res.stderr, out
    assert "copied recovery binaries to" not in out, out

    assert res.returncode == 0, f"rc {res.returncode}: the script stopped\n{out}"
    disc_bin = meta / "recovery" / "bin" / HOST_TARGET / "lcsas-restore"
    assert f"TIER1: {disc_bin}" in res.stdout, (
        f"tier 1 did not run from the disc:\n{out}"
    )

    args = [ln[len("ARG: "):] for ln in res.stdout.splitlines() if ln.startswith("ARG: ")]
    assert "--meta-disc" in args and args[args.index("--meta-disc") + 1] == str(meta), args
    assert "CWD: /" in res.stdout.splitlines(), (
        f"tier 1 ran with its cwd on the disc, holding it:\n{res.stdout}"
    )
