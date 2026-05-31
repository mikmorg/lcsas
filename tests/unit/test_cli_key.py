"""End-to-end CLI tests for `lcsas key split` / `lcsas key combine`.

Drives the real CLI entry point (``main([...])``).  Targets 100% line
coverage of ``cmd_key_split`` / ``cmd_key_combine`` and their helpers in
``lcsas.cli.main`` plus the config additions.

Also exercises the password<->master-secret codec directly so the
documented gate command (which runs only this file) covers every branch
of ``lcsas.keyshare.codec`` — the CLI combine path never reaches the
decode-error raises because corrupted/foreign shares fail earlier in
``recover_secret``.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.keyshare import (
    KeyShareError,
    decode_master_secret,
    encode_master_secret,
)

# A password with an interior byte that is NOT a trailing newline, so the
# read-time .rstrip(b"\n") cannot accidentally mask a round-trip bug.
PASSWORD = b"correct horse\x00battery staple"


def _write_pw_file(path: Path, pw: bytes = PASSWORD, trailing_nl: bool = True) -> Path:
    path.write_bytes(pw + (b"\n" if trailing_nl else b""))
    return path


def _share_mnemonic_files(out_dir: Path, repo: str = "alpha") -> list[Path]:
    """Just the mnemonic files (exclude the -card.txt files)."""
    return sorted(
        p for p in out_dir.glob(f"{repo}-share-*.txt")
        if not p.name.endswith("-card.txt")
    )


def _config_file(tmp_path: Path, **defaults: object) -> Path:
    """Write a minimal TOML config with optional [defaults] overrides."""
    pw_file = tmp_path / "alpha.pw"
    _write_pw_file(pw_file)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    lines = [
        "[paths]",
        f'mirror_base = "{tmp_path / "mirror_base"}"',
        f'staging = "{tmp_path / "staging"}"',
        f'database = "{tmp_path / "archive.db"}"',
    ]
    if defaults:
        lines.append("[defaults]")
        for k, v in defaults.items():
            lines.append(f"{k} = {v}")
    lines += [
        "[repos.alpha]",
        f'mirror_path = "{mirror}"',
        f'password_file = "{pw_file}"',
    ]
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


# --------------------------------------------------------------------------
# Happy path: split via --password-file, combine back byte-identical.
# --------------------------------------------------------------------------

class TestSplitCombineRoundTrip:
    def test_split_then_combine_any_two(self, tmp_path: Path, capsys) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        assert main([
            "key", "split", "--repo", "alpha",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0

        mfiles = _share_mnemonic_files(out)
        assert len(mfiles) == 5
        # Cards exist too.
        assert len(list(out.glob("alpha-share-*-card.txt"))) == 5

        recovered = tmp_path / "recovered"
        assert main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(mfiles[3]),
            "--out", str(recovered),
        ]) == 0
        assert recovered.read_bytes() == PASSWORD

    def test_combine_to_stdout_raw_no_newline(self, tmp_path: Path, capsys) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ])
        capsys.readouterr()
        mfiles = _share_mnemonic_files(out)
        assert main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(mfiles[1]),
        ]) == 0
        captured = capsys.readouterr()
        # Raw bytes on stdout, no trailing newline added.
        assert captured.out.encode("utf-8", "surrogateescape") == PASSWORD

    def test_share_files_mode_0600(self, tmp_path: Path) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ])
        for p in out.glob("alpha-share-*"):
            assert (os.stat(p).st_mode & 0o777) == 0o600

    def test_out_file_mode_0600(self, tmp_path: Path) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ])
        mfiles = _share_mnemonic_files(out)
        recovered = tmp_path / "recovered"
        main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(mfiles[1]),
            "--out", str(recovered),
        ])
        assert (os.stat(recovered).st_mode & 0o777) == 0o600

    def test_default_out_dir(self, tmp_path: Path, monkeypatch) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        monkeypatch.chdir(tmp_path)
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file),
        ]) == 0
        assert (tmp_path / "keyshares-alpha").is_dir()
        assert len(_share_mnemonic_files(tmp_path / "keyshares-alpha")) == 5

    def test_card_content(self, tmp_path: Path) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw_file), "--out", str(out),
        ])
        card = (out / "alpha-share-1-card.txt").read_text()
        assert "LCSAS KEY SHARE" in card
        assert "Share      : 1 of 5" in card
        assert "ANY 2 of the 5" in card
        assert "Other people each hold" in card
        assert "can never be recovered" in card


# --------------------------------------------------------------------------
# Config-driven defaults for K/N.
# --------------------------------------------------------------------------

class TestConfigDefaults:
    def test_default_k_n_from_config(self, tmp_path: Path) -> None:
        cfg = _config_file(tmp_path)  # no overrides -> 2-of-5
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0
        assert len(_share_mnemonic_files(out)) == 5  # default N=5

    def test_config_override_changes_k_n(self, tmp_path: Path) -> None:
        cfg = _config_file(tmp_path, key_threshold=3, key_shares=4)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0
        mfiles = _share_mnemonic_files(out)
        assert len(mfiles) == 4  # overridden N=4
        # And the threshold is genuinely 3: any 2 must NOT reconstruct.
        assert main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(mfiles[1]),
        ]) == 1

    def test_config_password_file_used(self, tmp_path: Path) -> None:
        cfg = _config_file(tmp_path)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0
        mfiles = _share_mnemonic_files(out)
        recovered = tmp_path / "recovered"
        main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(mfiles[1]),
            "--out", str(recovered),
        ])
        assert recovered.read_bytes() == PASSWORD


# --------------------------------------------------------------------------
# Split error branches.
# --------------------------------------------------------------------------

class TestSplitErrors:
    def test_no_password_source_no_config(self, tmp_path: Path, capsys) -> None:
        assert main(["key", "split", "--repo", "alpha"]) == 1
        assert "No password source" in capsys.readouterr().out

    def test_repo_not_in_config(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        assert main([
            "--config", str(cfg), "key", "split", "--repo", "ghost",
        ]) == 1
        assert "not defined in the config" in capsys.readouterr().out

    def test_repo_has_no_password_file(self, tmp_path: Path, capsys) -> None:
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        cfg = tmp_path / "lcsas.toml"
        cfg.write_text(textwrap.dedent(f"""
            [paths]
            mirror_base = "{tmp_path / 'mirror_base'}"
            staging = "{tmp_path / 'staging'}"
            database = "{tmp_path / 'archive.db'}"
            [repos.alpha]
            mirror_path = "{mirror}"
        """))
        assert main([
            "--config", str(cfg), "key", "split", "--repo", "alpha",
        ]) == 1
        assert "no password_file configured" in capsys.readouterr().out

    def test_password_file_missing(self, tmp_path: Path, capsys) -> None:
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(tmp_path / "nope"),
        ]) == 1
        assert "Password file does not exist" in capsys.readouterr().out

    def test_keyshare_error_surfaced(self, tmp_path: Path, capsys) -> None:
        # threshold > shares is invalid -> KeyShareError from split_secret.
        pw_file = _write_pw_file(tmp_path / "pw")
        assert main([
            "key", "split", "--repo", "alpha",
            "--threshold", "5", "--shares", "2",
            "--password-file", str(pw_file), "--out", str(tmp_path / "s"),
        ]) == 1
        assert "Could not split password" in capsys.readouterr().out

    def test_oversized_password_surfaced(self, tmp_path: Path, capsys) -> None:
        pw_file = tmp_path / "big"
        pw_file.write_bytes(b"\x01" * (0xFFFF + 5))  # no trailing \n to strip
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(tmp_path / "s"),
        ]) == 1
        assert "Could not split password" in capsys.readouterr().out

    def test_missing_repo_arg_exits(self, tmp_path: Path) -> None:
        # argparse 'required=True' -> SystemExit(2).
        with pytest.raises(SystemExit):
            main(["key", "split", "--password-file", str(tmp_path / "x")])


# --------------------------------------------------------------------------
# Combine error branches + stdin path.
# --------------------------------------------------------------------------

class TestCombineErrors:
    def _make_shares(self, tmp_path: Path) -> list[Path]:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw_file), "--out", str(out),
        ])
        return _share_mnemonic_files(out)

    def test_no_shares_supplied(self, tmp_path: Path, capsys, monkeypatch) -> None:
        # No --share-file and empty stdin.
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert main(["key", "combine"]) == 1
        assert "No shares supplied" in capsys.readouterr().out

    def test_under_threshold_fails(self, tmp_path: Path, capsys) -> None:
        mfiles = self._make_shares(tmp_path)
        assert main(["key", "combine", "--share-file", str(mfiles[0])]) == 1
        assert "Could not reconstruct" in capsys.readouterr().out

    def test_corrupted_share_fails(self, tmp_path: Path, capsys) -> None:
        mfiles = self._make_shares(tmp_path)
        bad = tmp_path / "bad.txt"
        words = mfiles[1].read_text().strip().split()
        words[3] = "zzzzzz"  # not a valid wordlist word
        bad.write_text(" ".join(words))
        assert main([
            "key", "combine",
            "--share-file", str(mfiles[0]),
            "--share-file", str(bad),
        ]) == 1
        assert "Could not reconstruct" in capsys.readouterr().out

    def test_foreign_share_set_fails(self, tmp_path: Path, capsys) -> None:
        mine = self._make_shares(tmp_path)
        # A second, independent split -> different identifier.
        pw2 = _write_pw_file(tmp_path / "pw2", b"another-secret")
        out2 = tmp_path / "shares2"
        main([
            "key", "split", "--repo", "beta",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw2), "--out", str(out2),
        ])
        foreign = _share_mnemonic_files(out2, "beta")
        assert main([
            "key", "combine",
            "--share-file", str(mine[0]),
            "--share-file", str(foreign[0]),
        ]) == 1
        assert "Could not reconstruct" in capsys.readouterr().out

    def test_share_file_missing(self, tmp_path: Path, capsys) -> None:
        assert main([
            "key", "combine", "--share-file", str(tmp_path / "nope.txt"),
        ]) == 1
        assert "Share file does not exist" in capsys.readouterr().out

    def test_blank_share_file_skipped_then_no_shares(
        self, tmp_path: Path, capsys
    ) -> None:
        blank = tmp_path / "blank.txt"
        blank.write_text("   \n")
        assert main(["key", "combine", "--share-file", str(blank)]) == 1
        assert "No shares supplied" in capsys.readouterr().out

    def test_combine_from_stdin(self, tmp_path: Path, capsys, monkeypatch) -> None:
        import io
        mfiles = self._make_shares(tmp_path)
        stdin = "\n".join([
            mfiles[0].read_text().strip(),
            "",  # blank line is skipped
            mfiles[2].read_text().strip(),
        ]) + "\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        capsys.readouterr()  # drop the split's stdout
        assert main(["key", "combine"]) == 0
        out = capsys.readouterr().out
        assert out.encode("utf-8", "surrogateescape") == PASSWORD


# --------------------------------------------------------------------------
# Router / usage branches.
# --------------------------------------------------------------------------

class TestKeyRouter:
    def test_key_no_subcommand(self, capsys) -> None:
        assert main(["key"]) == 1
        assert "Usage: lcsas key" in capsys.readouterr().out


# --------------------------------------------------------------------------
# _write_private_file refuses to clobber an existing file.
# --------------------------------------------------------------------------

class TestWritePrivateFile:
    def test_refuses_existing(self, tmp_path: Path) -> None:
        from lcsas.cli.main import _write_private_file

        target = tmp_path / "exists"
        target.write_bytes(b"old")
        with pytest.raises(FileExistsError):
            _write_private_file(target, b"new")


# --------------------------------------------------------------------------
# Password <-> SLIP-0039 master-secret codec (kept in THIS file so the
# documented gate command, which runs only this file, covers codec.py 100%).
# --------------------------------------------------------------------------

class TestCodecRoundTrip:
    @pytest.mark.parametrize(
        "pw",
        [
            b"",                       # empty
            b"x",                      # 1 byte (odd body -> pad)
            b"odd",                    # 3 bytes
            b"even",                   # 4 bytes
            b"x" * 12,                 # body 14 -> exactly the 14-byte case
            b"x" * 14,                 # body 16
            b"x" * 16,                 # >16
            b"\x00\x01\x02\xff\x00",   # interior + leading/trailing zero bytes
            b"long" * 300,             # > 1KB
        ],
    )
    def test_roundtrip(self, pw: bytes) -> None:
        assert decode_master_secret(encode_master_secret(pw)) == pw

    def test_output_even_and_min_16(self) -> None:
        for pw in (b"", b"x", b"abc", b"a" * 13, b"a" * 100):
            ms = encode_master_secret(pw)
            assert len(ms) % 2 == 0
            assert len(ms) >= 16

    def test_body_14_pads_to_16(self) -> None:
        assert len(encode_master_secret(b"x" * 12)) == 16  # 2+12 body -> 16

    def test_body_16_stays_16(self) -> None:
        assert len(encode_master_secret(b"x" * 14)) == 16  # 2+14 body, already ok

    def test_odd_body_pads_even(self) -> None:
        assert len(encode_master_secret(b"x" * 15)) == 18  # 2+15=17 -> 18

    def test_max_length_ok(self) -> None:
        pw = b"\x00" * 0xFFFF
        assert decode_master_secret(encode_master_secret(pw)) == pw


class TestCodecErrors:
    def test_oversized_raises(self) -> None:
        with pytest.raises(KeyShareError, match="too long"):
            encode_master_secret(b"\x00" * (0xFFFF + 1))

    def test_decode_too_short_for_prefix(self) -> None:
        with pytest.raises(KeyShareError, match="too short"):
            decode_master_secret(b"\x00")

    def test_decode_empty_too_short(self) -> None:
        with pytest.raises(KeyShareError, match="too short"):
            decode_master_secret(b"")

    def test_decode_truncated_overruns(self) -> None:
        ms = (10).to_bytes(2, "big") + b"abc"  # claims 10, carries 3
        with pytest.raises(KeyShareError, match="corrupt or truncated"):
            decode_master_secret(ms)


class TestEntryPointExitCode:
    """Regression: `python -m lcsas` must propagate the handler's exit code.

    `src/lcsas/__main__.py` previously called `main()` without `sys.exit(...)`,
    so a failing command (e.g. `key combine` with too few shares) silently
    exited 0 under `python -m`.
    """

    def _run(self, args: list[str], stdin: str = "") -> int:
        import os
        import subprocess
        import sys
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        return subprocess.run(
            [sys.executable, "-m", "lcsas", *args],
            input=stdin, text=True, capture_output=True, env=env,
        ).returncode

    def test_combine_no_shares_exits_nonzero(self) -> None:
        assert self._run(["key", "combine"], stdin="") != 0

    def test_combine_insufficient_shares_exits_nonzero(self, tmp_path: Path) -> None:
        # one valid share from a real 2-of-5 split is below threshold
        from lcsas.keyshare import split_secret
        from lcsas.keyshare.codec import encode_master_secret
        share = split_secret(encode_master_secret(b"pw12345678"), 2, 5)[0]
        f = tmp_path / "s.txt"
        f.write_text(share, encoding="utf-8")
        assert self._run(["key", "combine", "--share-file", str(f)]) != 0
