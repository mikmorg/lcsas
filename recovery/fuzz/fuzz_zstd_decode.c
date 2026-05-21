/*
 * fuzz_zstd_decode.c -- LibFuzzer harness for lcsas_zstd_decode.
 *
 * Exercises both the probe path (out=NULL → return decompressed size) and
 * the decompress path.  Enforces the 256 MiB cap from repo.c:346:
 *   if (dsz <= 0 || dsz > (long)(256 * 1024 * 1024)) { ... }
 * so the fuzzer will OOM-abort rather than silently allocate unbounded memory.
 *
 * Compile:
 *   make -C recovery fuzz-zstd-smoke
 */
#include "zstd_dec.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>

#define MAX_DECOMP_CAP (256 * 1024 * 1024)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    long probed;
    void *buf;
    long got;

    /* Probe path: returns size without writing. */
    probed = lcsas_zstd_decode(data, size, NULL, 0);
    if (probed <= 0 || probed > (long)MAX_DECOMP_CAP) return 0;

    buf = malloc((size_t)probed);
    if (buf == NULL) return 0;

    /* Decompress path. */
    got = lcsas_zstd_decode(data, size, buf, (size_t)probed);
    (void)got;

    free(buf);
    return 0;
}
