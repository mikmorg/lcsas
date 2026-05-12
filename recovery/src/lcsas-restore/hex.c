/*
 * hex.c -- lowercase hex encode/decode + constant-time compare.
 */
#include "hex.h"

static const char hex_chars[] = "0123456789abcdef";

void
lcsas_hex_encode(const unsigned char *in, size_t len, char *out)
{
    size_t i;
    for (i = 0; i < len; i++) {
        out[i * 2 + 0] = hex_chars[(in[i] >> 4) & 0x0F];
        out[i * 2 + 1] = hex_chars[ in[i]       & 0x0F];
    }
}

static int
nyb(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int
lcsas_hex_decode(const char *in, size_t outlen, unsigned char *out)
{
    size_t i;
    int hi, lo;
    for (i = 0; i < outlen; i++) {
        hi = nyb((unsigned char)in[i * 2 + 0]);
        lo = nyb((unsigned char)in[i * 2 + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i] = (unsigned char)((hi << 4) | lo);
    }
    return 0;
}

int
lcsas_ct_memcmp(const void *a, const void *b, size_t len)
{
    const unsigned char *aa = (const unsigned char *)a;
    const unsigned char *bb = (const unsigned char *)b;
    unsigned char diff = 0;
    size_t i;
    for (i = 0; i < len; i++) diff |= (unsigned char)(aa[i] ^ bb[i]);
    return (int)diff;
}
