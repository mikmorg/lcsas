/*
 * test_ecc.c -- unit tests for the in-house RS03 codec (lcsas-ecc).
 *
 * Covers: GF(2^8) field over 0x187, CRC-32 vectors, RS03 header parse,
 * layout derivation, sector-index interleaving, and a full small-image
 * encode -> corrupt -> verify -> fix -> byte-identical round trip with
 * NO dvdisaster dependency (the always-on gate path).
 *
 * The encoder here is a TEST FIXTURE GENERATOR, not part of the shipped
 * tool: it builds a small, unpadded RS03-format image so verify/fix can
 * be exercised in milliseconds.  It uses the same erasure decoder as the
 * shipped fix path to fill the parity layers, which guarantees the
 * parity it writes is exactly what the decoder treats as valid.
 */
#include "gf256.h"
#include "rs03.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails = 0;

#define CHECK(cond, msg) do { \
    if (!(cond)) { fprintf(stderr, "FAIL: %s\n", msg); fails++; } \
} while (0)

/* Internal decoder is static in rs03.c; re-expose a tiny encoder here
 * that mirrors the decode math by treating parity positions as the
 * unknowns.  We reach the decoder indirectly: zero the parity sectors,
 * mark them "bad", and call rs03_fix -- the very routine under test --
 * which fills them to satisfy the RS constraints. */

/* ---- field tests ---------------------------------------------------- */

static void test_gf(void)
{
    int i;
    gf_init();

    /* a * a^-1 == 1 for every non-zero element. */
    for (i = 1; i < 256; i++) {
        unsigned char a = (unsigned char) i;
        CHECK(gf_mul(a, gf_inv(a)) == 1, "gf inverse");
    }
    /* multiplication is commutative and 1 is identity. */
    CHECK(gf_mul(0x53, 0xca) == gf_mul(0xca, 0x53), "gf commute");
    CHECK(gf_mul(0x53, 1) == 0x53, "gf identity");
    CHECK(gf_mul(0, 0x53) == 0, "gf zero");
    /* exp/log round trip. */
    for (i = 1; i < 256; i++) {
        unsigned char a = (unsigned char) i;
        CHECK(gf_exp(gf_log(a)) == a, "gf exp/log roundtrip");
    }
    /* alpha^255 == alpha^0 == 1 (field order). */
    CHECK(gf_exp(0) == 1, "gf alpha^0");
    CHECK(gf_exp(255) == 1, "gf alpha^255 wraps");
}

/* ---- crc tests ------------------------------------------------------ */

static void test_crc(void)
{
    /* dvdisaster's RS03 CRC is the standard reflected CRC-32 WITHOUT the
     * final inversion (src/crc32.c Crc32 returns the raw running register).
     * Standard CRC-32("123456789") = 0xCBF43926, so the dvdisaster value is
     * that with the final XOR undone: 0xCBF43926 ^ 0xffffffff = 0x340BC6D9.
     * The empty input yields the un-inverted init register, 0xFFFFFFFF.
     * (Conformance to dvdisaster proven against a real augmented image: a
     * 2048-byte zero sector's stored CRC is 0x0E174561.) */
    CHECK(rs03_crc32((const unsigned char *) "123456789", 9) == 0x340BC6D9UL,
          "crc32 check vector (dvdisaster, no final inversion)");
    CHECK(rs03_crc32((const unsigned char *) "", 0) == 0xFFFFFFFFUL,
          "crc32 empty (un-inverted init register)");
    {
        /* A 2048-byte zero sector matches dvdisaster's stored zero CRC. */
        unsigned char zero[RS03_SECTOR_SIZE];
        memset(zero, 0, sizeof(zero));
        CHECK(rs03_crc32(zero, RS03_SECTOR_SIZE) == 0x0E174561UL,
              "crc32 zero sector == dvdisaster 0x0E174561");
    }
}

/* ---- build a small RS03 image in memory ----------------------------- */
/*
 * Choose tiny geometry: sectors_per_layer = SPL, ndata = NDATA (incl
 * the CRC layer), nroots = 255 - NDATA.  data_sectors = D such that the
 * header sits at sector D and D < first_crc_pos.
 */
#define SPL    3
#define NDATA  85          /* 84 data layers + 1 CRC layer */
#define NROOTS (255 - NDATA)

