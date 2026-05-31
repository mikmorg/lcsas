/*
 * slip39.c -- C89 SLIP-0039 combiner + LCSAS password codec.
 *
 * Faithful port of src/lcsas/keyshare/slip39.py (the vector-passing
 * reference) and src/lcsas/keyshare/codec.py.  See slip39.h for the
 * public contract.  Strict C89; reuses lcsas_hmac_sha256 /
 * lcsas_pbkdf2_sha256 from lcsas-restore's pbkdf2.c.
 *
 * Memory: the SLIP-0039 master secret here frames an LCSAS password of
 * up to 65535 bytes, so a master secret can reach ~64 KiB.  Per-share
 * value buffers are therefore heap-allocated (sized to the actual,
 * uniform value length) rather than placed inline in fixed arrays, so a
 * 16x16 grouped structure cannot blow the stack.  Every error path runs
 * a single cleanup that frees exactly what was allocated (ASan-clean).
 */

#include "slip39.h"
#include "../lcsas-restore/pbkdf2.h"

#include <stdlib.h>
#include <string.h>

/* --------------------------------------------------------------------- */
/* SLIP-0039 constants (mirror slip39.py).                               */
/* --------------------------------------------------------------------- */

#define RADIX_BITS                  10
#define RADIX                       1024
#define ID_LENGTH_BITS              15
#define EXTENDABLE_FLAG_LENGTH_BITS 1
#define ITERATION_EXP_LENGTH_BITS   4

/* ceil((15 + 1 + 4) / 10) = 2. */
#define ID_EXP_LENGTH_WORDS         2

#define MAX_SHARE_COUNT             16
#define CHECKSUM_LENGTH_WORDS       3
#define DIGEST_LENGTH_BYTES         4

#define METADATA_LENGTH_WORDS       (ID_EXP_LENGTH_WORDS + 2 + CHECKSUM_LENGTH_WORDS)

#define MIN_STRENGTH_BITS           128
#define MIN_STRENGTH_WORDS          13   /* ceil(128/10) */
#define MIN_MNEMONIC_LENGTH_WORDS   (METADATA_LENGTH_WORDS + MIN_STRENGTH_WORDS)

#define BASE_ITERATION_COUNT        10000
#define ROUND_COUNT                 4
#define SECRET_INDEX                255
#define DIGEST_INDEX                254

#define MAX_SHARES                  MAX_SHARE_COUNT
#define MAX_GROUPS                  MAX_SHARE_COUNT

/*
 * Master-secret cap.  An LCSAS password is at most 65535 bytes; framed
 * (2-byte prefix, even, >=16) the master secret is at most 65536+2.  A
 * mnemonic word count is bounded accordingly.  Used to size the
 * per-mnemonic word buffer (a single automatic, ~262 KB worst case is
 * still heap below, so keep the word buffer modest by capping mnemonic
 * length to what 65538 secret bytes require).
 */
#define MAX_SECRET_BYTES            65538
/* ceil(65538*8/10) value words + METADATA = max mnemonic words. */
#define MAX_VALUE_WORDS             52431
#define MAX_MNEMONIC_WORDS          (MAX_VALUE_WORDS + METADATA_LENGTH_WORDS)

/* --------------------------------------------------------------------- */
/* RS1024 checksum over GF(1024) (slip39.py _rs1024_*).                  */
/* --------------------------------------------------------------------- */

static const unsigned long RS1024_GEN[10] = {
    0xE0E040UL,   0x1C1C080UL,  0x3838100UL,  0x7070200UL,  0xE0E0009UL,
    0x1C0C2412UL, 0x38086C24UL, 0x3090FC48UL, 0x21B1F890UL, 0x3F3F120UL
};

static const unsigned char CUSTOM_ORIG[6] = { 's','h','a','m','i','r' };
static const unsigned char CUSTOM_EXT[17] = {
    's','h','a','m','i','r','_','e','x','t','e','n','d','a','b','l','e'
};

