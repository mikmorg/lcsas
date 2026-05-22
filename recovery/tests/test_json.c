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

    /* true / false / null literals (parse_literal coverage) */
    {
        const char *src = "{\"t\":true,\"f\":false,\"n\":null}";
        lcsas_json_tok toks[16];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 16);
        long t_idx, f_idx, n_idx;
        if (ntoks <= 0) { fprintf(stderr, "FAIL parse literals\n"); fails++; }
        t_idx = lcsas_json_obj_get(src, toks, 0, "t");
        f_idx = lcsas_json_obj_get(src, toks, 0, "f");
        n_idx = lcsas_json_obj_get(src, toks, 0, "n");
        if (t_idx < 0 || toks[t_idx].type != LCSAS_JSON_TRUE) {
            fprintf(stderr, "FAIL true literal type=%d\n",
                    t_idx >= 0 ? (int)toks[t_idx].type : -1); fails++;
        }
        if (f_idx < 0 || toks[f_idx].type != LCSAS_JSON_FALSE) {
            fprintf(stderr, "FAIL false literal\n"); fails++;
        }
        if (n_idx < 0 || toks[n_idx].type != LCSAS_JSON_NULL) {
            fprintf(stderr, "FAIL null literal\n"); fails++;
        }
    }

    /* Malformed literal: "tru" should fail to parse cleanly (parse_literal
     * abort path when source diverges from expected literal). */
    {
        const char *src = "tru";
        lcsas_json_tok toks[4];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        if (ntoks > 0) {
            fprintf(stderr, "FAIL truncated literal accepted ntoks=%ld\n",
                    ntoks); fails++;
        }
    }

    /* All escape sequences (json_q.c lines 306-339 — \\, \/, \b, \f, \r). */
    {
        const char *src = "\"\\\\ \\/ \\b \\f \\r\"";
        lcsas_json_tok toks[4];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        char buf[32];
        long got;
        if (ntoks != 1 || toks[0].type != LCSAS_JSON_STRING) {
            fprintf(stderr, "FAIL escape full ntoks=%ld\n", ntoks); fails++;
        }
        got = lcsas_json_decode_string(src, &toks[0], buf, sizeof buf);
        if (got < 0) {
            fprintf(stderr, "FAIL decode all-escapes\n"); fails++;
        } else if (strcmp(buf, "\\ / \b \f \r") != 0) {
            fprintf(stderr, "FAIL escape value: %s\n", buf); fails++;
        }
    }

    /* Invalid escape (\\x) — must return -1, not decode partially. */
    {
        const char *src = "\"\\x\"";
        lcsas_json_tok toks[4];
        char buf[16];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        if (ntoks > 0
                && lcsas_json_decode_string(src, &toks[0], buf, sizeof buf) >= 0) {
            fprintf(stderr, "FAIL invalid escape \\x accepted\n"); fails++;
        }
    }

    /* Invalid hex digit in \u escape — should fail. */
    {
        const char *src = "\"\\uZZZZ\"";
        lcsas_json_tok toks[4];
        char buf[16];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        if (ntoks > 0
                && lcsas_json_decode_string(src, &toks[0], buf, sizeof buf) >= 0) {
            fprintf(stderr, "FAIL invalid \\u accepted\n"); fails++;
        }
    }

    /* Token overflow (alloc_tok return -2): parse a deeply-nested array
     * with only 2 tokens available — the parser must abort cleanly. */
    {
        const char *src = "[1,2,3,4,5,6,7,8,9]";
        lcsas_json_tok toks[2];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 2);
        if (ntoks >= 0) {
            fprintf(stderr, "FAIL token overflow accepted ntoks=%ld\n", ntoks);
            fails++;
        }
    }

    /* Malformed JSON — bare garbage. parse_value returns -1. */
    {
        const char *src = "@@@";
        lcsas_json_tok toks[4];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        if (ntoks > 0) {
            fprintf(stderr, "FAIL garbage accepted ntoks=%ld\n", ntoks);
            fails++;
        }
    }

    /* Object with key but no colon — exercises parse_object's
     * "expected colon" error branch (json_q.c line 140). */
    {
        const char *src = "{\"key\"}";
        lcsas_json_tok toks[8];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 8);
        if (ntoks > 0) {
            fprintf(stderr, "FAIL missing-colon accepted ntoks=%ld\n", ntoks);
            fails++;
        }
    }

    /* Object with stray char after value (no comma, no close brace) —
     * exercises parse_object's "expected comma or close-brace" branch
     * (json_q.c lines 155-156). */
    {
        const char *src = "{\"k\":\"v\"x}";
        lcsas_json_tok toks[8];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 8);
        if (ntoks > 0) {
            fprintf(stderr, "FAIL stray-char-after-value accepted ntoks=%ld\n",
                    ntoks);
            fails++;
        }
    }

    /* Array missing comma between elements — exercises parse_array's
     * "expected comma or close-bracket" branch (json_q.c lines 198-199). */
    {
        const char *src = "[1 2]";
        lcsas_json_tok toks[8];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 8);
        if (ntoks > 0) {
            fprintf(stderr, "FAIL missing-comma array accepted ntoks=%ld\n",
                    ntoks);
            fails++;
        }
    }

    /* ASCII \\u escape — exercises the cp < 0x80 branch in
     * lcsas_json_decode_string (json_q.c lines 324-325). */
    {
        const char *src = "\"\\u0041BC\"";
        lcsas_json_tok toks[4];
        long ntoks = lcsas_json_parse(src, strlen(src), toks, 4);
        char buf[16];
        long got;
        if (ntoks != 1 || toks[0].type != LCSAS_JSON_STRING) {
            fprintf(stderr, "FAIL ASCII unicode escape parse ntoks=%ld\n",
                    ntoks);
            fails++;
        }
        got = lcsas_json_decode_string(src, &toks[0], buf, sizeof buf);
        if (got != 3 || memcmp(buf, "ABC", 3) != 0) {
            fprintf(stderr,
                    "FAIL \\u0041 should decode to 'ABC', got %ld bytes\n", got);
            fails++;
        }
    }

    if (fails == 0) printf("test_json: OK\n");
    return fails ? 1 : 0;
}
