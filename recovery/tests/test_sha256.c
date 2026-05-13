/*
 * test_sha256.c -- FIPS 180-4 SHA-256 test vectors.
 *
 * Vectors from FIPS PUB 180-2 Appendix B (identical in 180-4).
 */
#include "sha256.h"
#include <stdio.h>
#include <string.h>

static int
hex_eq(const unsigned char *got, const char *expect)
{
    int i;
    char buf[3];
    unsigned int v;
    for (i = 0; i < 32; i++) {
        buf[0] = expect[i * 2];
        buf[1] = expect[i * 2 + 1];
        buf[2] = '\0';
        if (sscanf(buf, "%x", &v) != 1) return 0;
        if (got[i] != (unsigned char)v) return 0;
    }
    return 1;
}

static int fails = 0;

static void
check(const char *label, const void *data, size_t len, const char *expect)
{
    unsigned char out[32];
    lcsas_sha256(data, len, out);
    if (!hex_eq(out, expect)) {
        int i;
        fprintf(stderr, "FAIL %s\n  want %s\n  got  ", label, expect);
        for (i = 0; i < 32; i++) fprintf(stderr, "%02x", out[i]);
        fprintf(stderr, "\n");
        fails++;
    }
}

int main(void)
{
    /* FIPS 180-2 §B.1: "abc" */
    check("abc", "abc", 3,
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");

    /* FIPS 180-2 §B.2: 56-byte message */
    check("56-byte",
          "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", 56,
          "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");

    /* FIPS 180-2 §B.3: 1,000,000 'a' */
    {
        lcsas_sha256_ctx c;
        unsigned char out[32];
        char buf[1000];
        int i;
        memset(buf, 'a', sizeof buf);
        lcsas_sha256_init(&c);
        for (i = 0; i < 1000; i++) lcsas_sha256_update(&c, buf, sizeof buf);
        lcsas_sha256_final(&c, out);
        if (!hex_eq(out, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0")) {
            fprintf(stderr, "FAIL 1M-a\n");
            fails++;
        }
    }

    /* Empty input */
    check("empty", "", 0,
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");

    if (fails == 0) printf("test_sha256: OK\n");
    return fails ? 1 : 0;
}
