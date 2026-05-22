/*
 * test_pbkdf2.c -- PBKDF2-HMAC-SHA-256 test vectors from RFC 6070
 * (the RFC actually targets SHA-1; SHA-256 vectors below are from
 *  https://stackoverflow.com/q/5130513 cross-verified against
 *  OpenSSL `openssl kdf`).
 */
#include "pbkdf2.h"
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
    /* P = "password", S = "salt", c = 1, dkLen = 32. */
    {
        unsigned char dk[32];
        lcsas_pbkdf2_sha256((unsigned char *)"password", 8,
                            (unsigned char *)"salt", 4, 1, dk, 32);
        expect("c=1", dk,
               "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b", 32);
    }

    /* P = "password", S = "salt", c = 2, dkLen = 32. */
    {
        unsigned char dk[32];
        lcsas_pbkdf2_sha256((unsigned char *)"password", 8,
                            (unsigned char *)"salt", 4, 2, dk, 32);
        expect("c=2", dk,
               "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43", 32);
    }

    /* P = "password", S = "salt", c = 4096, dkLen = 32. */
    {
        unsigned char dk[32];
        lcsas_pbkdf2_sha256((unsigned char *)"password", 8,
                            (unsigned char *)"salt", 4, 4096, dk, 32);
        expect("c=4096", dk,
               "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a", 32);
    }

    /* Longer output (dklen 40). */
    {
        unsigned char dk[40];
        lcsas_pbkdf2_sha256((unsigned char *)"passwordPASSWORDpassword", 24,
                            (unsigned char *)"saltSALTsaltSALTsaltSALTsaltSALTsalt", 36,
                            4096, dk, 40);
        expect("c=4096 dklen=40", dk,
               "348c89dbcbd32b2f32d814b8116e84cf2b17347ebc1800181c4e2a1fb8dd53e1c635518c7dac47e9", 40);
    }

    /* Key longer than HMAC block size (64 bytes) — exercises the
     * keylen > BLK branch in pbkdf2.c (lines 27-28) where the key is
     * pre-hashed with SHA-256 before being used as HMAC key. */
    {
        unsigned char long_key[128];
        unsigned char dk[32];
        size_t i;
        for (i = 0; i < sizeof long_key; i++) long_key[i] = (unsigned char)i;
        /* Just verify it doesn't crash and produces deterministic output. */
        lcsas_pbkdf2_sha256(long_key, sizeof long_key,
                            (unsigned char *)"salt", 4,
                            1, dk, 32);
        /* Same inputs should produce same output. */
        {
            unsigned char dk2[32];
            lcsas_pbkdf2_sha256(long_key, sizeof long_key,
                                (unsigned char *)"salt", 4,
                                1, dk2, 32);
            if (memcmp(dk, dk2, 32) != 0) {
                fprintf(stderr, "FAIL: long-key pbkdf2 not deterministic\n");
                fails++;
            }
        }
    }

    if (fails == 0) printf("test_pbkdf2: OK\n");
    return fails ? 1 : 0;
}
