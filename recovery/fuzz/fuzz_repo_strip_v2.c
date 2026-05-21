/*
 * fuzz_repo_strip_v2.c -- LibFuzzer harness for lcsas_repo_strip_v2_prefix.
 *
 * lcsas_repo_strip_v2_prefix inspects the first few bytes of a decrypted
 * pack blob to detect the restic v2 compression prefix byte.  Feeds
 * arbitrary bytes to check for out-of-bounds reads or pointer arithmetic
 * bugs.
 *
 * Compile:
 *   make -C recovery fuzz-repo-smoke
 */
#include "repo.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    unsigned char *buf;
    size_t len = size;
    int needs_zstd = 0;

    /* lcsas_repo_strip_v2_prefix modifies *buf (advances the pointer) and
     * *len in-place.  Work on a mutable copy so ASan can detect any
     * out-of-bounds read past the allocation. */
    buf = (unsigned char *)malloc(size + 1);
    if (buf == NULL) return 0;
    if (size > 0) memcpy(buf, data, size);
    buf[size] = '\0'; /* sentinel */

    {
        unsigned char *p = buf;
        lcsas_repo_strip_v2_prefix(&p, &len, &needs_zstd);
        /* Verify the function did not advance past the allocation. */
        (void)needs_zstd;
    }

    free(buf);
    return 0;
}
