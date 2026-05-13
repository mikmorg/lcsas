/*
 * repo.c -- restic repository reader.
 */
#include "repo.h"
#include "aes.h"
#include "poly1305.h"
#include "sha256.h"
#include "scrypt.h"
#include "b64.h"
#include "hex.h"
#include "lcsas_io.h"
#include "json_q.h"
#include "zstd_dec.h"

#include "disc_locator.h"
#include "posix_compat.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* zstd magic; if we see this in plaintext we cannot proceed yet. */
static const unsigned char ZSTD_MAGIC[4] = { 0x28, 0xB5, 0x2F, 0xFD };

int
lcsas_repo_decrypt(const lcsas_master_key *key,
                   const unsigned char *data, size_t data_len,
                   unsigned char *out, size_t *out_len)
{
    const unsigned char *iv;
    const unsigned char *ct;
    const unsigned char *mac;
    size_t ct_len;
    unsigned char s[16];
    unsigned char tag[16];
    lcsas_aes128_key mac_kk;
    lcsas_aes256_key data_kk;

    if (data_len < 33) return -1;
    iv = data;
    ct = data + 16;
    ct_len = data_len - 32;
    mac = data + data_len - 16;

    lcsas_aes128_set_key(&mac_kk, key->mac_k);
    lcsas_aes128_encrypt(&mac_kk, iv, s);
    lcsas_poly1305_mac(key->mac_r, s, ct, ct_len, tag);
    if (lcsas_ct_memcmp(tag, mac, 16) != 0) return -1;

    lcsas_aes256_set_key(&data_kk, key->encrypt);
    lcsas_aes256_ctr(&data_kk, iv, ct, out, ct_len);
    *out_len = ct_len;
    return 0;
}

/* Decrypt a base64 field, returning a malloc'd buffer or NULL. */
static unsigned char *
b64_decode_field(const char *src,
                 const lcsas_json_tok *toks, long idx,
                 size_t *out_len)
{
    long n;
    unsigned char *raw;
    if (idx < 0) return NULL;
    if (toks[idx].type != LCSAS_JSON_STRING) return NULL;
    n = lcsas_b64_decode(src + toks[idx].start,
                         toks[idx].end - toks[idx].start, NULL);
    if (n < 0) return NULL;
    raw = (unsigned char *)malloc((size_t)n + 1);
    if (!raw) return NULL;
    n = lcsas_b64_decode(src + toks[idx].start,
                        toks[idx].end - toks[idx].start, raw);
    if (n < 0) { free(raw); return NULL; }
    raw[n] = '\0';
    *out_len = (size_t)n;
    return raw;
}

