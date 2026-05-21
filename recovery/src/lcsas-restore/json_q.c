/*
 * json_q.c -- minimal JSON tokenizer + typed accessors.
 *
 * Strict C89.  No allocations.  Tokens are caller-provided.
 */
#include "json_q.h"

typedef struct {
    const char *src;
    size_t len;
    size_t pos;
    lcsas_json_tok *toks;
    size_t max_toks;
    long ntoks;
    long parent;
} parser;

static int
nyb_v(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static long alloc_tok(parser *p) {
    if ((size_t)p->ntoks >= p->max_toks) return -2;
    p->toks[p->ntoks].type = LCSAS_JSON_INVALID;
    p->toks[p->ntoks].start = 0;
    p->toks[p->ntoks].end = 0;
    p->toks[p->ntoks].size = 0;
    p->toks[p->ntoks].parent = p->parent;
    return p->ntoks++;
}

static void skip_ws(parser *p) {
    while (p->pos < p->len) {
        char c = p->src[p->pos];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->pos++;
        else break;
    }
}

static long parse_value(parser *p);

static long parse_string(parser *p) {
    long idx;
    size_t start;

    if (p->pos >= p->len || p->src[p->pos] != '"') return -1;
    p->pos++;
    start = p->pos;
    while (p->pos < p->len && p->src[p->pos] != '"') {
        if (p->src[p->pos] == '\\') {
            if (p->pos + 1 >= p->len) return -1;
            p->pos += 2;
            continue;
        }
        if ((unsigned char)p->src[p->pos] < 0x20) return -1;
        p->pos++;
    }
    if (p->pos >= p->len) return -1;
    idx = alloc_tok(p);
    if (idx < 0) return idx;
    p->toks[idx].type = LCSAS_JSON_STRING;
    p->toks[idx].start = start;
    p->toks[idx].end = p->pos;
    p->toks[idx].size = (long)(p->pos - start);
    p->pos++; /* skip closing quote */
    return idx;
}

static long parse_number(parser *p) {
    long idx;
    size_t start = p->pos;
    if (p->pos < p->len && p->src[p->pos] == '-') p->pos++;
    while (p->pos < p->len) {
        char c = p->src[p->pos];
        if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
                c == '+' || c == '-') {
            p->pos++;
        } else break;
    }
    if (p->pos == start) return -1;
    idx = alloc_tok(p);
    if (idx < 0) return idx;
    p->toks[idx].type = LCSAS_JSON_NUMBER;
    p->toks[idx].start = start;
    p->toks[idx].end = p->pos;
    return idx;
}

static long parse_literal(parser *p, const char *lit, lcsas_json_type t) {
    size_t i = 0;
    long idx;
    size_t start = p->pos;
    while (lit[i]) {
        if (p->pos >= p->len || p->src[p->pos] != lit[i]) return -1;
        p->pos++; i++;
    }
    idx = alloc_tok(p);
    if (idx < 0) return idx;
    p->toks[idx].type = t;
    p->toks[idx].start = start;
    p->toks[idx].end = p->pos;
    return idx;
}

static long parse_object(parser *p) {
    long obj_idx;
    long count = 0;
    long saved_parent;

    if (p->pos >= p->len || p->src[p->pos] != '{') return -1;
    obj_idx = alloc_tok(p);
    if (obj_idx < 0) return obj_idx;
    p->toks[obj_idx].type = LCSAS_JSON_OBJECT;
    p->toks[obj_idx].start = p->pos;
    p->pos++;
    saved_parent = p->parent;
    p->parent = obj_idx;

    skip_ws(p);
    if (p->pos < p->len && p->src[p->pos] == '}') {
        p->toks[obj_idx].end = p->pos + 1;
        p->toks[obj_idx].size = 0;
        p->pos++;
        p->parent = saved_parent;
        return obj_idx;
    }

    for (;;) {
        long krc, vrc;
        skip_ws(p);
        krc = parse_string(p);
        if (krc < 0) { p->parent = saved_parent; return krc; }
        skip_ws(p);
        if (p->pos >= p->len || p->src[p->pos] != ':') {
            p->parent = saved_parent; return -1;
        }
        p->pos++;
        vrc = parse_value(p);
        if (vrc < 0) { p->parent = saved_parent; return vrc; }
        count++;
        skip_ws(p);
        if (p->pos < p->len && p->src[p->pos] == ',') { p->pos++; continue; }
        if (p->pos < p->len && p->src[p->pos] == '}') {
            p->toks[obj_idx].end = p->pos + 1;
            p->toks[obj_idx].size = count;
            p->pos++;
            p->parent = saved_parent;
            return obj_idx;
        }
        p->parent = saved_parent;
        return -1;
    }
}

