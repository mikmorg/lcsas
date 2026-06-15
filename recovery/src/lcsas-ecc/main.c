/*
 * main.c -- lcsas-ecc CLI: in-house dvdisaster RS03 verify/repair.
 *
 * Usage:
 *   lcsas-ecc info    <image>            print RS03 geometry
 *   lcsas-ecc verify  <image>            scan CRC layer, report damage
 *   lcsas-ecc fix     <image> [--out F]  erasure-repair (in place or to F)
 *   lcsas-ecc augment <image> [--out F]  write RS03-augmented full-medium
 *                                        image (encode; in place or to F)
 *
 * Exit codes:
 *   0  success (verify: no damage; fix: fully repaired)
 *   1  damage found (verify) / uncorrectable codewords remain (fix)
 *   2  no RS03 ECC header / not an augmented image
 *   3  usage / I/O error
 *
 * The repaired image is written back in place by default so that the
 * recovery scripts can simply re-mount and re-extract.  --out writes a
 * repaired copy and leaves the input untouched.
 *
 * C89; self-contained (stdio/stdlib/string only).  Built and cross-
 * built exactly like lcsas-keyshare.
 */
#include "rs03.h"
#include "gf256.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int slurp(const char *path, unsigned char **buf, size_t *len)
{
    FILE *f = fopen(path, "rb");
    long sz;
    unsigned char *b;
    size_t got;

    if (!f) {
        fprintf(stderr, "lcsas-ecc: cannot open %s\n", path);
        return -1;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (sz = ftell(f)) < 0
        || fseek(f, 0, SEEK_SET) != 0) {
        fprintf(stderr, "lcsas-ecc: cannot size %s\n", path);
        fclose(f);
        return -1;
    }
    b = (unsigned char *) malloc((size_t) sz ? (size_t) sz : 1);
    if (!b) {
        fprintf(stderr, "lcsas-ecc: out of memory\n");
        fclose(f);
        return -1;
    }
    got = fread(b, 1, (size_t) sz, f);
    fclose(f);
    if (got != (size_t) sz) {
        fprintf(stderr, "lcsas-ecc: short read on %s\n", path);
        free(b);
        return -1;
    }
    *buf = b;
    *len = (size_t) sz;
    return 0;
}

static int dump(const char *path, const unsigned char *buf, size_t len)
{
    FILE *f = fopen(path, "wb");
    size_t put;
    if (!f) {
        fprintf(stderr, "lcsas-ecc: cannot write %s\n", path);
        return -1;
    }
    put = fwrite(buf, 1, len, f);
    if (fclose(f) != 0 || put != len) {
        fprintf(stderr, "lcsas-ecc: short write on %s\n", path);
        return -1;
    }
    return 0;
}

static void print_info(const rs03_layout *L)
{
    printf("RS03 augmented image\n");
    printf("  data sectors      : %lu\n", L->data_sectors);
    printf("  ndata             : %d\n", L->ndata);
    printf("  nroots            : %d\n", L->nroots);
    printf("  sectors per layer : %lu\n", L->sectors_per_layer);
    printf("  total sectors     : %lu\n", L->total_sectors);
    printf("  ecc header pos    : %lu\n", L->ecc_header_pos);
    printf("  first CRC sector  : %lu\n", L->first_crc_pos);
    printf("  first ECC sector  : %lu\n", L->first_ecc_pos);
    printf("  redundancy        : %.1f%%\n",
           100.0 * (double) L->nroots / (double) L->ndata);
}

static int cmd_info(const char *path)
{
    unsigned char *img;
    size_t len;
    rs03_layout L;
    if (slurp(path, &img, &len) != 0) {
        return 3;
    }
    if (rs03_parse(img, len, &L) != 0) {
        fprintf(stderr, "lcsas-ecc: no RS03 ECC header in %s\n", path);
        free(img);
        return 2;
    }
    print_info(&L);
    free(img);
    return 0;
}

static int cmd_verify(const char *path)
{
    unsigned char *img, *bad;
    size_t len;
    rs03_layout L;
    long ndamaged;

    if (slurp(path, &img, &len) != 0) {
        return 3;
    }
    if (rs03_parse(img, len, &L) != 0) {
        fprintf(stderr, "lcsas-ecc: no RS03 ECC header in %s\n", path);
        free(img);
        return 2;
    }
    bad = (unsigned char *) malloc((size_t) L.total_sectors);
    if (!bad) {
        fprintf(stderr, "lcsas-ecc: out of memory\n");
        free(img);
        return 3;
    }
    ndamaged = rs03_verify(img, len, &L, bad);
    free(bad);
    free(img);
    if (ndamaged < 0) {
        fprintf(stderr, "lcsas-ecc: structural error verifying %s\n", path);
        return 3;
    }
    if (ndamaged == 0) {
        printf("OK: %s -- no damaged data sectors\n", path);
        return 0;
    }
    printf("DAMAGE: %s -- %ld damaged data sector(s); "
           "run 'lcsas-ecc fix %s' to repair\n", path, ndamaged, path);
    return 1;
}

