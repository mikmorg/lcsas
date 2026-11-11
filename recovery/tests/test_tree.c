/*
 * test_tree.c -- tree-walk guards (T1C-04).
 *
 * Links tree.c with stub definitions of its only two repo.c
 * dependencies (lcsas_blob_index_find + lcsas_repo_read_blob), so we
 * can feed arbitrary tree blobs to lcsas_tree_restore without an
 * encrypted fixture.  Covers:
 *   - depth cap: a tree deeper than lcsas_tree_max_depth fails loud
 *     (rc != 0) instead of SIGSEGV.
 *   - path-length guard: a joined path > 4095 bytes fails loud and
 *     leaves no truncated-path file behind.
 *   - malformed blob: a non-array / garbage tree fails cleanly.
 *   - #653: equal inode numbers are a hardlink only with links > 1 and
 *     the same device_id.
 *   - #654: a read-only directory with children restores (non-root),
 *     and a second run over it succeeds.
 *   - #655: a part-written read-only file is reopened by a resume.
 *   - #661: the resume skip needs size AND mtime to match.
 */
#include "tree.h"
#include "repo.h"
#include "json_q.h"   /* lcsas_json_max_tok_bytes + lcsas_json_tok (#383) */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <unistd.h>
#include <utime.h>

static int fails = 0;

/* ── stub blob source ───────────────────────────────────────────── */

/* The harness hands lcsas_tree_restore a chain of synthetic tree
 * blobs.  g_blob_fn produces the blob for call N; returning a NULL
 * pointer makes lcsas_repo_read_blob fail (terminates the walk). */
static char *(*g_blob_fn)(int call, size_t *len_out);
static int g_call;

const lcsas_blob_loc *
lcsas_blob_index_find(const lcsas_blob_index *ix,
                      const unsigned char id[LCSAS_BLOB_ID_LEN])
{
    static lcsas_blob_loc dummy;
    (void)ix; (void)id;
    return &dummy;
}

int
lcsas_repo_read_blob(const char *repo_path,
                     const lcsas_master_key *mk,
                     const lcsas_blob_loc *loc,
                     struct lcsas_disc_locator *extra_locator,
                     unsigned char **out, size_t *out_len)
{
    char *blob;
    size_t len = 0;
    (void)repo_path; (void)mk; (void)loc; (void)extra_locator;
    if (!g_blob_fn) return -1;
    blob = g_blob_fn(g_call, &len);
    g_call++;
    if (!blob) return -1;
    *out = (unsigned char *)blob;
    *out_len = len;
    return 0;
}

/* ── helpers ────────────────────────────────────────────────────── */

#define TREE_ID \
    "0000000000000000000000000000000000000000000000000000000000000000"

static void rmrf(const char *path)
{
    DIR *d;
    (void)chmod(path, 0700);   /* #654 tests leave 0555 directories */
    d = opendir(path);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            char child[8192];
            struct stat st;
            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
                continue;
            sprintf(child, "%s/%s", path, e->d_name);
            if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
                rmrf(child);
            else
                unlink(child);
        }
        closedir(d);
    }
    rmdir(path);
}

static char *mktarget(void)
{
    static char buf[256];
    sprintf(buf, "/tmp/lcsas_test_tree_%ld", (long)getpid());
    rmrf(buf);
    mkdir(buf, 0700);
    return buf;
}

/* Each call returns a one-node tree: a dir with a 900-char name and a
 * subtree pointer.  The chain never terminates on its own; the depth
 * cap or the path-length guard must stop it. */
static char *deep_chain_blob(int call, size_t *len_out)
{
    static char name[901];
    char *blob = (char *)malloc(2048);
    (void)call;
    if (!blob) return NULL;  /* injected malloc failure: read fails */
    memset(name, 'a', 900);
    name[900] = '\0';
    sprintf(blob,
            "{\"nodes\":[{\"name\":\"%s\",\"type\":\"dir\","
            "\"mode\":493,\"subtree\":\"" TREE_ID "\"}]}", name);
    *len_out = strlen(blob);
    return blob;
}

/* Each call returns a one-node tree: a dir with a short name and a
 * subtree pointer.  Short names keep the joined path well under 4096
 * so only the depth cap can stop the chain. */
static char *deep_chain_short(int call, size_t *len_out)
{
    char *blob = (char *)malloc(256);
    (void)call;
    if (!blob) return NULL;
    sprintf(blob,
            "{\"nodes\":[{\"name\":\"d\",\"type\":\"dir\","
            "\"mode\":493,\"subtree\":\"" TREE_ID "\"}]}");
    *len_out = strlen(blob);
    return blob;
}

