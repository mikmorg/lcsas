/*
 * pbkdf2.c -- HMAC-SHA-256 + PBKDF2-HMAC-SHA-256.
 *
 * RFC 2104 (HMAC) and RFC 8018 §5.2 (PBKDF2).  Strict C89.
 */
#include "pbkdf2.h"
#include "sha256.h"

#include <stdlib.h>

#define BLK 64

void
lcsas_hmac_sha256(const unsigned char *key, size_t keylen,
                  const unsigned char *msg, size_t msglen,
                  unsigned char out[32])
{
    unsigned char k0[BLK];
    unsigned char ipad[BLK];
    unsigned char opad[BLK];
    unsigned char inner[32];
    lcsas_sha256_ctx c;
    size_t i;

    /* k0 = key zero-extended (or hashed if too long). */
    if (keylen > BLK) {
        lcsas_sha256(key, keylen, k0);
        for (i = 32; i < BLK; i++) k0[i] = 0;
    } else {
        for (i = 0; i < keylen; i++) k0[i] = key[i];
        for (i = keylen; i < BLK; i++) k0[i] = 0;
    }

    for (i = 0; i < BLK; i++) {
        ipad[i] = (unsigned char)(k0[i] ^ 0x36);
        opad[i] = (unsigned char)(k0[i] ^ 0x5c);
    }

    lcsas_sha256_init(&c);
    lcsas_sha256_update(&c, ipad, BLK);
    lcsas_sha256_update(&c, msg, msglen);
    lcsas_sha256_final(&c, inner);

    lcsas_sha256_init(&c);
    lcsas_sha256_update(&c, opad, BLK);
    lcsas_sha256_update(&c, inner, 32);
    lcsas_sha256_final(&c, out);
}

void
lcsas_pbkdf2_sha256(const unsigned char *pw, size_t pwlen,
                    const unsigned char *salt, size_t saltlen,
                    unsigned long iters,
                    unsigned char *dk, size_t dklen)
{
    unsigned long block;
    size_t blocks;
    size_t i, j;
    unsigned char u[32];
    unsigned char t[32];
    unsigned char counter[4];
    unsigned char *salt_ext;
    size_t to_copy;
    unsigned long k;

    /*
     * For each output block i = 1..L:
     *   U_1 = HMAC(P, S || INT(i))
     *   U_2 = HMAC(P, U_1)
     *   ...
     *   T_i = U_1 XOR U_2 XOR ... XOR U_c
     *
     * Build (S || INT(i)) in a heap buffer of size saltlen + 4.
     * Scrypt's final PBKDF2 call passes a salt of size 128 * r * p
     * bytes (up to ~tens of KiB), so a fixed stack buffer is not safe.
     */
    salt_ext = (unsigned char *)malloc(saltlen + 4);
    if (!salt_ext) return;
    for (j = 0; j < saltlen; j++) salt_ext[j] = salt[j];

    blocks = (dklen + 31) / 32;

    for (block = 1; block <= blocks; block++) {
        counter[0] = (unsigned char)(block >> 24);
        counter[1] = (unsigned char)(block >> 16);
        counter[2] = (unsigned char)(block >>  8);
        counter[3] = (unsigned char)(block      );
        for (i = 0; i < 4; i++) salt_ext[saltlen + i] = counter[i];

        lcsas_hmac_sha256(pw, pwlen, salt_ext, saltlen + 4, u);
        for (i = 0; i < 32; i++) t[i] = u[i];

        for (k = 1; k < iters; k++) {
            lcsas_hmac_sha256(pw, pwlen, u, 32, u);
            for (i = 0; i < 32; i++) t[i] ^= u[i];
        }

        to_copy = (block == blocks) ? (dklen - (block - 1) * 32) : 32;
        for (i = 0; i < to_copy; i++) {
            dk[(block - 1) * 32 + i] = t[i];
        }
    }

    free(salt_ext);
}