static long parse_array(parser *p) {
    long arr_idx;
    long count = 0;
    long saved_parent;

    if (p->pos >= p->len || p->src[p->pos] != '[') return -1;
    arr_idx = alloc_tok(p);
    if (arr_idx < 0) return arr_idx;
    p->toks[arr_idx].type = LCSAS_JSON_ARRAY;
    p->toks[arr_idx].start = p->pos;
    p->pos++;
    saved_parent = p->parent;
    p->parent = arr_idx;

    skip_ws(p);
    if (p->pos < p->len && p->src[p->pos] == ']') {
        p->toks[arr_idx].end = p->pos + 1;
        p->toks[arr_idx].size = 0;
        p->pos++;
        p->parent = saved_parent;
        return arr_idx;
    }

    for (;;) {
        long vrc;
        skip_ws(p);
        vrc = parse_value(p);
        if (vrc < 0) { p->parent = saved_parent; return vrc; }
        count++;
        skip_ws(p);
        if (p->pos < p->len && p->src[p->pos] == ',') { p->pos++; continue; }
        if (p->pos < p->len && p->src[p->pos] == ']') {
            p->toks[arr_idx].end = p->pos + 1;
            p->toks[arr_idx].size = count;
            p->pos++;
            p->parent = saved_parent;
            return arr_idx;
        }
        p->parent = saved_parent;
        return -1;
    }
}

static long parse_value(parser *p) {
    char c;
    skip_ws(p);
    if (p->pos >= p->len) return -1;
    c = p->src[p->pos];
    if (c == '{') return parse_object(p);
    if (c == '[') return parse_array(p);
    if (c == '"') return parse_string(p);
    if (c == 't') return parse_literal(p, "true",  LCSAS_JSON_TRUE);
    if (c == 'f') return parse_literal(p, "false", LCSAS_JSON_FALSE);
    if (c == 'n') return parse_literal(p, "null",  LCSAS_JSON_NULL);
    if (c == '-' || (c >= '0' && c <= '9')) return parse_number(p);
    return -1;
}

long
lcsas_json_parse(const char *src, size_t len,
                 lcsas_json_tok *toks, size_t max_toks)
{
    parser p;
    long root;
    p.src = src; p.len = len; p.pos = 0;
    p.toks = toks; p.max_toks = max_toks;
    p.ntoks = 0; p.parent = -1;

    root = parse_value(&p);
    if (root < 0) return root;
    skip_ws(&p);
    if (p.pos != len) return -1;
    return p.ntoks;
}

/*
 * Find a key inside an object.  Walks tokens after `obj_idx` until
 * `size` keys have been seen.  Inside an object, child tokens are
 * arranged in source order: key, value, key, value, ...  All children
 * have `parent == obj_idx`.
 *
 * Returns the token index of the value, or -1 if not found.
 */
long
lcsas_json_obj_get(const char *src,
                   const lcsas_json_tok *toks,
                   long obj_idx,
                   const char *key)
{
    long i;
    long pairs_seen = 0;
    long target_pairs;
    size_t keylen = 0;
    int next_is_key = 1;   /* In an object, direct children alternate
                            * key, value, key, value, ...  Track parity. */

    if (obj_idx < 0) return -1;
    if (toks[obj_idx].type != LCSAS_JSON_OBJECT) return -1;
    target_pairs = toks[obj_idx].size;
    if (target_pairs == 0) return -1;

    while (key[keylen]) keylen++;

    for (i = obj_idx + 1; pairs_seen < target_pairs; i++) {
        if (toks[i].start >= toks[obj_idx].end) break;
        if (toks[i].parent == obj_idx) {
            if (next_is_key) {
                /* This must be a STRING by JSON syntax. */
                if (toks[i].type == LCSAS_JSON_STRING
                        && (size_t)toks[i].size == keylen) {
                    size_t k;
                    int match = 1;
                    for (k = 0; k < keylen; k++) {
                        if (src[toks[i].start + k] != key[k]) {
                            match = 0; break;
                        }
                    }
                    if (match) return i + 1;
                }
                next_is_key = 0;
            } else {
                pairs_seen++;
                next_is_key = 1;
            }
        }
    }
    return -1;
}