/* Garbage that parses but isn't a tree object's "nodes" array. */
static char *garbage_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[-]}");
    if (!blob) return NULL;
    *len_out = strlen(blob);
    return blob;
}

/* Valid JSON whose root token is NOT an object (an array); exercises the
 * "root token not OBJECT" reject that is distinct from the invalid-JSON
 * (parse-fail) path garbage_blob hits (#383). */
static char *not_object_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("[1,2,3]");
    if (!blob) return NULL;
    *len_out = strlen(blob);
    return blob;
}

/* A one-node tree with a symlink whose linktarget decodes to a string
 * containing an embedded NUL (JSON \u0000): decode_path_component returns
 * DECODE_PATH_NUL, so the symlink must be skipped (path-safety guard, #383). */
static char *symlink_nul_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[{\"name\":\"lnk\",\"type\":\"symlink\","
                  "\"linktarget\":\"a\\u0000b\"}]}");
    if (!blob) return NULL;
    *len_out = strlen(blob);
    return blob;
}

/* A small but multi-token tree blob; combined with a tiny
 * lcsas_json_max_tok_bytes ceiling it drives lcsas_json_parse_alloc to
 * return -2 ("too large for tier-1"), #383. */
static char *tiny_tree_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[]}");
    if (!blob) return NULL;
    *len_out = strlen(blob);
    return blob;
}

/* Scripted blobs: call N returns a copy of g_blobs[N] (NULL ends). */
static const char *g_blobs[8];

static char *scripted_blob(int call, size_t *len_out)
{
    char *blob;
    if (call < 0 || call >= 8 || !g_blobs[call]) return NULL;
    blob = strdup(g_blobs[call]);
    if (!blob) return NULL;
    *len_out = strlen(blob);
    return blob;
}

static int run_scripted(const char *target)
{
    lcsas_master_key mk;
    lcsas_blob_index ix;
    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = scripted_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;
    return lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                              target, target, NULL, NULL);
}

static void write_file(const char *path, const char *data, int mode)
{
    FILE *f = fopen(path, "wb");
    if (f) { fputs(data, f); fclose(f); }
    (void)chmod(path, (mode_t)mode);
}

static void read_file(const char *path, char *buf, size_t cap)
{
    FILE *f = fopen(path, "rb");
    size_t n = 0;
    if (f) { n = fread(buf, 1, cap - 1, f); fclose(f); }
    buf[n] = '\0';
}

/* Two empty file nodes "a" and "b" sharing inode 42, with the given
 * extra fields; returns 1 when the restore made them one inode. */
static int restored_as_one_inode(const char *a_extra, const char *b_extra,
                                 const char *tag)
{
    char *target = mktarget();
    char blob[512], pa[512], pb[512];
    struct stat sa, sb;
    int linked = -1;
    sprintf(blob,
            "{\"nodes\":["
            "{\"name\":\"a\",\"type\":\"file\",\"mode\":420,"
            "\"size\":0,\"inode\":42%s,\"content\":[]},"
            "{\"name\":\"b\",\"type\":\"file\",\"mode\":420,"
            "\"size\":0,\"inode\":42%s,\"content\":[]}]}",
            a_extra, b_extra);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL %s: restore rc != 0\n", tag);
        fails++;
    } else {
        sprintf(pa, "%s/a", target);
        sprintf(pb, "%s/b", target);
        if (stat(pa, &sa) != 0 || stat(pb, &sb) != 0) {
            fprintf(stderr, "FAIL %s: a or b missing\n", tag);
            fails++;
        } else {
            linked = (sa.st_ino == sb.st_ino);
        }
    }
    rmrf(target);
    return linked;
}

static void test_inode_collision_not_linked(void)
{
    /* #653: rustic records "inode" on every file.  Equal inode numbers
     * with links == 1 (or no links field) are two different files. */
    if (restored_as_one_inode(",\"links\":1", ",\"links\":1",
                              "inode_links1") == 1) {
        fprintf(stderr, "FAIL inode_links1: links==1 nodes were "
                "restored as one inode\n");
        fails++;
    }
    if (restored_as_one_inode("", "", "inode_nolinks") == 1) {
        fprintf(stderr, "FAIL inode_nolinks: nodes without links were "
                "restored as one inode\n");
        fails++;
    }
    /* A device_id that is present but not a number makes the node a
     * non-candidate: two files, never a guessed link. */
    if (restored_as_one_inode(",\"links\":2,\"device_id\":\"x\"",
                              ",\"links\":2,\"device_id\":\"x\"",
                              "inode_bad_device") == 1) {
        fprintf(stderr, "FAIL inode_bad_device: nodes with a non-numeric "
                "device_id were restored as one inode\n");
        fails++;
    }
    /* links that is not a number: not a hardlink candidate. */
    if (restored_as_one_inode(",\"links\":\"2\"", ",\"links\":\"2\"",
                              "inode_bad_links") == 1) {
        fprintf(stderr, "FAIL inode_bad_links: nodes with a non-numeric "
                "links were restored as one inode\n");
        fails++;
    }
    /* Equal inode on two different devices: two files. */
    if (restored_as_one_inode(",\"links\":2,\"device_id\":1",
                              ",\"links\":2,\"device_id\":2",
                              "inode_two_devices") == 1) {
        fprintf(stderr, "FAIL inode_two_devices: nodes on different "
                "devices were restored as one inode\n");
        fails++;
    }
}

