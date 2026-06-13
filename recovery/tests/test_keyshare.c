/*
 * test_keyshare.c -- SLIP-0039 combiner + LCSAS codec unit tests.
 *
 * Asserts all 45 official SLIP-0039 vectors (passphrase "TREZOR"):
 *   - valid  => recovered MASTER SECRET equals the expected hex.
 *   - invalid (empty expected hex) => lcsas_slip39_recover errors.
 * Then exercises the LCSAS password codec on hand-built cases.
 *
 * Vectors are embedded via the generated keyshare_vectors.h so the test
 * needs no JSON parser (sanitizer-clean, self-contained).
 */

#include "slip39.h"
#include "hex.h"
#include "keyshare_vectors.h"

#include <stdio.h>
#include <string.h>

static int fails = 0;

static const unsigned char TREZOR[6] = { 'T','R','E','Z','O','R' };

/* Decode an even-length hex string into out; returns byte length, -1 bad. */
static long hex_to_bytes(const char *hex, unsigned char *out, size_t cap)
{
    size_t hlen = strlen(hex);
    size_t blen;
    if (hlen % 2 != 0) {
        return -1;
    }
    blen = hlen / 2;
    if (blen > cap) {
        return -1;
    }
    if (blen == 0) {
        return 0;
    }
    if (lcsas_hex_decode(hex, blen, out) != 0) {
        return -1;
    }
    return (long)blen;
}

static void run_vectors(void)
{
    int passed = 0;
    int i;

    for (i = 0; i < KEYSHARE_VECTOR_COUNT; i++) {
        const keyshare_vector *v = &KEYSHARE_VECTORS[i];
        unsigned char got[LCSAS_SLIP39_MAX_SECRET];
        size_t gotlen = 0;
        int rc;

        rc = lcsas_slip39_recover(v->mnemonics, (size_t)v->nmnemonics,
                                  TREZOR, sizeof(TREZOR), got, &gotlen);

        if (v->secret_hex[0] == '\0') {
            /* INVALID vector: recovery MUST fail. */
            if (rc == 0) {
                fprintf(stderr,
                        "FAIL [%d] expected error but recovered a secret: %s\n",
                        i + 1, v->desc);
                fails++;
            } else {
                passed++;
            }
        } else {
            /* VALID vector: recovered secret must equal expected hex. */
            unsigned char want[LCSAS_SLIP39_MAX_SECRET];
            long wlen = hex_to_bytes(v->secret_hex, want, sizeof(want));
            if (rc != 0) {
                fprintf(stderr, "FAIL [%d] recovery failed: %s\n",
                        i + 1, v->desc);
                fails++;
            } else if (wlen < 0 || (size_t)wlen != gotlen ||
                       memcmp(got, want, gotlen) != 0) {
                fprintf(stderr, "FAIL [%d] secret mismatch: %s\n",
                        i + 1, v->desc);
                fails++;
            } else {
                passed++;
            }
        }
    }

    printf("test_keyshare: %d/%d official SLIP-0039 vectors\n",
           passed, KEYSHARE_VECTOR_COUNT);
    if (passed != KEYSHARE_VECTOR_COUNT) {
        fails++;
    }
}

/* Build a master secret of the form: [len_hi][len_lo] body, padded. */
static void codec_case(const unsigned char *ms, size_t mslen,
                       int expect_ok, const unsigned char *expect_pw,
                       size_t expect_pwlen, const char *name)
{
    unsigned char pw[LCSAS_KEYSHARE_MAX_PW];
    size_t pwlen = 0;
    int rc = lcsas_keyshare_decode_master_secret(ms, mslen, pw, &pwlen);

    if (expect_ok) {
        if (rc != 0) {
            fprintf(stderr, "FAIL codec %s: expected success\n", name);
            fails++;
        } else if (pwlen != expect_pwlen ||
                   (expect_pwlen > 0 && memcmp(pw, expect_pw, pwlen) != 0)) {
            fprintf(stderr, "FAIL codec %s: payload mismatch (len %lu)\n",
                    name, (unsigned long)pwlen);
            fails++;
        }
    } else {
        if (rc == 0) {
            fprintf(stderr, "FAIL codec %s: expected error\n", name);
            fails++;
        }
    }
}

