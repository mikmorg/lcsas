"""Boot-path quarantine tripwire (BOOT-08) + BOOT-06 record-keeping.

FAILURE MODE CAUGHT
-------------------
Nothing in this project has ever booted in any test tier, yet for a
long time every heir-facing "no working computer" route promised a
bootable disc that never existed (the BOOT-01 dead end).  The 2026-06
deep audit verdict was DROP: the boot scaffolding is quarantined in
experimental/boot/ and no automated boot gate exists.  These tripwires
keep that quarantine honest, permanently:

  1. No heir-facing document — static manuals or the docs generated
     onto burned discs — may carry a boot-the-disc instruction.
  2. No supported boot surface may reappear: no boot-ish CLI flag in
     the ``lcsas`` argparse tree, no ``bootable`` parameter on
     ``MetaVolumeBuilder`` (removed by BOOT-07).
  3. The quarantine record (experimental/boot/README.md) must keep
     its NOT-BOOTABLE banner, the defect index (BOOT-02/03/05/06),
     and the QEMU boot-smoke revival-precondition spec.  Booting may
     only be re-advertised once that gate exists and is green in CI.
  4. The operator readiness checklist must point at this automated
     gate, not at a manual boot drill against non-bootable discs.

Tests are static/builder-fixture only: no subprocess, no optical
hardware, no network.
"""
from __future__ import annotations

import argparse
import inspect
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "experimental" / "boot" / "README.md"
_DOCS = _REPO_ROOT / "recovery" / "docs"
_CHECKLIST = _DOCS / "READINESS_CHECKLIST.txt"

# A boot-the-disc promise.  Anchored on disc/medium, deliberately NOT
# on USB: BOOT.txt's live-USB instruction ("boot ... from USB") is the
# supported no-OS route and must stay allowed.
_BOOT_CLAIM = re.compile(
    r"(?i)boot\s+(directly\s+)?(from|the)\s+(the\s+)?(disc|recovery\s+medium)"
)

# Boot-ish identifier (flag or parameter).  "bootstrap" is restore
# vocabulary (metadata bootstrap phase), not a boot promise.
_BOOT_IDENT = re.compile(r"(?i)boot(?!strap)")


# ── 1. Heir-facing docs carry no boot-the-disc instruction ──────────


@pytest.mark.parametrize(
    "doc", ["RECOVER.txt", "RECOVER_WINDOWS.txt", "BOOT.txt"]
)
def test_static_heir_docs_carry_no_boot_the_disc_instruction(doc: str) -> None:
    text = (_DOCS / doc).read_text(encoding="utf-8")
    match = _BOOT_CLAIM.search(text)
    if match is not None:
        pytest.fail(
            f"recovery/docs/{doc} contains a boot-the-disc instruction "
            f"({match.group(0)!r}), but no LCSAS disc is bootable and no "
            "automated boot gate exists (BOOT-01/BOOT-08).  Booting may "
            "not be re-advertised until the QEMU boot-smoke gate "
            "specified in experimental/boot/README.md exists and is "
            "green in CI."
        )


@pytest.fixture(scope="module")
def generated_heir_docs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """Render every generated doc an heir reads off a burned disc.

    The artifact is the contract (UX-02): scan the rendered text, not
    the template source.  Uses a split-key config so the conditional
    share-card blocks render too.
    """
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.meta.builder import MetaVolumeBuilder
    from lcsas.staging.metadata import HolographicInjector

    tmp_path = tmp_path_factory.mktemp("boot-tripwire")
    config = LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
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
    meta = tmp_path / "meta"
    meta.mkdir()
    builder = MetaVolumeBuilder(meta, config=config)
    builder._write_readme()
    builder._write_readme_txt()
    builder._write_start_here()
    docs = {
        f"<meta>/{name}": (meta / name).read_text(encoding="utf-8")
        for name in (
            "README_RESTORE.md",
            "README_RESTORE.txt",
            "START_HERE.txt",
        )
    }
    docs["<meta-noconfig>/START_HERE.txt"] = builder._render_meta_start_here(
        None
    )
    data = tmp_path / "data-disc"
    data.mkdir()
    injector = HolographicInjector(data)
    injector.write_start_here(config)
    injector.write_restore_instructions()
    docs.update(
        {
            f"<data-disc>/{name}": (data / name).read_text(encoding="utf-8")
            for name in ("START_HERE.txt", "RESTORE_INSTRUCTIONS.txt")
        }
    )
    return docs


def test_generated_heir_docs_carry_no_boot_the_disc_instruction(
    generated_heir_docs: dict[str, str],
) -> None:
    offenders = {
        name: match.group(0)
        for name, text in generated_heir_docs.items()
        if (match := _BOOT_CLAIM.search(text)) is not None
    }
    assert not offenders, (
        f"Generated heir docs contain boot-the-disc instructions: "
        f"{offenders!r}.  No LCSAS disc is bootable; the no-OS route is "
        "the live-USB procedure (BOOT.txt).  Booting may not be "
        "re-advertised until the QEMU boot-smoke gate specified in "
        "experimental/boot/README.md exists and is green in CI."
    )


