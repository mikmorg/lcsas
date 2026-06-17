# dvdisaster RS03 Error Correction Format

> Bundled with LCSAS archive volumes for long-term survivability.
> This document specifies the RS03 format that LCSAS's OWN in-house
> ECC tool — `lcsas-ecc` (a C89 RS03 verify/repair decoder, cross-built
> for all six approved targets and bundled on every meta-volume, see
> `recovery/src/lcsas-ecc/`) — implements.  It also lets any future
> programmer re-implement RS03 verification/repair from scratch should
> both `lcsas-ecc` and the abandoned upstream `dvdisaster` binary be
> unavailable.  At restore time the heir does NOT need this document or
> dvdisaster: `restore.sh --check-disc <image>` drives the bundled
> `lcsas-ecc` directly.
>
> Source of record: the pinned dvdisaster source tarball
> (`dvdisaster/src/dvdisaster-0.79.10-pl6.tar.gz`, pinned in
> `recovery/UPSTREAM.sha256`, GPL v3), which LCSAS ships and audits on
> every meta-volume under `tools/src/` — not a third-party download.
> The corresponding public mirror of that source is
> [github.com/speed47/dvdisaster](https://github.com/speed47/dvdisaster).
> (Upstream dvdisaster is abandoned — last release 2020 — so no
> external download or fan-maintained mirror is part of the recovery
> path.)
>
> **This spec is definitive for dvdisaster 0.79.x as pinned in
> `recovery/UPSTREAM.sha256`** (`dvdisaster/src/dvdisaster-0.79.10-pl6.tar.gz`).
> Every offset, field width, byte order, and layout formula below is
> transcribed from that exact source (`src/dvdisaster.h`,
> `src/rs03-common.c`, `src/endian.c`) and verified against the real
> binary's `dvdisaster -t` output (see
> `tests/integration/test_rs03_doc_conformance.py`). The source tarball
> itself is bundled on every meta-volume under `tools/src/`, so a future
> engineer holding only a rescue disc has both this spec and the code it
> describes.
>
> Last updated: 2026-06-13

---

## 1. Overview

dvdisaster adds **Reed-Solomon** error correction data to ISO 9660
disc images.  If sectors on the optical disc become unreadable due
to physical damage (scratches, dye degradation, delamination), the
ECC data enables mathematical recovery of the lost sectors.

LCSAS uses **RS03** — the most recent dvdisaster codec, designed for
augmented images where the ECC data is appended directly to the ISO
file.

---

## 2. How RS03 Works

### 2.1 Conceptual Model

The ISO image is divided into fixed-size sectors (2048 bytes each,
per ISO 9660).  RS03 organizes these sectors into an error correction
matrix:

1. The image sectors form the "data" portion of a Reed-Solomon
   codeword.
2. Additional "parity" sectors are computed and appended after the
   ISO data.
3. The resulting Reed-Solomon code can correct up to `t` erased
   sectors per codeword (where `t` equals the number of parity
   sectors per codeword).

### 2.2 Redundancy

In augmented-image mode (what LCSAS uses) the redundancy is **not
configurable** — per the dvdisaster manual, "Setting the redundancy is
not possible due to constraints in the format. The codec will
automatically choose the size of the smallest fitting medium." The
image is padded up to the smallest medium on the ladder
CD → DVD → DVD9 → BD25 → BD50 → BDXL100 and the padding is filled with
parity, so the *effective* redundancy is `(padded − data) / data`
(LCSAS logs it after each augmentation).

- More slack between the data size and the next medium size = more
  parity = more tolerance for unreadable sectors
- The ECC data is appended to the end of the ISO, so the ISO
  remains a valid (readable) ISO 9660 image
- (Only the separate *error-correction-file* mode accepts `-n`; there
  a bare number means Reed-Solomon roots, and `%` means percent.)

### 2.3 Interleaving

RS03 interleaves the error correction across the entire disc surface.
This means that a large scratch affecting consecutive sectors does
not concentrate errors in a single codeword — instead, the errors
are spread across many codewords, each of which can correct a few
errors.  This is far more robust than non-interleaved approaches.

---

## 3. Binary Format

### 3.1 Layout

An RS03-augmented ISO has this structure:

