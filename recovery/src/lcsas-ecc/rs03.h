/*
 * rs03.h -- dvdisaster RS03 augmented-image layout, CRC verification,
 *           and Reed-Solomon erasure decoding.
 *
 * This is an independent, audited re-implementation of the read/repair
 * half of dvdisaster's RS03 codec, written against
 * docs/DVDISASTER_RS03_FORMAT.md (the definitive spec extracted from
 * the pinned dvdisaster 0.79.x source).  It does NOT depend on the
 * dvdisaster binary; it reads the parity bytes already burned on the
 * disc and spends them at restore time.
 *
 * Scope of this module: verify (locate damaged sectors via CRC) and
 * fix (erasure-decode damaged sectors using the parity layers).  The
 * augment/encode side is out of scope (the burn pipeline still uses
 * dvdisaster for that) -- see FMT-01 plan, phase 2.
 *
 * C89; depends only on gf256 and the standard library.
 */
#ifndef LCSAS_ECC_RS03_H
#define LCSAS_ECC_RS03_H

#include <stddef.h>

#define RS03_SECTOR_SIZE 2048
#define RS03_COOKIE      "*dvdisaster*"   /* 12 bytes, NOT NUL-terminated */
#define RS03_COOKIE_LEN  12

/*
 * Parsed + derived RS03 geometry for an augmented image.  All sector
 * counts are absolute image-sector indices.  Field names mirror the
 * spec (DVDISASTER_RS03_FORMAT.md sec 4.1).
 */
typedef struct {
    /* From the ECC header. */
    unsigned long data_sectors;     /* original ISO data sectors (= header pos) */
    int           ndata;            /* data symbols per codeword (incl CRC layer) */
    int           nroots;           /* parity symbols per codeword; ndata+nroots=255 */
    unsigned long sectors_per_layer;

    /* Derived. */
    unsigned long total_sectors;    /* 255 * sectors_per_layer */
    unsigned long ecc_header_pos;   /* = data_sectors */
    unsigned long first_crc_pos;    /* (ndata-1) * sectors_per_layer */
    unsigned long first_ecc_pos;    /* first_crc_pos + sectors_per_layer */
} rs03_layout;

/*
 * Map a (layer, position) pair to an absolute image sector index.
 * Implements RS03SectorIndex (spec sec 4.2).  `layer` is 0..254.
 */
unsigned long rs03_sector_index(const rs03_layout *L,
                                int layer, unsigned long pos);

/*
 * Locate the ECC header in `img` (whole augmented image in memory) and
 * fill *out.  Returns 0 on success, non-zero if no valid RS03 header is
 * found or the geometry is inconsistent.
 */
int rs03_parse(const unsigned char *img, size_t img_len, rs03_layout *out);

/*
 * dvdisaster-compatible CRC-32 (src/crc32.c Crc32): reflected CRC-32 with
 * polynomial 0xEDB88320, init 0xffffffff, and NO final inversion (returns
 * the raw running register, unlike the conventional zlib CRC-32 which XORs
 * 0xffffffff at the end).  Exposed for unit tests; rs03_verify uses it
 * internally.  A 2048-byte zero sector hashes to 0x0E174561.
 */
unsigned long rs03_crc32(const unsigned char *data, size_t len);

/*
 * Verify all data sectors against the stored CRC layer.  `bad` must
 * point to an array of at least `total_sectors` bytes; on return,
 * bad[s] is 1 for each data sector (0..first_crc_pos-1, excluding the
 * header/padding region per layout) whose CRC mismatches, else 0.
 * Returns the number of damaged data sectors, or -1 on a structural
 * error (image too short, etc).
 *
 * Sectors beyond the readable image length are treated as erasures
 * (damaged), so a truncated image is reported as damage, not crash.
 */
long rs03_verify(const unsigned char *img, size_t img_len,
                 const rs03_layout *L, unsigned char *bad);

/*
 * Compute the RS03 augmented-image layout for a plain data image of
 * `data_sectors` 2048-byte sectors, choosing the smallest fitting medium
 * from dvdisaster's ladder (CD -> DVD -> DVD9 -> BD25 -> BD50 -> BDXL) such
 * that the codec yields >= 8 parity roots.  This is a faithful port of
 * dvdisaster's CalcRS03Layout (ECC_IMAGE target, src/rs03-common.c) and is
 * the layout the encoder pads to so real dvdisaster recognises the result.
 *
 * Fills *out (ndata, nroots, sectors_per_layer, and all derived positions).
 * Returns 0 on success, -1 if `data_sectors` is 0 or exceeds the largest
 * medium (would yield < 8 roots even on a quad-layer BDXL).
 */
int rs03_calc_layout(unsigned long data_sectors, rs03_layout *out);

/*
 * Encode an RS03-augmented image (the "augment" / burn side).
 *
 * `data` is the plain data image (an ISO) of `data_len` bytes; `in_last`
 * is the number of valid bytes in its final 2048-byte sector (2048 when
 * the image is a whole number of sectors).  On success *out points to a
 * freshly malloc'd full-medium image of *out_len bytes (caller frees) that
 * contains: the data sectors (zero-padded to a sector boundary), the RS03
 * ECC header at sector data_sectors (byte-for-byte what dvdisaster writes,
 * so `dvdisaster -t` recognises it), data padding, the CRC layer, and the
 * Reed-Solomon parity layers.  The output is bidirectionally conformant:
 * both real dvdisaster and rs03_verify/rs03_fix accept and repair it.
 *
 * Returns 0 on success; -1 on a layout/size error; -2 on out-of-memory; a
 * positive count if the internal parity solve left codewords uncorrected
 * (should never happen for a well-formed full medium -- treated as error).
 *
 * gf_init() must have been called first.
 */
int rs03_augment(const unsigned char *data, size_t data_len,
                 unsigned int in_last,
                 unsigned char **out, size_t *out_len);

/*
 * Repair damaged sectors in `img` (modified in place).  `bad` is the
 * per-sector erasure flag array produced by rs03_verify (or supplied by
 * the caller from drive read-error reports), length total_sectors.
 * `img_len` must be >= total_sectors * 2048 (the caller is responsible
 * for zero-extending a short image to full medium size before calling).
 *
 * Returns 0 if every codeword was fully corrected, a positive count of
 * UNcorrectable codewords if some codeword had more than nroots
 * erasures, or -1 on a structural error.  After a successful return the
 * repaired data sectors verify clean.
 */
long rs03_fix(unsigned char *img, size_t img_len,
              const rs03_layout *L, const unsigned char *bad);

#endif /* LCSAS_ECC_RS03_H */
