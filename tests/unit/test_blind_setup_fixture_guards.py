"""The blind fixture is checked before any agent runs (#829, #836, #828).

* #829: a failed fixture burn used to print "burned 0 data ISOs" and go on;
  setup now stops at the burn with its message, and a burn must make as
  many discs as the bin-packer planned.
* #836: verify.sh's thrash check allows ``needed + 1`` distinct data discs.
  On every rc9 layout it was either impossible to fail or failed a correct
  restore.  setup.py now burns alpha's discs first and refuses such a
  layout before the agent starts.  The layouts below are the rc9 ones.
* #828: split-key-docs is now set up as a split archive is (config split,
  recorded split, config-built meta disc), so its discs carry the
  PASSWORD_INFO.txt its prompt names.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from lcsas.db.connection import get_connection
from lcsas.db.key_escrow import detect_escrow_drift, get_split_for_repo
from lcsas.db.packs import register_pack
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

_IDS = {
    "alpha": "6f1c0b9e-2d47-4a3e-9b85-0c7e4d2a1f36",
    "bravo": "d83a5e21-7c90-4f1b-a6e4-35b9c0d7e812",
}


def _labels(*numbers: int) -> list[str]:
    return [f"LCSAS_TEST_TINY_2026_{n:04d}" for n in numbers]


# --- #836: the thrash-check layout ---------------------------------------------


@pytest.mark.parametrize("alpha, total, tenants, verdict", [
    # rc9 5-tenant, failed 15/16 twice: a correct in-order sweep
    # inserts all 16 discs, 11 allowed.
    ((*range(1, 10), 16), 16, 5, "more than the 11 verify.sh allows"),
    # rc9 5-tenant, passed: 11 discs, 11 allowed, so it can never fail.
    ((*range(1, 10), 11), 11, 5, "can never fail"),
    # rc9 2-tenant variants: 6 discs (or 5), alpha on 1-5.
    (tuple(range(1, 6)), 6, 2, "can never fail"),
    (tuple(range(1, 6)), 5, 2, "can never fail"),
    # Alpha on one disc: no disc swap to test.
    ((1,), 4, 2, "alpha fits on one disc"),
    # The layout setup.py now builds: alpha first, two or more discs after.
    (tuple(range(1, 6)), 7, 2, None),
    (tuple(range(1, 7)), 9, 5, None),
    # A single-tenant archive: every disc is alpha's; allowed.
    (tuple(range(1, 5)), 4, 1, None),
])
def test_thrash_layout(alpha, total, tenants, verdict) -> None:
    problem = _setup._thrash_layout_problem(
        _labels(*alpha), _labels(*range(1, total + 1)), tenants,
    )
    if verdict is None:
        assert problem is None, problem
    else:
        assert problem is not None and verdict in problem, problem


def test_alpha_last_disc_is_the_newest_counts_once() -> None:
    # Alpha on 1-4 and 6 of 6 (the newest): the sweep inserts 6 discs,
    # 6 allowed, and 6 data discs: toothless, not unfair.
    problem = _setup._thrash_layout_problem(
        _labels(1, 2, 3, 4, 6), _labels(*range(1, 7)), 2,
    )
    assert problem is not None and "can never fail" in problem


def test_check_thrash_layout_stops_setup(monkeypatch) -> None:
    monkeypatch.setenv("LCSAS_TENANT_COUNT", "5")
    isos = [Path(f"/x/{label}.iso") for label in _labels(*range(1, 17))]
    with pytest.raises(SystemExit, match="before any agent runs"):
        _setup._check_thrash_layout(_labels(*range(1, 10), 16), isos)


def test_alpha_is_burned_first_and_on_its_own() -> None:
    """The layout the check needs comes from the burn order: alpha's own
    session, then the meta disc, then the other tenants."""
    src = (HARNESS / "setup.py").read_text()
    body = src[src.index("def _burn_fixture"):src.index("def main")]
    alpha = body.index('_run_burn_pipeline(conn, repo="alpha")')
    meta = body.index("_build_meta_iso()")
    rest = body.index("_run_burn_pipeline(conn)")
    checks = body.index("_check_thrash_layout(")
    assert alpha < meta < rest < checks


def test_other_tenants_fill_more_than_one_disc(monkeypatch) -> None:
    """Sizing only; the real disc count is checked by the setup itself.

    Since #811 the fixture sets no reserve: stage measures the disc
    metadata.  ``least`` is a lower bound of that measurement (the ISO's
    fixed part, the tier-3 reader and an empty catalog), so ``room`` is
    at least the pack space of a real disc.
    """
    import sqlite3

    from lcsas.config.media import MediaType
    from lcsas.db.schema import create_all
    from lcsas.restore.standalone_builder import build_standalone
    from lcsas.staging.metadata import (
        ISO_FIXED_BYTES,
        iso_file_bytes,
        live_catalog_bytes,
    )

    empty = sqlite3.connect(":memory:")
    create_all(empty)
    least = (ISO_FIXED_BYTES + iso_file_bytes(len(build_standalone().encode()))
             + iso_file_bytes(live_catalog_bytes(empty)))
    empty.close()
    monkeypatch.delenv("LCSAS_VARIANT", raising=False)
    for tenants in (2, 5):
        monkeypatch.setenv("LCSAS_TENANT_COUNT", str(tenants))
        reserve = max(_setup._fixture_config().metadata_reserve_bytes, least)
        room = MediaType.TEST_TINY.usable_bytes - reserve
        per_file = _setup._secondary_file_bytes(tenants)
        total = per_file * _setup.SECONDARY_FILES * (tenants - 1)
        assert total > room, (tenants, total, room)


# --- #829: a failed or short burn stops the setup -------------------------------


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LCSAS_VARIANT", raising=False)
    monkeypatch.setenv("LCSAS_TENANT_COUNT", "2")
    for name in ("MIRROR", "STAGING", "SECRETS", "ISO_OUT"):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setattr(_setup, name, path)
    monkeypatch.setattr(_setup, "DB_PATH", tmp_path / "catalog.db")
    conn = get_connection(tmp_path / "catalog.db")
    create_all(conn)
    for name, repo_id in _IDS.items():
        register_repo(conn, repo_id, name, str(tmp_path / "mirror" / name))
    for i in range(8):
        register_pack(conn, f"{i:064x}", 300_000, _IDS["alpha"])
    for i in range(8, 10):
        register_pack(conn, f"{i:064x}", 300_000, _IDS["bravo"])
    conn.commit()
    yield conn
    conn.close()


def test_failed_burn_stops_setup_with_its_message(catalog, monkeypatch) -> None:
    def fail(*_a, **_k):
        raise ValueError(
            "ISO LCSAS_TEST_TINY_2026_0001.iso is 2,101,248 bytes, exceeds "
            "TEST_TINY capacity of 2,097,152 bytes"
        )

    monkeypatch.setattr(_setup, "burn_to_isos", fail)
    with pytest.raises(SystemExit) as info:
        _setup._run_burn_pipeline(catalog, repo="alpha")
    message = str(info.value)
    assert "exceeds TEST_TINY capacity" in message
    assert "alpha's packs" in message
    assert "before any agent runs" in message


def test_burn_must_make_the_planned_discs(catalog, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def short(_orch, iso_out, *, media_type, pack_sha256s):
        seen["packs"] = list(pack_sha256s)
        iso = iso_out / "LCSAS_TEST_TINY_2026_0001.iso"
        iso.write_bytes(b"x")
        return [iso]

    monkeypatch.setattr(_setup, "burn_to_isos", short)
    with pytest.raises(SystemExit, match=r"made 1 data ISOs, but the "
                                         r"bin-packer planned [2-9]"):
        _setup._run_burn_pipeline(catalog, repo="alpha")
    # Only alpha's packs went into alpha's session.
    assert seen["packs"] == [f"{i:064x}" for i in range(8)]


def test_burned_fixture_check_refuses_leftover_packs(catalog) -> None:
    with pytest.raises(SystemExit, match="on no disc"):
        _setup._check_burned_fixture(catalog, [])


def test_burned_fixture_check_refuses_no_isos(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "empty.db")
    create_all(conn)
    try:
        with pytest.raises(SystemExit, match="do not match"):
            _setup._check_burned_fixture(conn, [])
    finally:
        conn.close()


def test_failed_burn_is_never_swallowed() -> None:
    src = (HARNESS / "setup.py").read_text()
    body = src[src.index("def _run_burn_pipeline"):src.index("def _check_burned_fixture")]
    assert "return []\n" not in body.split("burn_to_isos(", 1)[1], (
        "a burn failure must stop the setup, not return no ISOs"
    )
    assert re.search(r"except Exception as exc:\s+raise SystemExit", body)


# --- #828: split-key-docs is a real split archive --------------------------------

_SECRET = {"alpha": b"correct horse battery staple",
           "bravo": b"another long and random password"}


def test_split_key_docs_config_splits_every_tenant(monkeypatch) -> None:
    monkeypatch.setenv("LCSAS_VARIANT", "split-key-docs")
    monkeypatch.setenv("LCSAS_TENANT_COUNT", "2")
    config = _setup._fixture_config()
    assert config.key_split is True
    for name in ("alpha", "bravo"):
        assert config.repo_split(name) == (True, 2, 5)


def test_split_key_docs_records_the_cards_it_hands_out(
    catalog, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("LCSAS_VARIANT", "split-key-docs")
    for name, secret in _SECRET.items():
        (_setup.SECRETS / f"{name}.pw").write_bytes(secret + b"\n")
    monkeypatch.setattr(_setup, "SPLIT_CARDS", tmp_path / "cards")

    _setup._record_splits(catalog)

    for name, secret in _SECRET.items():
        cards = sorted((tmp_path / "cards").glob(f"{name}-share-*-card.txt"))
        assert len(cards) == 2  # the quorum of the 5 printed
        mnemonics = [extract_mnemonic(c.read_text()) for c in cards]
        assert decode_master_secret(combine_mnemonics(mnemonics)) == secret
        record = get_split_for_repo(catalog, _IDS[name])
        assert record is not None
        assert (record.threshold, record.shares) == (2, 5)
        assert record.slip39_id == share_identifier(mnemonics[0])

    config = _setup._fixture_config()
    assert detect_escrow_drift(
        catalog, list(_IDS.values()), config_split=config.key_split,
        config_threshold=config.key_threshold,
        config_shares=config.key_shares, split_for=config.repo_split,
    ) is None


def test_split_variants_build_the_meta_disc_from_the_config() -> None:
    src = (HARNESS / "setup.py").read_text()
    body = src[src.index("def _build_meta_iso"):src.index("def _apply_variant_mutations")]
    assert 'in SPLIT_VARIANTS' in body
    assert "config=_fixture_config() if split else None" in body
    assert 'PASSWORD_INFO.txt' in body
    assert set(_setup.SPLIT_VARIANTS) == {"mixed-split", "split-key-docs"}


def test_split_key_docs_prompt_names_password_info() -> None:
    """The prompt sends the agent to PASSWORD_INFO.txt, which the split
    variants' config-built meta disc now carries."""
    prompt = (HARNESS / "agent_prompt_split_docs.txt").read_text()
    assert "PASSWORD_INFO" in prompt
