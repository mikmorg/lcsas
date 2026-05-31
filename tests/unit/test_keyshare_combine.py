"""Phase 2 (K2.4) tests for the standalone key-share combiner.

Covers:
  (a) the standalone ``keyshare_combine.py`` reconstructs a real 2-of-5
      password — both in-process (for line coverage) and as a subprocess
      whose ONLY importable LCSAS surface is the keyshare package (proving
      the survives-even-if-the-rest-is-broken contract);
  (b) the meta bundler ships the keyshare package + wordlist + the
      combiner script;
  (c) KEY_INFO / START_HERE show the split-key pre-step for a split
      archive and hide it for a single-key archive.

Targets 100% line coverage of ``lcsas.meta.keyshare_combine`` and the new
``_bundle_keyshare_combiner`` / ``_share_recovery_lines`` / split-gating
branches.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import lcsas.keyshare as _ks_pkg
from lcsas.config.settings import LCSASConfig, RepositoryConfig, load_config
from lcsas.keyshare import encode_master_secret, split_secret
from lcsas.meta import keyshare_combine
from lcsas.staging.metadata import HolographicInjector

# A password with an interior byte that is not a trailing newline.
_PASSWORD = b"correct horse battery staple\x01"

_COMBINER_SRC = Path(keyshare_combine.__file__)


def _make_2of5() -> list[str]:
    """Return five SLIP-0039 share mnemonics for a 2-of-5 split of _PASSWORD."""
    master_secret = encode_master_secret(_PASSWORD)
    mnemonics: list[str] = split_secret(master_secret, 2, 5)
    return mnemonics


def _base_config(tmp_path: Path, **kw: Any) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        **kw,
    )


# ── (a) in-process: real 2-of-5 reconstruction + coverage ───────────


class TestCombinerInProcess:
    """Drive ``keyshare_combine.main`` directly for coverage of every branch."""

    def test_reconstructs_from_files(self, tmp_path, capsysbinary):
        mns = _make_2of5()
        f1 = tmp_path / "card1.txt"
        f4 = tmp_path / "card4.txt"
        f1.write_text(mns[0] + "\n")
        f4.write_text(mns[3] + "\n")

        rc = keyshare_combine.main([str(f1), str(f4)])

        assert rc == 0
        out = capsysbinary.readouterr().out
        # Raw bytes, no trailing newline.
        assert out == _PASSWORD

    def test_reconstructs_from_stdin(self, tmp_path, monkeypatch, capsysbinary):
        import io

        mns = _make_2of5()
        stdin_text = f"# header comment\n{mns[1]}\n\n{mns[2]}\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))

        rc = keyshare_combine.main([])

        assert rc == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_help(self, capsys):
        rc = keyshare_combine.main(["--help"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "keyshare_combine.py" in err

    def test_no_shares_errors(self, monkeypatch, capsys):
        import io

        # Empty stdin and no file args -> no mnemonics.
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = keyshare_combine.main([])
        assert rc == 2
        assert "no share mnemonics" in capsys.readouterr().err

    def test_unreadable_file_errors(self, tmp_path, capsys):
        rc = keyshare_combine.main([str(tmp_path / "does-not-exist.txt")])
        assert rc == 2
        assert "could not read share file" in capsys.readouterr().err

    def test_under_threshold_errors(self, tmp_path, capsys):
        mns = _make_2of5()
        f1 = tmp_path / "only.txt"
        f1.write_text(mns[0] + "\n")

        rc = keyshare_combine.main([str(f1)])

        assert rc == 1
        assert "could not reconstruct the password" in capsys.readouterr().err

    def test_default_argv_used_when_none(self, monkeypatch, capsys):
        """argv=None falls through to sys.argv[1:]."""
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr("sys.argv", ["keyshare_combine.py"])
        rc = keyshare_combine.main()  # argv defaults to None
        assert rc == 2
        assert "no share mnemonics" in capsys.readouterr().err


# ── (a') subprocess isolation: only the keyshare package importable ──


class TestCombinerIsolation:
    """The combiner reconstructs with ONLY the keyshare package importable.

    Replicates the EXACT meta-volume layout the builder produces:
    ``keyshare_combine.py`` at the meta-volume root and the bundled
    ``keyshare`` package under ``tools/lib/pythonX.Y/`` (where
    ``bundle_python_package`` lands it).  The script is run as a
    subprocess with NO PYTHONPATH and ``lcsas`` actively blocked, so it
    MUST discover the bundled package via its own sys.path bootstrap —
    proving the documented bare ``python3 keyshare_combine.py`` heir
    invocation works even when the rest of LCSAS is absent/broken.
    """

    @staticmethod
    def _stage_meta_layout(tmp_path: Path) -> Path:
        """Mirror ``MetaVolumeBuilder`` output for the keyshare bits.

        Combiner at the root; the ``keyshare`` package under
        ``tools/lib/pythonX.Y/keyshare`` (NOT a sibling of the combiner).
        """
        sandbox = tmp_path / "meta"
        lib_dir = (
            sandbox
            / "tools"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
        )
        lib_dir.mkdir(parents=True)
        shutil.copytree(
            Path(_ks_pkg.__path__[0]),
            lib_dir / "keyshare",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy(_COMBINER_SRC, sandbox / "keyshare_combine.py")
        return sandbox

    def test_wordlist_present_in_layout(self, tmp_path):
        sandbox = self._stage_meta_layout(tmp_path)
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        wordlist = sandbox / "tools" / "lib" / version / "keyshare" / "wordlist.txt"
        assert wordlist.is_file()

    def test_reconstructs_with_lcsas_blocked(self, tmp_path):
        sandbox = self._stage_meta_layout(tmp_path)
        mns = _make_2of5()
        (sandbox / "a.txt").write_text(mns[0] + "\n")
        (sandbox / "b.txt").write_text(mns[4] + "\n")

        # Block the whole ``lcsas`` namespace so the combiner is forced down
        # its top-level ``keyshare`` fallback import path.
        runner = sandbox / "run_blocked.py"
        runner.write_text(
            textwrap.dedent(
                """
                import sys
                import importlib.abc


                class _Blocker(importlib.abc.MetaPathFinder):
                    def find_spec(self, name, path, target=None):
                        if name == "lcsas" or name.startswith("lcsas."):
                            raise ImportError("lcsas blocked for isolation test")
                        return None


                sys.meta_path.insert(0, _Blocker())
                import runpy

                sys.argv = ["keyshare_combine.py", "a.txt", "b.txt"]
                runpy.run_path("keyshare_combine.py", run_name="__main__")
                """
            )
        )

        result = subprocess.run(
            [sys.executable, "run_blocked.py"],
            cwd=sandbox,
            capture_output=True,
            env={"PYTHONPATH": ""},
        )

        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout == _PASSWORD

    def test_lcsas_is_actually_blocked(self, tmp_path):
        """Sanity: the blocker truly prevents importing lcsas."""
        sandbox = self._stage_meta_layout(tmp_path)
        probe = sandbox / "probe.py"
        probe.write_text(
            textwrap.dedent(
                """
                import sys
                import importlib.abc


                class _Blocker(importlib.abc.MetaPathFinder):
                    def find_spec(self, name, path, target=None):
                        if name == "lcsas" or name.startswith("lcsas."):
                            raise ImportError("blocked")
                        return None


                sys.meta_path.insert(0, _Blocker())
                try:
                    import lcsas  # noqa: F401
                except ImportError:
                    print("blocked-ok")
                """
            )
        )
        result = subprocess.run(
            [sys.executable, "probe.py"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": ""},
        )
        assert result.stdout.strip() == "blocked-ok"


# ── (b) meta bundler ships package + wordlist + combiner ────────────


class TestBundlerShipsKeyshare:
    def test_combiner_bundled_at_root(self, tmp_path):
        from lcsas.meta.builder import MetaVolumeBuilder

        out = tmp_path / "meta"
        out.mkdir()
        builder = MetaVolumeBuilder(out)
        builder._bundle_keyshare_combiner()

        dst = out / "keyshare_combine.py"
        assert dst.is_file()
        content = dst.read_text()
        assert "recover_secret" in content
        assert "decode_master_secret" in content

    def test_combiner_missing_source_raises(self, tmp_path, monkeypatch):
        from lcsas.meta import builder as builder_mod
        from lcsas.meta.builder import MetaVolumeBuilder

        out = tmp_path / "meta"
        out.mkdir()
        builder = MetaVolumeBuilder(out)

        # Point __file__ resolution at a dir with no keyshare_combine.py.
        empty = tmp_path / "empty_pkg"
        empty.mkdir()
        monkeypatch.setattr(builder_mod, "__file__", str(empty / "builder.py"))
        with pytest.raises(FileNotFoundError, match="keyshare_combine.py missing"):
            builder._bundle_keyshare_combiner()

    def test_bundler_allows_keyshare_package(self):
        """The keyshare package is on the bundle allowlist and resolves."""
        from lcsas.meta.bundler import ToolBundler

        assert "lcsas.keyshare" in ToolBundler._BUNDLEABLE_PACKAGES
        pkg_dir = ToolBundler._find_installed_package("lcsas.keyshare")
        assert pkg_dir is not None
        assert (pkg_dir / "wordlist.txt").is_file()

    def test_bundle_python_package_ships_keyshare_with_wordlist(self, tmp_path):
        """bundle_python_package lands keyshare (top-level) incl. wordlist.txt."""
        from lcsas.meta.bundler import ToolBundler

        bundler = ToolBundler(tmp_path / "meta")
        lib_dir = tmp_path / "meta" / "tools" / "lib"
        lib_dir.mkdir(parents=True)
        bundler._lib_dir = lib_dir

        dest = bundler.bundle_python_package("lcsas.keyshare")

        assert dest is not None
        # importlib resolves lcsas.keyshare -> .../lcsas/keyshare, so it
        # lands as the top-level package directory ``keyshare``.
        assert dest.name == "keyshare"
        assert (dest / "wordlist.txt").is_file()
        assert (dest / "slip39.py").is_file()


# ── (c) KEY_INFO / START_HERE gating ────────────────────────────────


class TestHeirDocGating:
    def test_key_info_split_shows_prestep(self, tmp_path):
        config = _base_config(
            tmp_path,
            key_split=True,
            key_threshold=2,
            key_shares=5,
            repositories={
                "family": RepositoryConfig(
                    name="family",
                    mirror_path=tmp_path / "mirror" / "family",
                    password_file=Path("/keys/family.key"),
                ),
            },
        )
        root = tmp_path / "stage"
        root.mkdir()
        HolographicInjector(root).write_key_info(config)
        txt = (root / "KEY_INFO.txt").read_text()
        assert "SPLIT KEY" in txt
        assert "keyshare_combine.py" in txt
        assert "any 2" in txt
        assert "5 share cards" in txt
        assert "docs/KEY_SHARE_FORMAT.md" in txt

    def test_key_info_single_key_hides_prestep(self, tmp_path):
        config = _base_config(
            tmp_path,
            key_split=False,
            repositories={
                "family": RepositoryConfig(
                    name="family",
                    mirror_path=tmp_path / "mirror" / "family",
                ),
            },
        )
        root = tmp_path / "stage"
        root.mkdir()
        HolographicInjector(root).write_key_info(config)
        txt = (root / "KEY_INFO.txt").read_text()
        assert "SPLIT KEY" not in txt
        assert "keyshare_combine.py" not in txt

    def test_start_here_split_shows_prestep(self, tmp_path):
        config = _base_config(
            tmp_path,
            archive_owner="Jane Doe",
            key_split=True,
            key_threshold=3,
            key_shares=5,
        )
        root = tmp_path / "stage"
        root.mkdir()
        HolographicInjector(root).write_start_here(config)
        txt = (root / "START_HERE.txt").read_text()
        assert "SPLIT INTO 5 SHARE CARDS" in txt
        assert "keyshare_combine.py" in txt
        assert "Password:" in txt
        # No leftover template placeholder.
        assert "SPLIT_BLOCK" not in txt

    def test_start_here_single_key_hides_prestep(self, tmp_path):
        config = _base_config(tmp_path, archive_owner="Jane Doe", key_split=False)
        root = tmp_path / "stage"
        root.mkdir()
        HolographicInjector(root).write_start_here(config)
        txt = (root / "START_HERE.txt").read_text()
        assert "SHARE CARDS" not in txt
        assert "keyshare_combine.py" not in txt
        assert "SPLIT_BLOCK" not in txt
        # The normal single-key content is intact and not mangled.
        assert "HOW TO GET YOUR FILES BACK" in txt


# ── config parsing of key_split ─────────────────────────────────────


class TestConfigKeySplit:
    def test_key_split_defaults_false(self, tmp_path):
        config = _base_config(tmp_path)
        assert config.key_split is False

    def test_key_split_parsed_from_toml(self, tmp_path):
        cfg_file = tmp_path / "lcsas.toml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                [paths]
                mirror_base = "mirror"
                staging = "staging"
                database = "db.db"

                [defaults]
                key_split = true
                key_threshold = 3
                key_shares = 7
                """
            )
        )
        config = load_config(cfg_file)
        assert config.key_split is True
        assert config.key_threshold == 3
        assert config.key_shares == 7

    def test_key_split_default_in_toml_is_false(self, tmp_path):
        cfg_file = tmp_path / "lcsas.toml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                [paths]
                mirror_base = "mirror"
                staging = "staging"
                database = "db.db"
                """
            )
        )
        config = load_config(cfg_file)
        assert config.key_split is False