static unsigned long rs1024_polymod(const unsigned int *values, size_t n)
{
    unsigned long chk = 1UL;
    size_t k;
    int i;
    for (k = 0; k < n; k++) {
        unsigned long b = chk >> 20;
        chk = ((chk & 0xFFFFFUL) << 10) ^ (unsigned long)values[k];
        for (i = 0; i < 10; i++) {
            if ((b >> i) & 1UL) {
                chk ^= RS1024_GEN[i];
            }
        }
    }
    return chk;
}

/*
 * Verify RS1024 over the full mnemonic `data` (`n` words), with the
 * customization string selected by `extendable`.  Returns 1 if valid.
 * The customization words are folded into the polymod without copying
 * the (potentially long) data array.
 */
static int rs1024_verify(const unsigned int *data, size_t n, int extendable)
{
    unsigned int cbuf[17];
    const unsigned char *cust;
    size_t clen, i;
    unsigned long chk = 1UL;

    if (extendable) {
        cust = CUSTOM_EXT;
        clen = sizeof(CUSTOM_EXT);
    } else {
        cust = CUSTOM_ORIG;
        clen = sizeof(CUSTOM_ORIG);
    }
    for (i = 0; i < clen; i++) {
        cbuf[i] = cust[i];
    }
    chk = rs1024_polymod(cbuf, clen);

    /* Continue the polymod over `data` without an intermediate copy. */
    {
        size_t k;
        int j;
        for (k = 0; k < n; k++) {
            unsigned long b = chk >> 20;
            chk = ((chk & 0xFFFFFUL) << 10) ^ (unsigned long)data[k];
            for (j = 0; j < 10; j++) {
                if ((b >> j) & 1UL) {
                    chk ^= RS1024_GEN[j];
                }
            }
        }
    }
    return chk == 1UL ? 1 : 0;
}

/* --------------------------------------------------------------------- */
/* Word <-> index (binary search over the alphabetically sorted list).   */
/* --------------------------------------------------------------------- */

static int word_to_index(const char *w, size_t len)
{
    int lo = 0, hi = 1023;
    while (lo <= hi) {
        int mid = lo + (hi - lo) / 2;
        const char *cand = lcsas_slip39_wordlist[mid];
        size_t i = 0;
        int cmp = 0;
        while (i < len && cand[i] != '\0') {
            unsigned char a = (unsigned char)w[i];
            unsigned char b = (unsigned char)cand[i];
            if (a != b) { cmp = (a < b) ? -1 : 1; break; }
            i++;
        }
        if (cmp == 0) {
            if (i == len && cand[i] == '\0') {
                return mid;
            }
            cmp = (i == len) ? -1 : 1;
        }
        if (cmp < 0) {
            hi = mid - 1;
        } else {
            lo = mid + 1;
        }
    }
    return -1;
}

/*
 * Tokenize a space-separated mnemonic into word indices.  Returns the
 * word count on success, or -1 on an unknown word / overflow.
 */
static int mnemonic_to_indices(const char *m, unsigned int *out, size_t cap)
{
    size_t count = 0;
    const char *p = m;

    while (*p != '\0') {
        char word[32];
        size_t wlen = 0;
        int idx;

        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        while (*p != '\0' && *p != ' ' && *p != '\t' &&
               *p != '\n' && *p != '\r') {
            char c = *p;
            if (c >= 'A' && c <= 'Z') {
                c = (char)(c - 'A' + 'a');
            }
            if (wlen >= sizeof(word) - 1) {
                return -1;
            }
            word[wlen++] = c;
            p++;
        }
        word[wlen] = '\0';
        idx = word_to_index(word, wlen);
        if (idx < 0) {
            return -1;
        }
        if (count >= cap) {
            return -1;
        }
        out[count++] = (unsigned int)idx;
    }
    return (int)count;
}

/* --------------------------------------------------------------------- */
/* GF(256) arithmetic (Rijndael field) and Lagrange interpolation.       */
/* --------------------------------------------------------------------- */

