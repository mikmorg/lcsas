/*
 * hex.h -- lowercase hex encode/decode.
 *
 * Restic blob IDs and pack file names are 64-char lowercase hex
 * (SHA-256).
 */
#ifndef LCSAS_HEX_H
#define LCSAS_HEX_H

#include <stddef.h>

/* Encode `len` bytes as 2*len ASCII hex chars in `out` (no NUL). */
void lcsas_hex_encode(const unsigned char *in, size_t len, char *out);

/*
 * Decode 2*outlen hex chars into `out`.  Returns 0 on success, -1 on
 * non-hex input.  Accepts both upper and lower case.
 */
int lcsas_hex_decode(const char *in, size_t outlen, unsigned char *out);

/* Compare two byte arrays in constant time.  Returns 0 if equal. */
int lcsas_ct_memcmp(const void *a, const void *b, size_t len);

#endif
