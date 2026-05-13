/*
 * test_scrypt.c -- RFC 7914 §11 test vectors.
 */
#include "scrypt.h"
#include <stdio.h>
#include <string.h>

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

    if (fails == 0) printf("test_scrypt: OK\n");
    return fails ? 1 : 0;
}
