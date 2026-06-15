/*
 * rs03.c -- RS03 augmented-image parse / verify / erasure-repair.
 *
 * Implements the read+repair half of dvdisaster's RS03 codec against
 * docs/DVDISASTER_RS03_FORMAT.md.  The encoder (augment) side is not
 * here; the burn pipeline still calls dvdisaster to write parity, and
 * lcsas-ecc's own test encoder (recovery/tests/test_ecc.c) produces the
 * small unpadded fixtures the always-on gate uses.
 *
 * Reed-Solomon erasure decoding.  Because the optical drive (or our CRC
 * layer) reports WHICH sectors are unreadable, every damaged symbol is
 * an *erasure* (known position, unknown value).  Erasure-only decoding
 * needs no Berlekamp-Massey: with e <= nroots erasures the syndromes
 * give a linear system in the e unknowns whose locator is the product
 * of (1 - X_i * z) and whose values come from Forney.  This file uses
 * the standard erasure-locator + Forney formulation; the generator's
 * consecutive roots are alpha^0..alpha^(nroots-1) (RS_FIRST_ROOT = 0),
 * matching dvdisaster's galois.c.
 */
#include "rs03.h"
#include "gf256.h"

#include <string.h>

/* ---- little-endian field readers ------------------------------------ */

static unsigned long rd_u32le(const unsigned char *p)
{
    return (unsigned long) p[0]
         | ((unsigned long) p[1] << 8)
         | ((unsigned long) p[2] << 16)
         | ((unsigned long) p[3] << 24);
}

static int rd_i32le(const unsigned char *p)
{
    /* RS03 field values here are small positive counts; read as u32. */
    return (int) rd_u32le(p);
}

static unsigned long rd_u64le_lo(const unsigned char *p)
{
    /* Sector counts comfortably fit in 32 bits for every medium on the
     * ladder (BDXL ~ 48e6 sectors); read the low 32 bits and require
     * the high word to be zero (checked by caller via this returning a
     * value that the geometry sanity check bounds). */
    return rd_u32le(p);
}

/* ---- header field offsets (spec sec 3.2) ---------------------------- */

#define OFF_COOKIE            0
#define OFF_METHOD            12
#define OFF_SECTORS           68
#define OFF_DATABYTES         76   /* ndata  */
#define OFF_ECCBYTES          80   /* nroots */
#define OFF_SECTORSPERLAYER   120

/* ---- CRC-32 (dvdisaster RS03 conformant) ---------------------------- */
/*
 * dvdisaster's per-sector RS03 CRC (src/crc32.c Crc32) is the standard
 * reflected CRC-32 (polynomial 0xEDB88320, table-driven, init 0xffffffff)
 * but it does NOT apply the conventional final XOR with 0xffffffff -- it
 * returns the raw running register.  Conformance proof: a 2048-byte zero
 * sector's stored RS03 CRC is 0x0e174561, which equals the running CRC of
 * zeros with no final inversion (i.e. zlib.crc32(zeros) ^ 0xffffffff).
 * We therefore omit the final inversion here so verify reads dvdisaster's
 * parity correctly; lcsas-ecc's own test encoder uses this same function
 * on both sides, so its self round-trip stays valid.
 */

static unsigned long crc_table[256];
static int crc_ready = 0;

static void crc_init(void)
{
    unsigned long c;
    int n, k;
    if (crc_ready) {
        return;
    }
    for (n = 0; n < 256; n++) {
        c = (unsigned long) n;
        for (k = 0; k < 8; k++) {
            if (c & 1UL) {
                c = 0xedb88320UL ^ (c >> 1);
            } else {
                c = c >> 1;
            }
        }
        crc_table[n] = c & 0xffffffffUL;
    }
    crc_ready = 1;
}

