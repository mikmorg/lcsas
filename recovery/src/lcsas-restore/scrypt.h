/*
 * scrypt.h -- RFC 7914 scrypt KDF.
 *
 * Restic parameters: N = 32768, r = 8, p = 1, dklen = 64.
 * Output is split: first 32 bytes = AES-256 encrypt key, next 16 =
 * mac_k, next 16 = mac_r.  See docs/RESTIC_FORMAT_SPEC.md.
 *
 * Memory footprint: ~128 * r * N bytes = 32 MiB for restic's default.
 */
#ifndef LCSAS_SCRYPT_H
#define LCSAS_SCRYPT_H

#include <stddef.h>

/*
 * Limits on the cost parameters (#362, #470).  N, r and p come from the
 * repository key file and are used before any MAC check, so a corrupt
 * or hostile file must be refused quickly rather than run for hours or
 * exhaust memory.  The most restic/rustic write is N=2^20, r=8, p=1:
 * 2^23 of work and ~1 GiB.  These allow 4x that work, and 2 GiB in all.
 *
 *   work:   N * r * p                    <= LCSAS_SCRYPT_MAX_WORK
 *   memory: 128 * r * (N + p + 3) bytes  <= LCSAS_SCRYPT_MAX_MEM
 *           (V, B, and the X/T/Y scratch blocks)
 *
 * The tier-3 Python reader (src/lcsas/restore/restic_fallback.py) uses
 * the same limits; test_scrypt_limits_parity.py keeps them equal.
 */
#define LCSAS_SCRYPT_MAX_WORK (1UL << 25)
#define LCSAS_SCRYPT_MAX_MEM  (1UL << 31)

/*
 * Returns 1 if N is a power of two >= 2, r and p are >= 1, and both
 * limits above hold; 0 otherwise.  Never overflows unsigned long.
 */
int lcsas_scrypt_params_ok(unsigned long N, unsigned long r, unsigned long p);

/*
 * Returns 0 on success, -1 if lcsas_scrypt_params_ok() rejects N/r/p,
 * -2 on allocation failure.  `dk` must point to dklen writable bytes.
 */
int lcsas_scrypt(const unsigned char *pw, size_t pwlen,
                 const unsigned char *salt, size_t saltlen,
                 unsigned long N, unsigned long r, unsigned long p,
                 unsigned char *dk, size_t dklen);

#endif
