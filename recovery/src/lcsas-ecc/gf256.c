/*
 * gf256.c -- GF(2^8) arithmetic over primitive polynomial 0x187.
 *
 * Transcribed to match dvdisaster's src/galois.c table construction:
 * the generator alpha = 2, and reduction is modulo 0x187 once a power
 * reaches GF_FIELDSIZE.  This reproduces dvdisaster's exact field so
 * the decoded parity matches the bytes burned on the disc.
 */
#include "gf256.h"

static unsigned char alpha_to[GF_FIELDSIZE]; /* exp:  alpha^i           */
static int           index_of[GF_FIELDSIZE]; /* log:  index s.t. alpha^index = i */
static int           gf_ready = 0;

void gf_init(void)
{
    int i;
    int b;

    if (gf_ready) {
        return;
    }

    /* alpha_to[i] = alpha^i, with alpha = 2 and reduction mod 0x187. */
    b = 1;
    for (i = 0; i < GF_FIELDMAX; i++) {
        alpha_to[i] = (unsigned char) b;
        index_of[b] = i;
        b <<= 1;
        if (b & GF_FIELDSIZE) {
            b ^= GF_PRIM_POLY;
        }
        b &= 0xff;
    }
    /* alpha^255 == 1 == alpha^0; index_of[0] is the "log of zero"
     * sentinel (dvdisaster uses GF_FIELDMAX here so that adding logs
     * never accidentally lands on a valid index for a zero operand;
     * the gf_mul guard below makes the value moot, but keep it).
     * alpha_to[GF_ALPHA0] == 0 mirrors dvdisaster (galois.c:72) so the
     * ported decoder's `alpha_to[mod_fieldmax(...)]` matches exactly. */
    index_of[0] = GF_FIELDMAX;
    alpha_to[GF_ALPHA0] = 0;
    gf_ready = 1;
}

int gf_index_of(unsigned char a)
{
    return index_of[a];
}

unsigned char gf_alpha_to(int e)
{
    return alpha_to[e];
}

unsigned char gf_mul(unsigned char a, unsigned char b)
{
    int s;
    if (a == 0 || b == 0) {
        return 0;
    }
    s = index_of[a] + index_of[b];
    if (s >= GF_FIELDMAX) {
        s -= GF_FIELDMAX;
    }
    return alpha_to[s];
}

unsigned char gf_div(unsigned char a, unsigned char b)
{
    int s;
    if (a == 0 || b == 0) {
        return 0;
    }
    s = index_of[a] - index_of[b];
    if (s < 0) {
        s += GF_FIELDMAX;
    }
    return alpha_to[s];
}

unsigned char gf_inv(unsigned char a)
{
    int e;
    if (a == 0) {
        return 0;
    }
    e = GF_FIELDMAX - index_of[a];
    if (e >= GF_FIELDMAX) {
        e -= GF_FIELDMAX;   /* index_of[a]==0 -> e==255 wraps to 0 */
    }
    return alpha_to[e];
}

unsigned char gf_exp(int e)
{
    e %= GF_FIELDMAX;
    if (e < 0) {
        e += GF_FIELDMAX;
    }
    return alpha_to[e];
}

int gf_log(unsigned char a)
{
    if (a == 0) {
        return 0;
    }
    return index_of[a];
}
