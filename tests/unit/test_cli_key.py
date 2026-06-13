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

import base64
import hashlib
import json
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
from lcsas.restore._aes_pure import aes_ctr, aes_encrypt_block, key_schedule
from lcsas.restore.restic_fallback import MasterKey, _poly1305_mac

# A password with an interior byte that is NOT a trailing newline, so the
# read-time .rstrip(b"\n") cannot accidentally mask a round-trip bug.
PASSWORD = b"correct horse\x00battery staple"


def _write_pw_file(path: Path, pw: bytes = PASSWORD, trailing_nl: bool = True) -> Path:
    path.write_bytes(pw + (b"\n" if trailing_nl else b""))
    return path


def _make_key_file(keys_dir: Path, password: bytes, name: str = "key0") -> Path:
    """Write a restic-format key file under *keys_dir* unlockable by *password*.

    Uses scrypt with a small N for test speed (real restic uses 2^15).  The
    master key is random; only the password→key-file authentication matters
    for KEY-03's verification path.
    """
    keys_dir.mkdir(parents=True, exist_ok=True)
    n, r, p = 1024, 8, 1
    salt = os.urandom(64)
    derived = hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=64)
    kek_encrypt, kek_mac_k, kek_mac_r = derived[:32], derived[32:48], derived[48:64]

    mk = MasterKey(encrypt=os.urandom(32), mac_k=os.urandom(16), mac_r=os.urandom(16))
    master_json = json.dumps({
        "encrypt": base64.b64encode(mk.encrypt).decode(),
        "mac": {
            "k": base64.b64encode(mk.mac_k).decode(),
            "r": base64.b64encode(mk.mac_r).decode(),
        },
    }).encode()
    # restic authenticated encryption: IV(16) || ciphertext || Poly1305 tag(16)
    iv = os.urandom(16)
    ciphertext = aes_ctr(kek_encrypt, iv, master_json)
    s = aes_encrypt_block(iv, key_schedule(kek_mac_k))
    tag = _poly1305_mac(kek_mac_r, s, ciphertext)
    encrypted_master = iv + ciphertext + tag
    key_doc = {
        "kdf": "scrypt", "N": n, "r": r, "p": p,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(encrypted_master).decode(),
    }
    key_file = keys_dir / name
    key_file.write_text(json.dumps(key_doc))
    return key_file


def _share_mnemonic_files(out_dir: Path, repo: str = "alpha") -> list[Path]:
    """Just the mnemonic files (exclude the -card.txt files)."""
    return sorted(
        p for p in out_dir.glob(f"{repo}-share-*.txt")
        if not p.name.endswith("-card.txt")
    )


def _share_card_files(out_dir: Path, repo: str = "alpha") -> list[Path]:
    """Just the printable -card.txt files (the heir-facing artifact)."""
    return sorted(out_dir.glob(f"{repo}-share-*-card.txt"))


