"""#768: ``scripts/cdemu_drive.sh start`` leaves cdemu device 0 mapped;
#756: ``unload`` waits until device 0 is empty.

After a run of blind-restore teardowns the cdemu daemon stayed up with
device 0 mapped to no ``/dev/srN``, and ``make verify-burn-e2e`` errored
until someone restarted the user daemon by hand.  ``start`` now reads
``cdemu device-mapping``: device 0 unmapped means one restart of the
daemon, then a clear failure.

Runs the real script against stub ``cdemu``, ``systemctl``, ``lsmod``
and ``sudo`` on PATH, so no vhba module, daemon or root is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cdemu_drive.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="cdemu_drive.sh refuses to run as root",
    ),
]

MAPPED = "Device mapping:\nDEV   SCSI CD-ROM     SCSI generic\n0     /dev/null       /dev/null\n"
UNMAPPED = "Device mapping:\nDEV   SCSI CD-ROM     SCSI generic\n0     N/A             N/A\n"


def _stubs(tmp: Path, mode: str, loaded_for: int = 0) -> dict[str, str]:
    """A PATH with stub tools.  *mode*: ``mapped`` (device 0 mapped from
    the start), ``after-restart`` (mapped once the daemon restarts),
    ``never`` (never mapped).  ``cdemu status`` says device 0 is loaded
    for its first *loaded_for* calls (-1: always).  Every stub call is
    logged."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "calls.log"
    restarted = tmp / "restarted"
    (tmp / "mapped.txt").write_text(MAPPED)
    (tmp / "unmapped.txt").write_text(UNMAPPED)
    stubs = {
        "cdemu": f"""\
            echo "cdemu $*" >> "{log}"
            if [ "$1" = status ]; then
                n=$(cat "{tmp}/status-calls" 2>/dev/null || echo 0)
                echo $((n + 1)) > "{tmp}/status-calls"
                echo "Devices' status:"
                echo "DEV   LOADED     FILENAME"
                if [ {loaded_for} -lt 0 ] || [ "$n" -lt {loaded_for} ]; then
                    echo "0     True       /x/disc.iso"
                else
                    echo "0     False"
                fi
                exit 0
            fi
            [ "$1" = device-mapping ] || exit 0
            case "{mode}" in
                mapped) cat "{tmp}/mapped.txt" ;;
                after-restart)
                    if [ -e "{restarted}" ]; then cat "{tmp}/mapped.txt"
                    else cat "{tmp}/unmapped.txt"; fi ;;
                *) cat "{tmp}/unmapped.txt" ;;
            esac
            """,
        "systemctl": f"""\
            echo "systemctl $*" >> "{log}"
            case " $* " in *" restart "*) : > "{restarted}" ;; esac
            exit 0
            """,
        "lsmod": "echo 'vhba 45056 0'\n",
        "sudo": f'echo "sudo $*" >> "{log}"\nexit 0\n',
    }
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        p.chmod(0o755)
    ctl = tmp / "vhba_ctl"
    ctl.write_text("")
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp),
        "USER": "tester",
        "VHBA_CTL": str(ctl),
        "CDEMU_MAP_TRIES": "2",
        "CDEMU_UNLOAD_TRIES": "10",
    }


def _run(tmp: Path, mode: str, *args: str,
         loaded_for: int = 0) -> tuple[subprocess.CompletedProcess, str]:
    env = _stubs(tmp, mode, loaded_for)
    r = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True,
        text=True, timeout=60,
    )
    log = tmp / "calls.log"
    return r, log.read_text() if log.exists() else ""


def test_start_mapped_needs_no_restart(tmp_path):
    r, calls = _run(tmp_path, "mapped", "start")
    assert r.returncode == 0, r.stderr
    assert "virtual drive ready at /dev/null" in r.stdout
    assert "systemctl --user start cdemu-daemon.service" in calls
    assert "restart" not in calls
    assert "sudo" not in calls  # vhba is "loaded"


def test_start_unmapped_restarts_the_daemon_once(tmp_path):
    r, calls = _run(tmp_path, "after-restart", "start")
    assert r.returncode == 0, r.stderr
    assert calls.count("systemctl --user restart cdemu-daemon.service") == 1
    assert "restarting cdemu-daemon once" in r.stdout
    assert "virtual drive ready at /dev/null" in r.stdout


def test_start_still_unmapped_fails_naming_the_problem(tmp_path):
    r, calls = _run(tmp_path, "never", "start")
    assert r.returncode == 1
    assert calls.count("systemctl --user restart cdemu-daemon.service") == 1
    assert "device 0 is still not mapped" in r.stderr
    assert "virtual drive ready" not in r.stdout
    # What the daemon reported is shown, so the reader sees the row.
    assert "N/A" in r.stderr


def test_device_prints_the_mapped_node(tmp_path):
    r, _ = _run(tmp_path, "mapped", "device")
    assert r.returncode == 0 and r.stdout.strip() == "/dev/null"


def test_device_falls_back_to_sr0_when_unmapped(tmp_path):
    r, _ = _run(tmp_path, "never", "device")
    assert r.returncode == 0 and r.stdout.strip() == "/dev/sr0"


def test_unload_waits_until_device0_is_empty(tmp_path):
    """#756: ``cdemu unload 0`` exits 0 while the image stays in for about
    2 s after a umount; the next ``load`` then failed with "Device is
    already loaded".  ``unload`` returns only once ``status`` says empty,
    and asks again each second meanwhile."""
    r, calls = _run(tmp_path, "mapped", "unload", loaded_for=7)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "status-calls").read_text().strip() == "8"
    assert calls.count("cdemu unload 0") == 2  # the first ask, then at 1 s


def test_unload_fails_when_the_image_stays_in(tmp_path):
    r, _ = _run(tmp_path, "mapped", "unload", loaded_for=-1)
    assert r.returncode == 1
    assert "device 0 still holds an image" in r.stderr
    assert "/x/disc.iso" in r.stderr
    assert "ejected" not in r.stdout


def test_swap_does_not_load_while_the_image_stays_in(tmp_path):
    r, calls = _run(tmp_path, "mapped", "swap", str(tmp_path / "new.iso"),
                    loaded_for=-1)
    assert r.returncode == 1
    assert "cdemu load" not in calls
