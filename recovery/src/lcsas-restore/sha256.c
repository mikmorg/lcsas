/*
 * sha256.c -- FIPS 180-4 SHA-256.
 *
 * Strict C89.  Public-domain reference style.
 *
 * Spec: FIPS PUB 180-4 sections 5 and 6.2.
 *
 * All 32-bit arithmetic is done on `unsigned long` masked to 32 bits;
 * we do not assume `uint32_t` is available (it is C99).
 */
#include "sha256.h"

#define ROTR(x, n) (((x) >> (n)) | (((x) << (32 - (n))) & 0xFFFFFFFFUL))
#define CH(x, y, z)  (((x) & (y)) ^ ((~(x)) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define BSIG0(x) (ROTR((x),  2) ^ ROTR((x), 13) ^ ROTR((x), 22))
#define BSIG1(x) (ROTR((x),  6) ^ ROTR((x), 11) ^ ROTR((x), 25))
#define SSIG0(x) (ROTR((x),  7) ^ ROTR((x), 18) ^ ((x) >>  3))
#define SSIG1(x) (ROTR((x), 17) ^ ROTR((x), 19) ^ ((x) >> 10))
#define M32(x) ((x) & 0xFFFFFFFFUL)

static const unsigned long K[64] = {
    0x428A2F98UL, 0x71374491UL, 0xB5C0FBCFUL, 0xE9B5DBA5UL,
    0x3956C25BUL, 0x59F111F1UL, 0x923F82A4UL, 0xAB1C5ED5UL,
    0xD807AA98UL, 0x12835B01UL, 0x243185BEUL, 0x550C7DC3UL,
    0x72BE5D74UL, 0x80DEB1FEUL, 0x9BDC06A7UL, 0xC19BF174UL,
    0xE49B69C1UL, 0xEFBE4786UL, 0x0FC19DC6UL, 0x240CA1CCUL,
    0x2DE92C6FUL, 0x4A7484AAUL, 0x5CB0A9DCUL, 0x76F988DAUL,
    0x983E5152UL, 0xA831C66DUL, 0xB00327C8UL, 0xBF597FC7UL,
    0xC6E00BF3UL, 0xD5A79147UL, 0x06CA6351UL, 0x14292967UL,
    0x27B70A85UL, 0x2E1B2138UL, 0x4D2C6DFCUL, 0x53380D13UL,
    0x650A7354UL, 0x766A0ABBUL, 0x81C2C92EUL, 0x92722C85UL,
    0xA2BFE8A1UL, 0xA81A664BUL, 0xC24B8B70UL, 0xC76C51A3UL,
    0xD192E819UL, 0xD6990624UL, 0xF40E3585UL, 0x106AA070UL,
    0x19A4C116UL, 0x1E376C08UL, 0x2748774CUL, 0x34B0BCB5UL,
    0x391C0CB3UL, 0x4ED8AA4AUL, 0x5B9CCA4FUL, 0x682E6FF3UL,
    0x748F82EEUL, 0x78A5636FUL, 0x84C87814UL, 0x8CC70208UL,
    0x90BEFFFAUL, 0xA4506CEBUL, 0xBEF9A3F7UL, 0xC67178F2UL
};

static void
sha256_compress(unsigned long h[8], const unsigned char block[64])
{
    unsigned long w[64];
    unsigned long a, b, c, d, e, f, g, hh;
    unsigned long t1, t2;
    int i;

    for (i = 0; i < 16; i++) {
        w[i] = ((unsigned long)block[i * 4 + 0] << 24)
             | ((unsigned long)block[i * 4 + 1] << 16)
             | ((unsigned long)block[i * 4 + 2] <<  8)
             | ((unsigned long)block[i * 4 + 3]);
    }
    for (i = 16; i < 64; i++) {
        w[i] = M32(SSIG1(w[i - 2]) + w[i - 7] + SSIG0(w[i - 15]) + w[i - 16]);
    }

    a = h[0]; b = h[1]; c = h[2]; d = h[3];
    e = h[4]; f = h[5]; g = h[6]; hh = h[7];

    for (i = 0; i < 64; i++) {
        t1 = M32(hh + BSIG1(e) + CH(e, f, g) + K[i] + w[i]);
        t2 = M32(BSIG0(a) + MAJ(a, b, c));
        hh = g;
        g = f;
        f = e;
        e = M32(d + t1);
        d = c;
        c = b;
        b = a;
        a = M32(t1 + t2);
    }

    h[0] = M32(h[0] + a); h[1] = M32(h[1] + b);
    h[2] = M32(h[2] + c); h[3] = M32(h[3] + d);
    h[4] = M32(h[4] + e); h[5] = M32(h[5] + f);
    h[6] = M32(h[6] + g); h[7] = M32(h[7] + hh);
}

