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

/*
 * Decode-or-probe.  When `out == NULL` we return the upper bound on
 * the decompressed size so the caller can allocate.
 *
 * Restic-format index files (and several other rustic-format
 * artefacts) are emitted with zstd's "single-segment" framing flag
 * unset and no content-size field in the header.  That makes
 * ZSTD_getFrameContentSize return ZSTD_CONTENTSIZE_UNKNOWN — which
 * the old probe path treated as a fatal error, breaking restore on
 * every multi-tenant disc we shipped.
 *
 * The fix: fall back to ZSTD_decompressBound, which derives a safe
 * upper bound from the frame block headers without needing the
 * content-size hint.  The caller allocates that much, then the
 * actual decode returns the true size, which the caller uses.
 */
long
lcsas_zstd_decode(const void *src, size_t src_len,
                  void *out, size_t out_cap)
{
    size_t result;

    if (out == NULL) {
        /* Probe size only. */
        unsigned long long s = ZSTD_getFrameContentSize(src, src_len);
        if (s == ZSTD_CONTENTSIZE_ERROR) {
            return -1;
        }
        if (s == ZSTD_CONTENTSIZE_UNKNOWN) {
            /* Restic v2 index frames omit the content-size hint;
             * derive a safe upper bound from block headers. */
            size_t bound = ZSTD_decompressBound(src, src_len);
            if (ZSTD_isError(bound)) return -1;
            return (long)bound;
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
    if (s == ZSTD_CONTENTSIZE_ERROR) {
        return 0;
    }
    if (s == ZSTD_CONTENTSIZE_UNKNOWN) {
        size_t bound = ZSTD_decompressBound(src, src_len);
        if (ZSTD_isError(bound)) return 0;
        return (unsigned long long)bound;
    }
    return s;
}
