"""Loading a config must not touch the repo mirrors (#427).

The bug these pin: ``bdcbd31`` correctly routed *every* command's catalog
lookup through ``load_config``, but ``load_config`` probed each repo's
``mirror_path``/``password_file`` with ``Path.exists()``.  On a stale
NFS/CIFS mount a ``stat()`` does not fail — it blocks for the mount's
timeout — so purely local commands like ``repo list`` and ``status``
inherited a hang from a mirror they never read.

Two holes had to be closed, and both are pinned below:

1. the explicit ``exists()`` probe of every repo path, and
2. ``Path.resolve()`` on a *relative* ``mirror_path``, which lstats every
   component and blocks identically — invisible to any test that only
   uses absolute paths.

The missing-path check itself did not vanish: ``validate_config`` reports
it as a hard error, and every command that genuinely reads a mirror gates
on that via ``_validate_config_or_exit``.  ``TestTheCheckStillExists``
below holds that line.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.config.settings import load_config, validate_config

# Any filesystem access below this marker is treated as "the mount blocked".
STALE = "lcsas-stale-mount-DO-NOT-STAT"


class StaleMountError(AssertionError):
    """Raised in place of the block a real stale mount would produce."""


@pytest.fixture
def stale_mount(monkeypatch):
    """Make every syscall against a ``STALE`` path raise instead of hang.

    A real stale mount blocks; blocking is untestable, so we substitute a
    loud failure.  Only paths containing the marker are affected — every
    other call is delegated, so pytest's own I/O is untouched.
    """
    def guard(name):
        real = getattr(os, name)

        def wrapper(path, *a, **kw):
            try:
                text = os.fsdecode(path)
            except TypeError:
                text = ""  # an open file descriptor, not a path
            if STALE in text:
                raise StaleMountError(
                    f"os.{name}() reached the stale mount: {path!r}"
                )
            return real(path, *a, **kw)

        monkeypatch.setattr(os, name, wrapper)

    # exists() -> os.stat; resolve()/realpath -> os.lstat + os.readlink.
    for name in ("stat", "lstat", "readlink", "listdir", "scandir", "access"):
        guard(name)


def _write_config(
    tmp_path, mirror_path: str, *, password_file: str | None = None
) -> Path:
    """Write a config whose single repo points at *mirror_path*."""
    db = tmp_path / "archive.db"
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    body = textwrap.dedent(f"""\
        [paths]
        mirror_base = "{tmp_path}"
        staging = "{staging}"
        database = "{db}"

        [defaults]
        media_type = "CD700"

        [repos.family]
        mirror_path = "{mirror_path}"
    """)
    if password_file is not None:
        body += f'password_file = "{password_file}"\n'
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(body)
    return cfg


class TestLoadConfigTouchesNothing:
    """``load_config`` resolves paths lexically and probes nothing."""

    def test_absolute_mirror_path_is_never_stat_ed(self, tmp_path, stale_mount):
        cfg = _write_config(
            tmp_path,
            f"/mnt/{STALE}/family",
            password_file=f"/mnt/{STALE}/family.key",
        )
        config = load_config(cfg)
        assert str(config.repositories["family"].mirror_path) == (
            f"/mnt/{STALE}/family"
        )

    def test_relative_mirror_path_is_never_stat_ed(self, tmp_path, stale_mount):
        """The hole an absolute-only test leaves open.

        ``Path.resolve()`` on a relative path lstats every component, so a
        relative ``mirror_path`` pointing into a stale mount blocked even
        with the explicit probe removed.
        """
        cfg = _write_config(tmp_path, f"{STALE}/family")
        config = load_config(cfg)
        assert config.repositories["family"].mirror_path == (
            tmp_path / STALE / "family"
        )

    def test_relative_path_is_still_normalised(self, tmp_path, stale_mount):
        """Lexical normalisation, not raw concatenation: ``..`` collapses."""
        cfg = _write_config(tmp_path, f"sub/../{STALE}/family")
        config = load_config(cfg)
        assert config.repositories["family"].mirror_path == (
            tmp_path / STALE / "family"
        )

    def test_the_guard_itself_fires(self, tmp_path, stale_mount):
        """A gate observed only in the passing state is unproven.

        Both routes the old code took must be covered by the guard, or the
        tests above would pass for the wrong reason.
        """
        from pathlib import Path

        with pytest.raises(StaleMountError):
            os.stat(f"/mnt/{STALE}/family")
        with pytest.raises(StaleMountError):
            Path(f"/mnt/{STALE}/family").exists()      # the old probe
        with pytest.raises(StaleMountError):
            Path(f"{STALE}/family").resolve()          # the old resolve()


class TestReadOnlyCommandsDoNotBlock:
    """Acceptance criterion 1: local commands stay local."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["repo", "list"],
            ["status"],
            ["session", "list"],
        ],
        ids=["repo-list", "status", "session-list"],
    )
    def test_command_completes_with_an_unreachable_mirror(
        self, tmp_path, monkeypatch, stale_mount, argv
    ):
        cfg = _write_config(tmp_path, f"/mnt/{STALE}/family")
        monkeypatch.chdir(tmp_path)
        assert main(["--config", str(cfg), "init"]) == 0
        assert main(["--config", str(cfg), *argv]) == 0


class TestTheCheckStillExists:
    """Acceptance criterion 2: the missing-mirror report did not vanish."""

    def test_validate_config_reports_the_missing_mirror(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            str(tmp_path / "absent-mirror"),
            password_file=str(tmp_path / "absent.key"),
        )
        errors = validate_config(load_config(cfg))
        assert any("mirror_path does not exist" in e for e in errors)
        assert any("password_file does not exist" in e for e in errors)

    def test_scan_refuses_to_run_against_a_missing_mirror(
        self, tmp_path, monkeypatch, caplog
    ):
        """The commands that need the mirror still stop — harder than before.

        ``load_config`` used to warn and carry on; ``_validate_config_or_exit``
        makes it a non-zero exit.
        """
        import logging

        cfg = _write_config(tmp_path, str(tmp_path / "absent-mirror"))
        monkeypatch.chdir(tmp_path)
        assert main(["--config", str(cfg), "init"]) == 0
        with caplog.at_level(logging.ERROR, logger="lcsas"):
            assert main(["--config", str(cfg), "scan"]) == 1
        assert "mirror_path does not exist" in caplog.text
