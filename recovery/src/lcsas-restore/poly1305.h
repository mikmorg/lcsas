/*
 * poly1305.h -- Poly1305-AES MAC (Bernstein original construction).
 *
 * NOT RFC 8439 ChaCha20-Poly1305!  This is the *original* Poly1305
 * defined by Bernstein in "The Poly1305-AES message-authentication
 * code", FSE 2005, where the per-message s value is derived as
 * s = AES-128-ECB(k, nonce).
 *
 * This matches restic's MAC construction.  See
 * src/lcsas/restore/restic_fallback.py:_poly1305_mac for the reference
 * Python implementation and tests/unit/test_restic_fallback.py:270 for
 * its test vectors.
 *
 * Strict C89 with one well-known soft extension: `unsigned long long`
 * (universally available in any C compiler shipped post-1999).
 */
#ifndef LCSAS_POLY1305_H
#define LCSAS_POLY1305_H

#include <stddef.h>

/*
 * Compute a Poly1305-AES tag.
 *
 *   r_key:   16 bytes; will be internally clamped per the Poly1305 spec.
 *   s_key:   16 bytes; AES-128-ECB(mac_k, iv) computed by the caller.
 *   msg/len: arbitrary-length message.
 *   tag:     16-byte output.
 */
void lcsas_poly1305_mac(const unsigned char r_key[16],
                        const unsigned char s_key[16],
                        const unsigned char *msg,
                        size_t len,
                        unsigned char tag[16]);

#endif