static unsigned char GF_EXP[255];
static unsigned char GF_LOG[256];
static int gf_ready = 0;

static void gf_init(void)
{
    int i;
    unsigned int poly = 1;
    for (i = 0; i < 255; i++) {
        GF_EXP[i] = (unsigned char)poly;
        GF_LOG[poly] = (unsigned char)i;
        poly = (poly << 1) ^ poly;
        if (poly & 0x100) {
            poly ^= 0x11B;
        }
    }
    GF_LOG[0] = 0;
    gf_ready = 1;
}

/* A raw share: x-coordinate plus a heap-owned `len`-byte value. */
typedef struct {
    int x;
    size_t len;
    unsigned char *data;   /* malloc'd; freed by the owner */
} raw_share;

/*
 * Evaluate the interpolating polynomial(s) over GF(256) at `x`.  Writes
 * `vlen` result bytes to `out`.  Returns 0 on success, nonzero on a
 * structural error.  Mirrors slip39.py _interpolate, including the
 * non-negative modulo normalisation (C's % can go negative).
 */
static int gf_interpolate(const raw_share *shares, size_t count,
                          int x, unsigned char *out, size_t vlen)
{
    size_t i, j, b;
    long log_prod;

    for (i = 0; i < count; i++) {
        if (shares[i].len != vlen) {
            return -1;
        }
        for (j = i + 1; j < count; j++) {
            if (shares[i].x == shares[j].x) {
                return -1;
            }
        }
    }

    for (i = 0; i < count; i++) {
        if (shares[i].x == x) {
            memcpy(out, shares[i].data, vlen);
            return 0;
        }
    }

    log_prod = 0;
    for (i = 0; i < count; i++) {
        log_prod += GF_LOG[(unsigned char)(shares[i].x ^ x)];
    }

    for (b = 0; b < vlen; b++) {
        out[b] = 0;
    }

    for (i = 0; i < count; i++) {
        long sum_others = 0;
        long basis;
        for (j = 0; j < count; j++) {
            sum_others += GF_LOG[(unsigned char)(shares[i].x ^ shares[j].x)];
        }
        basis = log_prod - GF_LOG[(unsigned char)(shares[i].x ^ x)] - sum_others;
        basis = ((basis % 255) + 255) % 255;
        for (b = 0; b < vlen; b++) {
            unsigned char sv = shares[i].data[b];
            if (sv != 0) {
                long e = (GF_LOG[sv] + basis) % 255;
                out[b] ^= GF_EXP[e];
            }
        }
    }
    return 0;
}

/* digest = HMAC-SHA256(key=random_data, msg=shared_secret)[:4]. */
static void create_digest(const unsigned char *random_data, size_t rlen,
                          const unsigned char *secret, size_t slen,
                          unsigned char out[DIGEST_LENGTH_BYTES])
{
    unsigned char full[32];
    lcsas_hmac_sha256(random_data, rlen, secret, slen, full);
    memcpy(out, full, DIGEST_LENGTH_BYTES);
}

/*
 * Recover a single level's secret from `count` member shares with the
 * given `threshold`.  Writes `vlen` bytes to `out`.  Returns 0 on
 * success, nonzero on a digest/structural failure.  Allocates a scratch
 * buffer; frees it on every path.
 */
static int recover_secret(int threshold, const raw_share *shares,
                          size_t count, unsigned char *out, size_t vlen)
{
    unsigned char *digest_share;
    unsigned char want[DIGEST_LENGTH_BYTES];
    int rc = -1;

    if (threshold == 1) {
        if (count < 1) {
            return -1;
        }
        memcpy(out, shares[0].data, vlen);
        return 0;
    }
    if (vlen < DIGEST_LENGTH_BYTES) {
        return -1;
    }

    digest_share = (unsigned char *)malloc(vlen);
    if (digest_share == NULL) {
        return -1;
    }

    if (gf_interpolate(shares, count, SECRET_INDEX, out, vlen) != 0) {
        goto cleanup;
    }
    if (gf_interpolate(shares, count, DIGEST_INDEX, digest_share, vlen) != 0) {
        goto cleanup;
    }
    create_digest(digest_share + DIGEST_LENGTH_BYTES, vlen - DIGEST_LENGTH_BYTES,
                  out, vlen, want);
    if (memcmp(want, digest_share, DIGEST_LENGTH_BYTES) != 0) {
        goto cleanup;
    }
    rc = 0;

cleanup:
    free(digest_share);
    return rc;
}

