/*
 * zstd_dec.c -- thin wrapper around vendored zstd v1.5.6 decoder.
 *
 * The vendored amalgamation defines its own public API in zstd.h;
 * we expose a stripped-down restic-specific interface here.
 *
 * The vendored decoder is built as a separate translation unit (see
 * Makefile) with -std=gnu99 to accommodate its inline / __VA_ARGS__
 * usage; the lcsas-restore code calling it remains strict C89.
 */
#include "zstd_dec.h"
#include "../../vendored/zstd/zstd.h"

long
lcsas_zstd_decode(const void *src, size_t src_len,
                  void *out, size_t out_cap)
{
    size_t result;

    if (out == NULL) {
        /* Probe size only. */
        unsigned long long s = ZSTD_getFrameContentSize(src, src_len);
        if (s == ZSTD_CONTENTSIZE_ERROR || s == ZSTD_CONTENTSIZE_UNKNOWN) {
            return -1;
        }
        return (long)s;
    }

    result = ZSTD_decompress(out, out_cap, src, src_len);
    if (ZSTD_isError(result)) return -1;
    return (long)result;
}

unsigned long long
lcsas_zstd_frame_size(const void *src, size_t src_len)
{
    unsigned long long s = ZSTD_getFrameContentSize(src, src_len);
    if (s == ZSTD_CONTENTSIZE_ERROR || s == ZSTD_CONTENTSIZE_UNKNOWN) {
        return 0;
    }
    return s;
}
