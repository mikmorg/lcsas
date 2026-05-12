#include "hex.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

int main(void)
{
    unsigned char b[8] = { 0xde, 0xad, 0xbe, 0xef, 0x12, 0x34, 0x56, 0x78 };
    char buf[17];
    unsigned char back[8];

    lcsas_hex_encode(b, 8, buf);
    buf[16] = '\0';
    if (strcmp(buf, "deadbeef12345678") != 0) {
        fprintf(stderr, "FAIL hex_encode: got %s\n", buf);
        fails++;
    }

    if (lcsas_hex_decode("DEADbeef12345678", 8, back) != 0) {
        fprintf(stderr, "FAIL hex_decode\n"); fails++;
    } else if (memcmp(back, b, 8) != 0) {
        fprintf(stderr, "FAIL hex_decode roundtrip\n"); fails++;
    }

    if (lcsas_hex_decode("xx", 1, back) == 0) {
        fprintf(stderr, "FAIL hex_decode rejected bad input\n"); fails++;
    }

    /* constant-time compare */
    {
        unsigned char x[4] = { 1, 2, 3, 4 };
        unsigned char y[4] = { 1, 2, 3, 4 };
        unsigned char z[4] = { 1, 2, 3, 5 };
        if (lcsas_ct_memcmp(x, y, 4) != 0) { fprintf(stderr, "FAIL ct eq\n"); fails++; }
        if (lcsas_ct_memcmp(x, z, 4) == 0) { fprintf(stderr, "FAIL ct neq\n"); fails++; }
    }

    if (fails == 0) printf("test_hex: OK\n");
    return fails ? 1 : 0;
}
