/*
 * ecc_make_fixture.c -- write a tiny, UNPADDED RS03-augmented image to a
 * file, for the always-on no-dvdisaster ECC self-repair e2e test [FMT-01].
 *
 * This is a TEST FIXTURE GENERATOR, not shipped tooling.  It mirrors the
 * in-memory build_image() helper in test_ecc.c: lay down deterministic
 * data sectors, write the RS03 ECC header, compute the CRC layer, then
 * fill the parity layers by treating them as erasures and letting the
 * SHIPPED rs03_fix() solve them (so the parity is exactly what the
 * shipped decoder expects).  The result is a real RS03 image small
 * enough to repair in milliseconds -- unlike a dvdisaster-padded
 * full-medium image (~700 MB) -- so the repair path can be gated
 * always-on instead of opt-in.
 *
 * Usage:  ecc_make_fixture <out.img>
 * Exit:   0 on success, non-zero on error.
 *
 * Build (the e2e test compiles this on the fly):
 *   cc -std=c89 -I src/lcsas-ecc \
 *      tests/ecc_make_fixture.c src/lcsas-ecc/gf256.c src/lcsas-ecc/rs03.c \
 *      -o ecc_make_fixture
 */
#include "rs03.h"
#include "gf256.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Tiny geometry, identical to test_ecc.c. */
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

int main(int argc, char **argv)
{
    rs03_layout L;
    unsigned long spl = SPL;
    unsigned long total = 255UL * spl;
    size_t len = (size_t) total * RS03_SECTOR_SIZE;
    unsigned char *img;
    unsigned long first_crc = (unsigned long) (NDATA - 1) * spl;
    unsigned long data_sectors = first_crc - 2;
    unsigned char *hdr;
    unsigned long pos, sec;
    int layer;
    unsigned char *bad;
    long uncorr;
    FILE *f;

    if (argc != 2) {
        fprintf(stderr, "usage: %s <out.img>\n", argv[0]);
        return 2;
    }

    gf_init();   /* required before any rs03_fix encode/decode */

    img = (unsigned char *) calloc(len, 1);
    if (!img) { return 1; }

    for (sec = 0; sec < data_sectors; sec++) {
        unsigned long s;
        for (s = 0; s < RS03_SECTOR_SIZE; s++) {
            img[(size_t) sec * RS03_SECTOR_SIZE + s] =
                (unsigned char) ((sec * 131 + s * 7 + 11) & 0xff);
        }
    }

    hdr = img + (size_t) data_sectors * RS03_SECTOR_SIZE;
    memcpy(hdr + 0, RS03_COOKIE, RS03_COOKIE_LEN);
    memcpy(hdr + 12, "RS03", 4);
    put_u32le(hdr + 68, data_sectors);
    put_u32le(hdr + 76, NDATA);
    put_u32le(hdr + 80, NROOTS);
    put_u32le(hdr + 120, spl);

    if (rs03_parse(img, len, &L) != 0) {
        free(img);
        fprintf(stderr, "rs03_parse failed\n");
        return 1;
    }

    /* CRC layer the dvdisaster way (chain-back): every data-layer sector's
     * CRC at codeword position `pos` is stored in CRC sector (pos-1)%spl,
     * slot `layer` -- matching rs03.c read_stored_crc so verify is clean. */
    for (layer = 0; layer < L.ndata - 1; layer++) {
        for (pos = 0; pos < L.sectors_per_layer; pos++) {
            unsigned long crc;
            unsigned long crc_pos;
            unsigned long crc_sec;
            sec = rs03_sector_index(&L, layer, pos);
            crc = rs03_crc32(
                img + (size_t) sec * RS03_SECTOR_SIZE, RS03_SECTOR_SIZE);
            crc_pos = (pos + L.sectors_per_layer - 1) % L.sectors_per_layer;
            crc_sec = L.first_crc_pos + crc_pos;
            put_u32le(img + (size_t) crc_sec * RS03_SECTOR_SIZE
                          + (size_t) layer * 4, crc);
        }
    }

    bad = (unsigned char *) calloc((size_t) total, 1);
    if (!bad) { free(img); return 1; }
    for (layer = L.ndata; layer < 255; layer++) {
        for (pos = 0; pos < L.sectors_per_layer; pos++) {
            sec = rs03_sector_index(&L, layer, pos);
            bad[sec] = 1;
        }
    }
    uncorr = rs03_fix(img, len, &L, bad);
    free(bad);
    if (uncorr != 0) {
        free(img);
        fprintf(stderr, "rs03_fix(encode) left %ld uncorrectable\n", uncorr);
        return 1;
    }

    f = fopen(argv[1], "wb");
    if (!f) { free(img); fprintf(stderr, "cannot open %s\n", argv[1]); return 1; }
    if (fwrite(img, 1, len, f) != len) {
        fclose(f); free(img); fprintf(stderr, "short write\n"); return 1;
    }
    fclose(f);
    free(img);
    return 0;
}
