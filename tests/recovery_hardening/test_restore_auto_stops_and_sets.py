"""test_restore_auto_stops_and_sets.py -- restore_auto.sh's loop (#801).

restore_auto.sh runs restore.sh once per backup set.  Three bugs:

1. A set that stopped with exit 75 (the person typed q at a disc prompt)
   or with a full disk did not end the loop: the next set started.  Now
   the loop ends with that exit code and a message naming the set (and
   the sets not restored).  Ctrl-C, TERM and a closed terminal (130,
   143, 129) end it too.  restore.sh exits 1 for a full disk, as for any
   failure, so restore_auto.sh reads restore.sh's messages for the
   tier 1 / tier 3 / OS wording; a free-space shortfall found before
   the restore starts (nothing written) is an ordinary failed set.
2. Every set shared one tools folder, so a later set's success removed
   an earlier, stopped set's resume.sh and pack cache.  Now each set has
   its own, <tools folder>-<id>, which only that set's success removes.
3. Only the meta disc's sets were listed: a set on a data disc burned
   after the meta disc was never restored.  Now the mounted discs that
   restore.sh scans (LCSAS_MOUNT_DIRS) are listed too.

The first group stubs restore.sh, as test_restore_auto_gaps.py does.
The second runs the real restore.sh with a stub tier 1, so the tools
folder, resume.sh and the disc scan are restore.sh's own.
"""

from __future__ import annotations

import errno
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_AUTO = REPO_ROOT / "recovery" / "scripts" / "restore_auto.sh"
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

# What each tier says when the disk fills up.  test_full_disk_phrases_
# are_still_what_the_tiers_print pins them to the sources.
TIER1_FULL = "ERROR: target directory out of space (path=/x/y, errno=28)"
TIER3_FULL = (
    "[lcsas-restore] the filesystem that holds /x is full (No space left on "
    "device) while writing y.  Free some space there"
)
QUOTA_FULL = "rustic: Disk quota exceeded (os error 122)"


# ── Stubbed restore.sh ───────────────────────────────────────────────


def _stub(recovery: Path, behaviour: dict[str, str]) -> None:
    """restore.sh stub: record LCSAS_BACKUP_SET and LCSAS_TOOLS_DIR, print one
    stdout line, then run the shell snippet *behaviour[set]* (default:
    exit 0)."""
    lines = [
        "#!/bin/sh",
        'printf "%s|%s\\n" "${LCSAS_BACKUP_SET:-UNSET}" "${LCSAS_TOOLS_DIR-UNSET}" >> "$RECORD"',
        'printf "stdout of %s\\n" "$LCSAS_BACKUP_SET"',
        'case "$LCSAS_BACKUP_SET" in',
    ]
    for name, snippet in behaviour.items():
        lines.append(f"    {name}) {snippet} ;;")
    lines += ["esac", "exit 0"]
    scripts = recovery / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "restore.sh").write_text("\n".join(lines) + "\n")
    (scripts / "restore.sh").chmod(0o755)


class _Auto:
    def __init__(self, tmp_path: Path, sets: tuple[str, ...] = ("alpha", "bravo", "charlie")):
        self.tmp_path = tmp_path
        self.recovery = tmp_path / "recovery"
        for name in sets:
            (self.recovery / "repos" / name / "keys").mkdir(parents=True)
        self.target = tmp_path / "restored"
        self.record = tmp_path / "calls.txt"
        self.pwfile = tmp_path / "pw"
        self.pwfile.write_text("secret\n")
        self.tools = tmp_path / "tools"

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("LCSAS_PASSWORD_FILE", "LCSAS_TOOLS_DIR", "LCSAS_BACKUP_SET")}
        env.update({
            "LCSAS_PASSWORD_FILE": str(self.pwfile),
            "RECORD": str(self.record),
            "LCSAS_MOUNT_DIRS": "",
            "LCSAS_TOOLS_DIR": str(self.tools),
        })
        env.update(extra)
        return env

    def run(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(RESTORE_AUTO), str(self.recovery), str(self.target)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env=env or self.env(), timeout=30,
        )

    def calls(self) -> list[str]:
        if not self.record.exists():
            return []
        return [ln.split("|")[0] for ln in self.record.read_text().splitlines() if ln]

    def tools_dirs(self) -> list[str]:
        return [ln.split("|", 1)[1] for ln in self.record.read_text().splitlines() if ln]


