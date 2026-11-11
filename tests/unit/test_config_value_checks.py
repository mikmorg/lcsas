"""Config values of the wrong type or impossible K/N are named errors (#682).

``lcsas config check`` used to pass K=5 of N=3, read ``key_split = "false"``
as true, and crash with a raw TypeError on a quoted number.  Each bad value
must now stop the load with a message that names the key, and no example
may reach main()'s "Unexpected error" (traceback) path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.config.settings import key_split_pair_error, load_config


def _write(tmp_path: Path, defaults: str = "", repos: str = "") -> Path:
    mirror = tmp_path / "mirror"
    (mirror / "family").mkdir(parents=True, exist_ok=True)
    (mirror / "work").mkdir(parents=True, exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        textwrap.dedent(f"""\
            [paths]
            mirror_base = "{mirror}"
            staging = "{staging}"
            database = "{tmp_path / 'archive.db'}"

            [defaults]
            media_type = "TEST_TINY"
            metadata_reserve_mb = 0
            """)
        + textwrap.dedent(defaults)
        + "\n"
        + (repos or textwrap.dedent(f"""\
            [repos.family]
            mirror_path = "{mirror / 'family'}"
            """))
    )
    return cfg


# (id, [defaults] lines, [repos] text or None, key the message must name)
_BAD_DEFAULTS = [
    ("k-greater-than-n", "key_threshold = 5\nkey_shares = 3\n", "key_threshold"),
    ("k-one-n-five", "key_threshold = 1\nkey_shares = 5\n", "key_threshold"),
    ("n-forty", "key_threshold = 2\nkey_shares = 40\n", "key_shares"),
    ("k-n-zero", "key_threshold = 0\nkey_shares = 0\n", "key_threshold"),
    ("key-split-string", 'key_split = "false"\n', "key_split"),
    ("threshold-quoted", 'key_threshold = "3"\n', "key_threshold"),
    ("shares-quoted", 'key_shares = "5"\n', "key_shares"),
    ("reserve-quoted", 'metadata_reserve_mb = "100"\n', "metadata_reserve_mb"),
    ("label-prefix-number", "label_prefix = 7\n", "label_prefix"),
]


def _replace_defaults(cfg: Path, lines: str) -> None:
    """Swap in *lines* for any [defaults] key they set."""
    text = cfg.read_text()
    for line in lines.strip().splitlines():
        key = line.split("=", 1)[0].strip()
        kept = [
            ln for ln in text.splitlines()
            if ln.split("=", 1)[0].strip() != key
        ]
        text = "\n".join(kept) + "\n"
    text = text.replace("[defaults]\n", "[defaults]\n" + lines, 1)
    cfg.write_text(text)


@pytest.mark.parametrize(
    ("defaults", "key"),
    [pytest.param(d, k, id=i) for i, d, k in _BAD_DEFAULTS],
)
def test_bad_defaults_value_fails_config_check_naming_the_key(
    tmp_path, capsys, defaults, key,
):
    cfg = _write(tmp_path)
    _replace_defaults(cfg, defaults)

    with pytest.raises(ValueError, match=key):
        load_config(cfg)

    capsys.readouterr()
    rc = main(["--config", str(cfg), "config", "check"])
    out = capsys.readouterr().out

    assert rc != 0
    assert key in out
    assert "Unexpected error" not in out  # no traceback path


def test_media_type_number_fails_naming_the_key(tmp_path, capsys):
    cfg = _write(tmp_path)
    cfg.write_text(cfg.read_text().replace('media_type = "TEST_TINY"', "media_type = 25"))

    capsys.readouterr()
    rc = main(["--config", str(cfg), "config", "check"])
    out = capsys.readouterr().out

    assert rc != 0
    assert "media_type" in out
    assert "Unexpected error" not in out


def test_duplicate_mirror_path_fails_naming_both_repositories(tmp_path, capsys):
    mirror = tmp_path / "mirror" / "family"
    cfg = _write(tmp_path, repos=textwrap.dedent(f"""\
        [repos.family]
        mirror_path = "{mirror}"

        [repos.photos]
        mirror_path = "{mirror}/"
        """))

    capsys.readouterr()
    rc = main(["--config", str(cfg), "config", "check"])
    out = capsys.readouterr().out

    assert rc != 0
    assert "[repos.photos] mirror_path" in out
    assert "[repos.family]" in out
    assert "Unexpected error" not in out


def test_repo_override_with_impossible_pair_names_the_repository(tmp_path):
    mirror = tmp_path / "mirror"
    cfg = _write(tmp_path, repos=textwrap.dedent(f"""\
        [repos.work]
        mirror_path = "{mirror / 'work'}"
        key_split = true
        key_shares = 1
        """))

    with pytest.raises(ValueError, match=r"\[repos\.work\] key_threshold/key_shares"):
        load_config(cfg)


def test_repo_key_split_string_is_named(tmp_path):
    mirror = tmp_path / "mirror"
    cfg = _write(tmp_path, repos=textwrap.dedent(f"""\
        [repos.work]
        mirror_path = "{mirror / 'work'}"
        key_split = "false"
        """))

    with pytest.raises(ValueError, match=r"\[repos\.work\] key_split"):
        load_config(cfg)


@pytest.mark.parametrize(("k", "n"), [(1, 1), (2, 2), (2, 5), (3, 5), (16, 16)])
def test_usable_pairs_pass(k, n):
    assert key_split_pair_error(k, n) is None


@pytest.mark.parametrize(("k", "n"), [(5, 3), (1, 5), (2, 40), (0, 0), (2, 17)])
def test_impossible_pairs_fail(k, n):
    assert key_split_pair_error(k, n) is not None


def test_valid_split_config_still_loads(tmp_path):
    cfg = _write(
        tmp_path,
        defaults="key_split = true\nkey_threshold = 2\nkey_shares = 3\n",
    )
    config = load_config(cfg)
    assert config.repo_split("family") == (True, 2, 3)
    assert config.metadata_reserve_bytes == 0