def _config_file(
    tmp_path: Path, *, keys_password: bytes | None = PASSWORD, **defaults: object
) -> Path:
    """Write a minimal TOML config with optional [defaults] overrides.

    By default the repo mirror gets a real ``keys/`` file unlockable by
    ``PASSWORD`` (the configured password_file's content), so the default-on
    repo-unlock check passes.  Pass ``keys_password=None`` to omit ``keys/``
    (to exercise the fail-closed path) or a different password (to exercise
    the wrong-password path).
    """
    pw_file = tmp_path / "alpha.pw"
    _write_pw_file(pw_file)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    if keys_password is not None:
        _make_key_file(mirror / "keys", keys_password)
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

    def test_cli_combine_accepts_card_files(
        self, tmp_path: Path, capsys
    ) -> None:
        """KEY-01: real -card.txt files reconstruct via `lcsas key combine`."""
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        assert main([
            "key", "split", "--repo", "alpha",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        capsys.readouterr()

        cards = _share_card_files(out)
        assert len(cards) == 5
        assert main([
            "key", "combine",
            "--share-file", str(cards[0]),
            "--share-file", str(cards[3]),
        ]) == 0
        captured = capsys.readouterr()
        assert captured.out.encode("utf-8", "surrogateescape") == PASSWORD

    def test_cli_combine_card_stdin(self, tmp_path: Path, capsys, monkeypatch) -> None:
        """KEY-01: concatenated card text on stdin reconstructs."""
        import io

        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        capsys.readouterr()

        cards = _share_card_files(out)
        piped = cards[0].read_text() + cards[1].read_text()
        monkeypatch.setattr("sys.stdin", io.StringIO(piped))
        assert main(["key", "combine"]) == 0
        captured = capsys.readouterr()
        assert captured.out.encode("utf-8", "surrogateescape") == PASSWORD

    def test_cli_combine_rejects_truncated_card(
        self, tmp_path: Path, capsys
    ) -> None:
        """KEY-01: a truncated card fails, naming the file and word count."""
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        cards = _share_card_files(out)
        trunc = tmp_path / "trunc-card.txt"
        # Keep only the header (drop the share-words line) -> 0 share words.
        head = [
            ln for ln in cards[0].read_text().splitlines()
            if "THE SHARE WORDS" not in ln
        ]
        trunc.write_text("\n".join(head[:8]) + "\n")
        assert main([
            "key", "combine",
            "--share-file", str(trunc),
            "--share-file", str(cards[1]),
        ]) == 1

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


class TestSplitRecordsState:
    """KEY-08: --config split writes a key_escrow row + prints the next step."""

    def test_split_records_state(self, tmp_path: Path, capsys) -> None:
        from lcsas.config.settings import load_config
        from lcsas.db.connection import locked_connection
        from lcsas.db.key_escrow import get_split

        cfg = _config_file(tmp_path, key_threshold=3, key_shares=4)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0

        # The catalog records the 3/4 split.
        config = load_config(cfg)
        with locked_connection(config.db_path) as conn:
            rec = get_split(conn, "alpha")
        assert rec is not None
        assert (rec.threshold, rec.shares) == (3, 4)
        assert rec.slip39_id >= 0

        # The success output names the key_split next step.
        captured = capsys.readouterr()
        assert "NEXT STEP" in captured.out
        assert "key_split = true" in captured.out
        assert "key_threshold = 3" in captured.out
        assert "key_shares = 4" in captured.out

    def test_no_config_warns_not_recorded(self, tmp_path: Path, capsys) -> None:
        pw_file = _write_pw_file(tmp_path / "pw")
        out = tmp_path / "shares"
        assert main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(pw_file), "--out", str(out),
        ]) == 0
        # Reminder still printed; plus a loud "not recorded" warning (the
        # CLI logger writes to stdout, see lcsas.log._StdoutHandler).
        captured = capsys.readouterr()
        assert "NEXT STEP" in captured.out
        assert "was NOT recorded" in captured.out

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
        # A non-wordlist token poisons the line, so no share words are
        # extracted: the card-tolerant extractor rejects it early (rc 1,
        # naming the file and the count) before reconstruction. [KEY-01]
        assert "share words" in capsys.readouterr().out

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

    def test_blank_share_file_rejected(
        self, tmp_path: Path, capsys
    ) -> None:
        # A blank --share-file holds zero share words: the extractor names
        # the file and the count rather than silently skipping it. [KEY-01]
        blank = tmp_path / "blank.txt"
        blank.write_text("   \n")
        assert main(["key", "combine", "--share-file", str(blank)]) == 1
        assert "0 share words" in capsys.readouterr().out

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


# --------------------------------------------------------------------------
# KEY-03: verify-at-split + `lcsas key verify` drill.
# --------------------------------------------------------------------------


