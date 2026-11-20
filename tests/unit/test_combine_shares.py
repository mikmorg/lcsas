"""Phase 2 (K2.4) tests for the standalone share combiner.

Covers:
  (a) the standalone ``combine_shares.py`` reconstructs a real 2-of-5
      password — both in-process (for line coverage) and as a subprocess
      whose ONLY importable LCSAS surface is the keyshare package (proving
      the survives-even-if-the-rest-is-broken contract);
  (b) the meta bundler ships the keyshare package + wordlist + the
      combiner script;
  (c) PASSWORD_INFO / START_HERE show the split-password pre-step for a
      split archive and hide it for an archive whose password is not split.

Targets 100% line coverage of ``lcsas.meta.combine_shares`` and the new
``_bundle_keyshare_combiner`` / ``share_recovery_lines`` / split-gating
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
from lcsas.meta import combine_shares
from lcsas.staging.metadata import HolographicInjector

# A password with an interior byte that is not a trailing newline.
_PASSWORD = b"correct horse battery staple\x01"

_COMBINER_SRC = Path(combine_shares.__file__)


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
    """Drive ``combine_shares.main`` directly for coverage of every branch."""

    def test_reconstructs_from_files(self, tmp_path, capsysbinary):
        mns = _make_2of5()
        f1 = tmp_path / "card1.txt"
        f4 = tmp_path / "card4.txt"
        f1.write_text(mns[0] + "\n")
        f4.write_text(mns[3] + "\n")

        rc = combine_shares.main([str(f1), str(f4)])

        assert rc == 0
        out = capsysbinary.readouterr().out
        # Raw bytes, no trailing newline.
        assert out == _PASSWORD

    def test_reconstructs_from_stdin(self, tmp_path, monkeypatch, capsysbinary):
        import io

        mns = _make_2of5()
        stdin_text = f"# header comment\n{mns[1]}\n\n{mns[2]}\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))

        rc = combine_shares.main([])

        assert rc == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_help(self, capsys):
        rc = combine_shares.main(["--help"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "combine_shares.py" in err

    def test_no_shares_errors(self, monkeypatch, capsys):
        import io

        # Empty stdin and no file args -> no mnemonics.
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = combine_shares.main([])
        assert rc == 2
        assert "no share mnemonics" in capsys.readouterr().err

    def test_unreadable_file_errors(self, tmp_path, capsys):
        rc = combine_shares.main([str(tmp_path / "does-not-exist.txt")])
        assert rc == 2
        assert "could not read share file" in capsys.readouterr().err

    def test_under_threshold_errors(self, tmp_path, capsys):
        mns = _make_2of5()
        f1 = tmp_path / "only.txt"
        f1.write_text(mns[0] + "\n")

        rc = combine_shares.main([str(f1)])

        assert rc == 1
        assert "could not rebuild the password" in capsys.readouterr().err

    def test_default_argv_used_when_none(self, monkeypatch, capsys):
        """argv=None falls through to sys.argv[1:]."""
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr("sys.argv", ["combine_shares.py"])
        rc = combine_shares.main()  # argv defaults to None
        assert rc == 2
        assert "no share mnemonics" in capsys.readouterr().err

    def test_combine_accepts_real_card_files(self, tmp_path, capsysbinary):
        """KEY-01: the heir-facing -card.txt artifacts reconstruct.

        Generates real cards via ``lcsas password split`` (the exact artifact a
        share holder is handed and told to pass to ``combine_shares.py``) and
        feeds the cards — not the bare share files — to the combiner.
        """
        from lcsas.cli.main import main as cli_main

        pw_file = tmp_path / "pw"
        pw_file.write_bytes(_PASSWORD)
        out = tmp_path / "shares"
        assert cli_main([
            "password", "split", "--repo", "alpha",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        capsysbinary.readouterr()

        cards = sorted(out.glob("alpha-share-*-card.txt"))
        assert len(cards) == 5
        rc = combine_shares.main([str(cards[0]), str(cards[3])])
        assert rc == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_combine_card_stdin(self, tmp_path, monkeypatch, capsysbinary):
        """KEY-01: concatenated card text piped on stdin reconstructs."""
        import io

        from lcsas.cli.main import main as cli_main

        pw_file = tmp_path / "pw"
        pw_file.write_bytes(_PASSWORD)
        out = tmp_path / "shares"
        assert cli_main([
            "password", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        capsysbinary.readouterr()

        cards = sorted(out.glob("alpha-share-*-card.txt"))
        piped = cards[0].read_text() + cards[1].read_text()
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        rc = combine_shares.main([])
        assert rc == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_truncated_card_errors(self, tmp_path, capsys):
        """KEY-01: a truncated card fails (rc 3: a share is not valid),
        naming file + word count.  (Before rc6 every failure was rc 1.)"""
        from lcsas.cli.main import main as cli_main

        pw_file = tmp_path / "pw"
        pw_file.write_bytes(_PASSWORD)
        out = tmp_path / "shares"
        assert cli_main([
            "password", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        capsys.readouterr()

        cards = sorted(out.glob("alpha-share-*-card.txt"))
        trunc = tmp_path / "trunc-card.txt"
        # Drop the share-words line entirely -> 0 share words.
        from lcsas.keyshare import is_mnemonic_line
        kept = [
            ln for ln in cards[0].read_text().splitlines()
            if not is_mnemonic_line(ln)
        ]
        trunc.write_text("\n".join(kept) + "\n")
        rc = combine_shares.main([str(trunc), str(cards[1])])
        assert rc == combine_shares.EXIT_BAD_SHARE == 3
        err = capsys.readouterr().err
        assert "trunc-card.txt" in err
        assert "share words" in err
        assert err.isascii(), err


# ── (a') subprocess isolation: only the keyshare package importable ──


class TestCombinerIsolation:
    """The combiner reconstructs with ONLY the keyshare package importable.

    Replicates the EXACT meta disc layout the builder produces:
    ``combine_shares.py`` at the meta disc root and the bundled
    ``keyshare`` package under ``tools/lib/pythonX.Y/`` (where
    ``bundle_python_package`` lands it).  The script is run as a
    subprocess with NO PYTHONPATH and ``lcsas`` actively blocked, so it
    MUST discover the bundled package via its own sys.path bootstrap —
    proving the documented bare ``python3 combine_shares.py`` heir
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
        shutil.copy(_COMBINER_SRC, sandbox / "combine_shares.py")
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

                sys.argv = ["combine_shares.py", "a.txt", "b.txt"]
                runpy.run_path("combine_shares.py", run_name="__main__")
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

        dst = out / "combine_shares.py"
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

        # Point __file__ resolution at a dir with no combine_shares.py.
        empty = tmp_path / "empty_pkg"
        empty.mkdir()
        monkeypatch.setattr(builder_mod, "__file__", str(empty / "builder.py"))
        with pytest.raises(FileNotFoundError, match="combine_shares.py missing"):
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


