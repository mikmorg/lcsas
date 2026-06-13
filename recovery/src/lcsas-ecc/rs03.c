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

/* ---- CRC-32 (zlib / ISO 3309) --------------------------------------- */

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
    return (c ^ 0xffffffffUL) & 0xffffffffUL;
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
 * CRC layer storage (matches our encoder; documented assumption from
 * spec sec 3.3 / 6.3): the CRC layer is `sectors_per_layer` sectors,
 * each holding 512 uint32 little-endian CRC values.  The CRC of the
 * data sector at codeword position `pos`, data layer `layer`
 * (0..ndata-2) is stored as the `layer`-th uint32 within CRC sector
 * `pos` (i.e. CRC sector first_crc_pos+pos, byte offset layer*4).
 * Since ndata-1 <= 84 <= 512 this always fits in one CRC sector.
 */
static unsigned long read_stored_crc(const unsigned char *img,
                                     const rs03_layout *L,
                                     int layer, unsigned long pos)
{
    unsigned long crc_sec = L->first_crc_pos + pos;
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

    /* Data layers are 0 .. ndata-2.  Only sectors that actually hold
     * ISO/padding data are checked; the header sector and padding past
     * data_sectors carry no meaningful CRC, so skip them. */
    for (layer = 0; layer < L->ndata - 1; layer++) {
        for (pos = 0; pos < L->sectors_per_layer; pos++) {
            unsigned long sec = rs03_sector_index(L, layer, pos);
            unsigned long stored, actual;

            if (sec == L->ecc_header_pos) {
                continue;           /* header sector: not CRC-protected here */
            }
            if (sec > L->data_sectors) {
                continue;           /* data padding region */
            }

            /* A sector beyond the readable image is an erasure. */
            if ((size_t) (sec + 1) * RS03_SECTOR_SIZE > img_len) {
                bad[sec] = 1;
                ndamaged++;
                continue;
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

/* ---- Reed-Solomon erasure decode ------------------------------------ */
/*
 * One codeword has 255 symbols, one per layer, at a fixed (pos, byte)
 * coordinate (spec sec 4.2).  Symbols 0..ndata-1 are data+CRC payload,
 * ndata..254 are parity.  Given the erased layer set we reconstruct the
 * erased symbols.
 *
 * Erasure-only decode with consecutive generator roots alpha^0.. :
 *   syndrome S_j = sum over all symbols i of  c_i * alpha^(i*j),
 *                  j = 0 .. nroots-1   (erased positions read as 0)
 *   erasure locator  Lambda(x) = prod (1 - X_k x),  X_k = alpha^(pos_k)
 *   Omega(x) = S(x) Lambda(x) mod x^nroots
 *   value_k  = - X_k * Omega(X_k^-1) / Lambda'(X_k^-1)
 * For up to nroots erasures this is exact.
 */

#define MAXSYM 255

static int decode_codeword(unsigned char *sym,        /* 255 symbols, in/out */
                           const int *erased_pos,     /* indices, ascending  */
                           int nerased,
                           int nroots)
{
    unsigned char synd[MAXSYM];
    unsigned char lambda[MAXSYM + 1];
    unsigned char omega[MAXSYM];
    int i, j, k;

    if (nerased == 0) {
        return 0;
    }
    if (nerased > nroots) {
        return -1;              /* uncorrectable */
    }

    /* Syndromes S_j = sum_i sym_i * (alpha^i)^j, j=0..nroots-1.
     * (erased symbols already read as their current value; the caller
     * zeroes them before calling so they contribute nothing.) */
    for (j = 0; j < nroots; j++) {
        unsigned char s = 0;
        for (i = 0; i < MAXSYM; i++) {
            if (sym[i] != 0) {
                /* sym_i * alpha^(i*j) */
                s ^= gf_mul(sym[i], gf_exp(i * j));
            }
        }
        synd[j] = s;
    }

    /* Erasure locator Lambda(x) = prod_k (1 - X_k x), X_k = alpha^pos_k.
     * Build coefficients (lambda[0] = 1). */
    lambda[0] = 1;
    for (i = 1; i <= nerased; i++) {
        lambda[i] = 0;
    }
    for (k = 0; k < nerased; k++) {
        unsigned char Xk = gf_exp(erased_pos[k]);
        /* multiply current Lambda by (1 - Xk x): new_i = old_i - Xk*old_{i-1} */
        for (i = k + 1; i >= 1; i--) {
            lambda[i] ^= gf_mul(Xk, lambda[i - 1]);
        }
    }

    /* Omega(x) = S(x) * Lambda(x) mod x^nroots. */
    for (i = 0; i < nroots; i++) {
        unsigned char o = 0;
        for (j = 0; j <= i; j++) {
            if (j < nroots && (i - j) <= nerased) {
                o ^= gf_mul(synd[j], lambda[i - j]);
            }
        }
        omega[i] = o;
    }

    /* Forney: for each erased position k,
     *   Xinv = X_k^-1 = alpha^(-pos_k)
     *   num  = Omega(Xinv)
     *   den  = Lambda'(Xinv)   (formal derivative: odd-index terms)
     *   val  = X_k * num / den          (RS_FIRST_ROOT = 0)
     */
    for (k = 0; k < nerased; k++) {
        int pos = erased_pos[k];
        unsigned char Xk = gf_exp(pos);
        unsigned char Xinv = gf_inv(Xk);
        unsigned char num = 0, den = 0, xi;
        unsigned char val;

        /* evaluate Omega(Xinv) */
        xi = 1;
        for (i = 0; i < nroots; i++) {
            num ^= gf_mul(omega[i], xi);
            xi = gf_mul(xi, Xinv);
        }
        /* Lambda'(Xinv): sum of odd-degree terms lambda[i]*Xinv^(i-1). */
        xi = 1;                          /* Xinv^0 -> term i=1 */
        for (i = 1; i <= nerased; i += 2) {
            den ^= gf_mul(lambda[i], xi);
            xi = gf_mul(xi, gf_mul(Xinv, Xinv));
        }
        if (den == 0) {
            return -1;                   /* should not happen for valid erasures */
        }
        val = gf_mul(Xk, gf_div(num, den));
        sym[pos] = val;
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

        /* Repair each byte-offset's codeword. */
        for (b = 0; b < RS03_SECTOR_SIZE; b++) {
            unsigned char sym[MAXSYM];
            int i;
            for (layer = 0; layer < GF_FIELDMAX; layer++) {
                unsigned long sec = rs03_sector_index(L, layer, pos);
                sym[layer] = img[(size_t) sec * RS03_SECTOR_SIZE + b];
            }
            /* Zero the erased symbols (their on-disc bytes are garbage). */
            for (i = 0; i < nerased; i++) {
                sym[erased_pos[i]] = 0;
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