static int cmd_fix(const char *path, const char *out)
{
    unsigned char *img, *bad, *work;
    size_t len, need;
    rs03_layout L;
    long ndamaged, uncorrectable, recheck;

    if (slurp(path, &img, &len) != 0) {
        return 3;
    }
    if (rs03_parse(img, len, &L) != 0) {
        fprintf(stderr, "lcsas-ecc: no RS03 ECC header in %s\n", path);
        free(img);
        return 2;
    }

    bad = (unsigned char *) malloc((size_t) L.total_sectors);
    if (!bad) {
        fprintf(stderr, "lcsas-ecc: out of memory\n");
        free(img);
        return 3;
    }
    ndamaged = rs03_verify(img, len, &L, bad);
    if (ndamaged < 0) {
        fprintf(stderr, "lcsas-ecc: structural error verifying %s\n", path);
        free(bad);
        free(img);
        return 3;
    }
    if (ndamaged == 0) {
        printf("OK: %s -- no repair needed\n", path);
        free(bad);
        free(img);
        return 0;
    }

    /* The decoder needs the full medium in memory.  A short/truncated
     * image is zero-extended to total_sectors (the missing tail is all
     * erasures already flagged by verify). */
    need = (size_t) L.total_sectors * RS03_SECTOR_SIZE;
    if (len >= need) {
        work = img;
    } else {
        work = (unsigned char *) malloc(need);
        if (!work) {
            fprintf(stderr, "lcsas-ecc: out of memory\n");
            free(bad);
            free(img);
            return 3;
        }
        memcpy(work, img, len);
        memset(work + len, 0, need - len);
        free(img);
        img = work;
    }

    uncorrectable = rs03_fix(work, need, &L, bad);
    if (uncorrectable < 0) {
        fprintf(stderr, "lcsas-ecc: structural error repairing %s\n", path);
        free(bad);
        free(work);
        return 3;
    }

    /* Re-verify the repaired data region. */
    recheck = rs03_verify(work, need, &L, bad);
    free(bad);

    if (uncorrectable > 0 || recheck != 0) {
        fprintf(stderr,
                "lcsas-ecc: %s -- %ld uncorrectable codeword(s); "
                "damage exceeds RS03 capacity, NOT writing a partial repair\n",
                path, uncorrectable > 0 ? uncorrectable : recheck);
        free(work);
        return 1;
    }

    if (dump(out ? out : path, work, need) != 0) {
        free(work);
        return 3;
    }
    printf("REPAIRED: %s -- %ld sector(s) reconstructed -> %s\n",
           path, ndamaged, out ? out : path);
    free(work);
    return 0;
}

static int cmd_augment(const char *path, const char *out)
{
    unsigned char *data, *aug;
    size_t data_len, aug_len;
    unsigned int in_last;
    int rc;

    if (slurp(path, &data, &data_len) != 0) {
        return 3;
    }
    if (data_len == 0) {
        fprintf(stderr, "lcsas-ecc: %s is empty, cannot augment\n", path);
        free(data);
        return 3;
    }

    /* Bytes in the last data sector (2048 if the image is whole sectors). */
    in_last = (unsigned int) (data_len % RS03_SECTOR_SIZE);
    if (in_last == 0) {
        in_last = RS03_SECTOR_SIZE;
    }

    rc = rs03_augment(data, data_len, in_last, &aug, &aug_len);
    free(data);
    if (rc == -2) {
        fprintf(stderr, "lcsas-ecc: out of memory augmenting %s\n", path);
        return 3;
    }
    if (rc != 0) {
        fprintf(stderr,
            "lcsas-ecc: cannot augment %s (image too large for the RS03 "
            "medium ladder, or internal encode error)\n", path);
        return 3;
    }

    if (dump(out ? out : path, aug, aug_len) != 0) {
        free(aug);
        return 3;
    }
    printf("AUGMENTED: %s -- RS03 ECC written (%lu sectors total) -> %s\n",
           path, (unsigned long) (aug_len / RS03_SECTOR_SIZE),
           out ? out : path);
    free(aug);
    return 0;
}

int main(int argc, char **argv)
{
    gf_init();

    if (argc < 3) {
        fprintf(stderr,
            "usage: lcsas-ecc info|verify|fix|augment <image> "
            "[--out FILE]\n");
        return 3;
    }

    if (strcmp(argv[1], "info") == 0) {
        return cmd_info(argv[2]);
    }
    if (strcmp(argv[1], "verify") == 0) {
        return cmd_verify(argv[2]);
    }
    if (strcmp(argv[1], "fix") == 0) {
        const char *out = NULL;
        if (argc >= 5 && strcmp(argv[3], "--out") == 0) {
            out = argv[4];
        }
        return cmd_fix(argv[2], out);
    }
    if (strcmp(argv[1], "augment") == 0) {
        const char *out = NULL;
        if (argc >= 5 && strcmp(argv[3], "--out") == 0) {
            out = argv[4];
        }
        return cmd_augment(argv[2], out);
    }

    fprintf(stderr, "lcsas-ecc: unknown command '%s'\n", argv[1]);
    return 3;
}