class TestSplitVerifiesAtWrite:
    def test_split_roundtrip_verifies(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        """A corrupted written card is caught by the post-write recombine."""
        import lcsas.cli.main as cli
        from lcsas.keyshare import is_mnemonic_line

        real_write = cli._write_private_file

        def corrupting_write(path: Path, data: bytes) -> None:
            # On exactly one card, swap one mnemonic word for a different
            # wordlist word so the card still parses as 20 share words but
            # no longer reconstructs the password (RS1024 checksum breaks).
            if path.name.endswith("-share-1-card.txt"):
                lines = data.decode("utf-8").splitlines()
                for i, ln in enumerate(lines):
                    if is_mnemonic_line(ln):
                        words = ln.split()
                        words[0] = "zero" if words[0] != "zero" else "academic"
                        lines[i] = "  " + " ".join(words)
                        break
                data = ("\n".join(lines) + "\n").encode("utf-8")
            real_write(path, data)

        monkeypatch.setattr(cli, "_write_private_file", corrupting_write)
        out = tmp_path / "shares"
        rc = main([
            "key", "split", "--repo", "alpha", "--no-verify-repo",
            "--threshold", "2", "--shares", "5",
            "--password-file", str(_write_pw_file(tmp_path / "pw")),
            "--out", str(out),
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "FAILED VERIFICATION" in captured.out
        assert "Wrote" not in captured.out

    def test_split_rejects_wrong_repo_password(
        self, tmp_path: Path, capsys
    ) -> None:
        # keys/ unlockable only by a DIFFERENT password than the pw file.
        cfg = _config_file(tmp_path, keys_password=b"the-REAL-password")
        out = tmp_path / "shares"
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        assert rc == 1
        err = capsys.readouterr().out
        assert "does NOT unlock repository 'alpha'" in err
        assert "keys" in err
        assert "--no-verify-repo" in err
        assert not out.exists() or not list(out.glob("*"))

    def test_split_no_verify_repo_skips_unlock(
        self, tmp_path: Path, capsys, caplog
    ) -> None:
        cfg = _config_file(tmp_path, keys_password=b"the-REAL-password")
        out = tmp_path / "shares"
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--no-verify-repo", "--out", str(out),
        ])
        assert rc == 0
        assert "Wrote" in capsys.readouterr().out
        assert "Skipping repo-unlock check" in caplog.text

    def test_split_fails_closed_when_keys_dir_missing(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path, keys_password=None)  # no keys/
        out = tmp_path / "shares"
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        assert rc == 1
        err = capsys.readouterr().out
        assert "keys directory not found" in err
        assert str(tmp_path / "mirror" / "keys") in err
        assert not out.exists() or not list(out.glob("*"))

    def test_split_password_file_only_warns_unverified(
        self, tmp_path: Path, caplog
    ) -> None:
        out = tmp_path / "shares"
        rc = main([
            "key", "split", "--repo", "alpha",
            "--password-file", str(_write_pw_file(tmp_path / "pw")),
            "--out", str(out),
        ])
        assert rc == 0
        assert "NOT verified against any repository" in caplog.text


class TestCardStamps:
    def test_card_carries_split_date_and_id(self, tmp_path: Path) -> None:
        import re

        def split_into(out: Path) -> list[Path]:
            main([
                "key", "split", "--repo", "alpha", "--no-verify-repo",
                "--threshold", "2", "--shares", "5",
                "--password-file", str(_write_pw_file(tmp_path / "pw",
                                                      trailing_nl=False)),
                "--out", str(out),
            ])
            return _share_card_files(out)

        cards_a = split_into(tmp_path / "a")
        ids_a = set()
        for c in cards_a:
            text = c.read_text()
            assert re.search(r"^Split on   : \d{4}-\d\d-\d\d$", text,
                             re.MULTILINE)
            m = re.search(r"^Split ID   : (\d{5})$", text, re.MULTILINE)
            assert m
            ids_a.add(m.group(1))
        # All N cards of one split share one identifier.
        assert len(ids_a) == 1

        cards_b = split_into(tmp_path / "b")
        m_b = re.search(r"^Split ID   : (\d{5})$", cards_b[0].read_text(),
                        re.MULTILINE)
        assert m_b
        # Two independent splits almost certainly differ.
        assert m_b.group(1) != ids_a.pop()

    def test_stamped_cards_still_combine(self, tmp_path: Path, capsys) -> None:
        out = tmp_path / "shares"
        main([
            "key", "split", "--repo", "alpha", "--no-verify-repo",
            "--password-file", str(_write_pw_file(tmp_path / "pw")),
            "--out", str(out),
        ])
        capsys.readouterr()
        cards = _share_card_files(out)
        rc = main([
            "key", "combine",
            "--share-file", str(cards[0]),
            "--share-file", str(cards[1]),
        ])
        assert rc == 0
        assert capsys.readouterr().out.encode() == PASSWORD


class TestKeyVerify:
    def test_key_verify_ok_path(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)  # keys/ unlockable by PASSWORD
        out = tmp_path / "shares"
        main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        capsys.readouterr()
        cards = _share_card_files(out)
        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "alpha",
            "--share-file", str(cards[0]), "--share-file", str(cards[1]),
        ])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_key_verify_password_file_ok(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "alpha",
            "--password-file", str(tmp_path / "alpha.pw"),
        ])
        assert rc == 0
        assert "OK: the password unlocks 'alpha'" in capsys.readouterr().out

    def test_key_verify_detects_stale_shares(
        self, tmp_path: Path, capsys
    ) -> None:
        # Split against a keys/ for PASSWORD, then "re-key": replace keys/
        # with one derived from a different password.  The old cards no
        # longer unlock.
        cfg = _config_file(tmp_path)
        out = tmp_path / "shares"
        main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        capsys.readouterr()
        cards = _share_card_files(out)

        keys_dir = tmp_path / "mirror" / "keys"
        for kf in keys_dir.iterdir():
            kf.unlink()
        _make_key_file(keys_dir, b"rotated-new-password")

        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "alpha",
            "--share-file", str(cards[0]), "--share-file", str(cards[1]),
        ])
        assert rc != 0
        assert "does NOT unlock repository 'alpha'" in capsys.readouterr().out

    def test_key_verify_reconstruction_failure(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path)
        out = tmp_path / "shares"
        main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        capsys.readouterr()
        cards = _share_card_files(out)
        # One card is below threshold (K=2) → reconstruction fails.
        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "alpha",
            "--share-file", str(cards[0]),
        ])
        assert rc != 0
        assert "Reconstruction failed" in capsys.readouterr().out

    def test_key_verify_requires_config(self, tmp_path: Path, capsys) -> None:
        rc = main(["key", "verify", "--repo", "alpha",
                   "--password-file", str(tmp_path / "x")])
        assert rc == 1
        assert "--config is required" in capsys.readouterr().out

    def test_key_verify_requires_exactly_one_source(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path)
        # Neither source.
        rc = main(["--config", str(cfg), "key", "verify", "--repo", "alpha"])
        assert rc == 1
        assert "EITHER" in capsys.readouterr().out
        # Both sources.
        out = tmp_path / "shares"
        main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        capsys.readouterr()
        cards = _share_card_files(out)
        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "alpha",
            "--password-file", str(tmp_path / "alpha.pw"),
            "--share-file", str(cards[0]),
        ])
        assert rc == 1
        assert "EITHER" in capsys.readouterr().out

    def test_key_verify_unknown_repo(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        rc = main([
            "--config", str(cfg),
            "key", "verify", "--repo", "ghost",
            "--password-file", str(tmp_path / "alpha.pw"),
        ])
        assert rc == 1
        assert "not defined in the config" in capsys.readouterr().out

    def test_key_router_lists_verify(self, capsys) -> None:
        assert main(["key"]) == 1
        assert "verify" in capsys.readouterr().out


# --------------------------------------------------------------------------
# KEY-09: single-key Recovery Card (`lcsas key card`)
# --------------------------------------------------------------------------

class TestKeyCard:
    def test_key_card_renders(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path, label_prefix='"FAMILY"')
        # key_storage_hints lives in [survivability]; append it.
        cfg.write_text(
            cfg.read_text()
            + '[survivability]\nkey_storage_hints = "Safe at home; bank box #7"\n'
        )
        rc = main([
            "--config", str(cfg),
            "key", "card", "--repo", "alpha",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Card carries the repo name, key file name, hints, a date, and code.
        assert "alpha" in out
        assert "alpha.pw" in out          # repo_cfg.password_file.name
        assert "Safe at home; bank box #7" in out
        assert "FAMILY_*" in out
        # ISO date is present.
        import datetime
        assert datetime.date.today().isoformat() in out
        # 4-char check code = first 4 hex of SHA-256(PASSWORD).
        expected_code = hashlib.sha256(PASSWORD).hexdigest()[:4]
        assert expected_code in out
        assert "TRANSCRIPTION CHECK CODE" in out
        # The password itself is NEVER printed.
        assert PASSWORD.decode("latin-1") not in out
        assert "correct horse" not in out

    def test_key_card_no_check_code(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        rc = main([
            "--config", str(cfg),
            "key", "card", "--repo", "alpha", "--no-check-code",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "TRANSCRIPTION CHECK CODE" not in out
        expected_code = hashlib.sha256(PASSWORD).hexdigest()[:4]
        assert expected_code not in out

    def test_key_card_single_repo_no_flag(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        rc = main(["--config", str(cfg), "key", "card"])
        assert rc == 0
        assert "alpha" in capsys.readouterr().out

    def test_key_card_to_file_mode_0600(self, tmp_path: Path) -> None:
        cfg = _config_file(tmp_path)
        out_file = tmp_path / "card.txt"
        rc = main([
            "--config", str(cfg),
            "key", "card", "--repo", "alpha", "--out", str(out_file),
        ])
        assert rc == 0
        assert out_file.exists()
        assert (os.stat(out_file).st_mode & 0o777) == 0o600

    def test_key_card_requires_config(self, capsys) -> None:
        rc = main(["key", "card", "--repo", "alpha"])
        assert rc == 1
        assert "config" in capsys.readouterr().out.lower()

    def test_key_card_unknown_repo(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        rc = main(["--config", str(cfg), "key", "card", "--repo", "ghost"])
        assert rc == 1
        assert "not defined in the config" in capsys.readouterr().out

    def test_key_card_check_mode(self, tmp_path: Path, capsys) -> None:
        pw_file = _write_pw_file(tmp_path / "typed.pw")
        code = hashlib.sha256(PASSWORD).hexdigest()[:4]
        # Correct transcription -> MATCH, rc 0.
        rc = main([
            "key", "card", "--check", str(pw_file), "--code", code,
        ])
        assert rc == 0
        assert "MATCH" in capsys.readouterr().out

    def test_key_card_check_mismatch(self, tmp_path: Path, capsys) -> None:
        # One-char typo in the typed password file.
        typo = PASSWORD[:-1] + bytes([PASSWORD[-1] ^ 0x01])
        pw_file = _write_pw_file(tmp_path / "typed.pw", pw=typo)
        code = hashlib.sha256(PASSWORD).hexdigest()[:4]
        rc = main([
            "key", "card", "--check", str(pw_file), "--code", code,
        ])
        assert rc == 1
        assert "MISMATCH" in capsys.readouterr().out

    def test_key_card_check_requires_code(self, tmp_path: Path, capsys) -> None:
        pw_file = _write_pw_file(tmp_path / "typed.pw")
        rc = main(["key", "card", "--check", str(pw_file)])
        assert rc == 1
        assert "--code" in capsys.readouterr().out

    def test_key_card_check_missing_file(self, tmp_path: Path, capsys) -> None:
        rc = main([
            "key", "card", "--check", str(tmp_path / "nope.pw"),
            "--code", "abcd",
        ])
        assert rc == 1
        assert "does not exist" in capsys.readouterr().out

    def test_key_card_code_without_check(self, capsys) -> None:
        rc = main(["key", "card", "--code", "abcd"])
        assert rc == 1
        assert "--check" in capsys.readouterr().out

    def test_key_card_missing_password_file(self, tmp_path: Path, capsys) -> None:
        cfg = _config_file(tmp_path)
        # Remove the configured password file so the check-code path errors.
        (tmp_path / "alpha.pw").unlink()
        rc = main(["--config", str(cfg), "key", "card", "--repo", "alpha"])
        assert rc == 1
        assert "does not exist" in capsys.readouterr().out

    def test_key_card_router_lists_card(self, capsys) -> None:
        assert main(["key"]) == 1
        assert "card" in capsys.readouterr().out
