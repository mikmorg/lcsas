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


def _register_repo(db_path: Path, name: str, mirror: Path) -> str:
    """Register *name* the way ``lcsas repo add`` does: under a fresh UUID
    repo_id that differs from the name (#571).  Returns the repo_id."""
    from lcsas.db.connection import locked_connection
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import ensure_schema
    from lcsas.utils.labels import generate_uuid

    repo_id = generate_uuid()
    assert repo_id != name
    with locked_connection(db_path) as conn:
        ensure_schema(conn)
        register_repo(conn, repo_id, name, str(mirror.resolve()))
    return repo_id


def _config_file(
    tmp_path: Path,
    *,
    keys_password: bytes | None = PASSWORD,
    register: bool = True,
    **defaults: object,
) -> Path:
    """Write a minimal TOML config with optional [defaults] overrides.

    By default the repo mirror gets a real ``keys/`` file unlockable by
    ``PASSWORD`` (the configured password_file's content), so the default-on
    repo-unlock check passes.  Pass ``keys_password=None`` to omit ``keys/``
    (to exercise the fail-closed path) or a different password (to exercise
    the wrong-password path).

    By default ``alpha`` is also registered in the catalog under a UUID
    repo_id, as ``repo add`` would do, because ``key split --config``
    refuses a repository the catalog does not know (#571).  Pass
    ``register=False`` to leave the catalog absent.
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
    if register:
        _register_repo(tmp_path / "archive.db", "alpha", mirror)
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
        assert "LCSAS SHARE CARD" in card
        assert "Share      : 1 of 5" in card
        assert "ANY 2 of the 5" in card
        assert "Other people each hold" in card
        assert "can never be rebuilt" in card


# --------------------------------------------------------------------------
# FUP-03: weak-password advisory (warn, never block).
# --------------------------------------------------------------------------

class TestWeakPasswordWarning:
    def test_key_split_warns_on_short_password(
        self, tmp_path: Path, caplog
    ) -> None:
        """A short/low-entropy password file warns on stderr but the split
        still succeeds (exit code unchanged)."""
        import logging

        pw_file = _write_pw_file(tmp_path / "pw", pw=b"hunter2")
        out = tmp_path / "shares"
        with caplog.at_level(logging.WARNING, logger="lcsas.cli.main"):
            rc = main([
                "key", "split", "--repo", "alpha",
                "--no-verify-repo",
                "--password-file", str(pw_file), "--out", str(out),
            ])
        assert rc == 0  # warning, not a block
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "Weak password" in msgs
        assert "DISC_CONFIDENTIALITY.md" in msgs
        # The split still produced cards.
        assert len(_share_card_files(out)) == 5

    def test_key_split_no_warning_on_strong_password(
        self, tmp_path: Path, caplog
    ) -> None:
        """A long, mixed-class password does not trip the advisory."""
        import logging

        strong = b"Tr0ub4dor&3-correct-horse-battery-staple-7x"
        pw_file = _write_pw_file(tmp_path / "pw", pw=strong)
        out = tmp_path / "shares"
        with caplog.at_level(logging.WARNING, logger="lcsas.cli.main"):
            rc = main([
                "key", "split", "--repo", "alpha",
                "--no-verify-repo",
                "--password-file", str(pw_file), "--out", str(out),
            ])
        assert rc == 0
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "Weak password" not in msgs


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

        # The catalog records the 3/4 split under the repository's catalog
        # repo_id (a UUID), not under the name given to --repo (#571).
        config = load_config(cfg)
        with locked_connection(config.db_path) as conn:
            (repo_id,) = [
                r["repo_id"] for r in conn.execute(
                    "SELECT repo_id FROM repositories WHERE name = 'alpha'"
                )
            ]
            rec = get_split(conn, repo_id)
            by_name = get_split(conn, "alpha")
        assert repo_id != "alpha"
        assert rec is not None
        assert rec.repo_id == repo_id
        assert (rec.threshold, rec.shares) == (3, 4)
        assert rec.slip39_id >= 0
        assert by_name is None

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


class TestSplitRecordedUnderRepoId:
    """#571: ``key split --repo <name>`` records the split under the catalog
    repo_id that ``repo add`` assigned, which is the ID the stage preflight
    and the disc text look it up by.

    The end-to-end tests use only the CLI, the orchestrator and long-standing
    catalog helpers, so on the unfixed code they fail on the preflight
    ("records no split") rather than on an import.
    """

    @staticmethod
    def _repo_add_and_split(
        tmp_path: Path, *, k: int = 3, n: int = 4, **defaults: object
    ) -> Path:
        cfg = _config_file(
            tmp_path, register=False, key_threshold=k, key_shares=n, **defaults
        )
        assert main(["--config", str(cfg), "init"]) == 0
        assert main([
            "--config", str(cfg),
            "repo", "add", "alpha", str(tmp_path / "mirror"),
        ]) == 0
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(tmp_path / "shares"),
        ]) == 0
        return cfg

    @staticmethod
    def _stage_one_volume(cfg: Path, *, allow_drift: bool = False) -> Path:
        """Seed one pack for ``alpha`` and run the orchestrator's prepare()
        against the CLI's catalog.  Returns the volume's staging root."""
        import dataclasses
        from unittest.mock import MagicMock

        from lcsas.burn.operator import NullOperatorPrompt
        from lcsas.burn.orchestrator import BurnOrchestrator
        from lcsas.config.media import MediaType
        from lcsas.config.settings import load_config
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import register_pack

        config = load_config(cfg)
        mirror = config.repositories["alpha"].mirror_path
        for sub in ("data", "index", "snapshots", "keys"):
            (mirror / sub).mkdir(exist_ok=True)
        (mirror / "config").write_text('{"version": 2}')
        content = b"issue-571 pack " * 64
        sha = hashlib.sha256(content).hexdigest()
        (mirror / "data" / sha).write_bytes(content)
        config.staging_path.mkdir(parents=True, exist_ok=True)

        # The fake mirror has no real rustic data, so skip the FMT-03
        # decode proof (it needs a password_file to run at all).
        config = dataclasses.replace(
            config,
            default_media_type=MediaType.TEST_TINY,
            default_ecc_redundancy_pct=0,
            metadata_reserve_bytes=1000,
            label_prefix="TEST",
            allow_unverified_repo_format=True,
            repositories={
                "alpha": dataclasses.replace(
                    config.repositories["alpha"], password_file=None
                ),
            },
        )
        conn = get_connection(config.db_path)
        try:
            (repo_id,) = [
                r["repo_id"] for r in conn.execute(
                    "SELECT repo_id FROM repositories WHERE name = 'alpha'"
                )
            ]
            assert repo_id != "alpha"  # repo add assigns a UUID
            register_pack(conn, sha256=sha, size_bytes=len(content),
                          repo_id=repo_id)
            orch = BurnOrchestrator(
                config, conn, MagicMock(), MagicMock(),
                prompt=NullOperatorPrompt(),
                allow_escrow_drift=allow_drift,
            )
            manifest = orch.prepare()
        finally:
            conn.close()
        return manifest.staging_path

    def test_repo_add_split_then_stage_preflight_passes(
        self, tmp_path: Path
    ) -> None:
        """repo add -> key split -> stage with key_split=true: the preflight
        finds the split and the disc text names its K/N."""
        cfg = self._repo_add_and_split(tmp_path, key_split="true")
        root = self._stage_one_volume(cfg)  # must not raise EscrowDriftError
        start_here = (root / "START_HERE.txt").read_text(encoding="utf-8")
        assert "YOUR PASSWORD IS SPLIT INTO 4 SHARE CARDS" in start_here
        assert "You need ANY 3 of them." in start_here
        info = (root / "PASSWORD_INFO.txt").read_text(encoding="utf-8")
        assert "password is SPLIT into 4 share cards." in info
        assert "any 3 of them.  Fewer than 3 cards reveal nothing." in info

    def test_recorded_kn_reaches_disc_text_over_config(
        self, tmp_path: Path
    ) -> None:
        """The disc text takes K/N from the recorded split, not lcsas.toml.

        Split 3-of-4, then claim 2-of-5 in the config and pass the drift
        override: the discs must still say 3-of-4.
        """
        cfg = self._repo_add_and_split(tmp_path)
        text = cfg.read_text().replace(
            "key_threshold = 3", "key_threshold = 2"
        ).replace("key_shares = 4", "key_shares = 5\nkey_split = true")
        cfg.write_text(text)
        root = self._stage_one_volume(cfg, allow_drift=True)
        start_here = (root / "START_HERE.txt").read_text(encoding="utf-8")
        assert "YOUR PASSWORD IS SPLIT INTO 4 SHARE CARDS" in start_here
        assert "You need ANY 3 of them." in start_here
        info = (root / "PASSWORD_INFO.txt").read_text(encoding="utf-8")
        assert "password is SPLIT into 4 share cards." in info
        assert "any 3 of them.  Fewer than 3 cards reveal nothing." in info
        for name, text in (("START_HERE.txt", start_here),
                           ("PASSWORD_INFO.txt", info)):
            assert "split into 5" not in text.lower(), name
            assert "any 2" not in text.lower(), name

    def test_unregistered_repo_refused_before_any_share(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path, register=False)
        assert main(["--config", str(cfg), "init"]) == 0
        capsys.readouterr()
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 1
        msg = capsys.readouterr().out
        db = (tmp_path / "archive.db").resolve()
        mirror = (tmp_path / "mirror").resolve()
        assert (
            f"Repository 'alpha' is not registered in the catalog at {db}. "
            f"Register it first with 'lcsas --config ... repo add alpha "
            f"{mirror}', then split again. No shares were written."
        ) in msg
        assert not out.exists()
        assert "Wrote" not in msg

    def test_no_catalog_refused_before_any_share(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path, register=False)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 1
        msg = capsys.readouterr().out
        db = (tmp_path / "archive.db").resolve()
        mirror = (tmp_path / "mirror").resolve()
        assert (
            f"No catalog at {db}, so the split would not be recorded. If the "
            f"catalog is on a drive that is not mounted, mount it and split "
            f"again. If this is a new archive, run 'lcsas --config ... init', "
            f"then 'lcsas --config ... repo add alpha {mirror}', then split "
            f"again. No shares were written."
        ) in msg
        assert not out.exists()
        # The refusal must not leave a fresh catalog behind.
        assert not (tmp_path / "archive.db").exists()

    def test_two_repositories_with_one_name_refused(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path)  # registers alpha once ...
        _register_repo(tmp_path / "archive.db", "alpha", tmp_path / "mirror")
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 1
        msg = capsys.readouterr().out
        assert "More than one repository in the catalog is named 'alpha'" in msg
        assert "No shares were written." in msg
        assert not out.exists()

    def test_unreadable_catalog_still_writes_cards_and_warns(
        self, tmp_path: Path, capsys
    ) -> None:
        """A catalog that exists but cannot be read keeps the old behaviour:
        the cards are written and the owner is told to re-run later."""
        cfg = _config_file(tmp_path, register=False)
        (tmp_path / "archive.db").write_bytes(b"this is not an SQLite file" * 64)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0
        msg = capsys.readouterr().out
        assert "Could not read the catalog at" in msg
        assert "Split succeeded but recording it in the catalog failed" in msg
        assert "key split --repo alpha' so burn drift-checking works" in msg
        assert len(_share_card_files(out)) == 5

    def test_damaged_catalog_still_writes_cards_and_warns(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        """get_connection raises RuntimeError when the catalog's integrity
        check fails.  That is a catalog that cannot be read, so it takes the
        same warn-and-go-ahead path, not main()'s "Unexpected error"."""
        import lcsas.db.connection as connection

        cfg = _config_file(tmp_path)  # archive.db exists, alpha registered
        db = (tmp_path / "archive.db").resolve()
        damage = "Catalog integrity check failed (test)"

        def _damaged(db_path: object) -> object:
            raise RuntimeError(damage)

        monkeypatch.setattr(connection, "get_connection", _damaged)
        out = tmp_path / "shares"
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(out),
        ]) == 0
        msg = capsys.readouterr().out
        assert (
            f"Could not read the catalog at {db} ({damage}). The split will "
            f"go ahead, but it may not be recorded."
        ) in msg
        assert "Unexpected error" not in msg
        assert "Split succeeded but recording it in the catalog failed" in msg
        assert len(_share_card_files(out)) == 5

    def test_resplit_drops_a_legacy_name_keyed_record(
        self, tmp_path: Path
    ) -> None:
        from lcsas.db.connection import locked_connection
        from lcsas.db.key_escrow import get_split, record_split

        cfg = _config_file(tmp_path, key_threshold=3, key_shares=4)
        db = tmp_path / "archive.db"
        with locked_connection(db) as conn:
            record_split(conn, "alpha", 2, 5, 1)  # as builds before #571 did
        assert main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha", "--out", str(tmp_path / "s"),
        ]) == 0
        with locked_connection(db) as conn:
            (repo_id,) = [
                r["repo_id"] for r in conn.execute(
                    "SELECT repo_id FROM repositories WHERE name = 'alpha'"
                )
            ]
            rec = get_split(conn, repo_id)
            legacy = get_split(conn, "alpha")
            rows = conn.execute("SELECT COUNT(*) FROM key_escrow").fetchone()[0]
        assert rec is not None and (rec.threshold, rec.shares) == (3, 4)
        assert legacy is None
        assert rows == 1


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

    # #481: a damaged key file in the mirror is not a wrong password.
    _BAD_KEY = b'{"salt": "abc", "N": 1024, "r": 8, "p": 1, "data": "AAAA"}'

    def test_split_corrupt_keys_are_not_called_a_wrong_password(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path)
        keys_dir = tmp_path / "mirror" / "keys"
        for kf in keys_dir.iterdir():
            kf.write_bytes(self._BAD_KEY)
        out = tmp_path / "shares"
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        assert rc == 1
        err = capsys.readouterr().out
        assert "unreadable or corrupt" in err
        assert "does NOT unlock" not in err
        assert "--no-verify-repo" in err
        assert not out.exists() or not list(out.glob("*"))

    def test_split_skips_a_corrupt_key_ahead_of_the_good_one(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path)
        (tmp_path / "mirror" / "keys" / "0000-corrupt").write_bytes(self._BAD_KEY)
        out = tmp_path / "shares"
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(out),
        ])
        assert rc == 0
        assert "Wrote" in capsys.readouterr().out

    def test_split_wrong_password_also_names_the_corrupt_key(
        self, tmp_path: Path, capsys
    ) -> None:
        cfg = _config_file(tmp_path, keys_password=b"the-REAL-password")
        (tmp_path / "mirror" / "keys" / "0000-corrupt").write_bytes(self._BAD_KEY)
        rc = main([
            "--config", str(cfg),
            "key", "split", "--repo", "alpha",
            "--out", str(tmp_path / "shares"),
        ])
        assert rc == 1
        err = capsys.readouterr().out
        assert "does NOT unlock repository 'alpha' (1 key file(s) checked" in err
        assert "1 more could not be read" in err
        assert "0000-corrupt" in err

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
# KEY-09: password card (`lcsas key card`)
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
        # Card carries the repo name, password file name, hints, a date, and code.
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