int
lcsas_repo_load_key_file(const char *path,
                         const unsigned char *password, size_t pw_len,
                         lcsas_master_key *mk)
{
    unsigned char *file_buf = NULL;
    size_t file_len = 0;
    lcsas_json_tok toks[64];
    long ntoks;
    long salt_idx, n_idx, r_idx, p_idx, data_idx;
    unsigned char *salt = NULL;
    size_t salt_len = 0;
    unsigned char *encrypted = NULL;
    size_t encrypted_len = 0;
    long long N = 32768, R = 8, P = 1;
    unsigned char derived[64];
    lcsas_master_key kek;
    unsigned char *master_json = NULL;
    size_t master_len = 0;
    lcsas_json_tok mtoks[64];
    long mntoks;
    long e_idx, mac_idx, mk_idx, mr_idx;
    unsigned char *eb = NULL, *mkb = NULL, *mrb = NULL;
    size_t eb_n = 0, mkb_n = 0, mrb_n = 0;
    int rc = -1;

    if (lcsas_read_file(path, &file_buf, &file_len) != 0) return -1;
    ntoks = lcsas_json_parse((const char *)file_buf, file_len, toks, 64);
    if (ntoks <= 0) goto out;
    if (toks[0].type != LCSAS_JSON_OBJECT) goto out;

    salt_idx = lcsas_json_obj_get((char *)file_buf, toks, 0, "salt");
    n_idx    = lcsas_json_obj_get((char *)file_buf, toks, 0, "N");
    r_idx    = lcsas_json_obj_get((char *)file_buf, toks, 0, "r");
    p_idx    = lcsas_json_obj_get((char *)file_buf, toks, 0, "p");
    data_idx = lcsas_json_obj_get((char *)file_buf, toks, 0, "data");
    if (salt_idx < 0 || data_idx < 0) goto out;

    salt      = b64_decode_field((char *)file_buf, toks, salt_idx, &salt_len);
    encrypted = b64_decode_field((char *)file_buf, toks, data_idx, &encrypted_len);
    if (!salt || !encrypted) goto out;

    if (n_idx >= 0) lcsas_json_decode_int((char *)file_buf, &toks[n_idx], &N);
    if (r_idx >= 0) lcsas_json_decode_int((char *)file_buf, &toks[r_idx], &R);
    if (p_idx >= 0) lcsas_json_decode_int((char *)file_buf, &toks[p_idx], &P);

    if (lcsas_scrypt(password, pw_len, salt, salt_len,
                     (unsigned long)N, (unsigned long)R, (unsigned long)P,
                     derived, 64) != 0) goto out;

    {
        size_t i;
        for (i = 0; i < 32; i++) kek.encrypt[i] = derived[i];
        for (i = 0; i < 16; i++) kek.mac_k[i]   = derived[32 + i];
        for (i = 0; i < 16; i++) kek.mac_r[i]   = derived[48 + i];
    }

    master_json = (unsigned char *)malloc(encrypted_len + 1);
    if (!master_json) goto out;
    if (lcsas_repo_decrypt(&kek, encrypted, encrypted_len,
                           master_json, &master_len) != 0) goto out;
    master_json[master_len] = '\0';

    mntoks = lcsas_json_parse((const char *)master_json, master_len, mtoks, 64);
    if (mntoks <= 0) goto out;

    e_idx   = lcsas_json_obj_get((char *)master_json, mtoks, 0, "encrypt");
    mac_idx = lcsas_json_obj_get((char *)master_json, mtoks, 0, "mac");
    if (e_idx < 0 || mac_idx < 0) goto out;
    mk_idx  = lcsas_json_obj_get((char *)master_json, mtoks, mac_idx, "k");
    mr_idx  = lcsas_json_obj_get((char *)master_json, mtoks, mac_idx, "r");
    if (mk_idx < 0 || mr_idx < 0) goto out;

    eb  = b64_decode_field((char *)master_json, mtoks, e_idx,  &eb_n);
    mkb = b64_decode_field((char *)master_json, mtoks, mk_idx, &mkb_n);
    mrb = b64_decode_field((char *)master_json, mtoks, mr_idx, &mrb_n);
    if (!eb || !mkb || !mrb || eb_n != 32 || mkb_n != 16 || mrb_n != 16) goto out;

    {
        size_t i;
        for (i = 0; i < 32; i++) mk->encrypt[i] = eb[i];
        for (i = 0; i < 16; i++) mk->mac_k[i]   = mkb[i];
        for (i = 0; i < 16; i++) mk->mac_r[i]   = mrb[i];
    }
    rc = 0;

out:
    free(file_buf); free(salt); free(encrypted);
    free(master_json); free(eb); free(mkb); free(mrb);
    return rc;
}

int
lcsas_repo_load_keys_dir(const char *keys_dir,
                         const unsigned char *password, size_t pw_len,
                         lcsas_master_key *mk)
{
    DIR *d;
    struct dirent *e;
    char names[256][LCSAS_HEX_BLOB_ID_LEN + 1];
    size_t ncount = 0;
    int found = 0;
    size_t i;

    d = opendir(keys_dir);
    if (!d) return -1;
    while ((e = readdir(d)) != NULL && ncount < 256) {
        if (e->d_name[0] == '.') continue;
        if (strlen(e->d_name) > LCSAS_HEX_BLOB_ID_LEN) continue;
        strcpy(names[ncount++], e->d_name);
    }
    closedir(d);

    /* Sort ascending (deterministic). */
    {
        size_t a, b;
        for (a = 0; a < ncount; a++) {
            for (b = a + 1; b < ncount; b++) {
                if (strcmp(names[b], names[a]) < 0) {
                    char tmp[LCSAS_HEX_BLOB_ID_LEN + 1];
                    strcpy(tmp, names[a]);
                    strcpy(names[a], names[b]);
                    strcpy(names[b], tmp);
                }
            }
        }
    }

    for (i = 0; i < ncount; i++) {
        char path[4096];
        snprintf(path, sizeof path, "%s/%s", keys_dir, names[i]);
        if (lcsas_repo_load_key_file(path, password, pw_len, mk) == 0) {
            found = 1;
            break;
        }
    }
    return found ? 0 : -1;
}

