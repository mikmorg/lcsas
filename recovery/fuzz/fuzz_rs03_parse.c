/*
 * fuzz_rs03_parse.c -- LibFuzzer harness for the RS03 parser/decoder
 * (recovery/src/lcsas-ecc/rs03.c), the in-house RS03 ECC verify/repair
 * codec [FMT-01].
 *
 * rs03_parse() reads an untrusted disc image off a scratched optical
 * medium: it locates the "*dvdisaster*" ECC header, parses the
 * geometry (ndata/nroots/sectors_per_layer/data_sectors), and derives
 * the layout.  On the recovery path this input is fully attacker- /
 * corruption-controlled (a bit-rotted or maliciously crafted image), so
 * the parser and the verify/fix decoders must never read out of bounds,
 * over-/under-flow on the geometry arithmetic, or crash -- they must
 * fail cleanly.  This harness feeds arbitrary bytes through parse →
 * verify → fix and lets ASan/UBSan catch any memory or UB defect.
 *
 * Compile / run:
 *   make -C recovery fuzz-rs03-smoke      # 60 s
 *   make -C recovery fuzz-rs03            # 30 min
 */
#include "rs03.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

/* Cap the worked-on image so a fuzzer-chosen geometry can't make the
 * decoder allocate/scan absurd amounts; large enough to reach every
 * branch in parse/verify/fix on realistic small images. */
#define MAX_IMG (4u * 1024u * 1024u)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    unsigned char *img;
    unsigned char *bad;
    rs03_layout    L;

    if (size > MAX_IMG) {
        size = MAX_IMG;
    }

    /* Mutable copy so rs03_fix (writes in place) and ASan can see the
     * exact allocation bounds. */
    img = (unsigned char *)malloc(size + 1u);
    if (img == NULL) {
        return 0;
    }
    if (size > 0u) {
        memcpy(img, data, size);
    }
    img[size] = 0u; /* sentinel */

    memset(&L, 0, sizeof(L));
    if (rs03_parse(img, size, &L) == 0) {
        /* Parsed a (possibly bogus but self-consistent) geometry.  The
         * verify/fix paths must tolerate it.  total_sectors comes from
         * fuzzer-chosen fields, so guard the erasure-flag allocation. */
        if (L.total_sectors > 0u && L.total_sectors <= MAX_IMG) {
            bad = (unsigned char *)malloc((size_t)L.total_sectors);
            if (bad != NULL) {
                long ndmg;
                memset(bad, 0, (size_t)L.total_sectors);
                ndmg = rs03_verify(img, size, &L, bad);
                /* rs03_fix requires a full-medium-sized buffer; only
                 * call it when the image is already that big, matching
                 * the contract in rs03.h (caller zero-extends).  This
                 * still fuzzes the in-place decoder on conforming
                 * inputs without inventing a giant buffer here. */
                if (ndmg >= 0 &&
                    (size_t)L.total_sectors * RS03_SECTOR_SIZE <= size) {
                    (void)rs03_fix(img, size, &L, bad);
                }
            }
            free(bad);
        }
    }

    free(img);
    return 0;
}
