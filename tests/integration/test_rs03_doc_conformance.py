"""RS03 doc conformance (FMT-02): the spec must match the real binary.

`docs/DVDISASTER_RS03_FORMAT.md` §3.2 documents the byte-exact
`EccHeader` layout — the load-bearing claim that a future engineer can
parse an RS03 header from the spec alone.  This test proves that claim
against the real dvdisaster binary: it masters an ISO, augments it with
real RS03 ECC, parses the header using ONLY the cookie + offsets read
out of the spec's own table, and asserts the parsed
`dataBytes`/`eccBytes`/`sectors`/`sectorsPerLayer` and the §4.1 derived
positions agree with `dvdisaster -t -v` output.

If the doc and the binary ever diverge (a future dvdisaster bumps the
struct, or someone edits the table wrong), this fails — the spec can
never silently rot into being un-re-implementable.

SLOW + opt-in, like `test_ecc_repair.py`: RS03 augmented mode pads a
small image up to a full optical medium, so the augment pass takes
minutes.  Gated behind ``LCSAS_ECC_REPAIR=1``::

    LCSAS_ECC_REPAIR=1 pytest tests/integration/test_rs03_doc_conformance.py -v -m integration
"""

from __future__ import annotations

import os
import re
import struct
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
        reason="set LCSAS_ECC_REPAIR=1 to run the slow RS03 doc-conformance "
        "test (augments a real ISO; multi-minute dvdisaster pass)",
    ),
]

SECTOR = 2048
COOKIE = b"*dvdisaster*"
REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "DVDISASTER_RS03_FORMAT.md"


def _spec_offsets() -> dict[str, tuple[int, int]]:
    """Parse the §3.2 EccHeader table from the spec → {field: (offset, size)}.

    Reading the offsets out of the doc (rather than hard-coding them in
    the test) is what makes this a *conformance* test: edit the doc wrong
    and the parse below mismatches the binary.
    """
    text = SPEC.read_text(encoding="utf-8")
    offsets: dict[str, tuple[int, int]] = {}
    # Rows look like: | 76 | 4 | dataBytes | `gint32` | ... |
    row = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([A-Za-z]\w*)\s*\|")
    for line in text.splitlines():
        m = row.match(line.strip())
        if m:
            off, size, field = int(m.group(1)), int(m.group(2)), m.group(3)
            offsets[field] = (off, size)
    return offsets


def _make_iso(src: Path, iso: Path, label: str = "RS03DOC") -> None:
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-r", "-J", "-iso-level", "3",
         "-V", label, "-o", str(iso), str(src)],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _find_header(data: bytes) -> int:
    """Return the sector index whose first 12 bytes are the cookie."""
    total = len(data) // SECTOR
    for s in range(total):
        if data[s * SECTOR:s * SECTOR + len(COOKIE)] == COOKIE:
            return s
    raise AssertionError("dvdisaster cookie not found on any sector boundary")


def _dvdisaster_truth(iso: Path) -> dict[str, int]:
    """Run `dvdisaster -t -v` and scrape the layout values it prints."""
    proc = subprocess.run(
        ["dvdisaster", "-i", str(iso), "-t", "-v"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
    )
    out = proc.stdout + proc.stderr
    truth: dict[str, int] = {}
    patterns = {
        "total_sectors": r"total sectors\s*=\s*(\d+)",
        "data_sectors": r"data sectors\s*=\s*(\d+)",
        "layer_size": r"layer size\s*=\s*(\d+)",
        "first_ecc": r"first ECC sector\s*=\s*(\d+)",
        "nroots": r"nroots\s*=\s*(\d+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, out)
        if m:
            truth[key] = int(m.group(1))
    return truth


def test_rs03_header_matches_binary(tmp_path: Path) -> None:
    runner = SubprocessDVDisasterRunner()

    # 1. Small payload → ISO → augment with real RS03 ECC.
    src = tmp_path / "src"
    src.mkdir()
    (src / "payload.bin").write_bytes(os.urandom(3_000_000))
    iso = tmp_path / "vol.iso"
    _make_iso(src, iso)
    runner.augment_iso(iso, redundancy_pct=15)

    # 2. Parse the header using ONLY the spec's documented offsets.
    offsets = _spec_offsets()
    for required in (
        "cookie", "method", "dataBytes", "eccBytes", "sectors",
        "sectorsPerLayer",
    ):
        assert required in offsets, (
            f"spec §3.2 table is missing the {required!r} row — the doc "
            f"is no longer parseable as a definitive header layout"
        )

    data = iso.read_bytes()
    hdr_sector = _find_header(data)
    base = hdr_sector * SECTOR
    hdr = data[base:base + 4096]

    def _u32(field: str) -> int:
        off, size = offsets[field]
        assert size == 4, f"{field} documented size {size} != 4"
        return struct.unpack_from("<i", hdr, off)[0]

    def _u64(field: str) -> int:
        off, size = offsets[field]
        assert size == 8, f"{field} documented size {size} != 8"
        return struct.unpack_from("<Q", hdr, off)[0]

    # cookie + method, read at the documented offsets.
    c_off, c_size = offsets["cookie"]
    assert hdr[c_off:c_off + c_size] == COOKIE
    m_off, m_size = offsets["method"]
    assert hdr[m_off:m_off + m_size] == b"RS03"

    ndata = _u32("dataBytes")
    nroots = _u32("eccBytes")
    data_sectors = _u64("sectors")
    spl = _u64("sectorsPerLayer")

    # 3. RS03 invariants from the parsed header.
    assert ndata + nroots == 255, (
        f"ndata({ndata}) + nroots({nroots}) != 255 — header parsed wrong"
    )

    # 4. §4.1 derived positions.
    first_crc = (ndata - 1) * spl
    first_ecc = first_crc + spl
    total_sectors = 255 * spl

    # 5. Cross-check against the binary's own report.
    truth = _dvdisaster_truth(iso)
    assert truth, "could not scrape any layout values from dvdisaster -t -v"

    assert nroots == truth["nroots"], (
        f"parsed nroots {nroots} != dvdisaster {truth['nroots']}"
    )
    assert data_sectors == truth["data_sectors"], (
        f"parsed dataSectors {data_sectors} != dvdisaster "
        f"{truth['data_sectors']}"
    )
    assert spl == truth["layer_size"], (
        f"parsed sectorsPerLayer {spl} != dvdisaster {truth['layer_size']}"
    )
    assert total_sectors == truth["total_sectors"], (
        f"derived total {total_sectors} != dvdisaster {truth['total_sectors']}"
    )
    assert first_ecc == truth["first_ecc"], (
        f"derived firstEccPos {first_ecc} != dvdisaster {truth['first_ecc']}"
    )
    # The header sector itself sits at dataSectors (§4.1 eccHeaderPos).
    assert hdr_sector == data_sectors, (
        f"cookie at sector {hdr_sector} but header says dataSectors="
        f"{data_sectors} (eccHeaderPos mismatch)"
    )