static void test_real_hardlink_kept(void)
{
    /* #653 must not over-correct: same device, same inode, links > 1
     * is a real hardlink and stays one inode (#192). */
    if (restored_as_one_inode(",\"links\":2,\"device_id\":7",
                              ",\"links\":2,\"device_id\":7",
                              "real_hardlink") != 1) {
        fprintf(stderr, "FAIL real_hardlink: a true hardlink pair was "
                "not restored as one inode\n");
        fails++;
    }
    /* No device_id recorded (older snapshots): links > 1 alone, same
     * inode, is still a hardlink (device 0 on both sides). */
    if (restored_as_one_inode(",\"links\":2", ",\"links\":2",
                              "hardlink_no_device") != 1) {
        fprintf(stderr, "FAIL hardlink_no_device: links>1 pair without "
                "device_id was not restored as one inode\n");
        fails++;
    }
}

static void test_readonly_dir_with_children(void)
{
    /* #654: a 0555 directory holding a file must restore for a
     * non-root user, end with mode 0555, and a second run over the
     * finished (read-only) tree must also succeed. */
    char *target = mktarget();
    char dpath[512], fpath[512];
    struct stat st;
    int run;
    g_blobs[0] = "{\"nodes\":[{\"name\":\"ro\",\"type\":\"dir\","
                 "\"mode\":365,\"subtree\":\"" TREE_ID "\"}]}";
    g_blobs[1] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":420,\"size\":0,\"content\":[]}]}";
    g_blobs[2] = NULL;
    sprintf(dpath, "%s/ro", target);
    sprintf(fpath, "%s/ro/f", target);
    for (run = 1; run <= 2; run++) {
        if (run_scripted(target) != 0) {
            fprintf(stderr, "FAIL ro_dir: run %d rc != 0\n", run);
            fails++;
        }
        if (stat(fpath, &st) != 0) {
            fprintf(stderr, "FAIL ro_dir: run %d: ro/f missing\n", run);
            fails++;
        }
        if (stat(dpath, &st) != 0 || (st.st_mode & 07777) != 0555) {
            fprintf(stderr, "FAIL ro_dir: run %d: ro mode %o, want 555\n",
                    run, (unsigned)(st.st_mode & 07777));
            fails++;
        }
    }
    rmrf(target);
}

static void test_resume_readonly_partial(void)
{
    /* #655: a part-written 0444 file (wrong size) from an interrupted
     * run must be reopened and finished, then end at mode 0444. */
    char *target = mktarget();
    char fpath[512];
    struct stat st;
    sprintf(fpath, "%s/f", target);
    write_file(fpath, "xx", 0444);
    g_blobs[0] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":292,\"size\":5,\"content\":[]}]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL ro_partial: rc != 0\n");
        fails++;
    }
    if (stat(fpath, &st) != 0 || st.st_size != 5
            || (st.st_mode & 07777) != 0444) {
        fprintf(stderr, "FAIL ro_partial: size %ld mode %o, want 5 444\n",
                (long)st.st_size, (unsigned)(st.st_mode & 07777));
        fails++;
    }
    rmrf(target);
}

static void test_resume_skip_needs_mtime(void)
{
    /* #661: a same-size file whose mtime differs from the node is not
     * this snapshot's file and must be rewritten; a same-size file
     * with the node's mtime is skipped (resume). */
    char *target = mktarget();
    char fpath[512], buf[16];
    struct stat st;
    struct utimbuf ub;
    sprintf(fpath, "%s/f", target);
    g_blobs[0] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":420,\"size\":3,"
                 "\"mtime\":\"2020-01-01T00:00:00Z\",\"content\":[]}]}";
    g_blobs[1] = NULL;

    write_file(fpath, "abc", 0644);
    ub.actime = ub.modtime = (time_t)1000000000;
    (void)utime(fpath, &ub);
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL skip_mtime: rewrite run rc != 0\n");
        fails++;
    }
    read_file(fpath, buf, sizeof buf);
    if (strcmp(buf, "abc") == 0) {
        fprintf(stderr, "FAIL skip_mtime: old same-size file was kept\n");
        fails++;
    }
    if (stat(fpath, &st) != 0 || (long)st.st_mtime != 1577836800L) {
        fprintf(stderr, "FAIL skip_mtime: mtime not the node's\n");
        fails++;
    }

    write_file(fpath, "abc", 0644);
    ub.actime = ub.modtime = (time_t)1577836800L;
    (void)utime(fpath, &ub);
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL skip_mtime: skip run rc != 0\n");
        fails++;
    }
    read_file(fpath, buf, sizeof buf);
    if (strcmp(buf, "abc") != 0) {
        fprintf(stderr, "FAIL skip_mtime: finished file was rewritten\n");
        fails++;
    }
    rmrf(target);
}

