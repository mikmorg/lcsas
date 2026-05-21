/*
 * test_json_decode_string.c -- bounds-checking tests for
 * lcsas_json_decode_string.
 *
 * Regression test for issue #148: prior to the fix, the decoder wrote
 * to a caller-supplied buffer with no out_cap parameter and no bounds
 * check, allowing a 4 KiB tree-blob "name" or 33-byte "type" to
 * overflow stack buffers in tree.c / repo.c.
 *
 * Each case parses a JSON object, then calls lcsas_json_decode_string
 * with a deliberately-sized output buffer.  We use a guard byte AFTER
 * the buffer to catch overflows that would otherwise look like success
 * under the old (no-bounds) signature.
 */
#include "json_q.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

#define GUARD 0xAB

/*
 * Parse src and return the token index of the value of `key`, or -1.
 * Stores tokens in caller-provided array.
 */
static long
parse_and_find(const char *src,
               const char *key,
               lcsas_json_tok *toks,
               size_t max_toks)
{
    long ntoks = lcsas_json_parse(src, strlen(src), toks, max_toks);
    if (ntoks <= 0) return -1;
    return lcsas_json_obj_get(src, toks, 0, key);
}

int main(void)
{
    /* 1. Normal call with sufficient out_cap returns correct length
     *    and NUL-terminates. */
    {
        const char *src = "{\"k\":\"hello\"}";
        lcsas_json_tok toks[8];
        char buf[16];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, sizeof buf);
        if (rc != 5) {
            fprintf(stderr, "FAIL normal: rc=%ld\n", rc); fails++;
        }
        if (strcmp(buf, "hello") != 0) {
            fprintf(stderr, "FAIL normal: buf=%s\n", buf); fails++;
        }
        if (buf[6] != (char)GUARD) {
            fprintf(stderr, "FAIL normal: guard clobbered\n"); fails++;
        }
    }

    /* 2. Long input + small out_cap returns -1, no overflow. */
    {
        /* 16-char string into a 4-byte buffer. */
        const char *src = "{\"k\":\"abcdefghijklmnop\"}";
        lcsas_json_tok toks[8];
        char buf[8]; /* buf[0..3] = output area, buf[4..7] = guard */
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 4);
        if (rc != -1) {
            fprintf(stderr, "FAIL overflow: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        /* Guard bytes past out_cap MUST remain unchanged. */
        if (buf[4] != (char)GUARD || buf[5] != (char)GUARD
                || buf[6] != (char)GUARD || buf[7] != (char)GUARD) {
            fprintf(stderr, "FAIL overflow: guard clobbered\n"); fails++;
        }
    }

    /* 3. out_cap == 0 returns -1 immediately, no write. */
    {
        const char *src = "{\"k\":\"x\"}";
        lcsas_json_tok toks[8];
        char buf[4];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 0);
        if (rc != -1) {
            fprintf(stderr, "FAIL cap0: rc=%ld (expected -1)\n", rc); fails++;
        }
        if (buf[0] != (char)GUARD || buf[1] != (char)GUARD
                || buf[2] != (char)GUARD || buf[3] != (char)GUARD) {
            fprintf(stderr, "FAIL cap0: buf written\n"); fails++;
        }
    }

    /* 4a. out_cap == 1 with an empty source string succeeds (only NUL). */
    {
        const char *src = "{\"k\":\"\"}";
        lcsas_json_tok toks[8];
        char buf[4];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 1);
        if (rc != 0) {
            fprintf(stderr, "FAIL cap1-empty: rc=%ld (expected 0)\n", rc);
            fails++;
        }
        if (buf[0] != '\0') {
            fprintf(stderr, "FAIL cap1-empty: not NUL-terminated\n"); fails++;
        }
        if (buf[1] != (char)GUARD) {
            fprintf(stderr, "FAIL cap1-empty: guard clobbered\n"); fails++;
        }
    }

    /* 4b. out_cap == 1 with any non-empty content returns -1. */
    {
        const char *src = "{\"k\":\"a\"}";
        lcsas_json_tok toks[8];
        char buf[4];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 1);
        if (rc != -1) {
            fprintf(stderr, "FAIL cap1-nonempty: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        /* No byte past the cap should be touched. */
        if (buf[1] != (char)GUARD) {
            fprintf(stderr, "FAIL cap1-nonempty: guard clobbered\n"); fails++;
        }
    }

    /* 5. Escape-sequence overflow: é decodes to 2 UTF-8 bytes (0xC3 0xA9)
     *    plus a NUL terminator -- needs out_cap >= 3.  Pass out_cap=1 and
     *    expect -1 with no buffer write past the cap.
     *    Also test the multi-byte boundary case where one byte would fit
     *    but the second byte of the codepoint would not. */
    {
        const char *src = "{\"k\":\"\\u00E9\"}";
        lcsas_json_tok toks[8];
        char buf[8];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 1);
        if (rc != -1) {
            fprintf(stderr, "FAIL uesc-cap1: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        if (buf[1] != (char)GUARD) {
            fprintf(stderr, "FAIL uesc-cap1: guard clobbered\n"); fails++;
        }

        /* out_cap = 2 still cannot fit (need 2 bytes for codepoint + 1 NUL). */
        memset(buf, GUARD, sizeof buf);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 2);
        if (rc != -1) {
            fprintf(stderr, "FAIL uesc-cap2: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        /* The bounds check is per-codepoint: we check room for ALL
         * bytes of the encoding before writing any.  Nothing past the
         * cap should be touched. */
        if (buf[2] != (char)GUARD || buf[3] != (char)GUARD) {
            fprintf(stderr, "FAIL uesc-cap2: guard clobbered\n"); fails++;
        }

        /* out_cap = 3 fits exactly: 0xC3 0xA9 0x00. */
        memset(buf, GUARD, sizeof buf);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 3);
        if (rc != 2) {
            fprintf(stderr, "FAIL uesc-cap3: rc=%ld (expected 2)\n", rc);
            fails++;
        }
        if ((unsigned char)buf[0] != 0xC3
                || (unsigned char)buf[1] != 0xA9
                || buf[2] != '\0') {
            fprintf(stderr, "FAIL uesc-cap3: bad bytes %02x %02x %02x\n",
                    (unsigned char)buf[0], (unsigned char)buf[1],
                    (unsigned char)buf[2]);
            fails++;
        }
        if (buf[3] != (char)GUARD) {
            fprintf(stderr, "FAIL uesc-cap3: guard clobbered\n"); fails++;
        }
    }

    /* 6. 3-byte UTF-8 escape with insufficient capacity (regression for
     *    the BMP branch that writes 3 bytes per codepoint). */
    {
        const char *src = "{\"k\":\"\\u4E2D\"}"; /* 'CJK middle' -> 0xE4 0xB8 0xAD */
        lcsas_json_tok toks[8];
        char buf[8];
        long idx, rc;
        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 3);
        if (rc != -1) {
            fprintf(stderr, "FAIL uesc3-cap3: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        if (buf[3] != (char)GUARD) {
            fprintf(stderr, "FAIL uesc3-cap3: guard clobbered\n"); fails++;
        }

        /* out_cap = 4 fits exactly. */
        memset(buf, GUARD, sizeof buf);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, 4);
        if (rc != 3) {
            fprintf(stderr, "FAIL uesc3-cap4: rc=%ld (expected 3)\n", rc);
            fails++;
        }
        if ((unsigned char)buf[0] != 0xE4
                || (unsigned char)buf[1] != 0xB8
                || (unsigned char)buf[2] != 0xAD
                || buf[3] != '\0') {
            fprintf(stderr, "FAIL uesc3-cap4: bad bytes\n"); fails++;
        }
    }

    /* 7. The original tree.c attack: hostile blob with a > 1 KiB "name"
     *    no longer overflows a 1024-byte stack buffer. */
    {
        /* Build a JSON blob with a 2000-char string value. */
        static char src[4096];
        lcsas_json_tok toks[8];
        char buf[1024];
        long idx, rc;
        size_t p = 0, i;
        src[p++] = '{';
        src[p++] = '"';
        src[p++] = 'k';
        src[p++] = '"';
        src[p++] = ':';
        src[p++] = '"';
        for (i = 0; i < 2000; i++) src[p++] = 'A';
        src[p++] = '"';
        src[p++] = '}';
        src[p] = '\0';

        memset(buf, GUARD, sizeof buf);
        idx = parse_and_find(src, "k", toks, 8);
        rc = lcsas_json_decode_string(src, &toks[idx], buf, sizeof buf);
        if (rc != -1) {
            fprintf(stderr, "FAIL hostile: rc=%ld (expected -1)\n", rc);
            fails++;
        }
        /* The byte just past the buffer's last usable index (which would
         * be the next stack frame in the real attack) must be unchanged.
         * We can't probe past `buf` portably; instead we rely on the
         * earlier guard tests and the return value here.  ASan, if
         * present, would catch any actual out-of-bounds write. */
    }

    if (fails == 0) printf("test_json_decode_string: OK\n");
    return fails ? 1 : 0;
}
