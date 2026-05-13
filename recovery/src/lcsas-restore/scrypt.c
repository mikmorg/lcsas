/*
 * scrypt.c -- RFC 7914 scrypt with Salsa20/8 core.
 *
 * Strict C89.  Allocates a single buffer of size 128 * r * (N + 2) at
 * entry; freed on exit.  For restic defaults (N=32768, r=8) this is
 * ~32 MiB plus a small scratch area -- acceptable for a recovery
 * binary that runs once.
 *
 * Reference: RFC 7914 sections 3 (Salsa20/8), 4 (BlockMix), 5 (SMix),
 * 6 (scrypt).
 */
#include "scrypt.h"
#include "pbkdf2.h"

#include <stdlib.h>

#define U32 unsigned long

static U32
ld32_le(const unsigned char *p)
{
    return ((U32)p[0])
         | ((U32)p[1] <<  8)
         | ((U32)p[2] << 16)
         | ((U32)p[3] << 24);
}

static void
st32_le(unsigned char *p, U32 v)
{
    p[0] = (unsigned char)(v      );
    p[1] = (unsigned char)(v >>  8);
    p[2] = (unsigned char)(v >> 16);
    p[3] = (unsigned char)(v >> 24);
}

#define R(a, b) (((a) << (b)) | ((a) >> (32 - (b))))
#define M32(x)  ((x) & 0xFFFFFFFFUL)

static void
salsa20_8(unsigned char B[64])
{
    U32 x[16];
    int i;

    for (i = 0; i < 16; i++) x[i] = ld32_le(B + i * 4);

    for (i = 0; i < 4; i++) {
        x[ 4] ^= M32(R(M32(x[ 0] + x[12]),  7));
        x[ 8] ^= M32(R(M32(x[ 4] + x[ 0]),  9));
        x[12] ^= M32(R(M32(x[ 8] + x[ 4]), 13));
        x[ 0] ^= M32(R(M32(x[12] + x[ 8]), 18));
        x[ 9] ^= M32(R(M32(x[ 5] + x[ 1]),  7));
        x[13] ^= M32(R(M32(x[ 9] + x[ 5]),  9));
        x[ 1] ^= M32(R(M32(x[13] + x[ 9]), 13));
        x[ 5] ^= M32(R(M32(x[ 1] + x[13]), 18));
        x[14] ^= M32(R(M32(x[10] + x[ 6]),  7));
        x[ 2] ^= M32(R(M32(x[14] + x[10]),  9));
        x[ 6] ^= M32(R(M32(x[ 2] + x[14]), 13));
        x[10] ^= M32(R(M32(x[ 6] + x[ 2]), 18));
        x[ 3] ^= M32(R(M32(x[15] + x[11]),  7));
        x[ 7] ^= M32(R(M32(x[ 3] + x[15]),  9));
        x[11] ^= M32(R(M32(x[ 7] + x[ 3]), 13));
        x[15] ^= M32(R(M32(x[11] + x[ 7]), 18));

        x[ 1] ^= M32(R(M32(x[ 0] + x[ 3]),  7));
        x[ 2] ^= M32(R(M32(x[ 1] + x[ 0]),  9));
        x[ 3] ^= M32(R(M32(x[ 2] + x[ 1]), 13));
        x[ 0] ^= M32(R(M32(x[ 3] + x[ 2]), 18));
        x[ 6] ^= M32(R(M32(x[ 5] + x[ 4]),  7));
        x[ 7] ^= M32(R(M32(x[ 6] + x[ 5]),  9));
        x[ 4] ^= M32(R(M32(x[ 7] + x[ 6]), 13));
        x[ 5] ^= M32(R(M32(x[ 4] + x[ 7]), 18));
        x[11] ^= M32(R(M32(x[10] + x[ 9]),  7));
        x[ 8] ^= M32(R(M32(x[11] + x[10]),  9));
        x[ 9] ^= M32(R(M32(x[ 8] + x[11]), 13));
        x[10] ^= M32(R(M32(x[ 9] + x[ 8]), 18));
        x[12] ^= M32(R(M32(x[15] + x[14]),  7));
        x[13] ^= M32(R(M32(x[12] + x[15]),  9));
        x[14] ^= M32(R(M32(x[13] + x[12]), 13));
        x[15] ^= M32(R(M32(x[14] + x[13]), 18));
    }

    for (i = 0; i < 16; i++) {
        U32 v = M32(ld32_le(B + i * 4) + x[i]);
        st32_le(B + i * 4, v);
    }
}

