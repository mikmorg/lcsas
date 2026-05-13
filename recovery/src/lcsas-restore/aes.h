/*
 * aes.h -- FIPS 197 AES.
 *
 * Provides AES-128-ECB encrypt (one block) for Poly1305-AES, and
 * AES-256-CTR for restic blob encryption.  Decryption is not needed:
 * restic's "decrypt" is "encrypt the keystream and XOR with ciphertext",
 * which is what AES-256-CTR already does.
 *
 * Spec: FIPS PUB 197, "Advanced Encryption Standard (AES)", NIST 2001.
 * https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.197.pdf
 *
 * CTR: NIST SP 800-38A, "Recommendation for Block Cipher Modes of
 * Operation -- Methods and Techniques", appendix B.
 * The IV is treated as a 128-bit big-endian counter (matches restic).
 */
#ifndef LCSAS_AES_H
#define LCSAS_AES_H

#include <stddef.h>

#define LCSAS_AES_BLOCK_SIZE 16

/* AES-128 round keys: 11 round keys x 16 bytes = 176 bytes. */
typedef struct {
    unsigned char rk[176];
} lcsas_aes128_key;

/* AES-256 round keys: 15 round keys x 16 bytes = 240 bytes. */
typedef struct {
    unsigned char rk[240];
} lcsas_aes256_key;

void lcsas_aes128_set_key(lcsas_aes128_key *k, const unsigned char key[16]);
void lcsas_aes128_encrypt(const lcsas_aes128_key *k,
                          const unsigned char in[16],
                          unsigned char out[16]);

void lcsas_aes256_set_key(lcsas_aes256_key *k, const unsigned char key[32]);
void lcsas_aes256_encrypt(const lcsas_aes256_key *k,
                          const unsigned char in[16],
                          unsigned char out[16]);

/*
 * AES-256-CTR.  `iv` is a 16-byte counter, incremented big-endian for each
 * block, matching restic's behavior (see src/lcsas/restore/_aes_pure.py).
 * `out` may alias `in`.  `len` may be any byte length.
 */
void lcsas_aes256_ctr(const lcsas_aes256_key *k,
                      const unsigned char iv[16],
                      const unsigned char *in,
                      unsigned char *out,
                      size_t len);

#endif
