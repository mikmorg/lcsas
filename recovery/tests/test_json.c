/*
 * test_json.c -- minimal JSON tokenizer tests.
 */
#include "json_q.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

int main(void)
{
    /* Simple object */
    {
        const char *src =
            "{\"name\":\"hello\",\"N\":32768,\"r\":8,\"p\":1}";
        lcsas_json_tok toks[32];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 32);
        long name_idx, n_idx;
        char buf[64];
        long long vN;

        if (ntoks <= 0) { fprintf(stderr, "FAIL parse rc=%ld\n", ntoks); fails++; }
        if (toks[0].type != LCSAS_JSON_OBJECT) { fprintf(stderr, "FAIL root type\n"); fails++; }
        if (toks[0].size != 4) { fprintf(stderr, "FAIL root size=%ld\n", toks[0].size); fails++; }

        name_idx = lcsas_json_obj_get(src, toks, 0, "name");
        if (name_idx < 0) { fprintf(stderr, "FAIL get name\n"); fails++; }
        if (lcsas_json_decode_string(src, &toks[name_idx], buf, sizeof buf) < 0
                || strcmp(buf, "hello") != 0) {
            fprintf(stderr, "FAIL decode name: got %s\n", buf);
            fails++;
        }

        n_idx = lcsas_json_obj_get(src, toks, 0, "N");
        if (n_idx < 0) { fprintf(stderr, "FAIL get N\n"); fails++; }
        if (lcsas_json_decode_int(src, &toks[n_idx], &vN) != 0 || vN != 32768) {
            fprintf(stderr, "FAIL decode N: got %lld\n", vN); fails++;
        }
    }

    /* Escapes */
    {
        const char *src = "{\"k\":\"a\\nb\\tc\\\"d\\\\e\"}";
        lcsas_json_tok toks[8];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 8);
        long k_idx;
        char buf[32];
        if (ntoks <= 0) { fprintf(stderr, "FAIL escape parse\n"); fails++; }
        k_idx = lcsas_json_obj_get(src, toks, 0, "k");
        if (lcsas_json_decode_string(src, &toks[k_idx], buf, sizeof buf) < 0) {
            fprintf(stderr, "FAIL decode escapes\n"); fails++;
        } else if (strcmp(buf, "a\nb\tc\"d\\e") != 0) {
            fprintf(stderr, "FAIL escape value: %s\n", buf); fails++;
        }
    }

    /* Missing key */
    {
        const char *src = "{\"a\":1}";
        lcsas_json_tok toks[8];
        lcsas_json_parse(src, strlen(src), toks, 8);
        if (lcsas_json_obj_get(src, toks, 0, "missing") != -1) {
            fprintf(stderr, "FAIL missing key\n"); fails++;
        }
    }

    /* Empty object */
    {
        const char *src = "{}";
        lcsas_json_tok toks[4];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        if (ntoks != 1 || toks[0].size != 0) {
            fprintf(stderr, "FAIL empty obj ntoks=%ld size=%ld\n", ntoks, toks[0].size);
            fails++;
        }
    }

    /* Array of strings */
    {
        const char *src = "[\"x\",\"y\",\"z\"]";
        lcsas_json_tok toks[8];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 8);
        if (ntoks != 4 || toks[0].type != LCSAS_JSON_ARRAY || toks[0].size != 3) {
            fprintf(stderr, "FAIL array ntoks=%ld\n", ntoks); fails++;
        }
    }

    if (fails == 0) printf("test_json: OK\n");
    return fails ? 1 : 0;
}