```
┌─────────────────────────────────────────────────┐
│ Original ISO 9660 image                         │
│ (data sectors 0 .. N-1)                         │
├─────────────────────────────────────────────────┤
│ RS03 ECC Header (1 sector = 2048 bytes)         │
├─────────────────────────────────────────────────┤
│ CRC sectors                                     │
│ (checksums for each data sector)                │
├─────────────────────────────────────────────────┤
│ RS03 Parity sectors                             │
│ (Reed-Solomon parity data)                      │
└─────────────────────────────────────────────────┘
```

### 3.2 ECC Header

The RS03 ECC header is the `EccHeader` C struct (`src/dvdisaster.h`,
`typedef struct _EccHeader`).  In an augmented image it occupies **one
2048-byte sector** located at sector `dataSectors` — i.e. immediately
after the last ISO 9660 data sector (see §4 for the formula).  The
struct is 4096 bytes wide in memory, but only the first 2048 bytes land
in the header sector; bytes 2048+ hold a copy of the first ecc block's
CRC sums and are not part of the field table.

**This is the definitive, byte-exact layout** for the pinned 0.79.x
source.  Offsets account for the natural C alignment of the two
8-byte-aligned `guint64` fields (`__attribute__((aligned(8)))` in
`dvdisaster.h`); they were confirmed with `offsetof()` on the real
struct and by parsing a real augmented image (§4.1 worked example).

| Offset | Size | Field | Type | Description |
|--------|------|-------|------|-------------|
| 0 | 12 | cookie | `gint8[12]` | Magic bytes `"*dvdisaster*"` (exactly 12 bytes, **not** NUL-terminated) |
| 12 | 4 | method | `gint8[4]` | Method tag, ASCII `"RS03"` |
| 16 | 4 | methodFlags | `gint8[4]` | Per-method flag bytes (byte 3 reserved) |
| 20 | 16 | mediumFP | `guint8[16]` | MD5 fingerprint of the fingerprint sector |
| 36 | 16 | mediumSum | `guint8[16]` | MD5 of the whole medium |
| 52 | 16 | eccSum | `guint8[16]` | MD5 of the ecc section (ecc-file mode) |
| 68 | 8 | sectors | `guint8[8]` | Medium sectors without ecc (raw 8-byte LE integer, **not** aligned) |
| 76 | 4 | dataBytes | `gint32` | Data symbols per RS codeword = **ndata** |
| 80 | 4 | eccBytes | `gint32` | Parity symbols per RS codeword = **nroots** |
| 84 | 4 | creatorVersion | `gint32` | dvdisaster version that wrote it (e.g. 7910) |
| 88 | 4 | neededVersion | `gint32` | Oldest version that can decode it |
| 92 | 4 | fpSector | `gint32` | Sector used to compute `mediumFP` |
| 96 | 4 | selfCRC | `guint32` | CRC-32 of the header (computed with this field set to `0xffffffff`) |
| 100 | 16 | crcSum | `guint8[16]` | MD5 of the RS02 crc section (RS02 only) |
| 116 | 4 | inLast | `gint32` | Valid bytes in the last data sector |
| 120 | 8 | sectorsPerLayer | `guint64` | Sectors per RS layer (8-byte aligned) |
| 128 | 8 | sectorsAddedByEcc | `guint64` | Sectors added by ecc (8-byte aligned) |
| 136 | 3960 | padding | `gint8[3960]` | Pads the struct to 4096 bytes |

**Byte order.** All multi-byte integer fields are stored
**little-endian** on disc.  dvdisaster only byte-swaps when running on a
big-endian host (`src/endian.c` `SwapEccHeaderBytes`, guarded by
`#ifdef HAVE_BIG_ENDIAN`), and it swaps exactly these fields: `dataBytes`,
`eccBytes`, `creatorVersion`, `neededVersion`, `fpSector`, `inLast`
(32-bit) and `sectorsPerLayer`, `sectorsAddedByEcc` (64-bit).  The
byte-array fields (`cookie`, `method`, `mediumFP`, `mediumSum`, `eccSum`,
`sectors`, `crcSum`) are never swapped — `sectors` is read as raw
little-endian bytes.

