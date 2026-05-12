/*
 * pbkdf2.h -- HMAC-SHA-256 and PBKDF2-HMAC-SHA-256 (RFC 8018 §5.2).
 *
 * Used as scrypt's outer and inner KDF stages (RFC 7914 §6 step 1
 * and step 7).
 */
#ifndef LCSAS_PBKDF2_H
#define LCSAS_PBKDF2_H

#include <stddef.h>

void lcsas_hmac_sha256(const unsigned char *key, size_t keylen,
                       const unsigned char *msg, size_t msglen,
                       unsigned char out[32]);

void lcsas_pbkdf2_sha256(const unsigned char *pw, size_t pwlen,
                         const unsigned char *salt, size_t saltlen,
                         unsigned long iters,
                         unsigned char *dk, size_t dklen);

#endif