int
lcsas_repo_strip_v2_prefix(unsigned char **buf, size_t *len, int *needs_zstd)
{
    if (*len < 1) return -1;
    *needs_zstd = 0;

    if (*len > 5
            && (*buf)[1] == ZSTD_MAGIC[0]
            && (*buf)[2] == ZSTD_MAGIC[1]
            && (*buf)[3] == ZSTD_MAGIC[2]
            && (*buf)[4] == ZSTD_MAGIC[3]) {
        /* compression-type byte (0x01 or 0x02) followed by zstd frame */
        (*buf) += 1;
        (*len) -= 1;
        *needs_zstd = 1;
        return 0;
    }
    if (*len > 1
            && ((*buf)[0] == 0x00 || (*buf)[0] == 0x01 || (*buf)[0] == 0x02)
            && (*buf)[0] != '{') {
        (*buf) += 1;
        (*len) -= 1;
        return 0;
    }
    return 0;  /* v1 — no prefix */
}

/* ---- Blob index ---- */

void
lcsas_blob_index_init(lcsas_blob_index *ix)
{
    ix->entries = NULL;
    ix->count = 0;
    ix->cap = 0;
}

void
lcsas_blob_index_free(lcsas_blob_index *ix)
{
    free(ix->entries);
    ix->entries = NULL;
    ix->count = ix->cap = 0;
}

static int
blob_index_push(lcsas_blob_index *ix, const lcsas_blob_loc *loc)
{
    if (ix->count == ix->cap) {
        size_t newcap = ix->cap ? ix->cap * 2 : 256;
        lcsas_blob_loc *p = (lcsas_blob_loc *)realloc(
            ix->entries, newcap * sizeof(lcsas_blob_loc));
        if (!p) return -1;
        ix->entries = p;
        ix->cap = newcap;
    }
    ix->entries[ix->count++] = *loc;
    return 0;
}

const lcsas_blob_loc *
lcsas_blob_index_find(const lcsas_blob_index *ix,
                      const unsigned char id[LCSAS_BLOB_ID_LEN])
{
    size_t i;
    for (i = 0; i < ix->count; i++) {
        if (lcsas_ct_memcmp(ix->entries[i].id, id, LCSAS_BLOB_ID_LEN) == 0) {
            return &ix->entries[i];
        }
    }
    return NULL;
}

/* Decrypt a repository file and strip any v2 compression prefix.
 * For Phase 1, refuses zstd-compressed file content.
 * Returns malloc'd pointer + len, or NULL on error. */
static unsigned char *
decrypt_repo_file(const char *path, const lcsas_master_key *mk, size_t *out_len)
{
    unsigned char *raw = NULL;
    size_t raw_len = 0;
    unsigned char *pt = NULL;
    size_t pt_len = 0;
    unsigned char *p;
    size_t plen;
    int needs_zstd = 0;
    unsigned char *result = NULL;

    if (lcsas_read_file(path, &raw, &raw_len) != 0) return NULL;
    pt = (unsigned char *)malloc(raw_len + 1);
    if (!pt) { free(raw); return NULL; }
    if (lcsas_repo_decrypt(mk, raw, raw_len, pt, &pt_len) != 0) {
        free(raw); free(pt);
        return NULL;
    }
    free(raw);

    p = pt;
    plen = pt_len;
    if (lcsas_repo_strip_v2_prefix(&p, &plen, &needs_zstd) != 0) {
        free(pt);
        return NULL;
    }
    if (needs_zstd) {
        long dsz = lcsas_zstd_decode(p, plen, NULL, 0);
        unsigned char *out;
        long got;
        if (dsz <= 0 || dsz > (long)(256 * 1024 * 1024)) {
            fprintf(stderr,
                    "ERROR: zstd frame at %s reports invalid size %ld\n",
                    path, dsz);
            free(pt);
            return NULL;
        }
        out = (unsigned char *)malloc((size_t)dsz + 1);
        if (!out) { free(pt); return NULL; }
        got = lcsas_zstd_decode(p, plen, out, (size_t)dsz);
        if (got < 0) {
            fprintf(stderr, "ERROR: zstd decompression failed for %s\n", path);
            free(out); free(pt);
            return NULL;
        }
        out[got] = '\0';
        *out_len = (size_t)got;
        free(pt);
        return out;
    }

    /* Copy (so we can free pt's original head). */
    result = (unsigned char *)malloc(plen + 1);
    if (!result) { free(pt); return NULL; }
    memcpy(result, p, plen);
    result[plen] = '\0';
    *out_len = plen;
    free(pt);
    return result;
}