static void test_blob_failure_message(void)
{
    /* #654: a file whose data blob cannot be read fails the walk (rc != 0)
     * and the failure line carries no stale system error. */
    char *target = mktarget();
    g_blobs[0] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":420,\"size\":4,\"content\":[\"" TREE_ID "\"]}]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) == 0) {
        fprintf(stderr, "FAIL blob_failure: rc 0 for an unreadable blob\n");
        fails++;
    }
    rmrf(target);
}

/* ── tests ──────────────────────────────────────────────────────── */

static void test_path_too_long(void)
{
    /* 900-char dir names: after ~5 levels the joined path exceeds the
     * 4096-byte node_path buffer.  With a generous depth cap the
     * path-length guard must fire first (rc != 0) and no directory
     * with a 900-'a' name may appear beyond the truncation point. */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = deep_chain_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;   /* let the path guard win, not depth */

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL path_too_long: expected rc != 0, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_depth_cap(void)
{
    /* Short names so the path guard never fires; the depth cap must
     * stop the otherwise-infinite chain. */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = deep_chain_short;  /* short names: only depth can stop it */
    g_call = 0;
    lcsas_tree_max_depth = 4;

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL depth_cap: expected rc != 0 at depth 4, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_malformed_no_crash(void)
{
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = garbage_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    /* The contract is "doesn't crash"; rc may be 0 or non-zero
     * depending on how the parser classifies the garbage element. */
    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    (void)rc;
    rmrf(target);
}

static void test_root_not_object(void)
{
    /* A tree blob that parses as valid JSON but whose root is an array,
     * not an object, must be rejected (rc != 0) - the reject distinct
     * from the parse-fail path (#383). */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = not_object_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL root_not_object: expected rc != 0, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_symlink_embedded_nul_skipped(void)
{
    /* A symlink node whose linktarget decodes to a string containing an
     * embedded NUL must be SKIPPED, not created - the path-safety guard
     * (#383).  The walk continues cleanly; no target/lnk may appear. */
    char *target = mktarget();
    char linkpath[512];
    struct stat st;
    lcsas_master_key mk;
    lcsas_blob_index ix;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = symlink_nul_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    (void)lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                             target, target, NULL, NULL);
    sprintf(linkpath, "%s/lnk", target);
    if (lstat(linkpath, &st) == 0) {
        fprintf(stderr,
                "FAIL symlink_nul: unsafe symlink with embedded-NUL "
                "target was created at %s\n", linkpath);
        fails++;
    }
    rmrf(target);
}

static void test_tree_blob_too_large(void)
{
    /* With the JSON token-buffer ceiling clamped tiny, a multi-token
     * tree blob overflows the token cap: lcsas_json_parse_alloc returns
     * -2 and the walk fails loud (rc != 0) rather than truncating (#383).
     * Same clamp technique as T1C-01 (test_repo.c). */
    char *target = mktarget();
    size_t saved = lcsas_json_max_tok_bytes;
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = tiny_tree_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;
    lcsas_json_max_tok_bytes = sizeof(lcsas_json_tok);  /* -> cap 1 token */

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    lcsas_json_max_tok_bytes = saved;
    if (rc == 0) {
        fprintf(stderr,
                "FAIL tree_too_large: expected rc != 0 on -2, got 0\n");
        fails++;
    }
    rmrf(target);
}

int main(void)
{
    test_path_too_long();
    test_depth_cap();
    test_malformed_no_crash();
    test_root_not_object();
    test_symlink_embedded_nul_skipped();
    test_tree_blob_too_large();
    test_inode_collision_not_linked();
    test_real_hardlink_kept();
    test_readonly_dir_with_children();
    test_resume_readonly_partial();
    test_resume_skip_needs_mtime();
    test_blob_failure_message();
    if (fails) {
        fprintf(stderr, "test_tree: %d FAIL\n", fails);
        return 1;
    }
    printf("test_tree: ALL OK\n");
    return 0;
}