void
lcsas_sha256_init(lcsas_sha256_ctx *c)
{
    c->h[0] = 0x6A09E667UL; c->h[1] = 0xBB67AE85UL;
    c->h[2] = 0x3C6EF372UL; c->h[3] = 0xA54FF53AUL;
    c->h[4] = 0x510E527FUL; c->h[5] = 0x9B05688CUL;
    c->h[6] = 0x1F83D9ABUL; c->h[7] = 0x5BE0CD19UL;
    c->bitlen = 0;
    c->buflen = 0;
}

void
lcsas_sha256_update(lcsas_sha256_ctx *c, const void *data, size_t len)
{
    const unsigned char *p = (const unsigned char *)data;
    size_t n;

    c->bitlen += (unsigned long long)len * 8;

    if (c->buflen != 0) {
        n = 64 - c->buflen;
        if (n > len) n = len;
        {
            size_t i;
            for (i = 0; i < n; i++) c->buf[c->buflen + i] = p[i];
        }
        c->buflen += n;
        p += n;
        len -= n;
        if (c->buflen == 64) {
            sha256_compress(c->h, c->buf);
            c->buflen = 0;
        }
    }
    while (len >= 64) {
        sha256_compress(c->h, p);
        p += 64;
        len -= 64;
    }
    if (len > 0) {
        size_t i;
        for (i = 0; i < len; i++) c->buf[i] = p[i];
        c->buflen = len;
    }
}

void
lcsas_sha256_final(lcsas_sha256_ctx *c, unsigned char out[32])
{
    unsigned long long bits = c->bitlen;
    size_t i;
    unsigned char tail[72];
    size_t tail_len;
    size_t pad;

    /* Build the trailing block(s): 0x80, zero padding, 64-bit BE length. */
    pad = (c->buflen < 56) ? (56 - c->buflen) : (56 + 64 - c->buflen);
    tail[0] = 0x80;
    for (i = 1; i < pad; i++) tail[i] = 0;
    tail[pad + 0] = (unsigned char)(bits >> 56);
    tail[pad + 1] = (unsigned char)(bits >> 48);
    tail[pad + 2] = (unsigned char)(bits >> 40);
    tail[pad + 3] = (unsigned char)(bits >> 32);
    tail[pad + 4] = (unsigned char)(bits >> 24);
    tail[pad + 5] = (unsigned char)(bits >> 16);
    tail[pad + 6] = (unsigned char)(bits >>  8);
    tail[pad + 7] = (unsigned char)(bits      );
    tail_len = pad + 8;

    /* `bitlen` will be artificially incremented by update(); restore it. */
    c->bitlen = bits;
    lcsas_sha256_update(c, tail, tail_len);
    c->bitlen = bits;

    for (i = 0; i < 8; i++) {
        out[i * 4 + 0] = (unsigned char)(c->h[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(c->h[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(c->h[i] >>  8);
        out[i * 4 + 3] = (unsigned char)(c->h[i]      );
    }
}

void
lcsas_sha256(const void *data, size_t len, unsigned char out[32])
{
    lcsas_sha256_ctx c;
    lcsas_sha256_init(&c);
    lcsas_sha256_update(&c, data, len);
    lcsas_sha256_final(&c, out);
}
