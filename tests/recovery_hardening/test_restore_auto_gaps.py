"""test_restore_auto_gaps.py -- restore_auto.sh usage-error exits and repo-discovery layouts.

Issue #517 gap tests for the non-interactive restore wrapper
(recovery/scripts/restore_auto.sh), complementing the per-repo selection and
wrong-password tests in test_restore_auto_repo_select.py:

* every exit-2 usage path (too few arguments, LCSAS_PWFILE unset / empty /
  missing / a directory) -- fails before any repo is touched, the stub
  restore.sh is never run and the target is never created;
* the "root layout" branch: with no $RECOVERY/repos/ directory, repos are
  discovered as direct children of $RECOVERY that carry a keys/ directory;
  scripts/, docs/ and keys-less directories are ignored;
* the repos/ layout wins when both exist, and a non-zero restore.sh exit
  other than 77 fails the run while the remaining repos are still attempted.

restore.sh is stubbed; nothing here needs a recovery binary.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_AUTO = REPO_ROOT / "recovery" / "scripts" / "restore_auto.sh"

_PWFILE_MSG = "LCSAS_PWFILE must point to an existing password file"


def _write_stub(recovery: Path, *, fail_repos: dict[str, int] | None = None) -> Path:
    """Stub restore.sh: append 'REPO|$1|$2|$3' to $RECORD; exit per-repo code."""
    scripts = recovery / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    lines = [
        "#!/bin/sh",
        'printf "%s|%s|%s|%s\\n" "${LCSAS_REPO:-UNSET}" "$1" "$2" "$3" >> "$RECORD"',
    ]
    for repo, code in (fail_repos or {}).items():
        lines.append(f'if [ "${{LCSAS_REPO:-}}" = "{repo}" ]; then exit {code}; fi')
    lines.append("exit 0")
    stub = scripts / "restore.sh"
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    return stub


def _make_repo(base: Path, name: str) -> None:
    (base / name / "keys").mkdir(parents=True)


class _Run:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.recovery = tmp_path / "recovery"
        self.recovery.mkdir()
        self.target = tmp_path / "restored"
        self.record = tmp_path / "calls.txt"
        self.pwfile = tmp_path / "pw"
        self.pwfile.write_text("secret\n")

    def env(self, **overrides: str | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "LCSAS_PWFILE"}
        env["LCSAS_PWFILE"] = str(self.pwfile)
        env["RECORD"] = str(self.record)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(RESTORE_AUTO), *args],
            capture_output=True, text=True, env=env or self.env(), timeout=30,
        )

    def calls(self) -> list[str]:
        if not self.record.exists():
            return []
        return [ln for ln in self.record.read_text().splitlines() if ln.strip()]


@pytest.fixture
def r(tmp_path) -> _Run:
    run = _Run(tmp_path)
    _make_repo(run.recovery / "repos", "alpha")  # one valid repo to tempt a bad run
    _write_stub(run.recovery)
    return run


# ---------------------------------------------------------------------------
# Exit 2: usage errors
# ---------------------------------------------------------------------------


def _assert_refused_untouched(r: _Run, res: subprocess.CompletedProcess) -> None:
    assert res.returncode == 2, f"expected usage exit 2, got {res.returncode}:\n{res.stderr}"
    assert res.stdout == ""
    assert r.calls() == [], "restore.sh must not run when the invocation is refused"
    assert not r.target.exists(), "refused run must not create the target tree"


class TestUsageErrors:
    def test_no_arguments(self, r):
        res = r.run()
        _assert_refused_untouched(r, res)
        assert "usage:" in res.stderr
        assert "RECOVERY_ROOT TARGET_ROOT" in res.stderr
        assert "LCSAS_PWFILE" in res.stderr  # usage reminds about the env var

    def test_only_recovery_root_given(self, r):
        res = r.run(str(r.recovery))
        _assert_refused_untouched(r, res)
        assert "usage:" in res.stderr

    def test_usage_is_reported_before_the_password_file_problem(self, r):
        """Too few args AND no LCSAS_PWFILE: the usage line wins."""
        res = r.run(str(r.recovery), env=r.env(LCSAS_PWFILE=None))
        _assert_refused_untouched(r, res)
        assert "usage:" in res.stderr
        assert _PWFILE_MSG not in res.stderr

    def test_pwfile_unset(self, r):
        res = r.run(str(r.recovery), str(r.target), env=r.env(LCSAS_PWFILE=None))
        _assert_refused_untouched(r, res)
        assert _PWFILE_MSG in res.stderr
        assert "usage:" not in res.stderr

    def test_pwfile_set_but_empty(self, r):
        res = r.run(str(r.recovery), str(r.target), env=r.env(LCSAS_PWFILE=""))
        _assert_refused_untouched(r, res)
        assert _PWFILE_MSG in res.stderr

    def test_pwfile_points_at_a_missing_file(self, r):
        gone = r.tmp_path / "no-such-password-file"
        res = r.run(str(r.recovery), str(r.target), env=r.env(LCSAS_PWFILE=str(gone)))
        _assert_refused_untouched(r, res)
        assert _PWFILE_MSG in res.stderr

    def test_pwfile_points_at_a_directory(self, r):
        res = r.run(str(r.recovery), str(r.target), env=r.env(LCSAS_PWFILE=str(r.tmp_path)))
        _assert_refused_untouched(r, res)
        assert _PWFILE_MSG in res.stderr

    def test_control_valid_invocation_is_not_refused(self, r):
        """Same fixture, correct arguments: the wrapper proceeds and restores."""
        res = r.run(str(r.recovery), str(r.target))
        assert res.returncode == 0, res.stderr
        assert len(r.calls()) == 1
        assert (r.target / "alpha").is_dir()


# ---------------------------------------------------------------------------
# Repo discovery: root layout vs repos/ layout
# ---------------------------------------------------------------------------


@pytest.fixture
def root_layout(tmp_path) -> _Run:
    """A recovery tree with NO repos/ directory: repos sit directly under it."""
    run = _Run(tmp_path)
    _make_repo(run.recovery, "alpha")
    _make_repo(run.recovery, "bravo")
    # Non-repo siblings the discovery loop must walk past.
    (run.recovery / "docs").mkdir()
    (run.recovery / "docs" / "README.txt").write_text("docs\n")
    (run.recovery / "emptydir").mkdir()
    (run.recovery / "nokeys").mkdir()
    (run.recovery / "nokeys" / "data").mkdir()
    (run.recovery / "fakekeys").mkdir()
    (run.recovery / "fakekeys" / "keys").write_text("a FILE named keys, not a directory\n")
    (run.recovery / "RECOVERY.txt").write_text("plain file at the root\n")
    _write_stub(run.recovery)  # creates scripts/ -- also a keys-less sibling
    return run


class TestRootLayout:
    def test_repos_are_discovered_directly_under_the_recovery_root(self, root_layout):
        r = root_layout
        assert not (r.recovery / "repos").exists()

        res = r.run(str(r.recovery), str(r.target))

        assert res.returncode == 0, res.stderr
        calls = sorted(r.calls())
        assert [c.split("|")[0] for c in calls] == ["alpha", "bravo"], calls

    def test_each_repo_gets_its_own_target_subdir_and_the_documented_args(self, root_layout):
        r = root_layout
        res = r.run(str(r.recovery), str(r.target))
        assert res.returncode == 0, res.stderr

        for name in ("alpha", "bravo"):
            assert f"{name}|{r.recovery}|{r.target / name}|latest" in r.calls()
            assert (r.target / name).is_dir()
        assert "==> restoring alpha" in res.stderr
        assert "==> restoring bravo" in res.stderr

    def test_siblings_without_a_keys_directory_are_not_repos(self, root_layout):
        r = root_layout
        res = r.run(str(r.recovery), str(r.target))
        assert res.returncode == 0, res.stderr

        # scripts/ (holds restore.sh itself), docs/, emptydir/, nokeys/ and
        # fakekeys/ (keys is a regular file) must all be ignored.
        created = sorted(p.name for p in r.target.iterdir())
        assert created == ["alpha", "bravo"]

    def test_repos_path_that_is_a_file_does_not_hide_the_root_layout(self, root_layout):
        """Only a *directory* named repos selects the repos/ layout."""
        r = root_layout
        (r.recovery / "repos").write_text("not a directory\n")

        res = r.run(str(r.recovery), str(r.target))

        assert res.returncode == 0, res.stderr
        assert sorted(c.split("|")[0] for c in r.calls()) == ["alpha", "bravo"]

    def test_failing_repo_fails_the_run_but_the_rest_are_still_attempted(self, root_layout):
        r = root_layout
        _write_stub(r.recovery, fail_repos={"alpha": 3})

        res = r.run(str(r.recovery), str(r.target))

        assert res.returncode == 1
        assert sorted(c.split("|")[0] for c in r.calls()) == ["alpha", "bravo"]
        assert "restore of alpha FAILED (exit 3)" in res.stderr
        assert "restore of bravo FAILED" not in res.stderr
        assert "wrong password" not in res.stderr  # exit 3 is not the 77 case


class TestReposDirLayout:
    def test_repos_directory_wins_over_top_level_candidates(self, tmp_path):
        run = _Run(tmp_path)
        _make_repo(run.recovery / "repos", "alpha")
        _make_repo(run.recovery, "stray")  # has keys/, but sits outside repos/
        _write_stub(run.recovery)

        res = run.run(str(run.recovery), str(run.target))

        assert res.returncode == 0, res.stderr
        assert [c.split("|")[0] for c in run.calls()] == ["alpha"]
        assert not (run.target / "stray").exists()

    def test_repos_without_keys_are_skipped_in_the_repos_layout_too(self, tmp_path):
        run = _Run(tmp_path)
        _make_repo(run.recovery / "repos", "alpha")
        (run.recovery / "repos" / "keyless").mkdir()
        _write_stub(run.recovery)

        res = run.run(str(run.recovery), str(run.target))

        assert res.returncode == 0, res.stderr
        assert [c.split("|")[0] for c in run.calls()] == ["alpha"]
        assert not (run.target / "keyless").exists()
