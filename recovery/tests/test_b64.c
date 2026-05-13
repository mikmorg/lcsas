#include "b64.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

static void check(const char *enc, const char *want, size_t wantlen)
{
    unsigned char buf[256];
    long n = lcsas_b64_decode(enc, strlen(enc), buf);
    if (n < 0 || (size_t)n != wantlen || memcmp(buf, want, wantlen) != 0) {
        fprintf(stderr, "FAIL b64 %s: n=%ld (want %zu)\n", enc, n, wantlen);
        fails++;
    }
}

int main(void)
{
    check("",       "",       0);
    check("Zg==",   "f",      1);
    check("Zm8=",   "fo",     2);
    check("Zm9v",   "foo",    3);
    check("Zm9vYg==", "foob", 4);
    check("Zm9vYmFy", "foobar", 6);
    /* with newlines (wrapped input) */
    check("Zm9v\nYmFy", "foobar", 6);

    if (fails == 0) printf("test_b64: OK\n");
    return fails ? 1 : 0;
}