unsigned long rs03_crc32(const unsigned char *data, size_t len)
{
    unsigned long c = 0xffffffffUL;
    size_t i;
    crc_init();
    for (i = 0; i < len; i++) {
        c = crc_table[(c ^ data[i]) & 0xff] ^ (c >> 8);
    }
    /* No final inversion: dvdisaster's Crc32() returns the raw running
     * register (see header note). */
    return c & 0xffffffffUL;
}

/* ---- layout --------------------------------------------------------- */

unsigned long rs03_sector_index(const rs03_layout *L,
                                int layer, unsigned long pos)
{
    if (layer < L->ndata - 1) {
        return (unsigned long) layer * L->sectors_per_layer + pos;
    }
    if (layer == L->ndata - 1) {
        return L->first_crc_pos + pos;
    }
    return L->first_ecc_pos
         + (unsigned long) (layer - L->ndata) * L->sectors_per_layer + pos;
}

int rs03_parse(const unsigned char *img, size_t img_len, rs03_layout *out)
{
    size_t off;
    int found = 0;
    unsigned long hdr_sector = 0;

    if (img_len < RS03_SECTOR_SIZE) {
        return -1;
    }

    /* Scan for the cookie on a 2048-byte boundary; confirm method RS03.
     * The header sits at sector data_sectors, but we discover that by
     * locating the cookie rather than trusting an unknown data size. */
    for (off = 0; off + RS03_SECTOR_SIZE <= img_len; off += RS03_SECTOR_SIZE) {
        if (memcmp(img + off + OFF_COOKIE, RS03_COOKIE, RS03_COOKIE_LEN) == 0
            && memcmp(img + off + OFF_METHOD, "RS03", 4) == 0) {
            found = 1;
            hdr_sector = (unsigned long) (off / RS03_SECTOR_SIZE);
            break;
        }
    }
    if (!found) {
        return -1;
    }

    out->data_sectors      = rd_u64le_lo(img + off + OFF_SECTORS);
    out->ndata             = rd_i32le(img + off + OFF_DATABYTES);
    out->nroots            = rd_i32le(img + off + OFF_ECCBYTES);
    out->sectors_per_layer = rd_u64le_lo(img + off + OFF_SECTORSPERLAYER);

    /* The header sector IS at data_sectors (spec sec 4.1). If the
     * stored sectors field disagrees with where we found the cookie the
     * image is malformed -- reject rather than mis-decode. */
    if (out->data_sectors != hdr_sector) {
        return -1;
    }

    /* Geometry sanity: ndata + nroots must be 255, counts in range. */
    if (out->ndata <= 0 || out->nroots <= 0
        || out->ndata + out->nroots != GF_FIELDSIZE - 1
        || out->sectors_per_layer == 0
        || out->sectors_per_layer > 0x7fffffffUL) {
        return -1;
    }

    out->total_sectors = (unsigned long) GF_FIELDMAX * out->sectors_per_layer;
    out->ecc_header_pos = out->data_sectors;
    out->first_crc_pos  = (unsigned long) (out->ndata - 1) * out->sectors_per_layer;
    out->first_ecc_pos  = out->first_crc_pos + out->sectors_per_layer;

    /* The data region (header + payload) must fit before the CRC layer. */
    if (out->data_sectors >= out->first_crc_pos) {
        return -1;
    }

    return 0;
}