class TestStops:
    def test_exit_75_ends_the_loop_with_75_and_names_the_set(self, tmp_path):
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": "exit 75"})

        res = a.run()

        assert res.returncode == 75, res.stderr
        assert a.calls() == ["alpha"], "no set may start after a stop at a disc prompt"
        assert "The restore of alpha stopped: you chose to stop" in res.stderr
        assert "These backup sets were not restored: bravo, charlie" in res.stderr
        assert "run this same command again" in res.stderr

    @pytest.mark.parametrize("message", [TIER1_FULL, TIER3_FULL, QUOTA_FULL],
                             ids=["tier1", "tier3", "quota"])
    def test_a_full_disk_ends_the_loop_with_its_exit_code(self, tmp_path, message):
        a = _Auto(tmp_path)
        _stub(a.recovery, {"bravo": f"printf '%s\\n' '{message}' >&2; exit 1"})

        res = a.run()

        assert res.returncode == 1, res.stderr
        assert a.calls() == ["alpha", "bravo"], "no set may start after a full disk"
        assert "The restore of bravo stopped: the disk is full" in res.stderr
        assert "These backup sets were not restored: charlie" in res.stderr
        # restore.sh's own words still reach the screen, once.
        assert res.stderr.count(message) == 1, res.stderr

    def test_exit_74_ends_the_loop_with_74(self, tmp_path):
        """restore.sh exits 74 when the disk is full (#799): no message
        needs to be read for it."""
        a = _Auto(tmp_path)
        _stub(a.recovery, {"bravo": "exit 74"})

        res = a.run()

        assert res.returncode == 74, res.stderr
        assert a.calls() == ["alpha", "bravo"], "no set may start after a full disk"
        assert "The restore of bravo stopped: the disk is full" in res.stderr
        assert "These backup sets were not restored: charlie" in res.stderr

    def test_exit_4_is_finished_and_the_loop_goes_on(self, tmp_path):
        """restore.sh exit 4 (#799): the set finished, but some items
        could not be restored.  The next sets are restored; the loop
        ends with 4, and 1 wins when a set failed."""
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": "exit 4"})

        res = a.run()

        assert res.returncode == 4, res.stderr
        assert a.calls() == ["alpha", "bravo", "charlie"]
        assert "alpha finished, but some items could not be restored" in res.stderr
        assert f"{a.target}/alpha/RESTORE_FAILURES.txt" in res.stderr
        assert "FAILED" not in res.stderr, res.stderr

        b = _Auto(tmp_path / "b")
        _stub(b.recovery, {"alpha": "exit 4", "bravo": "exit 9"})
        assert b.run().returncode == 1

    def test_help_prints_the_header(self, tmp_path):
        res = subprocess.run(["sh", str(RESTORE_AUTO), "--help"],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        assert "keep a disc\nthat holds it mounted" in res.stdout, res.stdout
        assert "Every other set is restored again" in res.stdout, res.stdout
        assert "#!/bin/sh" not in res.stdout
        assert "set -eu" not in res.stdout

    def test_a_full_disk_on_the_last_set_names_no_other(self, tmp_path):
        a = _Auto(tmp_path)
        _stub(a.recovery, {"charlie": f"echo '{TIER1_FULL}' >&2; exit 1"})
        res = a.run()
        assert res.returncode == 1, res.stderr
        assert "The restore of charlie stopped: the disk is full" in res.stderr
        assert "were not restored" not in res.stderr

    def test_a_space_shortfall_before_the_restore_is_an_ordinary_failure(self, tmp_path):
        """restore.sh's free-space check refuses a set before writing
        anything: the disk is not full, and a smaller set may fit."""
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": (
            "echo 'restore.sh: Need about 9.0 GB free at /t; only 1.0 GB available.' >&2;"
            " exit 1")})
        res = a.run()
        assert res.returncode == 1, res.stderr
        assert a.calls() == ["alpha", "bravo", "charlie"]
        assert "restore of alpha FAILED (exit 1)" in res.stderr
        assert "stopped" not in res.stderr

    def test_a_full_disk_message_from_a_set_that_succeeded_is_ignored(self, tmp_path):
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": f"echo '{TIER1_FULL}' >&2; exit 0"})
        res = a.run()
        assert res.returncode == 0, res.stderr
        assert a.calls() == ["alpha", "bravo", "charlie"]

    def test_wrong_password_still_carries_on(self, tmp_path):
        """#384 is unchanged: exit 77 is one set's password, not a stop."""
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": "exit 77"})
        res = a.run()
        assert res.returncode == 1, res.stderr
        assert a.calls() == ["alpha", "bravo", "charlie"]
        assert "restore of alpha FAILED: wrong password (exit 77)" in res.stderr

    @pytest.mark.parametrize("code", [129, 130, 143])
    def test_a_signal_exit_ends_the_loop(self, tmp_path, code):
        a = _Auto(tmp_path)
        _stub(a.recovery, {"alpha": f"exit {code}"})
        res = a.run()
        assert res.returncode == code, res.stderr
        assert a.calls() == ["alpha"]
        assert "The restore of alpha stopped: it was interrupted" in res.stderr

    def test_restore_sh_stdout_and_stderr_keep_their_streams(self, tmp_path):
        """The copy of restore.sh's messages for the full-disk check must
        not move them: stdout stays stdout, stderr stays stderr."""
        a = _Auto(tmp_path, sets=("alpha",))
        _stub(a.recovery, {"alpha": "echo 'on stderr' >&2; exit 0"})
        res = a.run()
        assert res.returncode == 0, res.stderr
        assert res.stdout == "stdout of alpha\n"
        assert "on stderr" in res.stderr
        assert "lcsas-auto-rc" not in res.stdout + res.stderr


