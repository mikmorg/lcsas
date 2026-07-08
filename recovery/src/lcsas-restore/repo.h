/*
 * repo.h -- restic repository reader.
 *
 * Loads the master key by decrypting a key file with the user's
 * password; loads index and snapshot lists; provides blob lookup and
 * decryption.
 *
 * See docs/RESTIC_FORMAT_SPEC.md for the on-disc format.
 */
#ifndef LCSAS_REPO_H
#define LCSAS_REPO_H

#include <stddef.h>

#define LCSAS_BLOB_ID_LEN 32         /* SHA-256 digest length */
#define LCSAS_HEX_BLOB_ID_LEN 64     /* hex string length */

typedef struct {
    unsigned char encrypt[32];       /* AES-256-CTR data key */
    unsigned char mac_k[16];         /* AES-128 key for Poly1305 nonce */
    unsigned char mac_r[16];         /* Poly1305 r key */
} lcsas_master_key;

/*
 * Decrypt restic AEAD ciphertext.  Format: IV(16) || ct || MAC(16).
 *
 *   key:        master key.
 *   data:       full ciphertext bytes.
 *   data_len:   total bytes (>= 33).
 *   out:        caller-allocated buffer of >= (data_len - 32) bytes.
 *   out_len:    on success, set to number of plaintext bytes written.
 *
 * Returns 0 on success, -1 on MAC failure or size error.
 */
int lcsas_repo_decrypt(const lcsas_master_key *key,
                       const unsigned char *data, size_t data_len,
                       unsigned char *out, size_t *out_len);

/*
 * Distinct failure code returned by lcsas_repo_decrypt on a Poly1305
 * MAC mismatch -- the one decrypt outcome that means "this key did not
 * authenticate the ciphertext" (i.e. wrong password/key), as opposed
 * to the generic -1 it returns for a structurally too-short input
 * where no MAC is ever computed.  Same numeric value as
 * LCSAS_REPO_ERR_WRONG_PASSWORD: at the key-file layer a MAC mismatch
 * IS a positively-rejected password.
 */
#define LCSAS_REPO_ERR_MAC (-2)

/*
 * Distinct failure code for "a structurally valid key file POSITIVELY
 * rejected the password" (Poly1305 MAC mismatch on the KEK-encrypted
 * master key).  main.c maps this -- and ONLY this -- to the terminal
 * wrong-password exit status 77 (#384).  Unreadable/absent keys dir,
 * malformed/truncated key files, and allocation failures return the
 * generic -1 so the recovery cascade is NOT told the password is wrong
 * when it may not be.
 */
#define LCSAS_REPO_ERR_WRONG_PASSWORD (-2)

/*
 * Load and decrypt one key file with the given password.  Returns 0
 * on success and fills *mk, LCSAS_REPO_ERR_WRONG_PASSWORD when the
 * file parsed but its MAC rejected the derived key (wrong password),
 * and -1 for any other failure (unreadable, malformed, OOM).
 */
int lcsas_repo_load_key_file(const char *path,
                             const unsigned char *password, size_t pw_len,
                             lcsas_master_key *mk);

/*
 * Try every regular file in keys_dir until one decrypts.  Returns 0
 * on success; LCSAS_REPO_ERR_WRONG_PASSWORD when at least one key
 * file positively rejected the password (and none succeeded); -1 when
 * the directory is unreadable, holds no candidate key files, or an
 * allocation failed -- i.e. "could not even try", not "wrong password".
 */
int lcsas_repo_load_keys_dir(const char *keys_dir,
                             const unsigned char *password, size_t pw_len,
                             lcsas_master_key *mk);

/*
 * Strip a restic v2 leading compression-type byte if present.  Returns
 * pointer to (possibly offset) start and updates *len.
 *
 *   - leading 0x00, 0x01, or 0x02: strip one byte
 *   - if 1..5 begin with the zstd magic 28 b5 2f fd, callers must
 *     decompress (we return success but mark *needs_zstd = 1)
 */
int lcsas_repo_strip_v2_prefix(unsigned char **buf, size_t *len,
                               int *needs_zstd);

/* ---- Blob index ---- */

typedef struct {
    unsigned char id[LCSAS_BLOB_ID_LEN];
    unsigned char pack_id[LCSAS_BLOB_ID_LEN];
    long long offset;
    long long length;
    int is_tree;                     /* 0 = data, 1 = tree */
    long long uncompressed_length;   /* -1 if absent */
} lcsas_blob_loc;

typedef struct {
    lcsas_blob_loc *entries;
    size_t count;
    size_t cap;
} lcsas_blob_index;

void lcsas_blob_index_init(lcsas_blob_index *ix);
void lcsas_blob_index_free(lcsas_blob_index *ix);

/*
 * Walk repo_path/index/, read each index file, decrypt, parse, and
 * accumulate blob locations into *ix.  Respects `supersedes`.
 */
int lcsas_repo_load_index(const char *repo_path,
                          const lcsas_master_key *mk,
                          lcsas_blob_index *ix);

/* Linear search for a blob by 32-byte ID; returns NULL if missing. */
const lcsas_blob_loc *
lcsas_blob_index_find(const lcsas_blob_index *ix,
                      const unsigned char id[LCSAS_BLOB_ID_LEN]);

/* ---- Snapshots ---- */

typedef struct {
    char file_name[LCSAS_HEX_BLOB_ID_LEN + 1];  /* snapshot file name (hex id) */
    char tree_id_hex[LCSAS_HEX_BLOB_ID_LEN + 1];
    char time[64];                              /* ISO 8601 string */
    char first_path[1024];                      /* first path in `paths` list */
} lcsas_snapshot;

typedef struct {
    lcsas_snapshot *items;
    size_t count;
    size_t cap;
} lcsas_snapshot_list;

void lcsas_snapshot_list_init(lcsas_snapshot_list *l);
void lcsas_snapshot_list_free(lcsas_snapshot_list *l);

int lcsas_repo_load_snapshots(const char *repo_path,
                              const lcsas_master_key *mk,
                              lcsas_snapshot_list *out);

/* Return index of latest snapshot (highest time string), or -1 if empty. */
long lcsas_snapshot_latest(const lcsas_snapshot_list *l);

/* Find snapshot by exact match or unique prefix; -1 if not found. */
long lcsas_snapshot_find(const lcsas_snapshot_list *l, const char *id);

/*
 * Read, decrypt, and verify a blob.  Returns malloc'd buffer in *out
 * (caller frees) and length in *out_len.  Returns 0 on success.
 *
 * `repo_path` is searched first (back-compat single-disc behaviour).
 * Then if `extra_locator` is non-NULL, it provides additional pack
 * search paths and optional interactive prompting.
 *
 * Zstd-compressed blobs are decompressed transparently.
 */
struct lcsas_disc_locator;
int lcsas_repo_read_blob(const char *repo_path,
                         const lcsas_master_key *mk,
                         const lcsas_blob_loc *loc,
                         struct lcsas_disc_locator *extra_locator,
                         unsigned char **out, size_t *out_len);

#endif