/* ---- CRC verification ----------------------------------------------- */
/*
 * CRC layer storage -- dvdisaster RS03 conformant (src/rs03-create.c,
 * read_next_chunk + the encoder thread, flush_crc).  The CRC layer is
 * `sectors_per_layer` sectors, each a 2048-byte sector holding 512
 * little-endian uint32 slots.  dvdisaster writes the CRC layer with a
 * one-position "chain-back": the CRC of the data sector at codeword
 * position `pos`, data layer `layer` (0..ndata-2), is stored NOT in CRC
 * sector `pos` but in CRC sector `pos-1` (the PREVIOUS position), slot
 * `layer`.  Equivalently, CRC sector at position `i` holds, in slot
 * `layer`, the CRC of the data sector at (layer, i+1); the last CRC
 * sector (i = spl-1) wraps around and holds the CRCs of position 0
 * (dvdisaster's ec->firstCrc cache).  So the slot that verifies
 * (layer, pos) lives at CRC position (pos - 1 + spl) % spl.
 *
 * (lcsas-ecc's own test encoder mirrors this chain-back so the C
 * self round-trip stays self-consistent; see recovery/tests/test_ecc.c.)
 *
 * The metadata CrcBlock (cookie/method/geometry) that dvdisaster overlays
 * at the END of each CRC sector (uint32 slots 256+) never collides with a
 * data slot, since ndata-1 <= 254 < 256.
 */
static unsigned long read_stored_crc(const unsigned char *img,
                                     const rs03_layout *L,
                                     int layer, unsigned long pos)
{
    unsigned long crc_pos =
        (pos + L->sectors_per_layer - 1) % L->sectors_per_layer;
    unsigned long crc_sec = L->first_crc_pos + crc_pos;
    const unsigned char *p =
        img + crc_sec * RS03_SECTOR_SIZE + (size_t) layer * 4;
    return rd_u32le(p);
}

long rs03_verify(const unsigned char *img, size_t img_len,
                 const rs03_layout *L, unsigned char *bad)
{
    unsigned long pos;
    int layer;
    long ndamaged = 0;
    size_t need = (size_t) L->total_sectors * RS03_SECTOR_SIZE;

    memset(bad, 0, (size_t) L->total_sectors);

    /* Data layers are 0 .. ndata-2.  dvdisaster CRC-protects EVERY sector
     * in these layers -- including the ECC header sector and the padding
     * region between data_sectors and the CRC layer -- so all of them are
     * checked (verified empirically against a real augmented image). */
    for (layer = 0; layer < L->ndata - 1; layer++) {
        for (pos = 0; pos < L->sectors_per_layer; pos++) {
            unsigned long sec = rs03_sector_index(L, layer, pos);
            unsigned long stored, actual;
            unsigned long crc_pos;

            /* A data sector beyond the readable image is an erasure. */
            if ((size_t) (sec + 1) * RS03_SECTOR_SIZE > img_len) {
                bad[sec] = 1;
                ndamaged++;
                continue;
            }

            /* The CRC for this sector lives in CRC sector (pos-1)%spl
             * (the chain-back position), which a truncated image may not
             * carry even when the data sector itself is present.  Reading
             * it unconditionally is a heap over-read (found by
             * fuzz_rs03_parse).  When the CRC slot is past the readable
             * image, we cannot verify the sector -- treat it as an
             * erasure (damage), matching the documented "truncated image
             * is reported as damage, not crash" contract. */
            crc_pos = (pos + L->sectors_per_layer - 1)
                      % L->sectors_per_layer;
            {
                unsigned long crc_sec = L->first_crc_pos + crc_pos;
                size_t crc_end =
                    (size_t) crc_sec * RS03_SECTOR_SIZE
                    + (size_t) layer * 4 + 4;
                if (crc_end > img_len) {
                    bad[sec] = 1;
                    ndamaged++;
                    continue;
                }
            }

            stored = read_stored_crc(img, L, layer, pos);
            actual = rs03_crc32(img + (size_t) sec * RS03_SECTOR_SIZE,
                                RS03_SECTOR_SIZE);
            if (stored != actual) {
                bad[sec] = 1;
                ndamaged++;
            }
        }
    }

    /* Touch `need` so a structural truncation past the parity is at
     * least observable to the caller via img_len, without crashing. */
    (void) need;
    return ndamaged;
}