long
lcsas_json_decode_string(const char *src,
                         const lcsas_json_tok *tok,
                         char *out, size_t out_cap)
{
    size_t i;
    size_t n = 0;
    if (tok->type != LCSAS_JSON_STRING) return -1;
    /* Must have room for at least the trailing NUL. */
    if (out_cap == 0) return -1;
    for (i = tok->start; i < tok->end; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c == '\\' && i + 1 < tok->end) {
            char e = src[i + 1];
            switch (e) {
                case '"':  if (n + 1 >= out_cap) return -1; out[n++] = '"';  break;
                case '\\': if (n + 1 >= out_cap) return -1; out[n++] = '\\'; break;
                case '/':  if (n + 1 >= out_cap) return -1; out[n++] = '/';  break;
                case 'b':  if (n + 1 >= out_cap) return -1; out[n++] = '\b'; break;
                case 'f':  if (n + 1 >= out_cap) return -1; out[n++] = '\f'; break;
                case 'n':  if (n + 1 >= out_cap) return -1; out[n++] = '\n'; break;
                case 'r':  if (n + 1 >= out_cap) return -1; out[n++] = '\r'; break;
                case 't':  if (n + 1 >= out_cap) return -1; out[n++] = '\t'; break;
                case 'u': {
                    int hi, lo, b3, b4;
                    unsigned long cp;
                    if (i + 5 >= tok->end) return -1;
                    hi = nyb_v((int)(unsigned char)src[i + 2]);
                    lo = nyb_v((int)(unsigned char)src[i + 3]);
                    b3 = nyb_v((int)(unsigned char)src[i + 4]);
                    b4 = nyb_v((int)(unsigned char)src[i + 5]);
                    if (hi < 0 || lo < 0 || b3 < 0 || b4 < 0) return -1;
                    cp = ((unsigned long)hi << 12) | ((unsigned long)lo << 8) |
                         ((unsigned long)b3 << 4) | (unsigned long)b4;
                    if (cp < 0x80) {
                        if (n + 1 >= out_cap) return -1;
                        out[n++] = (char)cp;
                    } else if (cp < 0x800) {
                        if (n + 2 >= out_cap) return -1;
                        out[n++] = (char)(0xC0 | (cp >> 6));
                        out[n++] = (char)(0x80 | (cp & 0x3F));
                    } else {
                        if (n + 3 >= out_cap) return -1;
                        out[n++] = (char)(0xE0 |  (cp >> 12));
                        out[n++] = (char)(0x80 | ((cp >>  6) & 0x3F));
                        out[n++] = (char)(0x80 |  (cp        & 0x3F));
                    }
                    i += 4;
                    break;
                }
                default: return -1;
            }
            i++; /* skip escape char */
        } else {
            if (n + 1 >= out_cap) return -1;
            out[n++] = (char)c;
        }
    }
    out[n] = '\0';
    return (long)n;
}

int
lcsas_json_decode_int(const char *src,
                      const lcsas_json_tok *tok,
                      long long *out)
{
    long long v = 0;
    int neg = 0;
    size_t i = tok->start;
    if (tok->type != LCSAS_JSON_NUMBER) return -1;
    if (i < tok->end && src[i] == '-') { neg = 1; i++; }
    if (i >= tok->end) return -1;
    while (i < tok->end) {
        char c = src[i];
        if (c < '0' || c > '9') return -1;
        v = v * 10 + (c - '0');
        i++;
    }
    *out = neg ? -v : v;
    return 0;
}
