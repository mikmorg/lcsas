"""test_restore_sh_stock_restic_tier.py -- restore.sh tier-2b dispatch.

Component B of the standard-tools tier: when the LCSAS-shipped binaries
(tier-1 lcsas-restore, tier-2 rustic-static) are absent or unrunnable,
restore.sh must AUTOMATICALLY fall through to a stock restic -- the
per-target copy bundled at bin/<arch>/restic, or restic/rustic on PATH --
before the Python last resort.

The blind-restore e2e exercises the MULTI-disc path (where tier 2/2b skip
because there is no single $REPO/data/), so it never drives tier-2b.  This
test pins the dispatch directly with a stub restic that records how
restore.sh invoked it: the bundled binary is preferred, the invocation uses
restic's flag form, and the password is passed via RESTIC_PASSWORD_FILE.
The companion test_stock_restic_compat.py proves a REAL stock restic then
restores the repo byte-identically.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _repo_with_data(metadata_root: Path, name: str) -> Path:
    """A restic-shaped repo dir WITH a data/ subtree (so tier 2/2b don't
    skip themselves as 'multi-disc')."""
    repo = metadata_root / name
    for sub in ("keys", "index", "data", "snapshots"):
        (repo / sub).mkdir(parents=True)
    (repo / "keys" / "stub_key").write_text("stub")
    (repo / "data" / "00").mkdir()
    (repo / "data" / "00" / ("0" * 64)).write_bytes(b"pack")
    return repo


def _install_restic_stub(recovery: Path, target: str = HOST_TARGET) -> Path:
    """A stub restic that records its argv and the password env var, exit 0."""
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "restic"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "PWFILE=${RESTIC_PASSWORD_FILE:-UNSET}"
        for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _run(recovery: Path, target_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "LCSAS_MOUNT_DIRS": "", **env}
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=full_env, timeout=20,
    )


def _args(stdout: str) -> list[str]:
    return [ln.removeprefix("ARG: ") for ln in stdout.splitlines()
            if ln.startswith("ARG: ")]


def test_tier2b_dispatches_bundled_stock_restic_when_tiers_1_2_absent(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    # tier 1 (lcsas-restore) + tier 2 (rustic-static) deliberately ABSENT.
    _install_restic_stub(recovery)  # only a stock restic is present
    repo = _repo_with_data(recovery / "metadata", "alpha")
    target = tmp_path / "restored"

    res = _run(recovery, target, env={})
    assert res.returncode == 0, (
        f"restore.sh did not complete via tier-2b.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    # The tier-2b dispatch message fired.
    assert "tier 2b" in res.stderr, (
        f"restore.sh did not report tier-2b dispatch.\nstderr:\n{res.stderr}"
    )
    args = _args(res.stdout)
    # restic flag form: -r <repo> restore latest --target <dir> --no-lock
    assert "-r" in args and args[args.index("-r") + 1] == str(repo), (
        f"tier-2b did not invoke restic with -r <repo>; argv: {args}"
    )
    assert "restore" in args and "latest" in args, f"argv: {args}"
    assert "--target" in args, f"tier-2b missing --target; argv: {args}"
    assert "--no-lock" in args, f"tier-2b missing --no-lock; argv: {args}"
    # Password delivered via env, not argv.
    assert "PWFILE=" in res.stdout and "PWFILE=UNSET" not in res.stdout, (
        "tier-2b did not pass the password via RESTIC_PASSWORD_FILE.\n"
        f"stdout:\n{res.stdout}"
    )


def test_tier2b_prefers_bundled_restic_over_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-target bundled restic must win over a PATH restic so the
    offline disc copy is what runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    bundled = _install_restic_stub(recovery)
    _repo_with_data(recovery / "metadata", "alpha")
    target = tmp_path / "restored"

    # A decoy restic on PATH that, if used, writes a marker file.
    pathdir = tmp_path / "pathbin"
    pathdir.mkdir()
    decoy = pathdir / "restic"
    decoy.write_text("#!/bin/sh\ntouch %s\nexit 0\n" % (tmp_path / "DECOY_RAN"))
    decoy.chmod(0o755)

    res = _run(recovery, target,
               env={"PATH": f"{pathdir}:{os.environ['PATH']}"})
    assert res.returncode == 0, res.stderr
    assert str(bundled) in res.stderr, (
        f"tier-2b did not prefer the bundled restic {bundled}; stderr:\n{res.stderr}"
    )
    assert not (tmp_path / "DECOY_RAN").exists(), (
        "tier-2b ran the PATH restic instead of the bundled one."
    )


def test_tier2b_classifies_restic_by_basename_not_path(tmp_path: Path) -> None:
    """#368: a stock restic whose full path contains 'rustic' (e.g. it lives
    under a rustic-named parent dir) must still be driven with restic's CLI
    (flag form + RESTIC_PASSWORD_FILE), not mis-classified as rustic from the
    path.  The old `case "$STDTOOL_BIN" in *rustic*` matched the whole path."""
    # Recovery tree under a parent whose name contains 'rustic', so the
    # bundled restic's absolute path contains 'rustic'.
    recovery = tmp_path / "rustic-backups" / "recovery"
    recovery.mkdir(parents=True)
    stub = _install_restic_stub(recovery)
    assert "rustic" in str(stub)  # precondition: path contains 'rustic'
    _repo_with_data(recovery / "metadata", "alpha")
    target = tmp_path / "restored"

    res = _run(recovery, target, env={})
    assert res.returncode == 0, res.stderr
    assert "[tier 2b] using stock restic" in res.stderr, (
        f"restic under a rustic-named path was mis-classified as rustic;\n"
        f"stderr:\n{res.stderr}"
    )
    args = _args(res.stdout)
    assert "--no-lock" in args and "--target" in args, (
        f"expected restic flag form; argv: {args}"
    )
    assert "--password-file" not in args, (
        f"restic takes the password via RESTIC_PASSWORD_FILE, not "
        f"--password-file (rustic's form); argv: {args}"
    )
    assert "PWFILE=" in res.stdout and "PWFILE=UNSET" not in res.stdout