/* --------------------------------------------------------------------- */
/* 4-round Feistel decrypt with a PBKDF2-HMAC-SHA256 round function.     */
/* --------------------------------------------------------------------- */

/*
 * f = PBKDF2-HMAC-SHA256( pw = [i] || passphrase,
 *                         salt = salt || r,
 *                         iters = (10000 << exp) / 4,
 *                         dklen = len(r) ).
 * The pw buffer holds the (short) SLIP-0039 passphrase, NOT a password,
 * so it is heap-allocated to plen+1 bytes.  The salt scratch holds the
 * extendable-flag salt (0 or 8 bytes) concatenated with the half-secret.
 */
static int round_function(int i, const unsigned char *passphrase, size_t plen,
                          int exponent, const unsigned char *salt, size_t saltlen,
                          const unsigned char *r, size_t rlen, unsigned char *out)
{
    unsigned char *pw;
    unsigned char *sbuf;
    unsigned long iters;
    size_t k;
    int rc = -1;

    pw = (unsigned char *)malloc(plen + 1);
    sbuf = (unsigned char *)malloc(saltlen + rlen > 0 ? saltlen + rlen : 1);
    if (pw == NULL || sbuf == NULL) {
        goto cleanup;
    }
    pw[0] = (unsigned char)i;
    for (k = 0; k < plen; k++) {
        pw[1 + k] = passphrase[k];
    }
    for (k = 0; k < saltlen; k++) {
        sbuf[k] = salt[k];
    }
    for (k = 0; k < rlen; k++) {
        sbuf[saltlen + k] = r[k];
    }
    iters = (unsigned long)(BASE_ITERATION_COUNT << exponent) / ROUND_COUNT;
    lcsas_pbkdf2_sha256(pw, plen + 1, sbuf, saltlen + rlen, iters, out, rlen);
    rc = 0;

cleanup:
    free(pw);
    free(sbuf);
    return rc;
}

/*
 * Decrypt the encrypted master secret via the SLIP-0039 Feistel network
 * (run in reverse: rounds 3,2,1,0).  `ems`/`out` length is `len` (even).
 * Returns 0 on success.  Allocates the half-buffers on the heap.
 */
static int feistel_decrypt(const unsigned char *ems, size_t len,
                           const unsigned char *passphrase, size_t plen,
                           int exponent, const unsigned char *salt, size_t saltlen,
                           unsigned char *out)
{
    unsigned char *left = NULL, *right = NULL, *f = NULL, *newright = NULL;
    size_t half;
    int i;
    size_t k;
    int rc = -1;

    if (len < 2 || (len & 1) != 0) {
        return -1;
    }
    half = len / 2;

    left = (unsigned char *)malloc(half);
    right = (unsigned char *)malloc(half);
    f = (unsigned char *)malloc(half);
    newright = (unsigned char *)malloc(half);
    if (left == NULL || right == NULL || f == NULL || newright == NULL) {
        goto cleanup;
    }

    memcpy(left, ems, half);
    memcpy(right, ems + half, half);

    for (i = ROUND_COUNT - 1; i >= 0; i--) {
        if (round_function(i, passphrase, plen, exponent,
                           salt, saltlen, right, half, f) != 0) {
            goto cleanup;
        }
        for (k = 0; k < half; k++) {
            newright[k] = (unsigned char)(left[k] ^ f[k]);
        }
        memcpy(left, right, half);
        memcpy(right, newright, half);
    }
    memcpy(out, right, half);
    memcpy(out + half, left, half);
    rc = 0;

cleanup:
    free(left);
    free(right);
    free(f);
    free(newright);
    return rc;
}

