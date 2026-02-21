# dvdisaster RS03 Error Correction Format

> Bundled with LCSAS archive volumes for long-term survivability.
> This document enables a future programmer to understand and
> potentially re-implement RS03 ECC verification/repair if the
> `dvdisaster` binary is no longer available.
>
> Sources: [dvdisaster documentation](https://dvdisaster.jcea.es/),
> [dvdisaster source code](https://github.com/speed47/dvdisaster)
> (GPL v3 license).
>
> Last updated: 2026-02-21

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

The `redundancy_pct` parameter (LCSAS default: 15%) controls how
many parity sectors are generated relative to the data size:

- 15% redundancy ≈ can tolerate ~15% of sectors being unreadable
- Higher redundancy = more protection but larger disc usage
- The ECC data is appended to the end of the ISO, so the ISO
  remains a valid (readable) ISO 9660 image

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

The RS03 ECC header is located immediately after the last ISO 9660
sector.  Key fields include:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 16 | cookie | Magic bytes: `"*dvdisaster*"` |
| 16 | 4 | methodFlags | Bit flags for ECC method |
| 20 | 16 | mediumFP | Fingerprint of the original medium |
| 36 | 16 | mediumSum | MD5 checksum of data sectors |
| 52 | 4 | eccBytes | Number of ECC bytes per code word |
| 56 | 4 | creatorVersion | dvdisaster version that created it |
| 60 | 4 | neededVersion | Minimum version needed to process |
| 64 | 4 | fpSector | Sector used for fingerprinting |
| 68 | 8 | selfCRC | CRC-32 of the header itself |
| 76 | 8 | inLay | Layout information |
| 84 | 8 | sectorsPerLayer | Sectors in each RS layer |
| 92 | 4 | nroots | Number of RS roots (parity symbols) |
| 96 | 4 | dataBytes | Data bytes per RS codeword |
| 100 | 8 | dataSectors | Number of original data sectors |
| 108 | 8 | eccSectors | Number of ECC parity sectors |

The actual struct layout and byte order may vary between dvdisaster
versions; consult the source code's `rs03-common.h` for the
definitive field layout.

**Important:** The header uses the `"*dvdisaster*"` magic string for
identification.  Any tool scanning for RS03 ECC data should search
for this cookie starting at the end of the ISO 9660 filesystem.

### 3.3 CRC Sectors

After the header, CRC-32 checksums are stored for each data sector.
These provide a fast way to detect which sectors are damaged before
attempting RS correction.

### 3.4 Parity Sectors

The parity sectors contain the Reed-Solomon parity symbols computed
over the data sectors (including the CRC sectors).  The RS code used
is GF(2^8) — operations in the Galois Field of order 256.

---

## 4. Reed-Solomon Parameters

- **Field:** GF(2^8) with primitive polynomial 0x11D
  (x^8 + x^4 + x^3 + x^2 + 1)
- **Code:** RS(255, 255-nroots) — up to 255 symbols per codeword
- **nroots:** Determined by redundancy percentage (e.g., 15% → ~32 roots)
- **Erasure correction:** Can correct up to `nroots` known-bad sectors
  per codeword (erasure channel model — dvdisaster knows WHICH sectors
  are bad because the drive reports read errors)
- **Interleaving factor:** Distributes codeword symbols across the
  entire disc surface

---

## 5. Operations

### 5.1 Verify (non-destructive)

```
dvdisaster -i image.iso -t
```

Reads all sectors, computes CRC-32, compares against stored CRCs.
Reports number of good/bad/missing sectors and whether the ECC can
repair the damage.

### 5.2 Repair

```
dvdisaster -i image.iso -f
```

Reads all sectors (including damaged ones), applies Reed-Solomon
error correction to reconstruct missing/bad sectors, writes repaired
image in place.

### 5.3 Augment (create ECC)

```
dvdisaster -i image.iso -mRS03 -n <redundancy_pct> -c
```

Computes RS03 parity data and appends it to the ISO file.

---

## 6. Re-implementing RS03

If dvdisaster is no longer available, a replacement tool needs to:

1. **Find the ECC header** — scan for `"*dvdisaster*"` magic after
   the ISO 9660 filesystem
2. **Parse the header** — extract nroots, dataSectors, eccSectors,
   sectorsPerLayer
3. **Read CRC sectors** — header tells you where they start
4. **Identify bad sectors** — compare each data sector's CRC-32
   against the stored CRC value
5. **Apply RS correction** — for each RS codeword, collect the
   interleaved symbols from across the disc, mark the known-bad
   positions as erasures, and solve the RS erasure correction
6. **Write repaired sectors** — replace the bad sectors in the image

### Required Math

- **GF(2^8) arithmetic** — addition (XOR), multiplication (log/exp
  tables), division
- **Reed-Solomon decoder** — Berlekamp-Massey or Euclidean algorithm
  for error locator polynomial; Forney algorithm for error values
  (though for pure erasure correction, the math simplifies)
- **Interleaving order** — must match dvdisaster's layout to correctly
  map sectors to RS codeword positions

### Reference Implementations

Reed-Solomon GF(2^8) implementations exist in many languages:

- Python: `reedsolo` library (pure Python, pip-installable)
- Rust: `reed-solomon-erasure` crate
- C: `libfec` by Phil Karn
- JavaScript: `@aspect/reedsolomon`

The main challenge is matching dvdisaster's specific interleaving
layout, which requires reading the RS03 source code.

---

## 7. Practical Notes for LCSAS

- LCSAS uses RS03 at 15% redundancy by default (configurable)
- ECC is applied at the ISO level, AFTER all data is packed
- The augmented ISO is still a valid ISO 9660 filesystem — the ECC
  data appears after the filesystem boundary
- Pack files inside the ISO are also protected by SHA-256 content
  hashing, providing an additional integrity layer
- If a pack file's SHA-256 doesn't match after extraction, the disc
  may be damaged — repair with dvdisaster first, then re-extract
