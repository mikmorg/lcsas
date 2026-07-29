"""Tests that every command resolves its catalog and device the same way.

The bug these pin: ``repo add``/``list``/``remove`` and ``status`` called
``_resolve_db_path(args)`` with no config, so they fell through to a
*relative* ``archive.db`` while ``scan``/``stage``/``burn`` used
``[paths].database``.  Nothing errored — the operator got a second, empty
catalog in whatever directory they were standing in, and ``scan`` reported
no repositories because the repo had been registered somewhere else.

The device equivalent: ``verify`` and ``burn-iso`` declared
``--device`` with a hard-coded ``default="/dev/sr0"``, which is
indistinguishable from an operator choice downstream and so shadowed
``optical_device`` from the config permanently.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lcsas.cli.main import (
    _load_config_opt,
    _resolve_db_path,
    _resolve_device,
    main,
)
from lcsas.exceptions import ConfigError


def _write_config(tmp_path, db, *, optical_device=None):
    """Write a minimal but canonical TOML config; return its path as str."""
    mirror_base = tmp_path / "mirror"
    mirror_base.mkdir(exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)

    body = (
        "[paths]\n"
        f'mirror_base = "{mirror_base}"\n'
        f'staging = "{staging}"\n'
        f'database = "{db}"\n'
        "\n"
        "[defaults]\n"
        'media_type = "CD700"\n'
    )
    if optical_device is not None:
        body += f'optical_device = "{optical_device}"\n'

    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(body)
    return str(cfg)


def _args(**kw):
    ns = argparse.Namespace(db=None, config=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestResolveDbPath:
    def test_db_flag_outranks_config(self, tmp_path):
        cfg = _write_config(tmp_path, tmp_path / "from_config.db")
        explicit = tmp_path / "from_flag.db"
        resolved = _resolve_db_path(_args(db=str(explicit), config=cfg))
        assert resolved == explicit

    def test_config_used_when_caller_passes_none(self, tmp_path):
        """The regression: a handler that never loads a config still lands
        on the configured catalog rather than a relative archive.db."""
        configured = tmp_path / "configured.db"
        cfg = _write_config(tmp_path, configured)
        assert _resolve_db_path(_args(config=cfg)) == configured

    def test_preloaded_config_is_not_reparsed(self, tmp_path):
        """A config passed in wins over --config: handlers that already
        loaded one must not pay for a second parse or get a different
        answer than the object they are about to use."""
        from lcsas.config.settings import load_config

        cfg = _write_config(tmp_path, tmp_path / "configured.db")
        loaded = load_config(Path(cfg))
        other = tmp_path / "other.db"
        cfg2 = tmp_path / "other.toml"
        cfg2.write_text(f'[paths]\ndatabase = "{other}"\n')
        # args points at cfg2, but the caller hands us cfg's object.
        assert _resolve_db_path(_args(config=str(cfg2)), loaded) == (
            tmp_path / "configured.db"
        )

    def test_falls_back_to_relative_archive_db(self):
        assert _resolve_db_path(_args()) == Path("archive.db")


class TestLoadConfigOpt:
    def test_returns_none_without_config(self):
        assert _load_config_opt(_args()) is None

    def test_missing_path_raises_actionable_error(self, tmp_path):
        missing = tmp_path / "nope.toml"
        with pytest.raises(ConfigError) as exc:
            _load_config_opt(_args(config=str(missing)))
        assert str(missing) in str(exc.value)
        assert exc.value.recovery_hint

    def test_malformed_toml_raises_config_error(self, tmp_path):
        bad = tmp_path / "bad.toml"
        bad.write_text("[paths\ndatabase = ")
        with pytest.raises(ConfigError) as exc:
            _load_config_opt(_args(config=str(bad)))
        assert "Malformed TOML" in str(exc.value)


class TestResolveDevice:
    def test_flag_outranks_config(self, tmp_path):
        cfg = _write_config(
            tmp_path, tmp_path / "a.db", optical_device="/dev/sr9"
        )
        assert _resolve_device(_args(device="/dev/sr3", config=cfg)) == "/dev/sr3"

    def test_config_used_when_flag_absent(self, tmp_path):
        cfg = _write_config(
            tmp_path, tmp_path / "a.db", optical_device="/dev/sr9"
        )
        assert _resolve_device(_args(device=None, config=cfg)) == "/dev/sr9"

    def test_default_when_neither_set(self):
        assert _resolve_device(_args(device=None)) == "/dev/sr0"

    @pytest.mark.parametrize(
        "argv",
        [
            ["verify", "VOL_001"],
            ["burn-iso", "/tmp/x.iso"],
            ["burn", "--session", "S1"],
        ],
    )
    def test_device_is_unset_when_not_passed(self, argv):
        """A hard-coded parser default would shadow the config forever, and
        is indistinguishable downstream from an operator choice — every
        device-taking command must leave the slot empty."""
        from lcsas.cli.main import build_parser

        assert build_parser().parse_args(argv).device is None


class TestCatalogIsSingular:
    """End-to-end: with only --config, every command must agree on one
    catalog file, and no stray archive.db may appear in the cwd."""

    def test_init_add_list_all_use_the_configured_catalog(
        self, tmp_path, monkeypatch, capsys
    ):
        configured = tmp_path / "archive.db"
        cfg = _write_config(tmp_path, configured)

        # Stand somewhere else entirely — this is what made the bug
        # invisible: the stray catalog appeared under the cwd.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert main(["--config", cfg, "init"]) == 0
        assert main(
            ["--config", cfg, "repo", "add", "family", str(tmp_path / "mirror")]
        ) == 0
        capsys.readouterr()

        assert main(["--config", cfg, "repo", "list"]) == 0
        out = capsys.readouterr().out
        assert "family" in out

        assert configured.exists()
        assert not (elsewhere / "archive.db").exists()

    def test_status_reads_the_configured_catalog(
        self, tmp_path, monkeypatch, capsys
    ):
        configured = tmp_path / "archive.db"
        cfg = _write_config(tmp_path, configured)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert main(["--config", cfg, "init"]) == 0
        capsys.readouterr()

        # Without the fix this raised "catalog not found" against
        # ./archive.db even though init had just created one.
        assert main(["--config", cfg, "status"]) == 0
        assert not (elsewhere / "archive.db").exists()

    def test_db_flag_overrides_config_for_session_list(
        self, tmp_path, capsys
    ):
        """``session list`` used to read config.db_path directly, so the
        global ``--db`` was accepted and then silently ignored."""
        configured = tmp_path / "configured.db"
        override = tmp_path / "override.db"
        cfg = _write_config(tmp_path, configured)

        main(["--db", str(override), "init"])
        assert override.exists()
        capsys.readouterr()

        # Points at a config whose catalog was never created; only the
        # --db override can make this succeed.
        assert main(["--config", cfg, "--db", str(override), "session", "list"]) == 0
        assert not configured.exists()

    def test_repo_remove_targets_the_configured_catalog(
        self, tmp_path, monkeypatch, capsys
    ):
        configured = tmp_path / "archive.db"
        cfg = _write_config(tmp_path, configured)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        main(["--config", cfg, "init"])
        main(["--config", cfg, "repo", "add", "family", str(tmp_path / "mirror")])

        from lcsas.db.connection import get_connection
        from lcsas.db.repos import list_repos

        conn = get_connection(str(configured))
        try:
            repo_id = list_repos(conn)[0].repo_id
        finally:
            conn.close()
        capsys.readouterr()

        monkeypatch.setattr("builtins.input", lambda *a: "yes")
        assert main(["--config", cfg, "repo", "remove", repo_id, "--force"]) == 0

        conn = get_connection(str(configured))
        try:
            assert list_repos(conn) == []
        finally:
            conn.close()
        assert not (elsewhere / "archive.db").exists()