/* --------------------------------------------------------------------- */
/* Share parsing.                                                        */
/* --------------------------------------------------------------------- */

typedef struct {
    unsigned int identifier;
    int extendable;
    int iteration_exponent;
    int group_index;
    int group_threshold;   /* decoded (+1 applied) */
    int group_count;       /* decoded (+1 applied) */
    int member_index;
    int member_threshold;  /* decoded (+1 applied) */
    size_t value_len;
    unsigned char *value;  /* malloc'd value_len bytes; owner frees */
} share_t;

/*
 * Parse one mnemonic into a share_t (allocating s->value).  Returns 0 on
 * success, nonzero on any malformation.  On failure s->value is NULL.
 */
static int parse_share(const char *mnemonic, share_t *s)
{
    unsigned int *words = NULL;
    int nwords;
    size_t n, nvalue, padding_len, value_bits, value_bytes;
    unsigned long id_exp, params;
    unsigned int mask;
    size_t i;
    unsigned long acc;
    int accbits;
    size_t out_i, remaining_pad;
    int rc = -1;

    s->value = NULL;

    words = (unsigned int *)malloc(MAX_MNEMONIC_WORDS * sizeof(unsigned int));
    if (words == NULL) {
        return -1;
    }

    nwords = mnemonic_to_indices(mnemonic, words, MAX_MNEMONIC_WORDS);
    if (nwords < 0) {
        goto cleanup;
    }
    n = (size_t)nwords;
    if (n < MIN_MNEMONIC_LENGTH_WORDS) {
        goto cleanup;
    }

    padding_len = (RADIX_BITS * (n - METADATA_LENGTH_WORDS)) % 16;
    if (padding_len > 8) {
        goto cleanup;
    }

    id_exp = (unsigned long)words[0] * RADIX + words[1];
    s->identifier = (unsigned int)(id_exp >>
        (EXTENDABLE_FLAG_LENGTH_BITS + ITERATION_EXP_LENGTH_BITS));
    s->extendable = (int)((id_exp >> ITERATION_EXP_LENGTH_BITS) & 1UL);
    s->iteration_exponent =
        (int)(id_exp & ((1UL << ITERATION_EXP_LENGTH_BITS) - 1));

    if (!rs1024_verify(words, n, s->extendable)) {
        goto cleanup;
    }

    params = (unsigned long)words[ID_EXP_LENGTH_WORDS] * RADIX +
             words[ID_EXP_LENGTH_WORDS + 1];
    mask = (1U << 4) - 1U;
    s->group_index      = (int)((params >> 16) & mask);
    s->group_threshold  = (int)((params >> 12) & mask) + 1;
    s->group_count      = (int)((params >> 8) & mask) + 1;
    s->member_index     = (int)((params >> 4) & mask);
    s->member_threshold = (int)(params & mask) + 1;

    if (s->group_count < s->group_threshold) {
        goto cleanup;
    }

    nvalue = n - METADATA_LENGTH_WORDS;
    value_bits = RADIX_BITS * nvalue - padding_len;
    value_bytes = (value_bits + 7) / 8;

    s->value = (unsigned char *)malloc(value_bytes > 0 ? value_bytes : 1);
    if (s->value == NULL) {
        goto cleanup;
    }

    acc = 0;
    accbits = 0;
    out_i = 0;
    remaining_pad = padding_len;
    for (i = 0; i < nvalue; i++) {
        unsigned int w = words[ID_EXP_LENGTH_WORDS + 2 + i];
        int bit;
        for (bit = RADIX_BITS - 1; bit >= 0; bit--) {
            unsigned int b = (w >> bit) & 1U;
            if (remaining_pad > 0) {
                if (b != 0) {
                    goto cleanup; /* nonzero padding bit => invalid */
                }
                remaining_pad--;
                continue;
            }
            acc = (acc << 1) | b;
            accbits++;
            if (accbits == 8) {
                if (out_i >= value_bytes) {
                    goto cleanup;
                }
                s->value[out_i++] = (unsigned char)(acc & 0xFF);
                acc = 0;
                accbits = 0;
            }
        }
    }
    if (accbits != 0 || out_i != value_bytes) {
        goto cleanup;
    }
    s->value_len = value_bytes;
    rc = 0;

cleanup:
    free(words);
    if (rc != 0) {
        free(s->value);
        s->value = NULL;
    }
    return rc;
}