static void run_codec(void)
{
    /* Empty password: prefix 0x0000 + 14 zero padding (16-byte ms). */
    {
        unsigned char ms[16];
        memset(ms, 0, sizeof(ms));
        codec_case(ms, sizeof(ms), 1, (const unsigned char *)"", 0, "empty");
    }
    /* Short password "hi": prefix 0x0002 'h' 'i' then zero padding. */
    {
        unsigned char ms[16];
        memset(ms, 0, sizeof(ms));
        ms[0] = 0x00; ms[1] = 0x02; ms[2] = 'h'; ms[3] = 'i';
        codec_case(ms, sizeof(ms), 1, (const unsigned char *)"hi", 2, "short");
    }
    /* Binary payload with embedded NUL: prefix 0x0004 00 01 ff 02. */
    {
        unsigned char ms[16];
        unsigned char want[4];
        memset(ms, 0, sizeof(ms));
        ms[0] = 0x00; ms[1] = 0x04;
        ms[2] = 0x00; ms[3] = 0x01; ms[4] = 0xff; ms[5] = 0x02;
        want[0] = 0x00; want[1] = 0x01; want[2] = 0xff; want[3] = 0x02;
        codec_case(ms, sizeof(ms), 1, want, 4, "binary-embedded-nul");
    }
    /* Odd-length payload (length 3) inside an even-length ms. */
    {
        unsigned char ms[16];
        memset(ms, 0, sizeof(ms));
        ms[0] = 0x00; ms[1] = 0x03; ms[2] = 'a'; ms[3] = 'b'; ms[4] = 'c';
        codec_case(ms, sizeof(ms), 1, (const unsigned char *)"abc", 3, "odd");
    }
    /* Full-payload (no padding): 2-byte prefix + exactly N bytes. */
    {
        unsigned char ms[6];
        ms[0] = 0x00; ms[1] = 0x04;
        ms[2] = 'w'; ms[3] = 'x'; ms[4] = 'y'; ms[5] = 'z';
        codec_case(ms, sizeof(ms), 1, (const unsigned char *)"wxyz", 4, "padded-full");
    }
    /* Too short to hold a 2-byte prefix => error. */
    {
        unsigned char ms[1];
        ms[0] = 0x00;
        codec_case(ms, sizeof(ms), 0, NULL, 0, "too-short");
    }
    /* Claimed length runs past the buffer => error. */
    {
        unsigned char ms[4];
        ms[0] = 0x00; ms[1] = 0xff; ms[2] = 'a'; ms[3] = 'b';
        codec_case(ms, sizeof(ms), 0, NULL, 0, "overrun");
    }

    if (fails == 0) {
        printf("test_keyshare: codec cases OK\n");
    }
}

/* Card-tolerant mnemonic extraction (KEY-01). */
static void run_extract(void)
{
    /* A real 20-word SLIP-0039 share (vector 1's mnemonic). */
    static const char SHARE[] =
        "duckling enlarge academic academic agency result length solution "
        "fridge kidney coal piece deal husband erode duke ajar critical "
        "decision keyboard";

    /* A printed share card: header lines + prose + the words on one line,
     * exactly the {repo}-share-N-card.txt shape (CRLF on one line to also
     * exercise carriage-return trimming). */
    static const char CARD[] =
        "================ LCSAS KEY SHARE ================\n"
        "Repository : alpha\r\n"
        "Share      : 1 of 5\n"
        "\n"
        "WHAT THIS IS\n"
        "  This card holds ONE share of the password.\n"
        "\n"
        "THE SHARE WORDS (keep every word, in order)\n"
        "  duckling enlarge academic academic agency result length solution "
        "fridge kidney coal piece deal husband erode duke ajar critical "
        "decision keyboard\r\n"
        "================================================\n";

    char out[4096];
    char err[64];
    int rc;

    /* Bare mnemonic passes through unchanged. */
    rc = lcsas_keyshare_extract(SHARE, out, sizeof(out), err, sizeof(err));
    if (rc != 0 || strcmp(out, SHARE) != 0) {
        fprintf(stderr, "FAIL extract: bare mnemonic (rc=%d)\n", rc);
        fails++;
    }

    /* Full card text yields exactly the embedded share words. */
    err[0] = '\0';
    rc = lcsas_keyshare_extract(CARD, out, sizeof(out), err, sizeof(err));
    if (rc != 0 || strcmp(out, SHARE) != 0) {
        fprintf(stderr, "FAIL extract: card text (rc=%d, out='%s')\n",
                rc, out);
        fails++;
    }

    /* The extracted card mnemonic actually recovers — feed it back. */
    {
        const char *m[1];
        unsigned char ms[LCSAS_SLIP39_MAX_SECRET];
        size_t mslen = 0;
        m[0] = out;
        if (lcsas_slip39_recover(m, 1, TREZOR, sizeof(TREZOR),
                                 ms, &mslen) != 0) {
            fprintf(stderr, "FAIL extract: recovered card share failed\n");
            fails++;
        }
    }

    /* Truncated share (only the first 5 words) => nonzero, word count named. */
    {
        static const char SHORT[] =
            "  duckling enlarge academic academic agency\n";
        err[0] = '\0';
        rc = lcsas_keyshare_extract(SHORT, out, sizeof(out),
                                    err, sizeof(err));
        if (rc == 0) {
            fprintf(stderr, "FAIL extract: truncated accepted\n");
            fails++;
        } else if (strstr(err, "5") == NULL) {
            fprintf(stderr, "FAIL extract: error lacks count: '%s'\n", err);
            fails++;
        }
    }

    /* Prose-only file (no wordlist line) => nonzero, "found 0". */
    {
        static const char PROSE[] =
            "================ LCSAS KEY SHARE ================\n"
            "Repository : alpha\n"
            "(the holder lost the words)\n";
        err[0] = '\0';
        rc = lcsas_keyshare_extract(PROSE, out, sizeof(out),
                                    err, sizeof(err));
        if (rc == 0) {
            fprintf(stderr, "FAIL extract: prose-only accepted\n");
            fails++;
        } else if (strstr(err, "0") == NULL) {
            fprintf(stderr, "FAIL extract: prose error lacks count: '%s'\n",
                    err);
            fails++;
        }
    }

    if (fails == 0) {
        printf("test_keyshare: card extraction cases OK\n");
    }
}

