/*
 * fuzz_json_parse.c -- LibFuzzer harness for the JSON parser.
 *
 * Feeds arbitrary bytes to lcsas_json_parse, then walks every returned
 * token and calls lcsas_json_decode_string (with a fixed 4 KiB buffer)
 * and lcsas_json_obj_get with a set of representative key strings.
 *
 * Compile (from repo root):
 *   clang -fsanitize=fuzzer,address,undefined -O1 -g \
 *         -I recovery/src/lcsas-restore \
 *         recovery/fuzz/fuzz_json_parse.c \
 *         recovery/src/lcsas-restore/json_q.c \
 *         -o recovery/build/fuzz/fuzz_json_parse
 *
 * Run (smoke, 60 seconds):
 *   recovery/build/fuzz/fuzz_json_parse \
 *       -max_total_time=60 \
 *       recovery/fuzz/corpus/json/
 */
#include "json_q.h"
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define MAX_TOKS 4096
#define OUT_CAP  4096

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    lcsas_json_tok toks[MAX_TOKS];
    char out[OUT_CAP];
    long ntoks, i;

    /* Parse the raw input as JSON. */
    ntoks = lcsas_json_parse((const char *)data, size, toks, MAX_TOKS);
    if (ntoks <= 0) return 0;

    /* Walk every STRING token and decode it with the bounded decoder.
     * This exercises the out_cap guard added in issue #148. */
    for (i = 0; i < ntoks; i++) {
        if (toks[i].type == LCSAS_JSON_STRING) {
            lcsas_json_decode_string((const char *)data, &toks[i],
                                     out, OUT_CAP);
        }
    }

    /* Try obj_get with representative restic schema keys. */
    if (ntoks > 0 && toks[0].type == LCSAS_JSON_OBJECT) {
        lcsas_json_obj_get((const char *)data, toks, 0, "id");
        lcsas_json_obj_get((const char *)data, toks, 0, "type");
        lcsas_json_obj_get((const char *)data, toks, 0, "name");
        lcsas_json_obj_get((const char *)data, toks, 0, "blobs");
        lcsas_json_obj_get((const char *)data, toks, 0, "supersedes");
        lcsas_json_obj_get((const char *)data, toks, 0, "time");
        lcsas_json_obj_get((const char *)data, toks, 0, "paths");
        lcsas_json_obj_get((const char *)data, toks, 0, "offset");
        lcsas_json_obj_get((const char *)data, toks, 0, "length");
        lcsas_json_obj_get((const char *)data, toks, 0, "linktarget");
    }

    return 0;
}