/* --------------------------------------------------------------------- */
/* Grouped two-level recovery.                                           */
/* --------------------------------------------------------------------- */

typedef struct {
    int group_index;
    int present;
    size_t nshares;
    share_t shares[MAX_SHARES];   /* shares hold malloc'd value pointers */
} group_t;

int lcsas_slip39_recover(const char *const *mnemonics, size_t n,
                         const unsigned char *passphrase, size_t plen,
                         unsigned char *out_secret, size_t *out_len)
{
    group_t *groups = NULL;
    size_t ngroups_seen = 0;
    share_t first;
    int have_common = 0;
    size_t i, gi;
    size_t value_len = 0;
    int group_threshold = 0;
    raw_share *group_secrets = NULL;
    size_t ngroup_secrets = 0;
    unsigned char *ciphertext = NULL;
    unsigned char salt[8];
    size_t saltlen;
    int rc = -1;

    if (!gf_ready) {
        gf_init();
    }
    if (n == 0) {
        return -1;
    }

    groups = (group_t *)calloc(MAX_GROUPS, sizeof(group_t));
    group_secrets = (raw_share *)calloc(MAX_GROUPS, sizeof(raw_share));
    if (groups == NULL || group_secrets == NULL) {
        goto cleanup;
    }

    memset(&first, 0, sizeof(first));

    /* Decode every mnemonic; bucket by group_index; enforce uniform
     * common parameters and group membership. */
    for (i = 0; i < n; i++) {
        share_t s;
        size_t k;
        int found;

        if (parse_share(mnemonics[i], &s) != 0) {
            goto cleanup;
        }

        if (!have_common) {
            /* Copy the metadata; keep ownership of s.value via the group
             * bucket below (do not free it here). */
            first = s;
            first.value = NULL; /* `first` is a metadata template only */
            have_common = 1;
            group_threshold = s.group_threshold;
            value_len = s.value_len;
        } else {
            if (s.identifier != first.identifier ||
                s.extendable != first.extendable ||
                s.iteration_exponent != first.iteration_exponent ||
                s.group_threshold != first.group_threshold ||
                s.group_count != first.group_count) {
                free(s.value);
                goto cleanup;
            }
        }

        found = 0;
        for (gi = 0; gi < ngroups_seen; gi++) {
            if (groups[gi].present && groups[gi].group_index == s.group_index) {
                found = 1;
                break;
            }
        }
        if (!found) {
            if (ngroups_seen >= MAX_GROUPS) {
                free(s.value);
                goto cleanup;
            }
            gi = ngroups_seen++;
            groups[gi].present = 1;
            groups[gi].group_index = s.group_index;
            groups[gi].nshares = 0;
        }

        {
            group_t *g = &groups[gi];
            if (g->nshares > 0 &&
                g->shares[0].member_threshold != s.member_threshold) {
                free(s.value);
                goto cleanup;
            }
            for (k = 0; k < g->nshares; k++) {
                if (g->shares[k].member_index == s.member_index) {
                    free(s.value);
                    goto cleanup;   /* duplicate member share */
                }
            }
            if (g->nshares >= MAX_SHARES) {
                free(s.value);
                goto cleanup;
            }
            g->shares[g->nshares++] = s;   /* takes ownership of s.value */
        }
    }

    if (ngroups_seen != (size_t)group_threshold) {
        goto cleanup;
    }

    ciphertext = (unsigned char *)malloc(value_len > 0 ? value_len : 1);
    if (ciphertext == NULL) {
        goto cleanup;
    }

    /* Recover each group's secret from its member shares. */
    for (gi = 0; gi < ngroups_seen; gi++) {
        group_t *g = &groups[gi];
        int member_threshold = g->shares[0].member_threshold;
        raw_share members[MAX_SHARES];
        size_t m;
        unsigned char *gsdata;

        if (g->nshares != (size_t)member_threshold) {
            goto cleanup;
        }
        for (m = 0; m < g->nshares; m++) {
            members[m].x = g->shares[m].member_index;
            members[m].len = g->shares[m].value_len;
            members[m].data = g->shares[m].value;  /* borrowed, not owned */
        }

        gsdata = (unsigned char *)malloc(value_len > 0 ? value_len : 1);
        if (gsdata == NULL) {
            goto cleanup;
        }
        if (recover_secret(member_threshold, members, g->nshares,
                           gsdata, value_len) != 0) {
            free(gsdata);
            goto cleanup;
        }
        group_secrets[ngroup_secrets].x = g->group_index;
        group_secrets[ngroup_secrets].len = value_len;
        group_secrets[ngroup_secrets].data = gsdata;  /* owned here */
        ngroup_secrets++;
    }

    if (recover_secret(group_threshold, group_secrets, ngroup_secrets,
                       ciphertext, value_len) != 0) {
        goto cleanup;
    }

    if (first.extendable) {
        saltlen = 0;
    } else {
        salt[0] = 's'; salt[1] = 'h'; salt[2] = 'a';
        salt[3] = 'm'; salt[4] = 'i'; salt[5] = 'r';
        salt[6] = (unsigned char)((first.identifier >> 8) & 0xFF);
        salt[7] = (unsigned char)(first.identifier & 0xFF);
        saltlen = 8;
    }

    if (feistel_decrypt(ciphertext, value_len, passphrase, plen,
                        first.iteration_exponent, salt, saltlen,
                        out_secret) != 0) {
        goto cleanup;
    }
    *out_len = value_len;
    rc = 0;

cleanup:
    if (groups != NULL) {
        for (gi = 0; gi < MAX_GROUPS; gi++) {
            size_t k;
            for (k = 0; k < groups[gi].nshares; k++) {
                free(groups[gi].shares[k].value);
            }
        }
        free(groups);
    }
    if (group_secrets != NULL) {
        for (gi = 0; gi < ngroup_secrets; gi++) {
            free(group_secrets[gi].data);
        }
        free(group_secrets);
    }
    free(ciphertext);
    return rc;
}