static int
parse_blob_entry(const char *src,
                 const lcsas_json_tok *toks,
                 long blob_idx,
                 const unsigned char pack_id_raw[32],
                 lcsas_blob_loc *out)
{
    long id_i = lcsas_json_obj_get(src, toks, blob_idx, "id");
    long off_i = lcsas_json_obj_get(src, toks, blob_idx, "offset");
    long len_i = lcsas_json_obj_get(src, toks, blob_idx, "length");
    long type_i = lcsas_json_obj_get(src, toks, blob_idx, "type");
    long ulen_i = lcsas_json_obj_get(src, toks, blob_idx, "uncompressed_length");
    char buf[128];
    long long off, len;

    if (id_i < 0 || off_i < 0 || len_i < 0 || type_i < 0) return -1;
    if (toks[id_i].type != LCSAS_JSON_STRING) return -1;
    if (toks[id_i].size != 64) return -1;
    if (lcsas_hex_decode(src + toks[id_i].start, 32, out->id) != 0) return -1;
    if (lcsas_json_decode_int(src, &toks[off_i], &off) != 0) return -1;
    if (lcsas_json_decode_int(src, &toks[len_i], &len) != 0) return -1;
    out->offset = off;
    out->length = len;
    {
        size_t i;
        for (i = 0; i < 32; i++) out->pack_id[i] = pack_id_raw[i];
    }
    if (lcsas_json_decode_string(src, &toks[type_i], buf) < 0) return -1;
    out->is_tree = (strcmp(buf, "tree") == 0);
    out->uncompressed_length = -1;
    if (ulen_i >= 0) {
        long long ul = -1;
        if (lcsas_json_decode_int(src, &toks[ulen_i], &ul) == 0) {
            out->uncompressed_length = ul;
        }
    }
    return 0;
}

