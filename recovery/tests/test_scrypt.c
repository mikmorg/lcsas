/*
 * test_scrypt.c -- RFC 7914 §11 test vectors, plus the cost-parameter
 * limits (#362, #470).
 */
#include "scrypt.h"
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int fails = 0;

static int
parse_hex(const char *s, unsigned char *out, size_t n)
{
    size_t i;
    char buf[3];
    unsigned int v;
    buf[2] = '\0';
    for (i = 0; i < n; i++) {
        buf[0] = s[i * 2];
        buf[1] = s[i * 2 + 1];
        if (sscanf(buf, "%x", &v) != 1) return -1;
        out[i] = (unsigned char)v;
    }
    return 0;
}

static void
expect(const char *label, const unsigned char *got,
       const char *want_hex, size_t n)
{
    unsigned char w[64];
    if (parse_hex(want_hex, w, n) < 0 || memcmp(got, w, n) != 0) {
        size_t i;
        fprintf(stderr, "FAIL %s\n  want %s\n  got  ", label, want_hex);
        for (i = 0; i < n; i++) fprintf(stderr, "%02x", got[i]);
        fprintf(stderr, "\n");
        fails++;
    }
}

int main(void)
{
    /* RFC 7914 §11 vector 1.
     * P = "", S = "", N = 16, r = 1, p = 1, dkLen = 64.
     */
    {
        unsigned char dk[64];
        int rc = lcsas_scrypt((unsigned char *)"", 0,
                              (unsigned char *)"", 0,
                              16, 1, 1, dk, 64);
        if (rc != 0) { fprintf(stderr, "FAIL v1 rc=%d\n", rc); fails++; }
        else expect("RFC7914 v1", dk,
                    "77d6576238657b203b19ca42c18a0497"
                    "f16b4844e3074ae8dfdffa3fede21442"
                    "fcd0069ded0948f8326a753a0fc81f17"
                    "e8d3e0fb2e0d3628cf35e20c38d18906", 64);
    }

    /* RFC 7914 §11 vector 2.
     * P = "password", S = "NaCl", N = 1024, r = 8, p = 16, dkLen = 64.
     */
    {
        unsigned char dk[64];
        int rc = lcsas_scrypt((unsigned char *)"password", 8,
                              (unsigned char *)"NaCl", 4,
                              1024, 8, 16, dk, 64);
        if (rc != 0) { fprintf(stderr, "FAIL v2 rc=%d\n", rc); fails++; }
        else expect("RFC7914 v2", dk,
                    "fdbabe1c9d3472007856e7190d01e9fe"
                    "7c6ad7cbc8237830e77376634b373162"
                    "2eaf30d92e22a3886ff109279d9830da"
                    "c727afb94a83ee6d8360cbdfa2cc0640", 64);
    }

    /* Issue #362: hostile scrypt cost parameters from a tampered key
     * file must be REJECTED (rc == -1), never overflow 128*r*N / 128*r*p
     * into a small malloc followed by a huge out-of-bounds write.  Each
     * value below passes the power-of-two / nonzero checks yet overflows
     * the size math on a 32-bit unsigned long. */
    {
        unsigned char dk[64];
        if (lcsas_scrypt((unsigned char *)"", 0, (unsigned char *)"", 0,
                         (1UL << 30), 8, 1, dk, 64) != -1) {
            fprintf(stderr, "FAIL scrypt accepted overflowing N\n"); fails++;
        }
        if (lcsas_scrypt((unsigned char *)"", 0, (unsigned char *)"", 0,
                         1024, (1UL << 25), 1, dk, 64) != -1) {
            fprintf(stderr, "FAIL scrypt accepted overflowing r\n"); fails++;
        }
        if (lcsas_scrypt((unsigned char *)"", 0, (unsigned char *)"", 0,
                         1024, 8, (1UL << 30), dk, 64) != -1) {
            fprintf(stderr, "FAIL scrypt accepted overflowing p\n"); fails++;
        }
    }

    /* Issue #470: the limits themselves.  lcsas_scrypt_params_ok() is
     * checked directly so the accepted edge cases (up to 128x restic's
     * default work) never actually run. */
    {
        static const struct {
            unsigned long N, r, p;
            int want;
            const char *what;
        } c[] = {
            { 32768, 8, 1, 1, "restic default" },
            { 1UL << 20, 8, 1, 1, "restic/rustic maximum (1 GiB)" },
            { 1UL << 22, 1, 8, 1, "work exactly at the cap" },
            { 1UL << 22, 1, 9, 0, "work one step over the cap (p)" },
            { 1UL << 16, 8, 1UL << 10, 0, "work cap via p (old code ran it)" },
            { 1UL << 4, 1UL << 22, 1, 0, "work cap via r" },
            { 1UL << 21, 8, 1, 0, "total memory just over 2 GiB" },
            { 1UL << 23, 1, 1, 1, "total memory 1 GiB with r=1" },
            { 1UL << 24, 1, 2, 0, "total memory over via N with r=1" },
            { 2, 1, 1, 1, "smallest valid" },
            { 1, 1, 1, 0, "N below 2" },
            { 3, 1, 1, 0, "N not a power of two" },
            { 16, 0, 1, 0, "r zero" },
            { 16, 1, 0, 0, "p zero" },
            { 2, 1UL << 25, 1, 0, "N*r past the work cap via r" },
            { 2, (1UL << 25) + 1, 1, 0, "r alone past the work cap" },
            { 1UL << 30, 1, 1, 0, "N alone past the work cap" }
        };
        size_t i;
        for (i = 0; i < sizeof c / sizeof c[0]; i++) {
            int got = lcsas_scrypt_params_ok(c[i].N, c[i].r, c[i].p);
            if (got != c[i].want) {
                fprintf(stderr, "FAIL params_ok(N=%lu, r=%lu, p=%lu) = %d, "
                        "want %d (%s)\n", c[i].N, c[i].r, c[i].p, got,
                        c[i].want, c[i].what);
                fails++;
            }
        }
    }

    /* #470: a key file whose N/r/p passed #362's per-buffer bound but
     * not the work cap (N=2^16, r=8, p=2^10: 64 MiB, 2^29 work) must be
     * refused, not derived.  Before the cap this ran for minutes, so
     * alarm() turns a regression into a failure instead of a hang. */
    {
        unsigned char dk[64];
        int rc;
        alarm(20);
        rc = lcsas_scrypt((unsigned char *)"", 0, (unsigned char *)"", 0,
                          1UL << 16, 8, 1UL << 10, dk, 64);
        alarm(0);
        if (rc != -1) {
            fprintf(stderr, "FAIL scrypt ran past the work cap (rc=%d)\n", rc);
            fails++;
        }
    }

    if (fails == 0) printf("test_scrypt: OK\n");
    return fails ? 1 : 0;
}
