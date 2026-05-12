/*
 * zstd_dec.h -- thin wrapper around vendored Facebook zstd decoder.
 *
 * The vendored zstd amalgamation is in recovery/vendored/zstd/.
 * It is BSD-3-Clause licensed and builds under -std=c99 (not c89,
 * see recovery/docs/BUILD.txt for the documented exception).
 */
#ifndef LCSAS_ZSTD_DEC_H
#define LCSAS_ZSTD_DEC_H

#include <stddef.h>

/*
 * Decompress a single zstd frame.  Returns the decompressed size on
 * success, or a negative value on error.  If `out` is NULL, returns
 * the decompressed size without writing.
 *
 *   src/src_len: input zstd frame.
 *   out/out_cap: output buffer / capacity.
 */
long lcsas_zstd_decode(const void *src, size_t src_len,
                       void *out, size_t out_cap);

/* Probe the decompressed size from a zstd frame header. */
unsigned long long lcsas_zstd_frame_size(const void *src, size_t src_len);

#endif