int
lcsas_repo_load_index(const char *repo_path,
                      const lcsas_master_key *mk,
                      lcsas_blob_index *ix)
{
    char index_dir[4096];
    DIR *d;
    struct dirent *e;
    /* Big arrays moved to the heap so we don't overflow Windows'
     * 1 MB default thread stack.  Pass-2 also uses 32768 tokens
     * (~1.3 MB) which alone would blow the stack. */
    char (*names)[72] = NULL;
    size_t ncount = 0;
    char (*super_set)[72] = NULL;
    size_t super_count = 0;
    lcsas_json_tok *pass1_toks = NULL;
    lcsas_json_tok *pass2_toks = NULL;

    size_t i;
    int rc = -1;

    /* calloc (zero-init) because the tok walks below tolerate looking
     * one token past the last-parsed entry; on Windows uninitialized
     * heap is genuinely random and was causing intermittent failures. */
    names      = (char (*)[72])calloc(2048, 72);
    super_set  = (char (*)[72])calloc(8192, 72);
    pass1_toks = (lcsas_json_tok *)calloc(16384, sizeof(lcsas_json_tok));
    pass2_toks = (lcsas_json_tok *)calloc(32768, sizeof(lcsas_json_tok));
    if (!names || !super_set || !pass1_toks || !pass2_toks) goto out;

    snprintf(index_dir, sizeof index_dir, "%s/index", repo_path);
    d = opendir(index_dir);
    if (!d) return -1;
    while ((e = readdir(d)) != NULL && ncount < 2048) {
        if (e->d_name[0] == '.') continue;
        if (strlen(e->d_name) > 64) continue;
        strcpy(names[ncount++], e->d_name);
    }
    closedir(d);

    /* Pass 1: collect supersedes. */
    for (i = 0; i < ncount; i++) {
        char path[4096];
        unsigned char *plain;
        size_t plen;
        lcsas_json_tok *toks = pass1_toks;
        long ntoks;
        long sup_arr;

        snprintf(path, sizeof path, "%s/%s", index_dir, names[i]);
        plain = decrypt_repo_file(path, mk, &plen);
        if (!plain) continue;
        ntoks = lcsas_json_parse((const char *)plain, plen, toks, 16384);
        if (ntoks <= 0) { free(plain); continue; }
        sup_arr = lcsas_json_obj_get((char *)plain, toks, 0, "supersedes");
        if (sup_arr >= 0 && toks[sup_arr].type == LCSAS_JSON_ARRAY) {
            long t;
            long children = toks[sup_arr].size;
            long found = 0;
            for (t = sup_arr + 1; found < children; t++) {
                if (toks[t].parent == sup_arr) {
                    if (toks[t].type == LCSAS_JSON_STRING
                            && super_count < 8192
                            && toks[t].size <= 64) {
                        size_t k;
                        for (k = 0; k < (size_t)toks[t].size; k++) {
                            super_set[super_count][k] =
                                ((char *)plain)[toks[t].start + k];
                        }
                        super_set[super_count][toks[t].size] = '\0';
                        super_count++;
                    }
                    found++;
                }
                if (toks[t].start >= toks[sup_arr].end) break;
            }
        }
        free(plain);
    }

    /* Pass 2: load entries, skipping superseded files. */
    for (i = 0; i < ncount; i++) {
        char path[4096];
        unsigned char *plain;
        size_t plen;
        lcsas_json_tok *toks = pass2_toks;
        long ntoks;
        long packs_arr;
        int superseded = 0;
        size_t s;

        for (s = 0; s < super_count; s++) {
            if (strcmp(super_set[s], names[i]) == 0) { superseded = 1; break; }
        }
        if (superseded) continue;

        snprintf(path, sizeof path, "%s/%s", index_dir, names[i]);
        plain = decrypt_repo_file(path, mk, &plen);
        if (!plain) continue;
        ntoks = lcsas_json_parse((const char *)plain, plen, toks, 32768);
        if (ntoks <= 0) { free(plain); continue; }

        packs_arr = lcsas_json_obj_get((char *)plain, toks, 0, "packs");
        if (packs_arr >= 0 && toks[packs_arr].type == LCSAS_JSON_ARRAY) {
            long pack_idx;
            long pack_count = toks[packs_arr].size;
            long p_found = 0;
            for (pack_idx = packs_arr + 1; p_found < pack_count; pack_idx++) {
                if (toks[pack_idx].parent == packs_arr
                        && toks[pack_idx].type == LCSAS_JSON_OBJECT) {
                    long id_i = lcsas_json_obj_get((char *)plain, toks, pack_idx, "id");
                    long blobs_i = lcsas_json_obj_get((char *)plain, toks, pack_idx, "blobs");
                    unsigned char pack_id[32];
                    long b_idx;
                    long blob_count;
                    long b_found = 0;

                    p_found++;

                    if (id_i < 0 || blobs_i < 0) continue;
                    if (toks[id_i].size != 64) continue;
                    if (lcsas_hex_decode((char *)plain + toks[id_i].start,
                                         32, pack_id) != 0) continue;
                    if (toks[blobs_i].type != LCSAS_JSON_ARRAY) continue;
                    blob_count = toks[blobs_i].size;
                    for (b_idx = blobs_i + 1; b_found < blob_count; b_idx++) {
                        if (toks[b_idx].parent == blobs_i
                                && toks[b_idx].type == LCSAS_JSON_OBJECT) {
                            lcsas_blob_loc loc;
                            b_found++;
                            if (parse_blob_entry((char *)plain, toks, b_idx,
                                                 pack_id, &loc) == 0) {
                                if (blob_index_push(ix, &loc) != 0) {
                                    free(plain);
                                    goto out;
                                }
                            }
                        }
                        if (toks[b_idx].start >= toks[blobs_i].end) break;
                    }
                }
                if (toks[pack_idx].start >= toks[packs_arr].end) break;
            }
        }
        free(plain);
    }
    rc = 0;

out:
    free(names);
    free(super_set);
    free(pass1_toks);
    free(pass2_toks);
    return rc;
}

/* ---- Snapshots ---- */