/* --------------------------------------------------------------------- */
/* LCSAS password codec (codec.py decode_master_secret).                 */
/* --------------------------------------------------------------------- */

int lcsas_keyshare_decode_master_secret(const unsigned char *ms, size_t mslen,
                                        unsigned char *out_pw, size_t *out_pwlen)
{
    size_t plen;
    if (mslen < 2) {
        return -1;
    }
    plen = ((size_t)ms[0] << 8) | (size_t)ms[1];
    if (2 + plen > mslen) {
        return -1;
    }
    if (plen > 0) {
        memcpy(out_pw, ms + 2, plen);
    }
    *out_pwlen = plen;
    return 0;
}

int lcsas_keyshare_recover_password(const char *const *mnemonics, size_t n,
                                    const unsigned char *passphrase, size_t plen,
                                    unsigned char *out_pw, size_t *out_pwlen)
{
    unsigned char *ms;
    size_t mslen = 0;
    int rc;

    ms = (unsigned char *)malloc(MAX_SECRET_BYTES);
    if (ms == NULL) {
        return -1;
    }
    rc = lcsas_slip39_recover(mnemonics, n, passphrase, plen, ms, &mslen);
    if (rc == 0) {
        rc = lcsas_keyshare_decode_master_secret(ms, mslen, out_pw, out_pwlen);
    }
    free(ms);
    return rc;
}