static void put_u32le(unsigned char *p, unsigned long v)
{
    p[0] = (unsigned char) (v & 0xff);
    p[1] = (unsigned char) ((v >> 8) & 0xff);
    p[2] = (unsigned char) ((v >> 16) & 0xff);
    p[3] = (unsigned char) ((v >> 24) & 0xff);
}

/* Fill a synthetic augmented image; returns malloc'd buffer + len. */
static unsigned char *build_image(rs03_layout *L, size_t *len_out)
{
    unsigned long spl = SPL;
    unsigned long total = 255UL * spl;
    size_t len = (size_t) total * RS03_SECTOR_SIZE;
    unsigned char *img = (unsigned char *) calloc(len, 1);
    unsigned long first_crc = (unsigned long) (NDATA - 1) * spl;
    unsigned long data_sectors = first_crc - 2; /* header + 1 reserved, < first_crc */
    unsigned char *hdr;
    unsigned long pos, sec;
    int layer;
    unsigned char *bad;
    long uncorr;

    if (!img) { return NULL; }

    /* Fill ISO data sectors with deterministic pseudo-random bytes. */
    for (sec = 0; sec < data_sectors; sec++) {
        unsigned long s;
        for (s = 0; s < RS03_SECTOR_SIZE; s++) {
            img[(size_t) sec * RS03_SECTOR_SIZE + s] =
                (unsigned char) ((sec * 131 + s * 7 + 11) & 0xff);
        }
    }

    /* Write the ECC header at sector data_sectors. */
    hdr = img + (size_t) data_sectors * RS03_SECTOR_SIZE;
    memcpy(hdr + 0, RS03_COOKIE, RS03_COOKIE_LEN);
    memcpy(hdr + 12, "RS03", 4);
    put_u32le(hdr + 68, data_sectors);   /* sectors */
    put_u32le(hdr + 76, NDATA);          /* dataBytes  */
    put_u32le(hdr + 80, NROOTS);         /* eccBytes   */
    put_u32le(hdr + 120, spl);           /* sectorsPerLayer */

    if (rs03_parse(img, len, L) != 0) {
        free(img);
        return NULL;
    }

    /* Compute the CRC layer the way dvdisaster does (chain-back): for
     * every data-layer sector (0..ndata-2, ALL positions incl. the header
     * and padding sectors), store crc32 of the sector at codeword position
     * `pos` in CRC sector at position (pos-1)%spl, slot `layer`.  This
     * mirrors rs03.c read_stored_crc so the self round-trip stays valid. */
    for (layer = 0; layer < L->ndata - 1; layer++) {
        for (pos = 0; pos < L->sectors_per_layer; pos++) {
            unsigned long crc_pos;
            unsigned long crc_sec;
            unsigned long crc;
            sec = rs03_sector_index(L, layer, pos);
            crc = rs03_crc32(
                img + (size_t) sec * RS03_SECTOR_SIZE, RS03_SECTOR_SIZE);
            crc_pos = (pos + L->sectors_per_layer - 1)
                      % L->sectors_per_layer;
            crc_sec = L->first_crc_pos + crc_pos;
            put_u32le(img + (size_t) crc_sec * RS03_SECTOR_SIZE
                          + (size_t) layer * 4, crc);
        }
    }

    /* Compute the parity layers by treating them as erasures and letting
     * rs03_fix solve them.  Mark every parity sector "bad", run fix. */
    bad = (unsigned char *) calloc((size_t) total, 1);
    if (!bad) { free(img); return NULL; }
    for (layer = L->ndata; layer < 255; layer++) {
        for (pos = 0; pos < L->sectors_per_layer; pos++) {
            sec = rs03_sector_index(L, layer, pos);
            bad[sec] = 1;
        }
    }
    uncorr = rs03_fix(img, len, L, bad);
    free(bad);
    if (uncorr != 0) {
        free(img);
        return NULL;
    }

    *len_out = len;
    return img;
}

/* ---- layout test ---------------------------------------------------- */