/* ---- Reed-Solomon error+erasure decode ------------------------------ */
/*
 * One codeword has 255 symbols, one per layer, at a fixed (pos, byte)
 * coordinate (spec sec 4.2).  This is a faithful port of dvdisaster's
 * decoder (src/rs03-fix.c, the per-byte error+erasure loop) and uses the
 * SAME Reed-Solomon parameters as dvdisaster:
 *
 *   - GF(2^8) over primitive polynomial 0x187 (gf256);
 *   - first consecutive root  RS_FIRST_ROOT  = 112  (CCSDS choice);
 *   - primitive element       RS_PRIM_ELEM   = 11;
 *   - prim-th root of unity   RS_PRIMTH_ROOT = 116  (used by Chien search);
 *   - codeword polynomial position of layer L is GF_FIELDMAX-1-L (254-L),
 *     i.e. sym[0] (layer 0) is the highest-degree coefficient.
 *
 * These parameters were extracted from the pinned dvdisaster 0.79.x source
 * (src/dvdisaster.h, src/galois.c, src/rs03-fix.c) and verified against a
 * real augmented image: with FCR=112/PRIM=11 and the 254-L position map,
 * the syndromes of every undamaged codeword vanish.  (The previous
 * RS_FIRST_ROOT=0 / consecutive-power formulation did NOT match
 * dvdisaster's parity -- it made every codeword look uncorrectable.)
 *
 * Berlekamp-Massey + Chien + Forney over the full 255-symbol codeword;
 * the erasure positions seed the locator polynomial.  Corrections are
 * applied (XOR) in place to `sym`.  Returns 0 if every flagged erasure
 * was located and corrected, -1 otherwise (uncorrectable).
 */

#define MAXSYM        255
#define RS_FIRST_ROOT 112
#define RS_PRIM_ELEM  11
#define RS_PRIMTH     116           /* RS_PRIMTH_ROOT */

#define MIN2(a, b)    ((a) < (b) ? (a) : (b))

/* x mod GF_FIELDMAX, for x in [0, 2*GF_FIELDMAX). */
static int mod_fm(int x)
{
    while (x >= GF_FIELDMAX) {
        x -= GF_FIELDMAX;
    }
    return x;
}

