/*
 * poly1305.c -- Poly1305-AES MAC.
 *
 * Implements h = ((h + n_i) * r) mod (2^130 - 5) using a radix-2^26
 * limb representation (5 limbs of 26 bits each, so 130 bits total).
 * Multiplications produce intermediates of at most 26+26 = 52 bits per
 * pair-product, which fit safely in `unsigned long long`.
 *
 * This follows the public-domain "poly1305-donna-32" structure by
 * Andrew Moon, simplified for the restic Poly1305-AES case where the
 * caller supplies the precomputed s = AES_k(nonce).
 *
 * The C standard relied upon is C89 + `unsigned long long` (C99
 * extension supported by every contemporary C compiler).  Documented in
 * recovery/docs/BUILD.txt.
 */
#include "poly1305.h"

#define ULL unsigned long long

static unsigned long
ld32_le(const unsigned char *p)
{
    return ((unsigned long)p[0])
         | ((unsigned long)p[1] <<  8)
         | ((unsigned long)p[2] << 16)
         | ((unsigned long)p[3] << 24);
}

void
lcsas_poly1305_mac(const unsigned char r_key[16],
                   const unsigned char s_key[16],
                   const unsigned char *msg,
                   size_t len,
                   unsigned char tag[16])
{
    /* Clamped r in radix-2^26 limbs (each <= 2^26 - 1). */
    unsigned long r0, r1, r2, r3, r4;
    /* h in radix-2^26 limbs. */
    unsigned long h0, h1, h2, h3, h4;
    /* Useful multiplications of r limbs by 5 (folded reduction). */
    unsigned long s1, s2, s3, s4;

    unsigned long t0, t1, t2, t3;
    ULL d0, d1, d2, d3, d4;
    unsigned long c;

    unsigned long g0, g1, g2, g3, g4;

    unsigned long f0, f1, f2, f3;
    ULL f;

    unsigned char block[16];
    size_t i;
    size_t n;

    /* Load r and clamp (mask 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF, LE). */
    t0 = ld32_le(r_key +  0);
    t1 = ld32_le(r_key +  4);
    t2 = ld32_le(r_key +  8);
    t3 = ld32_le(r_key + 12);

    r0 =   t0                         & 0x3FFFFFFUL;
    r1 = ((t0 >> 26) | (t1 <<  6))    & 0x3FFFF03UL;
    r2 = ((t1 >> 20) | (t2 << 12))    & 0x3FFC0FFUL;
    r3 = ((t2 >> 14) | (t3 << 18))    & 0x3F03FFFUL;
    r4 =  (t3 >>  8)                  & 0x00FFFFFUL;

    s1 = r1 * 5UL;
    s2 = r2 * 5UL;
    s3 = r3 * 5UL;
    s4 = r4 * 5UL;

    h0 = h1 = h2 = h3 = h4 = 0;

    /* Process full blocks. */
    while (len > 0) {
        n = (len >= 16) ? 16 : len;

        /* Build the block, padded with 0 then high bit set. */
        for (i = 0; i < n; i++) block[i] = msg[i];
        if (n < 16) {
            block[n] = 0x01;
            for (i = n + 1; i < 16; i++) block[i] = 0;
        }

        t0 = ld32_le(block +  0);
        t1 = ld32_le(block +  4);
        t2 = ld32_le(block +  8);
        t3 = ld32_le(block + 12);

        h0 += t0 & 0x3FFFFFFUL;
        h1 += ((t0 >> 26) | (t1 <<  6)) & 0x3FFFFFFUL;
        h2 += ((t1 >> 20) | (t2 << 12)) & 0x3FFFFFFUL;
        h3 += ((t2 >> 14) | (t3 << 18)) & 0x3FFFFFFUL;
        if (n == 16) {
            h4 += (t3 >> 8) | (1UL << 24);
        } else {
            /* Final partial block: high-bit was already inserted as 0x01. */
            h4 += (t3 >> 8);
        }

        /* h = h * r mod p */
        d0 = (ULL)h0 * r0 + (ULL)h1 * s4 + (ULL)h2 * s3 + (ULL)h3 * s2 + (ULL)h4 * s1;
        d1 = (ULL)h0 * r1 + (ULL)h1 * r0 + (ULL)h2 * s4 + (ULL)h3 * s3 + (ULL)h4 * s2;
        d2 = (ULL)h0 * r2 + (ULL)h1 * r1 + (ULL)h2 * r0 + (ULL)h3 * s4 + (ULL)h4 * s3;
        d3 = (ULL)h0 * r3 + (ULL)h1 * r2 + (ULL)h2 * r1 + (ULL)h3 * r0 + (ULL)h4 * s4;
        d4 = (ULL)h0 * r4 + (ULL)h1 * r3 + (ULL)h2 * r2 + (ULL)h3 * r1 + (ULL)h4 * r0;

        c  = (unsigned long)(d0 >> 26); h0 = (unsigned long)d0 & 0x3FFFFFFUL;
        d1 += c;
        c  = (unsigned long)(d1 >> 26); h1 = (unsigned long)d1 & 0x3FFFFFFUL;
        d2 += c;
        c  = (unsigned long)(d2 >> 26); h2 = (unsigned long)d2 & 0x3FFFFFFUL;
        d3 += c;
        c  = (unsigned long)(d3 >> 26); h3 = (unsigned long)d3 & 0x3FFFFFFUL;
        d4 += c;
        c  = (unsigned long)(d4 >> 26); h4 = (unsigned long)d4 & 0x3FFFFFFUL;
        h0 += c * 5UL;
        c  = h0 >> 26;                  h0 &= 0x3FFFFFFUL;
        h1 += c;

        msg += n;
        len -= n;
    }

    /* Final fold: h fully reduced mod p. */
    c = h1 >> 26; h1 &= 0x3FFFFFFUL; h2 += c;
    c = h2 >> 26; h2 &= 0x3FFFFFFUL; h3 += c;
    c = h3 >> 26; h3 &= 0x3FFFFFFUL; h4 += c;
    c = h4 >> 26; h4 &= 0x3FFFFFFUL; h0 += c * 5UL;
    c = h0 >> 26; h0 &= 0x3FFFFFFUL; h1 += c;

    /*
     * Compute h + 5 with carry through all limbs.  If the carry
     * propagates into bit 26 of g4 (i.e. spills past 2^130 - 1),
     * then h >= p; replace h with (h + 5) mod 2^130, which equals
     * h - p exactly.  This avoids width-dependent sign tricks.
     */
    g0 = h0 + 5UL;   c = g0 >> 26; g0 &= 0x3FFFFFFUL;
    g1 = h1 + c;     c = g1 >> 26; g1 &= 0x3FFFFFFUL;
    g2 = h2 + c;     c = g2 >> 26; g2 &= 0x3FFFFFFUL;
    g3 = h3 + c;     c = g3 >> 26; g3 &= 0x3FFFFFFUL;
    g4 = h4 + c;
    if (g4 & (1UL << 26)) {
        h0 = g0;
        h1 = g1;
        h2 = g2;
        h3 = g3;
        h4 = g4 & 0x3FFFFFFUL;
    }

    /*
     * Pack into 4 x 32-bit words.  Mask to 32 bits explicitly --
     * `unsigned long` may be 64 bits on this platform, in which case
     * `h1 << 26` would leak high bits into f0 and break the carry
     * propagation of the s-addition below.
     */
    f0 = ((h0      ) | (h1 << 26)) & 0xFFFFFFFFUL;
    f1 = ((h1 >>  6) | (h2 << 20)) & 0xFFFFFFFFUL;
    f2 = ((h2 >> 12) | (h3 << 14)) & 0xFFFFFFFFUL;
    f3 = ((h3 >> 18) | (h4 <<  8)) & 0xFFFFFFFFUL;

    /* Add s (little-endian, 4 x 32-bit). */
    f = (ULL)f0 + ld32_le(s_key +  0);
    f0 = (unsigned long)f & 0xFFFFFFFFUL;
    f = (ULL)f1 + ld32_le(s_key +  4) + (f >> 32);
    f1 = (unsigned long)f & 0xFFFFFFFFUL;
    f = (ULL)f2 + ld32_le(s_key +  8) + (f >> 32);
    f2 = (unsigned long)f & 0xFFFFFFFFUL;
    f = (ULL)f3 + ld32_le(s_key + 12) + (f >> 32);
    f3 = (unsigned long)f & 0xFFFFFFFFUL;

    tag[ 0] = (unsigned char)(f0      );
    tag[ 1] = (unsigned char)(f0 >>  8);
    tag[ 2] = (unsigned char)(f0 >> 16);
    tag[ 3] = (unsigned char)(f0 >> 24);
    tag[ 4] = (unsigned char)(f1      );
    tag[ 5] = (unsigned char)(f1 >>  8);
    tag[ 6] = (unsigned char)(f1 >> 16);
    tag[ 7] = (unsigned char)(f1 >> 24);
    tag[ 8] = (unsigned char)(f2      );
    tag[ 9] = (unsigned char)(f2 >>  8);
    tag[10] = (unsigned char)(f2 >> 16);
    tag[11] = (unsigned char)(f2 >> 24);
    tag[12] = (unsigned char)(f3      );
    tag[13] = (unsigned char)(f3 >>  8);
    tag[14] = (unsigned char)(f3 >> 16);
    tag[15] = (unsigned char)(f3 >> 24);
}