class TestPerSetToolsFolder:
    def test_each_set_gets_its_own_tools_folder(self, tmp_path):
        a = _Auto(tmp_path)
        _stub(a.recovery, {})
        res = a.run()
        assert res.returncode == 0, res.stderr
        assert a.tools_dirs() == [f"{a.tools}-{n}" for n in ("alpha", "bravo", "charlie")]

    def test_the_default_tools_folder_is_under_home(self, tmp_path):
        a = _Auto(tmp_path, sets=("alpha",))
        _stub(a.recovery, {})
        env = a.env(HOME=str(tmp_path / "home"))
        env.pop("LCSAS_TOOLS_DIR")
        res = a.run(env)
        assert res.returncode == 0, res.stderr
        assert a.tools_dirs() == [f"{tmp_path}/home/lcsas-tools-alpha"]

    def test_a_relative_tools_folder_is_made_absolute(self, tmp_path):
        a = _Auto(tmp_path, sets=("alpha",))
        _stub(a.recovery, {})
        res = subprocess.run(
            ["sh", str(RESTORE_AUTO), str(a.recovery), str(a.target)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env=a.env(LCSAS_TOOLS_DIR="rel/tools/"), cwd=tmp_path, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        assert a.tools_dirs() == [f"{tmp_path}/rel/tools-alpha"]

    def test_no_home_and_no_tools_folder_passes_none(self, tmp_path):
        a = _Auto(tmp_path, sets=("alpha",))
        _stub(a.recovery, {})
        env = a.env()
        env.pop("LCSAS_TOOLS_DIR")
        env.pop("HOME", None)
        res = a.run(env)
        assert res.returncode == 0, res.stderr
        assert a.tools_dirs() == [""]


class TestSetsOnMountedDiscs:
    def _disc_set(self, disc: Path, name: str, index: bool = True) -> None:
        (disc / "metadata" / name / "keys").mkdir(parents=True)
        if index:
            (disc / "metadata" / name / "index").mkdir()

    def test_a_set_only_on_a_data_disc_is_restored(self, tmp_path):
        a = _Auto(tmp_path, sets=())
        # The meta disc's layout: recovery/ next to metadata/.
        (tmp_path / "metadata" / "old-set" / "keys").mkdir(parents=True)
        media = tmp_path / "media"
        self._disc_set(media / "LCSAS_CD700_2026_0002", "old-set")
        self._disc_set(media / "LCSAS_CD700_2026_0002", "new-set")
        _stub(a.recovery, {})

        res = a.run(a.env(LCSAS_MOUNT_DIRS=str(media)))

        assert res.returncode == 0, res.stderr
        assert a.calls() == ["old-set", "new-set"]
        assert (a.target / "new-set").is_dir()

    def test_a_mount_folder_that_is_itself_the_disc(self, tmp_path):
        """/mnt holding metadata/ directly (the disc mounted AT /mnt)."""
        a = _Auto(tmp_path, sets=())
        drive = tmp_path / "mnt"
        self._disc_set(drive, "only-here")
        _stub(a.recovery, {})
        res = a.run(a.env(LCSAS_MOUNT_DIRS=f"{tmp_path}/missing::{drive}"))
        assert res.returncode == 0, res.stderr
        assert a.calls() == ["only-here"]

    def test_a_disc_copy_without_index_is_not_a_set(self, tmp_path):
        """restore.sh takes a disc's copy only with keys/ and index/."""
        a = _Auto(tmp_path, sets=())
        media = tmp_path / "media"
        self._disc_set(media / "disc", "keys-only", index=False)
        _stub(a.recovery, {})
        res = a.run(a.env(LCSAS_MOUNT_DIRS=str(media)))
        assert res.returncode == 1, res.stderr
        assert "no backup set found" in res.stderr
        assert a.calls() == []

    def test_empty_mount_dirs_scans_no_disc(self, tmp_path):
        a = _Auto(tmp_path, sets=())
        media = tmp_path / "media"
        self._disc_set(media / "disc", "on-disc")
        _stub(a.recovery, {})
        res = a.run(a.env(LCSAS_MOUNT_DIRS=""))
        assert res.returncode == 1, res.stderr
        assert a.calls() == []

    def test_the_repos_layout_still_wins(self, tmp_path):
        a = _Auto(tmp_path, sets=("alpha",))
        media = tmp_path / "media"
        self._disc_set(media / "disc", "on-disc")
        _stub(a.recovery, {})
        res = a.run(a.env(LCSAS_MOUNT_DIRS=str(media)))
        assert res.returncode == 0, res.stderr
        assert a.calls() == ["alpha"]


# ── The real restore.sh, with a stub tier 1 ─────────────────────────

# Tier 1 stub: exits with $STUB_RC_<id> (dashes as underscores) for the
# set named by --repo, after printing $STUB_MSG_<id> to stderr; on
# success writes restored.txt into --restore-folder.
TIER1 = textwrap.dedent("""\
    #!/bin/sh
    [ "${1:-}" = "--help" ] && exit 0
    repo="" target="" prev=""
    for a in "$@"; do
        [ "$prev" = "--repo" ] && repo="$a"
        [ "$prev" = "--restore-folder" ] && target="$a"
        prev="$a"
    done
    id="$(basename "$repo" | tr - _)"
    printf 'TIER1 %s\\n' "$repo"
    eval "msg=\\${STUB_MSG_$id:-}"
    eval "rc=\\${STUB_RC_$id:-0}"
    [ -z "$msg" ] || printf '%s\\n' "$msg" >&2
    [ "$rc" = 0 ] && printf 'restored\\n' > "$target/restored.txt"
    exit "$rc"
""")

SET_A = "0a1b2c3d-1111-4aaa-8bbb-000000000001"
SET_B = "0a1b2c3d-2222-4aaa-8bbb-000000000002"
SET_C = "0a1b2c3d-3333-4aaa-8bbb-000000000003"


def _set(md: Path, set_id: str) -> None:
    for sub in ("keys", "index", "snapshots"):
        (md / set_id / sub).mkdir(parents=True)
        (md / set_id / sub / f"{sub}-file").write_text(sub)
    (md / set_id / "LCSAS_BACKUP_SET.txt").write_text(
        f"format=1\nid={set_id}\nname=set {set_id[9:13]}\nsplit=none\nsplit_id=\n"
    )


class _Real:
    """A meta-disc-shaped tree (writable, so restore.sh runs in place)."""

    def __init__(self, tmp_path: Path, sets: tuple[str, ...]) -> None:
        self.tmp_path = tmp_path
        self.recovery = tmp_path / "disc" / "recovery"
        bin_dir = self.recovery / "bin" / HOST_TARGET
        bin_dir.mkdir(parents=True)
        (bin_dir / "lcsas-restore").write_text(TIER1)
        (bin_dir / "lcsas-restore").chmod(0o755)
        (self.recovery / "scripts").mkdir()
        (self.recovery / "scripts" / "restore.sh").write_text(RESTORE_SH.read_text())
        for set_id in sets:
            _set(tmp_path / "disc" / "metadata", set_id)
        self.target = tmp_path / "restored"
        self.tools = tmp_path / "tools"
        self.pwfile = tmp_path / "pw"
        self.pwfile.write_text("not-a-real-password\n")
        self.tmpdir = tmp_path / "tmpdir"
        self.tmpdir.mkdir()

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LCSAS_", "STUB_"))}
        env.update({
            "LCSAS_PASSWORD_FILE": str(self.pwfile),
            "LCSAS_TOOLS_DIR": str(self.tools),
            "LCSAS_MOUNT_DIRS": "",
            "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
            "LCSAS_SKIP_SPACE_CHECK": "1",
            "LCSAS_PLATFORM": HOST_TARGET,
            "TMPDIR": str(self.tmpdir),
        })
        env.update(extra)
        return subprocess.run(
            ["sh", str(RESTORE_AUTO), str(self.recovery), str(self.target)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
            timeout=60,
        )

    def tools_of(self, set_id: str) -> Path:
        return Path(f"{self.tools}-{set_id}")


def _key(set_id: str) -> str:
    return set_id.replace("-", "_")


class TestWithTheRealRestoreSh:
    def test_a_later_success_keeps_an_earlier_sets_resume_state(self, tmp_path):
        """Set A fails (the loop goes on), set B succeeds: A's resume.sh
        must still be there, B's tools folder gone.  With one shared
        tools folder, B's success removed A's."""
        r = _Real(tmp_path, (SET_A, SET_B))

        res = r.run(**{f"STUB_RC_{_key(SET_A)}": "1"})

        assert res.returncode == 1, res.stderr
        resume = r.tools_of(SET_A) / "resume.sh"
        assert resume.is_file(), res.stderr
        assert f"LCSAS_BACKUP_SET='{SET_A}'" in resume.read_text()
        assert f"LCSAS_TOOLS_DIR='{r.tools_of(SET_A)}'" in resume.read_text()
        assert not r.tools_of(SET_B).exists(), "a set's success removes its own tools folder"
        assert (r.target / SET_B / "restored.txt").is_file()
        assert not r.tools.exists(), "the shared tools folder is not used"

    def test_q_at_a_disc_prompt_stops_and_keeps_that_sets_resume_state(self, tmp_path):
        r = _Real(tmp_path, (SET_A, SET_B))

        res = r.run(**{f"STUB_RC_{_key(SET_A)}": "75"})

        assert res.returncode == 75, res.stderr
        assert f"The restore of {SET_A} stopped: you chose to stop" in res.stderr
        assert f"These backup sets were not restored: {SET_B}" in res.stderr
        assert (r.tools_of(SET_A) / "resume.sh").is_file()
        assert f"TIER1 {r.tmp_path}" in res.stdout
        assert SET_B not in res.stdout, "set B must not start"
        assert not (r.target / SET_B).exists()

    def test_running_again_carries_the_stopped_set_on_with_its_snapshot(self, tmp_path):
        """Set A stops at a disc prompt with only the meta disc in.  A
        newer data disc holding A is mounted before the second run: a
        fresh restore.sh would read A off that disc (perhaps a newer
        snapshot, leaving a mix of two); the loop runs A's resume.sh
        instead, which keeps the copy the stopped run used."""
        r = _Real(tmp_path, (SET_A, SET_B))
        first = r.run(**{f"STUB_RC_{_key(SET_A)}": "75"})
        assert first.returncode == 75, first.stderr

        media = tmp_path / "media"
        disc = media / "LCSAS_CD700_2026_0002"
        _set(disc / "metadata", SET_A)
        _set(disc / "metadata", SET_B)
        (disc / "data").mkdir()
        (disc / "VOLUME_INFO.txt").write_text("  Label:  LCSAS_CD700_2026_0002\n")

        res = r.run(LCSAS_MOUNT_DIRS=str(media))

        assert res.returncode == 0, res.stderr
        assert f"carrying on the restore of {SET_A} that stopped" in res.stderr
        assert f"TIER1 {r.tools_of(SET_A)}/metadata/{SET_A}" in res.stdout, res.stdout
        # Set B never ran before: a fresh run, off the newest disc.
        assert f"TIER1 {disc}/metadata/{SET_B}" in res.stdout, res.stdout
        assert (r.target / SET_A / "restored.txt").is_file()
        assert not r.tools_of(SET_A).exists(), "A's success removes its tools folder"

    def test_a_resume_sh_for_another_folder_is_not_used(self, tmp_path):
        """resume.sh carries on into the folder it names; a run into
        another TARGET_ROOT restores the set afresh."""
        r = _Real(tmp_path, (SET_A,))
        first = r.run(**{f"STUB_RC_{_key(SET_A)}": "75"})
        assert first.returncode == 75, first.stderr
        r.target = tmp_path / "elsewhere"

        res = r.run()

        assert res.returncode == 0, res.stderr
        assert "carrying on" not in res.stderr
        assert (r.target / SET_A / "restored.txt").is_file()

    def test_tier1_out_of_space_stops_the_loop(self, tmp_path):
        r = _Real(tmp_path, (SET_A, SET_B))

        res = r.run(**{f"STUB_RC_{_key(SET_A)}": "1", f"STUB_MSG_{_key(SET_A)}": TIER1_FULL})

        assert res.returncode == 1, res.stderr
        assert f"The restore of {SET_A} stopped: the disk is full" in res.stderr
        assert SET_B not in res.stdout, "set B must not start"
        assert (r.tools_of(SET_A) / "resume.sh").is_file()

    def test_a_set_only_on_a_newer_data_disc_is_restored_from_it(self, tmp_path):
        """Set C was added after the meta disc was made: only the data
        disc holds it.  It is in the loop, and restore.sh reads it from
        the disc."""
        r = _Real(tmp_path, (SET_A,))
        media = tmp_path / "media"
        disc = media / "LCSAS_CD700_2026_0002"
        _set(disc / "metadata", SET_A)
        _set(disc / "metadata", SET_C)
        (disc / "data").mkdir()
        (disc / "VOLUME_INFO.txt").write_text("  Label:  LCSAS_CD700_2026_0002\n")

        res = r.run(LCSAS_MOUNT_DIRS=str(media))

        assert res.returncode == 0, res.stderr
        assert f"TIER1 {disc}/metadata/{SET_C}" in res.stdout, res.stdout
        assert (r.target / SET_A / "restored.txt").is_file()
        assert (r.target / SET_C / "restored.txt").is_file()


def test_full_disk_phrases_are_still_what_the_tiers_print():
    """restore_auto.sh matches these words; each tier must still say
    them when the disk fills up."""
    auto = RESTORE_AUTO.read_text()
    for phrase in ("out of space", "no space left on device", "disk quota exceeded"):
        assert f"'{phrase}'" in auto, phrase
    src = REPO_ROOT / "recovery" / "src" / "lcsas-restore"
    for c_file in ("tree.c", "main.c"):
        assert "target directory out of space" in (src / c_file).read_text(), c_file
    fallback = (REPO_ROOT / "src" / "lcsas" / "restore" / "restic_fallback.py").read_text()
    # Tier 3 puts the OS's words (strerror: "No space left on device",
    # "Disk quota exceeded") in its message.
    assert "exc.strerror" in fallback and "_DISK_FULL_ERRNOS" in fallback
    assert os.strerror(errno.ENOSPC) == "No space left on device"
