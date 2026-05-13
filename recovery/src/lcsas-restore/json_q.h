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
 * Look up `key` (NUL-terminated) inside the object at `toks[obj_idx]`.
 * Returns the token index of the value, or -1 if not found.
 */
long lcsas_json_obj_get(const char *src,
                        const lcsas_json_tok *toks,
                        long obj_idx,
                        const char *key);

/*
 * Decode a STRING token into a NUL-terminated buffer.  Returns the
 * decoded length on success, or -1 on error.  `out` must be at least
 * (tok.end - tok.start + 1) bytes.
 */
long lcsas_json_decode_string(const char *src,
                              const lcsas_json_tok *tok,
                              char *out);

/*
 * Parse a NUMBER token as a long long.  Returns 0 on success.
 */
int lcsas_json_decode_int(const char *src,
                          const lcsas_json_tok *tok,
                          long long *out);

#endif