void
lcsas_snapshot_list_init(lcsas_snapshot_list *l)
{
    l->items = NULL; l->count = 0; l->cap = 0;
}

void
lcsas_snapshot_list_free(lcsas_snapshot_list *l)
{
    free(l->items);
    l->items = NULL; l->count = l->cap = 0;
}

static int
snap_list_push(lcsas_snapshot_list *l, const lcsas_snapshot *s)
{
    if (l->count == l->cap) {
        size_t newcap = l->cap ? l->cap * 2 : 16;
        lcsas_snapshot *p = (lcsas_snapshot *)realloc(
            l->items, newcap * sizeof(lcsas_snapshot));
        if (!p) return -1;
        l->items = p;
        l->cap = newcap;
    }
    l->items[l->count++] = *s;
    return 0;
}

int
lcsas_repo_load_snapshots(const char *repo_path,
                          const lcsas_master_key *mk,
                          lcsas_snapshot_list *out)
{
    char snap_dir[4096];
    DIR *d;
    struct dirent *e;
    int rc = -1;

    snprintf(snap_dir, sizeof snap_dir, "%s/snapshots", repo_path);
    d = opendir(snap_dir);
    if (!d) return -1;

    while ((e = readdir(d)) != NULL) {
        char path[4096];
        unsigned char *plain;
        size_t plen;
        lcsas_json_tok toks[256];
        long ntoks;
        long tree_i, time_i, paths_i;
        lcsas_snapshot snap;

        if (e->d_name[0] == '.') continue;
        if (strlen(e->d_name) > LCSAS_HEX_BLOB_ID_LEN) continue;

        snprintf(path, sizeof path, "%s/%s", snap_dir, e->d_name);
        plain = decrypt_repo_file(path, mk, &plen);
        if (!plain) continue;
        ntoks = lcsas_json_parse((const char *)plain, plen, toks, 256);
        if (ntoks <= 0) { free(plain); continue; }

        memset(&snap, 0, sizeof snap);
        {
            size_t nl = strlen(e->d_name);
            if (nl >= sizeof(snap.file_name)) nl = sizeof(snap.file_name) - 1;
            memcpy(snap.file_name, e->d_name, nl);
            snap.file_name[nl] = '\0';
        }

        tree_i = lcsas_json_obj_get((char *)plain, toks, 0, "tree");
        time_i = lcsas_json_obj_get((char *)plain, toks, 0, "time");
        paths_i = lcsas_json_obj_get((char *)plain, toks, 0, "paths");

        if (tree_i >= 0 && toks[tree_i].type == LCSAS_JSON_STRING) {
            size_t k, n = (size_t)toks[tree_i].size;
            if (n > sizeof(snap.tree_id_hex) - 1) n = sizeof(snap.tree_id_hex) - 1;
            for (k = 0; k < n; k++) {
                snap.tree_id_hex[k] = ((char *)plain)[toks[tree_i].start + k];
            }
            snap.tree_id_hex[n] = '\0';
        }
        if (time_i >= 0) {
            lcsas_json_decode_string((char *)plain, &toks[time_i], snap.time);
        }
        if (paths_i >= 0 && toks[paths_i].type == LCSAS_JSON_ARRAY
                && toks[paths_i].size > 0) {
            long t;
            for (t = paths_i + 1; t <= paths_i + 64; t++) {
                if (toks[t].parent == paths_i
                        && toks[t].type == LCSAS_JSON_STRING) {
                    lcsas_json_decode_string((char *)plain, &toks[t],
                                             snap.first_path);
                    break;
                }
            }
        }

        snap_list_push(out, &snap);
        free(plain);
    }
    closedir(d);

    /* Sort by time string. */
    {
        size_t a, b;
        for (a = 0; a < out->count; a++) {
            for (b = a + 1; b < out->count; b++) {
                if (strcmp(out->items[b].time, out->items[a].time) < 0) {
                    lcsas_snapshot tmp = out->items[a];
                    out->items[a] = out->items[b];
                    out->items[b] = tmp;
                }
            }
        }
    }
    rc = 0;
    return rc;
}

long
lcsas_snapshot_latest(const lcsas_snapshot_list *l)
{
    if (l->count == 0) return -1;
    return (long)(l->count - 1);
}

