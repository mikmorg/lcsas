/*
 * scrypt.h -- RFC 7914 scrypt KDF.
 *
 * Restic parameters: N = 32768, r = 8, p = 1, dklen = 64.
 * Output is split: first 32 bytes = AES-256 encrypt key, next 16 =
 * mac_k, next 16 = mac_r.  See docs/RESTIC_FORMAT_SPEC.md.
 *
 * Memory footprint: 128 * r * N bytes = 32 MiB for restic's default.
 */
#ifndef LCSAS_SCRYPT_H
#define LCSAS_SCRYPT_H

#include <stddef.h>

/*
 * Returns 0 on success, non-zero on parameter or allocation failure.
 * `dk_buf` must point to dklen writable bytes.
 */
int lcsas_scrypt(const unsigned char *pw, size_t pwlen,
                 const unsigned char *salt, size_t saltlen,
                 unsigned long N, unsigned long r, unsigned long p,
                 unsigned char *dk, size_t dklen);

#endif