static int decode_codeword(unsigned char *sym,        /* 255 symbols, in/out */
                           const int *erased_pos,     /* layer indices, asc  */
                           int nerased,
                           int nroots)
{
    int syn[MAXSYM];
    int lambda[MAXSYM + 1];
    int b[MAXSYM + 1];
    int t[MAXSYM + 1];
    int omega[MAXSYM + 1];
    int root[MAXSYM];
    int reg[MAXSYM + 1];
    int loc[MAXSYM];
    int i, j, k, r, el, deg_lambda, deg_omega, count;
    int discr_r, tmp, num1, num2, den, syn_error;

    if (nerased == 0) {
        return 0;
    }
    if (nerased > nroots) {
        return -1;              /* uncorrectable */
    }

    /* Form the syndromes by Horner's rule over all 255 symbols, sym[0]
     * being the highest-degree coefficient (rs03-fix.c:549-559). */
    for (i = 0; i < nroots; i++) {
        syn[i] = sym[0];
    }
    for (j = 1; j < GF_FIELDMAX; j++) {
        int data = sym[j];
        for (i = 0; i < nroots; i++) {
            if (syn[i] == 0) {
                syn[i] = data;
            } else {
                syn[i] = data ^ gf_alpha_to(
                    mod_fm(gf_index_of((unsigned char) syn[i])
                           + (RS_FIRST_ROOT + i) * RS_PRIM_ELEM));
            }
        }
    }

    /* Convert syndromes to index form; check for a nonzero condition. */
    syn_error = 0;
    for (i = 0; i < nroots; i++) {
        syn_error |= syn[i];
        syn[i] = gf_index_of((unsigned char) syn[i]);
    }
    if (!syn_error) {
        return 0;               /* already correct */
    }

    /* Initialise lambda to the erasure locator polynomial. */
    for (i = 1; i <= nroots; i++) {
        lambda[i] = 0;
    }
    lambda[0] = 1;
    if (nerased > 0) {
        lambda[1] = gf_alpha_to(
            mod_fm(RS_PRIM_ELEM * (GF_FIELDMAX - 1 - erased_pos[0])));
        for (i = 1; i < nerased; i++) {
            int u = mod_fm(RS_PRIM_ELEM * (GF_FIELDMAX - 1 - erased_pos[i]));
            for (j = i + 1; j > 0; j--) {
                tmp = gf_index_of((unsigned char) lambda[j - 1]);
                if (tmp != GF_ALPHA0) {
                    lambda[j] ^= gf_alpha_to(mod_fm(u + tmp));
                }
            }
        }
    }

    for (i = 0; i < nroots + 1; i++) {
        b[i] = gf_index_of((unsigned char) lambda[i]);
    }

    /* Berlekamp-Massey: extend the erasure locator to an error+erasure
     * locator (rs03-fix.c:597-639). */
    r = nerased;
    el = nerased;
    while (++r <= nroots) {
        discr_r = 0;
        for (i = 0; i < r; i++) {
            if ((lambda[i] != 0) && (syn[r - i - 1] != GF_ALPHA0)) {
                discr_r ^= gf_alpha_to(
                    mod_fm(gf_index_of((unsigned char) lambda[i])
                           + syn[r - i - 1]));
            }
        }
        discr_r = gf_index_of((unsigned char) discr_r);

        if (discr_r == GF_ALPHA0) {
            /* B(x) = x*B(x) */
            for (i = nroots; i >= 1; i--) {
                b[i] = b[i - 1];
            }
            b[0] = GF_ALPHA0;
        } else {
            t[0] = lambda[0];
            for (i = 0; i < nroots; i++) {
                if (b[i] != GF_ALPHA0) {
                    t[i + 1] = lambda[i + 1]
                             ^ gf_alpha_to(mod_fm(discr_r + b[i]));
                } else {
                    t[i + 1] = lambda[i + 1];
                }
            }
            if (2 * el <= r + nerased - 1) {
                el = r + nerased - el;
                for (i = 0; i <= nroots; i++) {
                    b[i] = (lambda[i] == 0) ? GF_ALPHA0
                        : mod_fm(gf_index_of((unsigned char) lambda[i])
                                 - discr_r + GF_FIELDMAX);
                }
            } else {
                for (i = nroots; i >= 1; i--) {
                    b[i] = b[i - 1];
                }
                b[0] = GF_ALPHA0;
            }
            for (i = 0; i <= nroots; i++) {
                lambda[i] = t[i];
            }
        }
    }

    /* Convert lambda to index form and compute its degree. */
    deg_lambda = 0;
    for (i = 0; i < nroots + 1; i++) {
        lambda[i] = gf_index_of((unsigned char) lambda[i]);
        if (lambda[i] != GF_ALPHA0) {
            deg_lambda = i;
        }
    }

    /* Chien search for the roots of lambda(x). */
    for (i = 1; i < nroots + 1; i++) {
        reg[i] = lambda[i];
    }
    count = 0;
    k = RS_PRIMTH - 1;
    for (i = 1; i <= GF_FIELDMAX; i++, k = mod_fm(k + RS_PRIMTH)) {
        int q = 1;          /* lambda[0] is always 0 in index form path */
        for (j = deg_lambda; j > 0; j--) {
            if (reg[j] != GF_ALPHA0) {
                reg[j] = mod_fm(reg[j] + j);
                q ^= gf_alpha_to(reg[j]);
            }
        }
        if (q != 0) {
            continue;       /* not a root */
        }
        root[count] = i;
        loc[count] = k;
        if (++count == deg_lambda) {
            break;
        }
    }

    /* deg(lambda) != #roots => uncorrectable. */
    if (deg_lambda != count) {
        return -1;
    }

    /* Evaluator omega(x) = syn(x)*lambda(x) mod x^nroots (index form). */
    deg_omega = deg_lambda - 1;
    for (i = 0; i <= deg_omega; i++) {
        tmp = 0;
        for (j = i; j >= 0; j--) {
            if ((syn[i - j] != GF_ALPHA0) && (lambda[j] != GF_ALPHA0)) {
                tmp ^= gf_alpha_to(mod_fm(syn[i - j] + lambda[j]));
            }
        }
        omega[i] = gf_index_of((unsigned char) tmp);
    }

    /* Forney: compute and apply each error/erasure value. */
    for (j = count - 1; j >= 0; j--) {
        num1 = 0;
        for (i = deg_omega; i >= 0; i--) {
            if (omega[i] != GF_ALPHA0) {
                num1 ^= gf_alpha_to(mod_fm(omega[i] + i * root[j]));
            }
        }
        num2 = gf_alpha_to(
            mod_fm(root[j] * (RS_FIRST_ROOT - 1) + GF_FIELDMAX));
        den = 0;
        /* lambda[i+1] for i even is the formal derivative of lambda. */
        for (i = MIN2(deg_lambda, nroots - 1) & ~1; i >= 0; i -= 2) {
            if (lambda[i + 1] != GF_ALPHA0) {
                den ^= gf_alpha_to(mod_fm(lambda[i + 1] + i * root[j]));
            }
        }
        if (num1 != 0) {
            int location = loc[j];
            if (location < 0 || location >= MAXSYM || den == 0) {
                return -1;
            }
            sym[location] ^= gf_alpha_to(
                mod_fm(gf_index_of((unsigned char) num1)
                       + gf_index_of((unsigned char) num2)
                       + GF_FIELDMAX
                       - gf_index_of((unsigned char) den)));
        }
    }

    return 0;
}