/*
 * BlockMix (RFC 7914 §4).  Input/output: B is 128 * r bytes.
 * `Y` is a caller-allocated scratch of size 128 * r bytes.
 */
static void
block_mix(unsigned char *B, unsigned long r, unsigned char *Y)
{
    unsigned char X[64];
    unsigned long blk;
    unsigned long i;

    /* X = B[2r - 1] */
    for (i = 0; i < 64; i++) X[i] = B[(2 * r - 1) * 64 + i];

    for (blk = 0; blk < 2 * r; blk++) {
        for (i = 0; i < 64; i++) X[i] ^= B[blk * 64 + i];
        salsa20_8(X);
        for (i = 0; i < 64; i++) Y[blk * 64 + i] = X[i];
    }

    /* B' = Y[0], Y[2], ..., Y[2r-2], Y[1], Y[3], ..., Y[2r-1]. */
    for (blk = 0; blk < r; blk++) {
        for (i = 0; i < 64; i++) {
            B[blk * 64 + i]       = Y[(2 * blk + 0) * 64 + i];
            B[(r + blk) * 64 + i] = Y[(2 * blk + 1) * 64 + i];
        }
    }
}

/*
 * SMix (RFC 7914 §5).
 *   B:    128 * r byte input/output buffer.
 *   V:    caller-allocated, 128 * r * N bytes.
 *   X:    caller-allocated, 128 * r bytes (scratch).
 *   T:    caller-allocated, 128 * r bytes (scratch).
 *   Y:    caller-allocated, 128 * r bytes (BlockMix scratch).
 */
static void
smix(unsigned char *B, unsigned long r, unsigned long N,
     unsigned char *V, unsigned char *X, unsigned char *T,
     unsigned char *Y)
{
    unsigned long step;
    unsigned long j;
    unsigned long k;
    unsigned long bs = 128UL * r;

    for (k = 0; k < bs; k++) X[k] = B[k];

    for (step = 0; step < N; step++) {
        for (k = 0; k < bs; k++) V[step * bs + k] = X[k];
        block_mix(X, r, Y);
    }

    for (step = 0; step < N; step++) {
        /* j = Integerify(X) mod N -- last 64-byte block's first u32. */
        j = ld32_le(X + (2 * r - 1) * 64) & (N - 1);
        for (k = 0; k < bs; k++) T[k] = (unsigned char)(X[k] ^ V[j * bs + k]);
        for (k = 0; k < bs; k++) X[k] = T[k];
        block_mix(X, r, Y);
    }

    for (k = 0; k < bs; k++) B[k] = X[k];
}

int
lcsas_scrypt(const unsigned char *pw, size_t pwlen,
             const unsigned char *salt, size_t saltlen,
             unsigned long N, unsigned long r, unsigned long p,
             unsigned char *dk, size_t dklen)
{
    unsigned char *B = NULL;
    unsigned char *V = NULL;
    unsigned char *X = NULL;
    unsigned char *T = NULL;
    unsigned char *Y = NULL;
    unsigned long i;
    unsigned long bs;
    int rc = 0;

    if (N < 2 || (N & (N - 1)) != 0) return -1;
    if (r == 0 || p == 0) return -1;

    bs = 128UL * r;

    B = (unsigned char *)malloc(bs * p);
    V = (unsigned char *)malloc(bs * N);
    X = (unsigned char *)malloc(bs);
    T = (unsigned char *)malloc(bs);
    Y = (unsigned char *)malloc(bs);
    if (!B || !V || !X || !T || !Y) {
        rc = -2;
        goto out;
    }

    /* Step 1. */
    lcsas_pbkdf2_sha256(pw, pwlen, salt, saltlen, 1UL, B, bs * p);

    /* Step 2. */
    for (i = 0; i < p; i++) {
        smix(B + i * bs, r, N, V, X, T, Y);
    }

    /* Step 3. */
    lcsas_pbkdf2_sha256(pw, pwlen, B, bs * p, 1UL, dk, dklen);

out:
    free(B); free(V); free(X); free(T); free(Y);
    return rc;
}