long
lcsas_snapshot_find(const lcsas_snapshot_list *l, const char *id)
{
    size_t i;
    size_t idlen = 0;
    long match = -1;
    int multi = 0;

    while (id[idlen]) idlen++;

    for (i = 0; i < l->count; i++) {
        if (strcmp(l->items[i].file_name, id) == 0) return (long)i;
    }
    for (i = 0; i < l->count; i++) {
        if (strncmp(l->items[i].file_name, id, idlen) == 0) {
            if (match >= 0) multi = 1;
            match = (long)i;
        }
    }
    if (multi) return -1;
    return match;
}

int
lcsas_repo_read_blob(const char *repo_path,
                     const lcsas_master_key *mk,
                     const lcsas_blob_loc *loc,
                     struct lcsas_disc_locator *extra_locator,
                     unsigned char **out, size_t *out_len)
{
    char path[4096];
    char hex[65];
    int fd;
    unsigned char *enc = NULL;
    unsigned char *pt = NULL;
    size_t pt_len = 0;
    unsigned char digest[32];
    struct stat st;
    int rc = -1;
    int found = 0;

    lcsas_hex_encode(loc->pack_id, 32, hex);
    hex[64] = '\0';

    /* Try two-level layout: data/<XX>/<id> in the primary repo. */
    snprintf(path, sizeof path, "%s/data/%c%c/%s", repo_path, hex[0], hex[1], hex);
    if (stat(path, &st) == 0) {
        found = 1;
    } else {
        /* Flat layout: data/<id> */
        snprintf(path, sizeof path, "%s/data/%s", repo_path, hex);
        if (stat(path, &st) == 0) found = 1;
    }

    /* Fall through to the disc locator if the primary repo doesn't
     * have it.  The locator may scan additional mount points and may
     * prompt the user interactively. */
    if (!found && extra_locator != NULL) {
        if (lcsas_disc_locate_pack(extra_locator, loc->pack_id,
                                   path, sizeof path) == 0) {
            found = 1;
        }
    }

    if (!found) {
        fprintf(stderr, "pack not found: %s\n", hex);
        return -1;
    }

    fd = open(path, O_RDONLY | O_BINARY);
    if (fd < 0) return -1;
    enc = (unsigned char *)malloc((size_t)loc->length);
    if (!enc) { close(fd); return -1; }
    if (lcsas_pread_exact(fd, enc, (size_t)loc->length, loc->offset) != 0) {
        close(fd); free(enc); return -1;
    }
    close(fd);

    pt = (unsigned char *)malloc((size_t)loc->length);
    if (!pt) { free(enc); return -1; }
    if (lcsas_repo_decrypt(mk, enc, (size_t)loc->length, pt, &pt_len) != 0) {
        free(enc); free(pt); return -1;
    }
    free(enc);

    /* Inline pack blobs in restic v2 are zstd-compressed (no prefix byte). */
    if (pt_len >= 4
            && pt[0] == ZSTD_MAGIC[0] && pt[1] == ZSTD_MAGIC[1]
            && pt[2] == ZSTD_MAGIC[2] && pt[3] == ZSTD_MAGIC[3]) {
        long dsz;
        unsigned char *dec;
        long got;

        if (loc->uncompressed_length > 0) {
            dsz = (long)loc->uncompressed_length;
        } else {
            dsz = lcsas_zstd_decode(pt, pt_len, NULL, 0);
        }
        if (dsz <= 0 || dsz > (long)(256 * 1024 * 1024)) {
            fprintf(stderr, "ERROR: bad zstd blob size %ld\n", dsz);
            free(pt);
            return -1;
        }
        dec = (unsigned char *)malloc((size_t)dsz);
        if (!dec) { free(pt); return -1; }
        got = lcsas_zstd_decode(pt, pt_len, dec, (size_t)dsz);
        if (got < 0) {
            fprintf(stderr, "ERROR: zstd blob decompression failed\n");
            free(dec); free(pt);
            return -1;
        }
        free(pt);
        pt = dec;
        pt_len = (size_t)got;
    }

    lcsas_sha256(pt, pt_len, digest);
    if (lcsas_ct_memcmp(digest, loc->id, 32) != 0) {
        fprintf(stderr, "blob hash mismatch\n");
        free(pt);
        return -1;
    }

    *out = pt;
    *out_len = pt_len;
    rc = 0;
    return rc;
}
