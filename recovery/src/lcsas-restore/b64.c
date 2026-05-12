/*
 * b64.c -- RFC 4648 base64 decode.
 *
 * Strict C89.  Skips '\n' and '\r' to tolerate wrapped input (Python's
 * `b64encode` output is unwrapped, but JSON parsers may inject
 * whitespace).
 */
#include "b64.h"

static int
val(unsigned char c)
{
    if (c >= 'A' && c <= 'Z') return (int)(c - 'A');
    if (c >= 'a' && c <= 'z') return (int)(c - 'a') + 26;
    if (c >= '0' && c <= '9') return (int)(c - '0') + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

long
lcsas_b64_decode(const char *src, size_t slen, unsigned char *dst)
{
    size_t i;
    unsigned long acc = 0;
    int bits = 0;
    long out = 0;
    int v;

    for (i = 0; i < slen; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c == '=' || c == ' ' || c == '\n' || c == '\r' || c == '\t') continue;
        v = val(c);
        if (v < 0) return -1;
        acc = (acc << 6) | (unsigned long)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (dst) dst[out] = (unsigned char)((acc >> bits) & 0xFFUL);
            out++;
        }
    }
    return out;
}
