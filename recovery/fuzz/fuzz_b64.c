/*
 * fuzz_b64.c -- LibFuzzer harness for lcsas_b64_decode.
 *
 * Feeds arbitrary bytes to the base64 decoder.  Exercises the probe path
 * (dst=NULL) and the decode path with a correctly-sized buffer.
 *
 * Compile:
 *   make -C recovery fuzz-b64-smoke
 */
#include "b64.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    long needed, got;
    unsigned char *buf;

    /* Probe: determine required output size. */
    needed = lcsas_b64_decode((const char *)data, size, NULL);
    if (needed <= 0) return 0;

    buf = (unsigned char *)malloc((size_t)needed);
    if (buf == NULL) return 0;

    /* Decode into caller-allocated buffer. */
    got = lcsas_b64_decode((const char *)data, size, buf);
    (void)got;

    free(buf);
    return 0;
}