static void test_layout(void)
{
    rs03_layout L;
    size_t len;
    unsigned char *img = build_image(&L, &len);
    CHECK(img != NULL, "build_image");
    if (!img) { return; }

    CHECK(L.ndata == NDATA, "layout ndata");
    CHECK(L.nroots == NROOTS, "layout nroots");
    CHECK(L.sectors_per_layer == SPL, "layout spl");
    CHECK(L.total_sectors == 255UL * SPL, "layout total");
    CHECK(L.first_crc_pos == (unsigned long)(NDATA - 1) * SPL, "layout firstcrc");
    CHECK(L.first_ecc_pos == L.first_crc_pos + SPL, "layout firstecc");

    /* interleaving: data layer 0 pos 0 -> sector 0. */
    CHECK(rs03_sector_index(&L, 0, 0) == 0, "idx data 0");
    CHECK(rs03_sector_index(&L, 0, 1) == 1, "idx data 1");
    /* CRC layer (ndata-1). */
    CHECK(rs03_sector_index(&L, NDATA - 1, 0) == L.first_crc_pos, "idx crc");
    /* first parity layer (ndata). */
    CHECK(rs03_sector_index(&L, NDATA, 0) == L.first_ecc_pos, "idx ecc");

    free(img);
}

/* ---- verify clean --------------------------------------------------- */

static void test_verify_clean(void)
{
    rs03_layout L;
    size_t len;
    unsigned char *img = build_image(&L, &len);
    unsigned char *bad;
    long n;
    if (!img) { fails++; return; }
    bad = (unsigned char *) malloc((size_t) L.total_sectors);
    n = rs03_verify(img, len, &L, bad);
    CHECK(n == 0, "verify clean image == 0 damage");
    free(bad);
    free(img);
}

/* ---- corrupt + repair round trip ------------------------------------ */

static void test_repair(void)
{
    rs03_layout L;
    size_t len;
    unsigned char *orig = build_image(&L, &len);
    unsigned char *img;
    unsigned char *bad;
    long ndmg, uncorr;
    unsigned long victims[3];
    int nv, i;

    if (!orig) { fails++; return; }
    img = (unsigned char *) malloc(len);
    memcpy(img, orig, len);

    /* Corrupt three distinct data sectors (different codeword positions
     * so each codeword has at most a few erasures -- well within nroots). */
    victims[0] = 0;                 /* layer 0, pos 0 */
    victims[1] = 1;                 /* layer 0, pos 1 */
    victims[2] = (unsigned long) 5 * SPL + 2; /* layer 5, pos 2 */
    nv = 3;
    for (i = 0; i < nv; i++) {
        size_t base = (size_t) victims[i] * RS03_SECTOR_SIZE;
        size_t j;
        for (j = 0; j < RS03_SECTOR_SIZE; j++) {
            img[base + j] ^= 0xff;  /* flip every byte */
        }
    }

    bad = (unsigned char *) malloc((size_t) L.total_sectors);
    ndmg = rs03_verify(img, len, &L, bad);
    CHECK(ndmg == nv, "verify detects all corrupted sectors");

    uncorr = rs03_fix(img, len, &L, bad);
    CHECK(uncorr == 0, "fix reports fully corrected");

    /* Repaired image must be byte-identical to the original. */
    CHECK(memcmp(img, orig, len) == 0, "repaired image byte-identical");

    /* And re-verify clean. */
    CHECK(rs03_verify(img, len, &L, bad) == 0, "re-verify clean after fix");

    free(bad);
    free(img);
    free(orig);
}

/* ---- uncorrectable: too many erasures in one codeword --------------- */

static void test_uncorrectable(void)
{
    rs03_layout L;
    size_t len;
    unsigned char *orig = build_image(&L, &len);
    unsigned char *bad;
    long uncorr;
    int layer;
    if (!orig) { fails++; return; }

    /* Erase nroots+1 layers at pos 0 -> beyond capacity. */
    bad = (unsigned char *) calloc((size_t) L.total_sectors, 1);
    for (layer = 0; layer <= L.nroots; layer++) {
        bad[rs03_sector_index(&L, layer, 0)] = 1;
    }
    uncorr = rs03_fix(orig, len, &L, bad);
    CHECK(uncorr > 0, "fix flags uncorrectable codeword");
    free(bad);
    free(orig);
}

/* ---- reject non-RS03 input ------------------------------------------ */

static void test_parse_reject(void)
{
    rs03_layout L;
    unsigned char buf[RS03_SECTOR_SIZE * 2];
    memset(buf, 0, sizeof(buf));
    CHECK(rs03_parse(buf, sizeof(buf), &L) != 0, "parse rejects non-RS03");
    CHECK(rs03_parse(buf, 10, &L) != 0, "parse rejects tiny input");
}

int main(void)
{
    test_gf();
    test_crc();
    test_layout();
    test_verify_clean();
    test_repair();
    test_uncorrectable();
    test_parse_reject();

    if (fails == 0) {
        printf("test_ecc: OK\n");
    }
    return fails ? 1 : 0;
}
