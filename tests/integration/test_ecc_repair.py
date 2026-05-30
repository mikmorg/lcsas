"""RS03 ECC repair: real dvdisaster augment → damage → repair → recover.

Issue #302. Every existing dvdisaster test is mocked, and the corrupt-disc
failover test stubs the ECC layer entirely (``_NoOpDVDisaster``). This is the
only test that exercises the **real** RS03 error-correction layer end to end:
it masters an ISO, augments it with real dvdisaster RS03 ECC, overwrites data
sectors to simulate bit-rot, repairs the image from the embedded ECC, and
asserts the ISO's file content is recovered byte-for-byte.

This is the layer that defends against disc bit-rot — and it sits *below* the
tier-1 binary's Poly1305/SHA-256 integrity gates (which reject corruption
rather than heal it; see #301). So this is the genuine bit-rot-recovery path,
and until now nothing exercised it against the real binary.

SLOW + opt-in. RS03 augmented-image mode pads a small image up to a full
optical medium (≈700 MB here), so each dvdisaster pass takes minutes. The test
is gated behind ``LCSAS_ECC_REPAIR=1`` so it never runs in the default suite::

    LCSAS_ECC_REPAIR=1 pytest tests/integration/test_ecc_repair.py -v -m integration

Validated manually 2026-05-29: augment(15%) → overwrite 20 data sectors →
repair → extract recovered all 20 files byte-identical.
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
from pathlib import Path

import pytest

from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_xorriso,
    pytest.mark.requires_dvdisaster,
    pytest.mark.skipif(
        not os.environ.get("LCSAS_ECC_REPAIR"),
        reason="set LCSAS_ECC_REPAIR=1 to run the slow RS03 ECC repair test "
        "(augments a real ISO; multi-minute dvdisaster passes)",
    ),
]

SECTOR = 2048
RNG_SEED = 20260529
NUM_FILES = 20
# Matches the manually-validated config. At this scale RS03 pads the image up
# to a full medium, so the exact percentage barely affects the layout — but we
# pass it through the production wrapper to exercise the real augment path.
REDUNDANCY_PCT = 15
# Damage well within ECC capacity: 20 sectors out of a multi-hundred-sector
# data region, far below the configured redundancy.
DAMAGE_SECTORS = 20
DAMAGE_START_SECTOR = 100


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_iso(src: Path, iso: Path, label: str = "ECCTEST") -> None:
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-r", "-J", "-iso-level", "3",
         "-V", label, "-o", str(iso), str(src)],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _extract(iso: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["xorriso", "-indev", str(iso), "-osirrox", "on",
         "-extract", "/", str(dest)],
        check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _damage_sectors(iso: Path, n_sectors: int, start_sector: int) -> None:
    """Overwrite ``n_sectors`` 2 KiB sectors with 0xFF to simulate bit-rot.

    The sectors remain *readable* (this is a file image, not failing media),
    so the only corruption signal is dvdisaster's per-sector CRC layer — which
    is exactly what RS03 repair must detect and correct.
    """
    buf = bytearray(iso.read_bytes())
    for s in range(start_sector, start_sector + n_sectors):
        off = s * SECTOR
        if off + SECTOR <= len(buf):
            buf[off:off + SECTOR] = b"\xff" * SECTOR
    iso.write_bytes(buf)


def test_rs03_repairs_bitrot_byte_identical(tmp_path: Path) -> None:
    runner = SubprocessDVDisasterRunner()

    # 1. Source data + manifest of expected hashes.
    src = tmp_path / "src"
    src.mkdir()
    rng = random.Random(RNG_SEED)
    manifest: dict[str, str] = {}
    for i in range(NUM_FILES):
        data = rng.randbytes(rng.randint(100_000, 200_000))
        name = f"file_{i:03d}.bin"
        (src / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()

    # 2. Master an ISO and augment it with real RS03 ECC (production wrapper).
    iso = tmp_path / "vol.iso"
    _make_iso(src, iso)
    base_size = iso.stat().st_size
    runner.augment_iso(iso, redundancy_pct=REDUNDANCY_PCT)
    assert iso.stat().st_size > base_size, "augment must grow the image with ECC"

    # 3. A freshly augmented image verifies clean.
    assert runner.verify_iso(iso) is True, "augmented image should verify clean"

    # 4. Simulate bit-rot inside the data region; verify must detect it.
    _damage_sectors(iso, DAMAGE_SECTORS, DAMAGE_START_SECTOR)
    assert runner.verify_iso(iso) is False, "verify must detect the damage"

    # 5. Repair from the embedded RS03 ECC. repair_iso() confirms the outcome
    #    by re-verifying the image rather than trusting dvdisaster's `-f` exit
    #    code (which is nonzero even on a successful corrective repair), so it
    #    returns True iff the image is actually intact again. See #305.
    assert runner.repair_iso(iso) is True, "repair_iso must report success"

    # 6. Ground truth: every file extracts byte-for-byte identical.
    out = tmp_path / "extracted"
    _extract(iso, out)
    recovered = {p.name: _sha(p) for p in out.iterdir() if p.is_file()}
    for name, expected in manifest.items():
        assert name in recovered, f"missing after repair: {name}"
        assert recovered[name] == expected, (
            f"{name}: content not recovered byte-identical after RS03 repair"
        )


def test_rs03_fails_loud_when_damage_exceeds_capacity(tmp_path: Path) -> None:
    """Damage ABOVE the ECC correction capacity must fail loud — #302.

    The companion test proves recoverable damage round-trips byte-identical.
    This proves the other half of the contract: when damage exceeds what RS03
    can correct, repair_iso() must report **failure** (return False), so the
    recovery path (restore/executor.py:82) aborts / falls back to another disc
    copy rather than silently proceeding with un-repaired, corrupt data.

    It also real-binary-validates the unrecoverable->False branch of the #305
    fix (repair_iso confirms via re-verify): the unit tests mock that branch;
    here a real dvdisaster `-f` runs against a genuinely unrecoverable image.
    """
    runner = SubprocessDVDisasterRunner()

    src = tmp_path / "src"
    src.mkdir()
    rng = random.Random(RNG_SEED + 1)
    for i in range(NUM_FILES):
        (src / f"file_{i:03d}.bin").write_bytes(
            rng.randbytes(rng.randint(100_000, 200_000))
        )

    iso = tmp_path / "vol.iso"
    _make_iso(src, iso)
    runner.augment_iso(iso, redundancy_pct=REDUNDANCY_PCT)

    # Obliterate the LAST 80% of the medium's sectors — far beyond any RS03
    # redundancy. (RS03 pads a small image up to a full medium, so the
    # configured % yields a high absolute redundancy; we damage a large
    # fraction to exceed it unambiguously rather than tune to the exact
    # correction threshold.)
    #
    # Damage the TAIL, preserving the first 20% (the ISO header + the leading
    # RS03 ECC headers), so dvdisaster still RECOGNIZES the image as
    # ECC-augmented and attempts — then fails — correction. Wiping from
    # sector 0 instead destroys the ECC signature, after which dvdisaster
    # reports "no ECC / clean" and the damage goes UNDETECTED (verify would
    # wrongly pass). Validated empirically: tail-80% → verify False, repair
    # False; sector-0-80% → verify True (signature gone).
    total_sectors = iso.stat().st_size // SECTOR
    keep = int(total_sectors * 0.2)
    _damage_sectors(iso, n_sectors=total_sectors - keep, start_sector=keep)

    # Detection still works...
    assert runner.verify_iso(iso) is False, "verify must detect the damage"

    # ...but repair must REPORT FAILURE rather than silently claim success.
    # This is the anti-silent-restore contract: a False here is what makes the
    # executor log "ECC repair failed" and stop trusting this disc.
    assert runner.repair_iso(iso) is False, (
        "repair_iso must report failure when damage exceeds ECC capacity "
        "(no silent partial restore)"
    )
