/*
 * test_zstd.c -- vendored zstd decoder round-trip.
 *
 * We don't have a zstd encoder in this build, so we hard-code a known
 * zstd frame produced by `zstd --ultra -22` on a deterministic input.
 * Verifies both probe and full decode paths.
 */
#include "zstd_dec.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

/*
 * zstd frame produced by:
 *   echo -n "Hello, LCSAS zstd! Hello, LCSAS zstd! Hello, LCSAS zstd!" \
 *       | zstd --ultra -22 -
 * (56-byte plaintext, repeated "Hello, LCSAS zstd! " * 3 minus trailing space)
 *
 * We embed the frame as a byte array.
 */
static const unsigned char ZSTD_FRAME[] = {
    0x28, 0xb5, 0x2f, 0xfd, 0x20, 0x38, 0xd5, 0x00, 0x00, 0xa0, 0x48, 0x65,
    0x6c, 0x6c, 0x6f, 0x2c, 0x20, 0x4c, 0x43, 0x53, 0x41, 0x53, 0x20, 0x7a,
    0x73, 0x74, 0x64, 0x21, 0x20, 0x48, 0x01, 0x00, 0x1a, 0x39, 0x99
};

static const char EXPECTED[] =
    "Hello, LCSAS zstd! Hello, LCSAS zstd! Hello, LCSAS zstd!";

int main(void)
{
    char out[256];
    long sz;
    long got;
    unsigned long long fsz;

    sz = lcsas_zstd_decode(ZSTD_FRAME, sizeof ZSTD_FRAME, NULL, 0);
    if (sz != (long)strlen(EXPECTED)) {
        fprintf(stderr, "FAIL probe: got %ld, want %zu\n", sz, strlen(EXPECTED));
        fails++;
    }

    fsz = lcsas_zstd_frame_size(ZSTD_FRAME, sizeof ZSTD_FRAME);
    if (fsz != strlen(EXPECTED)) {
        fprintf(stderr, "FAIL frame_size: got %llu, want %zu\n",
                (unsigned long long)fsz, strlen(EXPECTED));
        fails++;
    }

    got = lcsas_zstd_decode(ZSTD_FRAME, sizeof ZSTD_FRAME, out, sizeof out);
    if (got < 0 || (size_t)got != strlen(EXPECTED)
            || memcmp(out, EXPECTED, strlen(EXPECTED)) != 0) {
        fprintf(stderr, "FAIL decode: got=%ld\n", got);
        if (got >= 0) {
            fwrite(out, 1, (size_t)got, stderr);
            fputc('\n', stderr);
        }
        fails++;
    }

    /* Truncated input should fail cleanly. */
    if (lcsas_zstd_decode(ZSTD_FRAME, 4, out, sizeof out) >= 0) {
        fprintf(stderr, "FAIL: truncated frame should error\n");
        fails++;
    }

    if (fails == 0) printf("test_zstd: OK\n");
    return fails ? 1 : 0;
}
