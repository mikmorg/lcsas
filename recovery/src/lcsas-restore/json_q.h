/*
 * json_q.h -- minimal JSON tokenizer + typed accessors.
 *
 * Designed for the structure of restic's key files, config files, and
 * tree blobs:
 *   - Top-level object.
 *   - String, integer, and array fields.
 *   - UTF-8 strings with limited escape support (\\, \", \n, \t, \r,
 *     \b, \f, \/, \uXXXX -> UTF-8).
 *
 * Tokens are stored in a caller-provided array.  No allocations.
 *
 * Replaces vendored jsmn for this codebase.  See recovery/docs/BUILD.txt
 * for the rationale (license simplicity + restic-specific tailoring).
 */
#ifndef LCSAS_JSON_Q_H
#define LCSAS_JSON_Q_H

#include <stddef.h>

typedef enum {
    LCSAS_JSON_INVALID = 0,
    LCSAS_JSON_OBJECT,
    LCSAS_JSON_ARRAY,
    LCSAS_JSON_STRING,
    LCSAS_JSON_NUMBER,
    LCSAS_JSON_TRUE,
    LCSAS_JSON_FALSE,
    LCSAS_JSON_NULL
} lcsas_json_type;

typedef struct {
    lcsas_json_type type;
    size_t start;     /* byte offset of value start (for strings, after the opening quote) */
    size_t end;       /* exclusive end offset (for strings, of the closing quote) */
    long size;        /* for OBJECT: # of key/value pairs; for ARRAY: # of elements;
                       *  for STRING: byte length of raw source between quotes */
    long parent;      /* index of parent token, or -1 */
} lcsas_json_tok;

/*
 * Tokenize `src` (length `len`).  Returns the number of tokens parsed
 * on success, or a negative value on error:
 *   -1: invalid JSON syntax
 *   -2: ran out of tokens (caller must enlarge `toks`)
 *
 * Tokens are returned in document order: root is toks[0].
 */
long lcsas_json_parse(const char *src, size_t len,
                      lcsas_json_tok *toks, size_t max_toks);

/*
 * Parse with a heap-grown token buffer (T1C-01).  The tokenizer core
 * (lcsas_json_parse) stays allocation-free; this wrapper is the
 * documented exception so call sites never have to guess a fixed cap.
 *
 * Starts at `initial_toks`, doubles on a cap-hit (-2), and stops
 * growing at min(len + 1 tokens, lcsas_json_max_tok_bytes).  The
 * len+1 ceiling is exact: every token consumes at least one source
 * byte, so a document of `len` bytes never yields more than `len`
 * tokens (the +1 covers an empty document / root literal).
 *
 * On success `*toks_out` is malloc'd (caller frees) and the token
 * count is returned.  On failure `*toks_out` is set to NULL and:
 *   -1: malformed JSON
 *   -2: still over the ceiling after growth (input too large for tier-1)
 *   -3: out of memory
 */
long lcsas_json_parse_alloc(const char *src, size_t len,
                            lcsas_json_tok **toks_out,
                            size_t initial_toks);

/*
 * Ceiling on token-buffer memory for lcsas_json_parse_alloc, in bytes.
 * Defaults to 256 MiB.  Bounds heap growth on 32-bit targets (armv7)
 * where an unbounded doubling could exhaust the address space.  main.c
 * lets LCSAS_MAX_JSON_MIB override it (test seam + escape hatch).
 */
extern size_t lcsas_json_max_tok_bytes;

/*
 * Look up `key` (NUL-terminated) inside the object at `toks[obj_idx]`.
 * Returns the token index of the value, or -1 if not found.
 */
long lcsas_json_obj_get(const char *src,
                        const lcsas_json_tok *toks,
                        long obj_idx,
                        const char *key);

/*
 * Decode a STRING token into a NUL-terminated buffer.  Returns the
 * decoded length on success, or -1 on error.  `out_cap` is the size
 * of `out` in bytes (including the trailing NUL).  If the decoded
 * value would not fit -- including the trailing NUL -- the function
 * returns -1 without overflowing the buffer.  `out_cap == 0` always
 * returns -1 (no write).  This is enforced in code, not by convention.
 */
long lcsas_json_decode_string(const char *src,
                              const lcsas_json_tok *tok,
                              char *out, size_t out_cap);

/*
 * Parse a NUMBER token as a long long.  Returns 0 on success.
 */
int lcsas_json_decode_int(const char *src,
                          const lcsas_json_tok *tok,
                          long long *out);

#endif
