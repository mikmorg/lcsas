/*
 * b64.h -- RFC 4648 base64 decode.
 *
 * Restic key files store the salt, IV, and ciphertext as base64
 * (standard alphabet, with '=' padding).  We only need the decoder.
 */
#ifndef LCSAS_B64_H
#define LCSAS_B64_H

#include <stddef.h>

/*
 * Decode `src` (length `slen`) into `dst`.  Returns the number of
 * bytes written on success, or -1 on invalid input.  Pass dst=NULL to
 * compute the required buffer length.
 */
long lcsas_b64_decode(const char *src, size_t slen,
                      unsigned char *dst);

#endif