**`nroots`/`ndata`.** RS03 always uses a 255-symbol Reed-Solomon code,
so `dataBytes` (ndata) + `eccBytes` (nroots) = 255.  Those two header
fields are the parity specification; the per-layer/per-image sector
counts are *derived* from `sectors`, `sectorsPerLayer`, `ndata`, and
`nroots` via the formulas in §4.

**Cookie semantics.** The 12-byte cookie `"*dvdisaster*"` (bytes
`2a 64 76 64 69 73 61 73 74 65 72 2a`) identifies any dvdisaster ECC
header.  A tool scanning for RS03 ECC data searches for this cookie on a
2048-byte sector boundary at or after the ISO 9660 filesystem size; the
adjacent `method` field (`"RS03"`) distinguishes RS03 from RS01/RS02.

### 3.3 CRC Sectors

After the header, CRC-32 checksums are stored for each data sector.
These provide a fast way to detect which sectors are damaged before
attempting RS correction.

### 3.4 Parity Sectors

The parity sectors contain the Reed-Solomon parity symbols computed
over the data sectors (including the CRC sectors).  The RS code used
is GF(2^8) — operations in the Galois Field of order 256.

---

## 4. Image Layout and Interleaving (augmented image)

Everything here is for the augmented-image case (`target = ECC_IMAGE`),
which is what LCSAS produces.  The formulas are transcribed from
`CalcRS03Layout` and `RS03SectorIndex` in `src/rs03-common.c`.

### 4.1 Layout formula

Let `GF_FIELDMAX = 255`.  Given the original image data-sector count
`dataSectors` (from the header `sectors` field) and the selected medium
capacity `mediumCapacity` (the smallest medium on the ladder
CD → DVD → DVD9 → BD25 → BD50 → BDXL whose layout yields ≥ 8 roots; see
§2.2), dvdisaster computes:

```
sectorsPerLayer = mediumCapacity / 255          # integer division
totalSectors    = 255 * sectorsPerLayer

ndata = ceil((dataSectors + 2) / sectorsPerLayer)   # data layers
if ndata < 84: ndata = 84                            # clip redundancy at 170 roots
dataPadding = ndata * sectorsPerLayer - dataSectors - 2
ndata  = ndata + 1        # the CRC layer is protected too → counts as data
nroots = 255 - ndata

eccHeaderPos = dataSectors                       # the header sector
firstCrcPos  = (ndata - 1) * sectorsPerLayer     # first CRC sector
firstEccPos  = firstCrcPos + sectorsPerLayer     # first parity sector
```

The `+2` accounts for the ECC header sector plus one sector reserved for
chaining CRC sums.  Note `sectorsPerLayer` and `ndata`/`nroots` from the
header (`sectorsPerLayer`, `dataBytes`, `eccBytes`) are authoritative;
the equations above let you re-derive them and the layer positions.

**Image structure** (sectors, in order):

```
0                         .. dataSectors-1     : ISO 9660 data
dataSectors (eccHeaderPos)                     : ECC header (1 sector)
dataSectors+1 .. firstCrcPos-1                 : data padding
firstCrcPos   .. firstCrcPos+sectorsPerLayer-1 : CRC layer (one per data sector)
firstEccPos   .. totalSectors-1                : nroots parity layers
```

### 4.2 Codeword interleaving

A Reed-Solomon codeword has 255 symbols (one per layer).  The image is
divided into 255 *layers*, each `sectorsPerLayer` sectors long.  For a
given layer index `L` (0-based) and a position `n` within the layer
(`0 ≤ n < sectorsPerLayer`), the absolute image sector is:

```
RS03SectorIndex(L, n) =
    L * sectorsPerLayer + n                              if L <  ndata-1   (data layers)
    firstCrcPos + n                                      if L == ndata-1   (CRC layer)
    firstEccPos + (L - ndata) * sectorsPerLayer + n      if L >= ndata     (parity layers)
```

The first `ndata-1` layers are the contiguous data region; layer
`ndata-1` is the CRC layer; layers `ndata .. 254` are the `nroots`
parity layers.

**Codeword assembly.** Fix a layer position `n` and a byte offset
`b` within a sector (`0 ≤ b < 2048`).  The 255-symbol codeword
`C(n, b)` is:

```
C(n, b)[L] = image[ 2048 * RS03SectorIndex(L, n) + b ]   for L = 0 .. 254
```

