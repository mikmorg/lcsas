/*
 * sha256.h -- FIPS 180-4 SHA-256.
 *
 * Strict C89.  Zero dependencies beyond <stddef.h> / <stdint.h>.
 *
 * Spec: FIPS PUB 180-4, "Secure Hash Standard", National Institute of
 * Standards and Technology, August 2015.
 * https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.180-4.pdf
 */
#ifndef LCSAS_SHA256_H
#define LCSAS_SHA256_H

#include <stddef.h>

#define LCSAS_SHA256_DIGEST_SIZE 32
#define LCSAS_SHA256_BLOCK_SIZE  64

typedef struct {
    unsigned long h[8];          /* state, 8 x 32-bit words */
    unsigned long long bitlen;   /* total bits processed */
    unsigned char buf[64];       /* partial block */
    size_t buflen;
} lcsas_sha256_ctx;

void lcsas_sha256_init(lcsas_sha256_ctx *c);
void lcsas_sha256_update(lcsas_sha256_ctx *c, const void *data, size_t len);
void lcsas_sha256_final(lcsas_sha256_ctx *c, unsigned char out[32]);
void lcsas_sha256(const void *data, size_t len, unsigned char out[32]);

#endif
