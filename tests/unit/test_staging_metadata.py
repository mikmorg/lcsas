"""Split-key heir docs must name the C combiner first [KEY-05].

Phase 5 shipped a tier-1-grade static combiner (``lcsas-keyshare``)
on all six targets precisely so split-key reconstruction needs no
Python — but the rendered heir docs (START_HERE.txt, PASSWORD_INFO.txt)
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
    """Render START_HERE.txt + PASSWORD_INFO.txt for the given key mode."""
    root = tmp_path / ("split" if key_split else "single")
    root.mkdir()
    injector = HolographicInjector(root)
    config = _config(tmp_path, key_split=key_split)
    injector.write_start_here(config)
    injector.write_key_info(config)
    return {
        name: (root / name).read_text(encoding="utf-8")
        for name in ("START_HERE.txt", "PASSWORD_INFO.txt")
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


def test_kn_comes_from_recorded_split_not_config(tmp_path: Path) -> None:
    """KEY-08: when an escrow_override is passed, the rendered K/N comes from
    the recorded split, NOT the config.  Config says 2-of-5; the recorded
    split is 3-of-5; the disc text must say 'any 3' of 5."""
    root = tmp_path / "drift"
    root.mkdir()
    injector = HolographicInjector(root)
    config = _config(tmp_path, key_split=True)  # config = 2-of-5
    injector.write_start_here(config, escrow_override=(3, 5))
    injector.write_key_info(config, escrow_override=(3, 5))
    for name in ("START_HERE.txt", "PASSWORD_INFO.txt"):
        text = (root / name).read_text(encoding="utf-8")
        assert "any 3" in text.lower(), (
            f"{name}: rendered K must come from the record (3), not config (2)"
        )
        assert "any 2" not in text.lower(), (
            f"{name}: the config K (2) leaked into the heir-facing text"
        )


def _render_mixed(tmp_path: Path) -> dict[str, str]:
    """A mixed archive (#587): one split repository, one not."""
    root = tmp_path / "mixed"
    root.mkdir()
    config = _config(tmp_path, key_split=False)
    config.repositories["photos"] = RepositoryConfig(
        name="photos",
        mirror_path=tmp_path / "mirror" / "photos",
        password_file=Path("/keys/photos.key"),
        key_split=True,
        key_threshold=2,
        key_shares=3,
    )
    injector = HolographicInjector(root)
    injector.write_start_here(config)
    injector.write_key_info(config)
    return {
        name: (root / name).read_text(encoding="utf-8")
        for name in ("START_HERE.txt", "PASSWORD_INFO.txt")
    }


def test_split_blocks_name_the_disc_folders_and_the_card_prompt(
    tmp_path: Path,
) -> None:
    """#605/#607: START_HERE.txt is the file the heir reads first, and its
    split block kept the source-tree folder names (x86_64, aarch64,
    x86_64-windows), which the META disc does not have: it stores the
    binaries under the rust triples.  It also said the password must
    FIRST be rebuilt and typed at the Password: prompt, while an rc6
    script asks for the cards (P lets a rebuilt password in).  Only the
    PASSWORD_INFO.txt copy had been fixed."""
    renders = {
        **{f"uniform/{k}": v for k, v in _render(tmp_path, key_split=True).items()},
        **{f"mixed/{k}": v for k, v in _render_mixed(tmp_path).items()},
    }
    for name, text in renders.items():
        for triple in ("x86_64-unknown-linux-musl", "x86_64-pc-windows-gnu"):
            assert triple in text, f"{name}: does not name the {triple} folder"
        assert "x86_64 for" not in text, f"{name}: source-tree folder name"
        assert "x86_64-windows" not in text, f"{name}: source-tree folder name"
        assert "must FIRST rebuild" not in text, (
            f"{name}: a newer script asks for the cards itself"
        )
        assert "type P" in text, f"{name}: no word on P for a rebuilt password"
        assert "/mnt/recovery/bin/<machine>/lcsas-keyshare" in text, (
            f"{name}: the combiner must be given by its full path"
        )


def test_restore_instructions_follow_the_rc6_journey(tmp_path: Path) -> None:
    """#605/#609: RESTORE_INSTRUCTIONS.txt on every data disc still said
    to repeat swaps until "RESTORE COMPLETE", which nothing on the rc6
    path prints, and knew neither the newest-disc step, the tools folder,
    the share cards nor resume.sh."""
    root = tmp_path / "ri"
    root.mkdir()
    HolographicInjector(root).write_restore_instructions()
    text = (root / "RESTORE_INSTRUCTIONS.txt").read_text(encoding="utf-8")
    assert "RESTORE COMPLETE" not in text
    assert "[lcsas-restore] restore complete" in text
    assert "NEWEST data disc" in text
    assert "share cards" in text
    assert "~/lcsas-restore-tools/resume.sh" in text


def test_lcsas_source_on_disc_has_no_bytecode(tmp_path: Path, monkeypatch) -> None:
    """#815: the LCSAS source copied to a data disc must leave out
    __pycache__, .pyc and .pyo.  A .pyc file records the full path of
    the machine that compiled it (a user name, a folder layout)."""
    import lcsas.staging.metadata as metadata_mod

    pkg = tmp_path / "pkg" / "lcsas"
    for sub in ("restore", "utils", "db"):
        (pkg / sub / "__pycache__").mkdir(parents=True)
        (pkg / sub / "__init__.py").write_text("")
        (pkg / sub / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(
            b"/home/someone/src/lcsas"
        )
        (pkg / sub / "stale.pyc").write_bytes(b"pyc")
        (pkg / sub / "old.pyo").write_bytes(b"pyo")
    (pkg / "staging").mkdir()
    # write_lcsas_source finds the package from this module's own path.
    monkeypatch.setattr(
        metadata_mod, "__file__", str(pkg / "staging" / "metadata.py")
    )

    root = tmp_path / "disc"
    root.mkdir()
    HolographicInjector(root).write_lcsas_source()

    src = root / "lcsas_src"
    for sub in ("restore", "utils", "db"):
        assert (src / sub / "__init__.py").is_file(), sub
    bytecode = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*")
        if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")
    ]
    assert bytecode == []


def _share_section(text: str, heading: str) -> list[str]:
    """The share-card section under *heading*, dedented, heading dropped."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    indent = len(lines[start]) - len(lines[start].lstrip())
    body: list[str] = []
    for line in lines[start + 2:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break  # the next section's heading
        body.append(line[indent:])
    while body and not body[-1]:
        body.pop()
    return body


def test_share_card_steps_have_one_source(tmp_path: Path) -> None:
    """#819: START_HERE.txt and PASSWORD_INFO.txt wrote the share-card
    steps twice, with different wording.  Under their own headings both
    now carry the same text, for an archive split alike and a mixed one.
    The saved-password advice keeps the full path, which works with
    restore.sh on master and with #849, and drops the claim that a short
    name "is not found" (#849 makes restore.sh resolve it)."""
    cases = {
        "uniform": (
            _render(tmp_path, key_split=True),
            "YOUR PASSWORD IS SPLIT INTO 5 SHARE CARDS",
            "SPLIT PASSWORD — YOUR PASSWORD IS IN SHARE CARDS",
        ),
        "mixed": (
            _render_mixed(tmp_path),
            "SOME PASSWORDS ARE SPLIT INTO SHARE CARDS",
            "SPLIT PASSWORDS — SOME PASSWORDS ARE IN SHARE CARDS",
        ),
    }
    for kind, (texts, start_heading, info_heading) in cases.items():
        start = _share_section(texts["START_HERE.txt"], start_heading)
        info = _share_section(texts["PASSWORD_INFO.txt"], info_heading)
        assert start == info, kind
        joined = "\n".join(info)
        assert "sh /mnt/restore.sh --key ~/repo.key --target ~/restored" in joined
        assert "not found" not in joined, kind
