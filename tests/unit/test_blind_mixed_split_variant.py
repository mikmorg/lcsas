"""#587: the mixed-split blind variant's fixture.

One archive: alpha's password split 2-of-3 in its own config table, bravo
on an ordinary password.  Pins the setup.py pieces that make the discs
carry the per-repository split text: the fixture config, the recorded
split (so the burn's drift guard passes), the agent's 2 of 3 cards, and
the wiring in run_variant.sh / run.sh.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.key_escrow import detect_escrow_drift, get_split_for_repo
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.keyshare import (
    combine_mnemonics,
    decode_master_secret,
    extract_mnemonic,
    share_identifier,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"

_spec = importlib.util.spec_from_file_location("blind_setup", HARNESS / "setup.py")
assert _spec is not None and _spec.loader is not None
_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_setup)

_SECRET = b"correct horse battery staple"


def test_fixture_config_splits_only_alpha(monkeypatch) -> None:
    monkeypatch.setenv("LCSAS_VARIANT", "mixed-split")
    config = _setup._fixture_config()
    assert config.repo_split("alpha") == (True, 2, 3)
    assert config.repo_split("bravo")[0] is False
    assert config.key_split is False  # nothing archive-wide


def test_other_variants_split_nothing(monkeypatch) -> None:
    monkeypatch.setenv("LCSAS_VARIANT", "split-key-docs")
    config = _setup._fixture_config()
    assert not any(config.repo_split(n)[0] for n in config.repositories)


def test_recorded_split_matches_cards_and_passes_drift(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("LCSAS_VARIANT", "mixed-split")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "alpha.pw").write_bytes(_SECRET + b"\n")
    monkeypatch.setattr(_setup, "SECRETS", secrets)
    monkeypatch.setattr(_setup, "MIXED_SPLIT_CARDS", secrets / "cards")
    conn = get_memory_connection()
    create_all(conn)
    for name in ("alpha", "bravo"):
        register_repo(conn, name, name, f"/m/{name}")

    _setup._record_mixed_split(conn)

    cards = sorted((secrets / "cards").glob("alpha-share-*-card.txt"))
    assert len(cards) == 2  # the heir's quorum of the 3 printed
    mnemonics = [extract_mnemonic(c.read_text()) for c in cards]
    assert decode_master_secret(combine_mnemonics(mnemonics)) == _SECRET
    record = get_split_for_repo(conn, "alpha")
    assert record is not None
    assert (record.threshold, record.shares) == (2, 3)
    assert record.slip39_id == share_identifier(mnemonics[0])
    assert get_split_for_repo(conn, "bravo") is None

    config = _setup._fixture_config()
    assert detect_escrow_drift(
        conn, ["alpha", "bravo"], config_split=False, config_threshold=2,
        config_shares=5, split_for=config.repo_split,
    ) is None


@pytest.mark.parametrize("script, needle", [
    ("run_variant.sh", "mixed-split) : ;;"),
    ("run.sh", '[ "${LCSAS_VARIANT:-}" = "mixed-split" ]'),
])
def test_variant_is_wired(script: str, needle: str) -> None:
    assert needle in (HARNESS / script).read_text(encoding="utf-8")
