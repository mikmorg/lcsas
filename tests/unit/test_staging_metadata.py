"""Split-key heir docs must name the C combiner first [KEY-05].

Phase 5 shipped a tier-1-grade static combiner (``lcsas-keyshare``)
on all six targets precisely so split-key reconstruction needs no
Python — but the rendered heir docs (START_HERE.txt, KEY_INFO.txt)
named only ``python3 keyshare_combine.py``.  If python3 won't run —
the very scenario the tier-1 design exists for — the heir had no
documented fallback.  These tests pin the tier ordering: the static
binary is the primary documented path; python3 appears only after it,
as the fallback.  Single-key archives must render zero share
instructions.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.staging.metadata import HolographicInjector


def _config(tmp_path: Path, *, key_split: bool) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        key_split=key_split,
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


def _render(tmp_path: Path, *, key_split: bool) -> dict[str, str]:
    """Render START_HERE.txt + KEY_INFO.txt for the given key mode."""
    root = tmp_path / ("split" if key_split else "single")
    root.mkdir()
    injector = HolographicInjector(root)
    config = _config(tmp_path, key_split=key_split)
    injector.write_start_here(config)
    injector.write_key_info(config)
    return {
        name: (root / name).read_text(encoding="utf-8")
        for name in ("START_HERE.txt", "KEY_INFO.txt")
    }


def test_split_block_names_c_combiner(tmp_path: Path) -> None:
    """key_split=True renders must present lcsas-keyshare (with its
    recovery/bin on-disc path) BEFORE any python3 mention."""
    for name, text in _render(tmp_path, key_split=True).items():
        assert "recovery/bin" in text, (
            f"{name}: missing the recovery/bin on-disc combiner path"
        )
        c_idx = text.find("lcsas-keyshare")
        assert c_idx != -1, (
            f"{name}: the tier-1 static combiner lcsas-keyshare is not named"
        )
        py_idx = text.find("python3")
        assert py_idx != -1, f"{name}: the python3 fallback is missing"
        assert c_idx < py_idx, (
            f"{name}: python3 appears before lcsas-keyshare — the static C "
            "combiner must be the primary documented path (python3 is the "
            "fallback for when the binary will not run)"
        )


def test_split_block_offers_bundled_python_fallback(tmp_path: Path) -> None:
    """A host with no python3 at all must still have a documented path
    to the combiner pre-step: the per-target CPython bundled on the
    META disc (UX-02).  Chain: lcsas-keyshare → python3 → bundled
    recovery/bin/<platform>/python/bin/python3."""
    fallback = "recovery/bin/<platform>/python/bin/python3"
    for name, text in _render(tmp_path, key_split=True).items():
        idx = text.find(fallback)
        assert idx != -1, (
            f"{name}: no bundled-python fallback for the combiner pre-step"
        )
        assert "keyshare_combine.py" in text[idx : idx + 200], (
            f"{name}: the bundled-python fallback must run keyshare_combine.py"
        )


def test_single_key_render_has_no_share_instructions(tmp_path: Path) -> None:
    """Single-key archives must not mention any combiner at all."""
    for name, text in _render(tmp_path, key_split=False).items():
        assert "lcsas-keyshare" not in text, (
            f"{name}: share-combiner instructions leaked into a "
            "single-key render"
        )
        assert "keyshare_combine" not in text, (
            f"{name}: share-combiner instructions leaked into a "
            "single-key render"
        )
        assert "SHARE CARDS" not in text, (
            f"{name}: split-key block leaked into a single-key render"
        )