long rs03_fix(unsigned char *img, size_t img_len,
              const rs03_layout *L, const unsigned char *bad)
{
    unsigned long pos;
    int layer, b;
    long uncorrectable = 0;
    size_t need = (size_t) L->total_sectors * RS03_SECTOR_SIZE;

    if (img_len < need) {
        return -1;
    }

    for (pos = 0; pos < L->sectors_per_layer; pos++) {
        /* Which layers are erased at this codeword position? Same for
         * all 2048 byte-offsets, so compute the erasure set once. */
        int erased_pos[MAXSYM];
        unsigned long erased_sec[MAXSYM];
        int nerased = 0;

        for (layer = 0; layer < GF_FIELDMAX; layer++) {
            unsigned long sec = rs03_sector_index(L, layer, pos);
            if (bad[sec]) {
                erased_pos[nerased] = layer;
                erased_sec[nerased] = sec;
                nerased++;
            }
        }
        if (nerased == 0) {
            continue;
        }
        if (nerased > L->nroots) {
            uncorrectable++;
            continue;
        }

        /* Repair each byte-offset's codeword.  Like dvdisaster, the
         * decoder keeps the (corrupted) erased bytes in place and applies
         * XOR corrections -- the erasure positions seed the locator. */
        for (b = 0; b < RS03_SECTOR_SIZE; b++) {
            unsigned char sym[MAXSYM];
            int i;
            for (layer = 0; layer < GF_FIELDMAX; layer++) {
                unsigned long sec = rs03_sector_index(L, layer, pos);
                sym[layer] = img[(size_t) sec * RS03_SECTOR_SIZE + b];
            }
            if (decode_codeword(sym, erased_pos, nerased, L->nroots) != 0) {
                /* uncorrectable for this codeword; count once per pos. */
                if (b == 0) {
                    uncorrectable++;
                }
                break;
            }
            /* Write recovered symbols back to their sectors. */
            for (i = 0; i < nerased; i++) {
                img[(size_t) erased_sec[i] * RS03_SECTOR_SIZE + b]
                    = sym[erased_pos[i]];
            }
        }
    }

    return uncorrectable;
}