# ── 2. No supported boot surface ─────────────────────────────────────


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Collect every option string in the parser tree (recursing into
    subparsers)."""
    seen: set[str] = set()
    stack = [parser]
    while stack:
        current = stack.pop()
        for action in current._actions:
            seen.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return seen


def test_cli_parser_exposes_no_boot_flag() -> None:
    """The phantom ``--recovery-boot`` flag (documented for years,
    never implemented) — or any other boot-ish flag — must not enter
    the argparse tree while the boot path is quarantined."""
    from lcsas.cli.main import build_parser

    offenders = sorted(
        opt for opt in _all_option_strings(build_parser()) if _BOOT_IDENT.search(opt)
    )
    assert not offenders, (
        f"lcsas CLI exposes boot-related flag(s) {offenders!r}, but the "
        "boot path is quarantined (BOOT-01/BOOT-07).  Do not reintroduce "
        "a boot surface without the QEMU boot-smoke gate of "
        "experimental/boot/README.md green in CI."
    )


def test_meta_builder_has_no_bootable_parameter() -> None:
    """BOOT-07 removed ``MetaVolumeBuilder(bootable=...)``; it must not
    come back while the boot path is quarantined."""
    from lcsas.meta.builder import MetaVolumeBuilder

    params = inspect.signature(MetaVolumeBuilder.__init__).parameters
    offenders = sorted(name for name in params if _BOOT_IDENT.search(name))
    assert not offenders, (
        f"MetaVolumeBuilder.__init__ grew boot-related parameter(s) "
        f"{offenders!r} (removed by BOOT-07).  Do not reintroduce a boot "
        "surface without the QEMU boot-smoke gate of "
        "experimental/boot/README.md green in CI."
    )


# ── 3. Quarantine record intact ──────────────────────────────────────


def _readme_text() -> str:
    assert _README.is_file(), (
        f"missing {_README} — the quarantine record was deleted.  It "
        "carries the NOT-BOOTABLE banner, the BOOT-02/03/05/06 defect "
        "index, and the boot-smoke revival spec; restore it from git "
        "history rather than re-deriving any of that."
    )
    return _README.read_text(encoding="utf-8")


def test_quarantine_readme_keeps_banner_and_defect_index() -> None:
    text = _readme_text()
    assert "NOT BOOTABLE" in text, (
        "experimental/boot/README.md lost its 'NOT BOOTABLE' banner — "
        "the one line that stops a reader assuming this scaffolding ever "
        "produced a bootable disc."
    )
    for plan in ("BOOT-02", "BOOT-03", "BOOT-05", "BOOT-06"):
        assert plan in text, (
            f"experimental/boot/README.md defect index lost its {plan} "
            "row.  The index exists so a future revival does not "
            "re-discover known defects from scratch."
        )


def test_readme_records_optical_only_scan_defect() -> None:
    """BOOT-06 record-keeping: the sentinel-scan fix spec and its two
    revival-gated tests (C unit test + USB-attach boot-smoke leg)."""
    text = _readme_text()
    for marker in ("sentinel", "test_init_medium_scan.c", "usb-storage"):
        assert marker in text, (
            "experimental/boot/README.md no longer records the BOOT-06 "
            f"sentinel-scan fix spec (missing {marker!r})"
        )


def test_quarantine_readme_keeps_boot_smoke_revival_spec() -> None:
    """The revival precondition (BOOT-08) is content, asserted here so
    deleting the spec breaks the suite."""
    text = _readme_text()
    assert "## Revival precondition" in text, (
        "experimental/boot/README.md lost its revival-precondition "
        "section header (BOOT-08)."
    )
    for marker in (
        "boot-smoke",
        "qemu-system-x86_64",
        "OVMF",
        "SeaBIOS",
        "[lcsas-init] starting",
        "UPSTREAM.sha256",
        "weekly",
    ):
        assert marker in text, (
            "experimental/boot/README.md no longer specifies the QEMU "
            f"boot-smoke revival gate (missing {marker!r}, see BOOT-08)."
        )
    assert "no heir-facing doc may re-advertise booting" in text, (
        "experimental/boot/README.md lost the rule that no heir-facing "
        "doc may re-advertise booting until the boot-smoke gate exists "
        "and is green in CI — that rule is what this tripwire enforces."
    )


# ── 4. Readiness checklist points at the automated gate ─────────────


def test_readiness_checklist_points_at_automated_gate() -> None:
    text = _CHECKLIST.read_text(encoding="utf-8")
    assert "META DISC BOOT TEST" not in text, (
        "READINESS_CHECKLIST.txt re-grew the manual 'META DISC BOOT "
        "TEST' item.  LCSAS discs are not bootable; the boot drill "
        "applies only if experimental/boot/ is ever revived."
    )
    assert "test_boot_path_quarantined.py" in text, (
        "READINESS_CHECKLIST.txt no longer references the automated "
        "boot-path quarantine gate "
        "(tests/recovery_hardening/test_boot_path_quarantined.py)."
    )
    assert "NOT bootable" in text, (
        "READINESS_CHECKLIST.txt no longer states that LCSAS discs are "
        "NOT bootable — operators must not be left to assume a boot "
        "path exists."
    )