/* Per-share diagnostics + 4-letter-prefix entry (KEY-07). */
static void run_check_share(void)
{
    /* A real, individually-valid 20-word SLIP-0039 share (vector 1). */
    static const char SHARE[] =
        "duckling enlarge academic academic agency result length solution "
        "fridge kidney coal piece deal husband erode duke ajar critical "
        "decision keyboard";

    char err[128];
    int rc;

    /* A valid share passes the per-share check with no message. */
    err[0] = '\0';
    rc = lcsas_keyshare_check_share(SHARE, err, sizeof(err));
    if (rc != 0) {
        fprintf(stderr, "FAIL check_share: valid share rejected ('%s')\n", err);
        fails++;
    }

    /* (a) One mistyped word (word 7 'length' -> 'lenght') => named position
     *     + offending token, NOT a generic error. */
    {
        static const char TYPO[] =
            "duckling enlarge academic academic agency result lenght solution "
            "fridge kidney coal piece deal husband erode duke ajar critical "
            "decision keyboard";
        err[0] = '\0';
        rc = lcsas_keyshare_check_share(TYPO, err, sizeof(err));
        if (rc == 0) {
            fprintf(stderr, "FAIL check_share: typo accepted\n");
            fails++;
        } else if (strstr(err, "word 7") == NULL ||
                   strstr(err, "lenght") == NULL) {
            fprintf(stderr,
                    "FAIL check_share: typo error lacks position/token: '%s'\n",
                    err);
            fails++;
        }
    }

    /* (b) Every word truncated to its 4-letter prefix => still valid, and
     *     recovers byte-identically to the full-word share. */
    {
        static const char PREFIX[] =
            "duck enla acad acad agen resu leng solu "
            "frid kidn coal piec deal husb erod duke ajar crit "
            "deci keyb";
        const char *full[1];
        const char *pre[1];
        unsigned char ms_full[LCSAS_SLIP39_MAX_SECRET];
        unsigned char ms_pre[LCSAS_SLIP39_MAX_SECRET];
        size_t lf = 0, lp = 0;

        err[0] = '\0';
        if (lcsas_keyshare_check_share(PREFIX, err, sizeof(err)) != 0) {
            fprintf(stderr, "FAIL check_share: prefix share rejected ('%s')\n",
                    err);
            fails++;
        }
        full[0] = SHARE;
        pre[0] = PREFIX;
        if (lcsas_slip39_recover(full, 1, TREZOR, sizeof(TREZOR),
                                 ms_full, &lf) != 0 ||
            lcsas_slip39_recover(pre, 1, TREZOR, sizeof(TREZOR),
                                 ms_pre, &lp) != 0) {
            fprintf(stderr, "FAIL prefix: recovery failed\n");
            fails++;
        } else if (lf != lp || memcmp(ms_full, ms_pre, lf) != 0) {
            fprintf(stderr, "FAIL prefix: prefix secret != full-word secret\n");
            fails++;
        }
    }

    /* (c) A 3-letter token is too short to be an unambiguous prefix => the
     *     share is rejected naming that token. */
    {
        static const char SHORT[] =
            "duc enlarge academic academic agency result length solution "
            "fridge kidney coal piece deal husband erode duke ajar critical "
            "decision keyboard";
        err[0] = '\0';
        rc = lcsas_keyshare_check_share(SHORT, err, sizeof(err));
        if (rc == 0) {
            fprintf(stderr, "FAIL check_share: 3-letter token accepted\n");
            fails++;
        } else if (strstr(err, "word 1") == NULL || strstr(err, "duc") == NULL) {
            fprintf(stderr,
                    "FAIL check_share: 3-letter error lacks token: '%s'\n",
                    err);
            fails++;
        }
    }

    /* A whole-share checksum failure (swap two value words) is reported as a
     * checksum error, not a word error. */
    {
        static const char BADCK[] =
            "duckling enlarge academic academic agency result length solution "
            "fridge kidney coal piece deal husband erode duke ajar critical "
            "keyboard decision";   /* last two words swapped */
        err[0] = '\0';
        rc = lcsas_keyshare_check_share(BADCK, err, sizeof(err));
        if (rc == 0) {
            fprintf(stderr, "FAIL check_share: bad-checksum share accepted\n");
            fails++;
        } else if (strstr(err, "checksum") == NULL) {
            fprintf(stderr,
                    "FAIL check_share: checksum error not reported: '%s'\n",
                    err);
            fails++;
        }
    }

    if (fails == 0) {
        printf("test_keyshare: per-share check + prefix cases OK\n");
    }
}

int main(void)
{
    run_vectors();
    run_codec();
    run_extract();
    run_check_share();
    if (fails == 0) {
        printf("test_keyshare: OK\n");
    }
    return fails ? 1 : 0;
}