i.e. symbol `L` of the codeword is byte `b` of the layer-`L` sector at
position `n`.  Symbols `0 .. ndata-1` are the data+CRC payload; symbols
`ndata .. 254` are parity.  There are `sectorsPerLayer * 2048` such
codewords, and because consecutive image sectors live in the *same*
layer (not consecutive codewords), a physical scratch spanning many
sectors hits one symbol in each of many codewords — that is the
interleaving (§2.3).

### 4.3 Worked example (verify by hand)

A 3 000 000-byte payload masters to a **1648-sector** ISO.  Augmented to
the smallest fitting medium it lands on the BDXL ladder with these
header values (parsed from a real augmented image — see the conformance
test):

```
dataSectors      = 1648
sectorsPerLayer  = 1409
dataBytes  (ndata)  = 85
eccBytes   (nroots) = 170     # 85 + 170 = 255  ✓
```

Re-derive the layout:

```
totalSectors = 255 * 1409          = 359295
ndata-1      = 84                   (84 data layers)
firstCrcPos  = 84 * 1409           = 118356
firstEccPos  = 118356 + 1409       = 119765
eccHeaderPos = dataSectors         = 1648
```

`dvdisaster -t -v` on the same image prints `total sectors = 359295`,
`first ECC sector = 119765`, `nroots = 170 (200.0%)` — matching every
derived value.  The ECC header cookie is found at byte
`2048 * 1648 = 3375104`.

### 4.4 Reed-Solomon parameters

- **Field:** GF(2^8) with the generator (primitive) polynomial
  `0x187` = x^8 + x^7 + x^2 + x + 1
  (`RS_GENERATOR_POLY` in `src/dvdisaster.h`; `GF_FIELDSIZE = 256`,
  `GF_FIELDMAX = 255`).  This is **not** the common 0x11D primitive —
  dvdisaster uses 0x187, so a decoder must build its log/exp tables from
  0x187 to match the parity.
- **Code:** RS(255, ndata) — 255 symbols per codeword, `ndata` data
  symbols, `nroots = 255 - ndata` parity symbols.
- **nroots:** From the header `eccBytes`; equals the redundancy chosen by
  padding to the smallest fitting medium (clipped to ≤ 170 roots).
- **Erasure correction:** Corrects up to `nroots` known-bad sectors per
  codeword (erasure channel — the drive reports which sectors are
  unreadable, so positions are known).
- **Interleaving factor:** `sectorsPerLayer` — codeword symbols are one
  layer apart in the image (§4.2).

---

## 5. Operations

These are the operations the in-house `lcsas-ecc` binary
(`recovery/src/lcsas-ecc/`) implements; it is the tool the recovery
scripts actually drive (`restore.sh --check-disc <image>`), and it
reads/writes the exact RS03 layout specified above.  The abandoned
upstream `dvdisaster` equivalents are noted for reference — its output
remains byte-compatible — but no `dvdisaster` binary is on the recovery
path.

### 5.1 Verify (non-destructive)

```
lcsas-ecc verify <image>          # dvdisaster equivalent: dvdisaster -i image.iso -t
```

Reads all sectors, computes CRC-32, compares against stored CRCs.
Reports number of good/bad/missing sectors and whether the ECC can
repair the damage.  Exit 0 = clean, 1 = damage found, 2 = no RS03
header, 3 = usage/I-O error (see `main.c`).

### 5.2 Repair

```
lcsas-ecc fix <image> [--out F]   # dvdisaster equivalent: dvdisaster -i image.iso -f
```

Reads all sectors (including damaged ones), applies Reed-Solomon
erasure correction to reconstruct missing/bad sectors, and writes the
repaired image back in place (or to `--out F`).  `lcsas-ecc` repairs
atomically — it refuses to write a partially repaired image if any
codeword remains uncorrectable (exit 1).

### 5.3 Augment (create ECC)

```
lcsas-ecc augment <image> [--out F]   # dvdisaster equivalent: dvdisaster -i image.iso -mRS03 -c
```

Computes RS03 parity data and appends it to the ISO file, padding the
image up to the smallest fitting medium size (no redundancy knob:
augmented-image redundancy is not settable — see §2.2).  This is what
the burn pipeline (`src/lcsas/ecc/dvdisaster.py`) calls to ECC-protect
each volume.