# ── (c) PASSWORD_INFO / START_HERE gating ────────────────────────────────


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
        txt = (root / "PASSWORD_INFO.txt").read_text()
        assert "SPLIT PASSWORD" in txt
        assert "combine_shares.py" in txt
        assert "any 2" in txt
        assert "5 share cards" in txt
        assert "docs/SHARE_FORMAT.md" in txt

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
        txt = (root / "PASSWORD_INFO.txt").read_text()
        assert "SPLIT PASSWORD" not in txt
        assert "combine_shares.py" not in txt

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
        assert "combine_shares.py" in txt
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
        assert "combine_shares.py" not in txt
        assert "SPLIT_BLOCK" not in txt
        # The normal content for an unsplit password is intact and not mangled.
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
                catalog = "db.db"

                [defaults]
                split = true
                split_threshold = 3
                split_shares = 7
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
                catalog = "db.db"
                """
            )
        )
        config = load_config(cfg_file)
        assert config.key_split is False


class TestCombinerPerShareValidation:
    """The per-share pre-pass pinpoints a single mistyped share."""

    def test_invalid_share_flagged_and_returns_3(self, tmp_path, capsys):
        # Swap two valid words so the share still parses structurally (all
        # words in the wordlist, correct count) but its SLIP-0039 CHECKSUM
        # is broken -> _read_mnemonics accepts it, but the per-share
        # check_share pre-pass returns a reason, so main() prints a named
        # verdict and aborts with the aggregate error before reconstruction
        # (combine_shares.py:158-159, 161-165).
        mns = _make_2of5()
        words = mns[0].split()
        i, j = 4, 5
        if words[i] == words[j]:
            j = 6
        words[i], words[j] = words[j], words[i]
        bad = tmp_path / "bad-card.txt"
        bad.write_text(" ".join(words) + "\n")

        rc = combine_shares.main([str(bad)])

        # rc6: a share that is not valid is exit 3 (it was 1, the same as
        # "cannot combine"), so a script can ask for that card again.
        assert rc == 3
        err = capsys.readouterr().err
        assert "failed individual validation" in err

    def test_more_than_k_shares_rebuild(self, tmp_path, capsysbinary):
        """rc6 (#476 drill): the cards say "any K", so all the cards a
        reader has -- here 4 of a 2-of-5 split, one of them twice -- must
        rebuild the password, not fail as "too many"."""
        mns = _make_2of5()
        files = []
        for i, m in enumerate([mns[0], mns[2], mns[4], mns[1], mns[2]]):
            f = tmp_path / f"s{i}.txt"
            f.write_text(m + "\n")
            files.append(str(f))
        assert combine_shares.main(files) == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_too_few_shares_is_exit_1(self, tmp_path, capsys):
        mns = _make_2of5()
        f = tmp_path / "one.txt"
        f.write_text(mns[3] + "\n")
        assert combine_shares.main([str(f)]) == combine_shares.EXIT_CANNOT_COMBINE == 1
        assert "at least 2" in capsys.readouterr().err

    def test_stdin_typo_is_named_not_skipped(self, monkeypatch, capsys):
        """rc6: on stdin a share line with a typo used to be dropped as
        prose, so the reader saw only "no shares".  Now it is checked
        like a file and the bad word is named (exit 3)."""
        import io

        mns = _make_2of5()
        words = mns[0].split()
        words[6] = "buidling"
        piped = "Tip: you may type just the first 4 letters of each word -- the\n"
        piped += " ".join(words) + "\n" + mns[1] + "\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        rc = combine_shares.main([])
        err = capsys.readouterr().err
        assert rc == combine_shares.EXIT_BAD_SHARE, err
        assert "stdin line 2" in err
        assert "word 7 'buidling'" in err
        assert "stdin line 1" not in err  # card prose is still skipped

    def test_stdin_prefix_typed_share_rebuilds(self, monkeypatch, capsysbinary):
        """A share typed as 4-letter prefixes is a share line and rebuilds."""
        import io

        mns = _make_2of5()
        prefixed = " ".join(w[:4] for w in mns[3].split())
        monkeypatch.setattr("sys.stdin", io.StringIO(prefixed + "\n" + mns[4] + "\n"))
        assert combine_shares.main([]) == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_prefix_typed_card_files_rebuild(self, tmp_path, capsysbinary):
        """#797: card FILES with the words cut to 4 letters (as the card
        invites), or mixed with full words in any case, rebuild."""
        from lcsas.cli.main import _share_card_text

        mns = _make_2of5()
        typed = [
            " ".join(w[:4] for w in mns[0].split()),
            " ".join(w.upper() if i % 2 else w[:4] for i, w in enumerate(mns[2].split())),
        ]
        files = []
        for i, words in enumerate(typed, start=1):
            f = tmp_path / f"card{i}.txt"
            f.write_text(_share_card_text("alpha", i, 2, 5, words, "2026-01-01", 1))
            files.append(str(f))
        assert combine_shares.main(files) == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_file_typo_is_named_not_word_count(self, tmp_path, capsys):
        """#797: a card file with one mistyped word names that word and its
        position (as the C combiner does), not "found 0 share words"."""
        from lcsas.cli.main import _share_card_text

        mns = _make_2of5()
        words = [w[:4] for w in mns[0].split()]
        words[6] = "qqqq"
        bad = tmp_path / "card1.txt"
        bad.write_text(_share_card_text("alpha", 1, 2, 5, " ".join(words), "2026-01-01", 1))
        good = tmp_path / "card2.txt"
        good.write_text(mns[1] + "\n")
        rc = combine_shares.main([str(bad), str(good)])
        err = capsys.readouterr().err
        assert rc == combine_shares.EXIT_BAD_SHARE, err
        assert f"share ({bad}): word 7 'qqqq' is not a share word" in err
        assert f"share ({good}): OK" in err

    def test_stdin_short_line_is_skipped_like_c(self, monkeypatch, capsysbinary):
        """A short line of share words ("that" is the prefix of a word) is
        not a share: it is skipped, as the C combiner skips it."""
        import io

        mns = _make_2of5()
        piped = "that\n" + " ".join(mns[0].split()[:5]) + "\n" + mns[1] + "\n" + mns[2] + "\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        assert combine_shares.main([]) == 0
        assert capsysbinary.readouterr().out == _PASSWORD

    def test_card_with_no_share_line_says_so(self, tmp_path, capsys):
        """#844: a card file whose share line is missing says no share
        words were found in it, not that a prose word is not a share word."""
        from lcsas.cli.main import _share_card_text

        mns = _make_2of5()
        card = _share_card_text("alpha", 1, 2, 5, mns[0], "2026-01-01", 1)
        lines = [ln for ln in card.splitlines() if mns[0] not in ln]
        bad = tmp_path / "card1.txt"
        bad.write_text("\n".join(lines) + "\n")
        rc = combine_shares.main([str(bad)])
        err = capsys.readouterr().err
        assert rc == combine_shares.EXIT_BAD_SHARE, err
        assert "no share words were found in this file" in err
        assert "is not a share word" not in err

    def test_stdin_split_share_hints_one_line(self, monkeypatch, capsys):
        """#844: a share wrapped over two stdin lines is skipped; when too
        few shares remain, the error says how many lines were skipped and
        that each card's words go on one line."""
        import io

        mns = _make_2of5()
        words = mns[0].split()
        # Lines too short to be taken for a share with typing mistakes.
        chunks = [words[i:i + 11] for i in range(0, len(words), 11)]
        assert len(chunks) == 3 and all(len(c) < 13 for c in chunks)
        piped = (" ".join(chunks[0]) + "\n\n" + " ".join(chunks[1]) + "\n"
                 + " ".join(chunks[2]) + "\n" + mns[1] + "\n")
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        rc = combine_shares.main([])
        err = capsys.readouterr().err
        assert rc == combine_shares.EXIT_CANNOT_COMBINE, err
        assert "3 line(s) of input were not read as shares" in err
        assert "on ONE line" in err

    def test_stdin_no_share_at_all_hints_one_line(self, monkeypatch, capsys):
        import io

        mns = _make_2of5()
        words = mns[0].split()
        piped = "".join(
            " ".join(words[i:i + 11]) + "\n" for i in range(0, len(words), 11)
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        assert combine_shares.main([]) == combine_shares.EXIT_USAGE
        assert "3 line(s) of input were not read" in capsys.readouterr().err

    def test_stdin_blank_lines_only_no_hint(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("\n  \n"))
        assert combine_shares.main([]) == combine_shares.EXIT_USAGE
        assert "were not read as shares" not in capsys.readouterr().err

    def test_help_documents_exit_status(self, capsys):
        assert combine_shares.main(["--help"]) == 0
        err = capsys.readouterr().err
        for code in ("  0  ", "  1  ", "  2  ", "  3  "):
            assert code in err
