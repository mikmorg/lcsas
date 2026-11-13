"""Tests for the ``lcsas recovery`` command (``lcsas.cli.commands.recovery``)."""

from __future__ import annotations

from lcsas.cli.main import main


class _FakeRecoveryBuilder:
    """Stand-in for :class:`lcsas.recovery.RecoveryBuilder`.

    Records each method invocation so tests can assert which dispatch
    branch ``cmd_recovery`` took, without actually invoking ``make`` or
    a C compiler.
    """

    # Class-level call log so the inner ``from lcsas.recovery import
    # RecoveryBuilder`` resolves to the same patched object across the
    # test and ``cmd_recovery``.
    calls: list[tuple[str, tuple, dict]] = []

    def __init__(self, recovery_dir):
        self.recovery_dir = recovery_dir
        type(self).calls.append(("__init__", (recovery_dir,), {}))

    @classmethod
    def reset(cls):
        cls.calls = []

    def build_host(self, verbose: bool = False):
        from lcsas.recovery.build import RecoveryArtifacts

        type(self).calls.append(("build_host", (), {"verbose": verbose}))
        return RecoveryArtifacts(
            arch="x86_64",
            lcsas_restore=self.recovery_dir / "build" / "lcsas-restore",
            lcsas_iso9660=None,
        )

    def cross_build(self, arch: str, cc=None, verbose: bool = False):
        from lcsas.recovery.build import RecoveryArtifacts

        type(self).calls.append(
            ("cross_build", (arch,), {"cc": cc, "verbose": verbose})
        )
        return RecoveryArtifacts(
            arch=arch,
            lcsas_restore=self.recovery_dir / "bin" / arch / "lcsas-restore",
            lcsas_iso9660=None,
        )

    def run_tests(self, verbose: bool = False) -> bool:
        type(self).calls.append(("run_tests", (), {"verbose": verbose}))
        return True

    def write_manifest(self, manifest_path=None):
        from pathlib import Path

        type(self).calls.append(
            ("write_manifest", (), {"manifest_path": manifest_path})
        )
        # Write a tiny non-empty manifest so the handler can count lines.
        target = (
            Path(manifest_path)
            if manifest_path is not None
            else self.recovery_dir / "MANIFEST.sha256"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("deadbeef  README\n")
        return target


def _patch_recovery_builder(monkeypatch):
    """Install ``_FakeRecoveryBuilder`` in place of the real builder.

    ``cmd_recovery`` does ``from lcsas.recovery import RecoveryBuilder``
    inside the function body, so we need to patch the attribute on the
    ``lcsas.recovery`` package itself (not on ``lcsas.cli.main``).
    """
    import lcsas.recovery as recovery_pkg

    _FakeRecoveryBuilder.reset()
    monkeypatch.setattr(recovery_pkg, "RecoveryBuilder", _FakeRecoveryBuilder)
    return _FakeRecoveryBuilder


class TestCmdRecovery:
    def test_recovery_build_invokes_builder(self, monkeypatch, capsys):
        """`recovery build --arch x86_64` dispatches to ``cross_build``.

        (``--arch host`` would route to ``build_host`` instead; the
        issue's illustrative ``build_target`` name maps onto the real
        builder's ``cross_build`` for non-host architectures.)
        """
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "build", "--arch", "x86_64"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, args, kwargs = method_calls[0]
        assert name == "cross_build"
        assert args == ("x86_64",)
        assert kwargs == {"cc": None, "verbose": False}

    def test_recovery_build_host_invokes_build_host(self, monkeypatch, capsys):
        """`recovery build` (default ``--arch host``) calls ``build_host``."""
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "build"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "build_host"
        assert kwargs == {"verbose": False}

    def test_recovery_test_invokes_test(self, monkeypatch, capsys):
        """`recovery test` calls ``RecoveryBuilder.run_tests``."""
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "test"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "run_tests"
        assert kwargs == {"verbose": False}

        out = capsys.readouterr().out
        assert "recovery tests: OK" in out

    def test_recovery_manifest_invokes_manifest(
        self, monkeypatch, tmp_path, capsys
    ):
        """`recovery manifest` calls ``RecoveryBuilder.write_manifest``."""
        fake = _patch_recovery_builder(monkeypatch)

        out_path = tmp_path / "MANIFEST.sha256"
        result = main(["recovery", "manifest", "-o", str(out_path)])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "write_manifest"
        # cmd_recovery passes the argparse ``Path`` value through.
        assert kwargs["manifest_path"] == out_path
        assert out_path.is_file()

    def test_recovery_verify_invokes_verify(self, monkeypatch, capsys):
        """`recovery verify` shells out to ``make ... repro-check``.

        The dispatcher invokes ``subprocess.run(["make", "-C", <dir>,
        "repro-check"])`` directly (rather than calling a builder
        method), so we stub ``subprocess.run`` at the cli.main level to
        avoid spawning make/cc and assert the right command was issued.
        """
        _patch_recovery_builder(monkeypatch)

        recorded: dict = {}

        class _CompletedStub:
            returncode = 0

        def _fake_run(cmd, *args, **kwargs):
            recorded["cmd"] = list(cmd)
            recorded["args"] = args
            recorded["kwargs"] = kwargs
            return _CompletedStub()

        monkeypatch.setattr("lcsas.cli.commands.recovery.subprocess.run", _fake_run)

        result = main(["recovery", "verify"])
        assert result == 0
        assert recorded["cmd"][:2] == ["make", "-C"]
        assert recorded["cmd"][-1] == "repro-check"

    def test_recovery_missing_tree_errors(
        self, monkeypatch, tmp_path, caplog
    ):
        """If ``recovery/`` is absent, the handler exits non-zero with an
        error.

        ``cmd_recovery`` derives the recovery tree from
        ``Path(__file__).resolve().parents[4]`` of the
        ``cli.commands.recovery`` module; patching ``__file__`` to point
        at a tmp tree without a ``recovery/`` subdir exercises the error
        branch without touching the real repo layout.
        """
        import logging

        import lcsas.cli.commands.recovery as cli_recovery

        fake_root = tmp_path / "fake_project"
        fake_module_path = (
            fake_root / "src" / "lcsas" / "cli" / "commands" / "recovery.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("# stub\n")
        # No recovery/ directory under fake_root.

        monkeypatch.setattr(cli_recovery, "__file__", str(fake_module_path))
        # Also patch RecoveryBuilder so we know it was *not* constructed.
        fake = _patch_recovery_builder(monkeypatch)

        with caplog.at_level(logging.ERROR, logger="lcsas"):
            result = main(["recovery", "build"])

        assert result == 1
        # We never got far enough to construct the builder.
        assert fake.calls == []
        # Handler logged a clear error referencing the missing tree.
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "recovery/" in combined or "recovery" in combined
        assert str(fake_root / "recovery") in combined

    def test_recovery_unknown_subcommand_errors(self, monkeypatch, capsys):
        """argparse rejects unknown ``recovery`` subcommands cleanly."""
        import pytest

        _patch_recovery_builder(monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            main(["recovery", "frobnicate"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "frobnicate" in err or "invalid choice" in err
