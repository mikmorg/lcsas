/*
 * test_aes.c -- FIPS 197 + NIST SP 800-38A test vectors.
 *
 * Vectors:
 *   - AES-128-ECB single block from FIPS 197 Appendix C.1
 *   - AES-256-ECB single block from FIPS 197 Appendix C.3
 *   - AES-256-CTR multi-block from NIST SP 800-38A Appendix F.5.5
 */
#include "aes.h"
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
    unsigned char want[64];
    if (parse_hex(want_hex, want, n) < 0) {
        fprintf(stderr, "bad want hex in %s\n", label);
        fails++;
        return;
    }
    if (memcmp(got, want, n) != 0) {
        size_t i;
        fprintf(stderr, "FAIL %s\n  want %s\n  got  ", label, want_hex);
        for (i = 0; i < n; i++) fprintf(stderr, "%02x", got[i]);
        fprintf(stderr, "\n");
        fails++;
    }
}

int main(void)
{
    /* FIPS 197 §C.1 AES-128 example. */
    {
        unsigned char key[16];
        unsigned char pt[16];
        unsigned char ct[16];
        lcsas_aes128_key k;
        parse_hex("000102030405060708090a0b0c0d0e0f", key, 16);
        parse_hex("00112233445566778899aabbccddeeff", pt, 16);
        lcsas_aes128_set_key(&k, key);
        lcsas_aes128_encrypt(&k, pt, ct);
        expect("AES-128 FIPS C.1", ct,
               "69c4e0d86a7b0430d8cdb78070b4c55a", 16);
    }

    /* FIPS 197 §C.3 AES-256 example. */
    {
        unsigned char key[32];
        unsigned char pt[16];
        unsigned char ct[16];
        lcsas_aes256_key k;
        parse_hex("000102030405060708090a0b0c0d0e0f"
                  "101112131415161718191a1b1c1d1e1f", key, 32);
        parse_hex("00112233445566778899aabbccddeeff", pt, 16);
        lcsas_aes256_set_key(&k, key);
        lcsas_aes256_encrypt(&k, pt, ct);
        expect("AES-256 FIPS C.3", ct,
               "8ea2b7ca516745bfeafc49904b496089", 16);
    }

    /* NIST SP 800-38A Appendix F.5.5: AES-256-CTR. */
    {
        unsigned char key[32];
        unsigned char iv[16];
        unsigned char pt[64];
        unsigned char ct[64];
        lcsas_aes256_key k;
        parse_hex("603deb1015ca71be2b73aef0857d7781"
                  "1f352c073b6108d72d9810a30914dff4", key, 32);
        parse_hex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff", iv, 16);
        parse_hex("6bc1bee22e409f96e93d7e117393172a"
                  "ae2d8a571e03ac9c9eb76fac45af8e51"
                  "30c81c46a35ce411e5fbc1191a0a52ef"
                  "f69f2445df4f9b17ad2b417be66c3710", pt, 64);
        lcsas_aes256_set_key(&k, key);
        lcsas_aes256_ctr(&k, iv, pt, ct, 64);
        expect("AES-256-CTR SP800-38A F.5.5", ct,
               "601ec313775789a5b7a7f504bbf3d228"
               "f443e3ca4d62b59aca84e990cacaf5c5"
               "2b0930daa23de94ce87017ba2d84988d"
               "dfc9c58db67aada613c2dd08457941a6", 64);
    }

    if (fails == 0) printf("test_aes: OK\n");
    return fails ? 1 : 0;
}