### 5.4 Geometry

```
lcsas-ecc info <image>
```

Prints the parsed RS03 geometry (ndata/nroots/sectorsPerLayer and the
derived layer positions of §4) without reading the whole image.

---

## 6. Re-implementing RS03

If dvdisaster is no longer available, a replacement tool needs to:

1. **Find the ECC header** — scan for the 12-byte `"*dvdisaster*"`
   cookie on a 2048-byte boundary at or after the ISO 9660 size; confirm
   the adjacent `method` field is `"RS03"` (§3.2).
2. **Parse the header** — read `dataBytes` (ndata), `eccBytes` (nroots),
   `sectors` (dataSectors), and `sectorsPerLayer` at the offsets in §3.2;
   re-derive `firstCrcPos`/`firstEccPos`/`totalSectors` per §4.1.
3. **Read CRC sectors** — the CRC layer starts at `firstCrcPos`; CRC
   sector `i` holds 512 CRC-32 values (one per data sector of layer `i`).
4. **Identify bad sectors** — compute each data sector's CRC-32 and
   compare against the stored value, OR take the drive's read-error
   reports directly as erasures.
5. **Apply RS correction** — for each codeword `C(n, b)` (§4.2), gather
   the 255 interleaved symbols, mark known-bad positions as erasures, and
   solve the RS(255, ndata) erasure system over GF(2^8) with generator
   polynomial 0x187.
6. **Write repaired sectors** — write the recovered symbols back to the
   bad sectors in the image.

### Required Math

- **GF(2^8) arithmetic** — addition (XOR), multiplication / division via
  log/exp tables built from the primitive polynomial **0x187** (§4.4).
- **Reed-Solomon decoder** — Berlekamp-Massey or Euclidean algorithm for
  the error-locator polynomial; Forney for error values.  For pure
  erasure correction (positions known) the math reduces to solving a
  linear system, which is what dvdisaster does.
- **Interleaving order** — `RS03SectorIndex` (§4.2) is the exact mapping
  from `(layer, position)` to image sector; matching it is the whole
  "challenge" the older revisions of this doc hand-waved.

### Reference Sources

The authoritative reference is the pinned dvdisaster source itself,
**bundled on this meta-volume** at
`tools/src/dvdisaster-0.79.10-pl6.tar.gz` (GPLv3) and pinned by SHA-256
in `recovery/UPSTREAM.sha256`.  The load-bearing files are:

- `src/dvdisaster.h` — the `EccHeader` struct (§3.2) and the RS field
  constants (`GF_FIELDSIZE`, `RS_GENERATOR_POLY`).
- `src/rs03-common.c` — `CalcRS03Layout` (§4.1) and `RS03SectorIndex`
  (§4.2).
- `src/galois.c` — the GF(2^8) log/exp table construction.
- `src/endian.c` — `SwapEccHeaderBytes` (byte order, §3.2).

The LCSAS in-house RS03 decoder (FMT-01) lives at
`recovery/src/lcsas-ecc/` (`gf256.c`, `rs03.c`, `main.c`) and is a
second, audited reference implementation built against this spec — it
augments, verifies, and repairs RS03 images itself, so the recovery
path no longer depends on the abandoned upstream binary.  General-purpose
RS GF(2^8)
codecs (e.g. Phil Karn's `libfec` in C, or `reed-solomon-erasure` in
Rust) can supply the field/decoder primitives, but the decoder must be
parameterised to 0x187 and wrapped in the §4.2 interleaving to match
dvdisaster's parity.

---

## 7. Practical Notes for LCSAS

- LCSAS uses RS03 augmented images; the redundancy is whatever padding
  the smallest fitting medium leaves (not configurable — see §2.2)
- ECC is applied at the ISO level, AFTER all data is packed
- The augmented ISO is still a valid ISO 9660 filesystem — the ECC
  data appears after the filesystem boundary
- Pack files inside the ISO are also protected by SHA-256 content
  hashing, providing an additional integrity layer
- If a pack file's SHA-256 doesn't match after extraction, the disc
  may be damaged — repair with `lcsas-ecc fix <image>` (or the upstream
  `dvdisaster -f`) first, then re-extract
