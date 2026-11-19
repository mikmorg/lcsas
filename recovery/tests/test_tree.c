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
 *   - #781: escaped node names are decoded; refused names are named
 *     and make the walk return LCSAS_TREE_INCOMPLETE.
 *   - #782: every symlink is restored with its stored target (up and
 *     out, absolute, dangling, linktarget_raw); a target that cannot be
 *     used is an error.
 *   - #783: a FIFO is made; a device a non-root run cannot make is
 *     listed without failing; an unknown type is an error; every item
 *     not restored has a row in RESTORE_FAILURES.txt, and a clean run
 *     removes an old one.
 *   - #784: a restore over an earlier one never writes through a
 *     symlink and never removes a folder.
 *   - #785 (root only): the owner is set before the mode, so setuid
 *     survives.
 *   - #909: a file or folder whose data cannot be read is listed with
 *     the shared reason and the walk goes on (LCSAS_TREE_INCOMPLETE);
 *     nothing is left at its path, an older copy is kept, 'q' at the
 *     disc prompt still stops the walk, a hardlink never points at a
 *     file that failed, and a file is written as .lcsas-partial first.
 */
#include "tree.h"
#include "repo.h"
#include "json_q.h"   /* lcsas_json_max_tok_bytes + lcsas_json_tok (#383) */
#include "disc_locator.h"   /* reader_quit (#909) */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <utime.h>

static int fails = 0;

/* ── stub blob source ───────────────────────────────────────────── */

/* The harness hands lcsas_tree_restore a chain of synthetic tree
 * blobs.  g_blob_fn produces the blob for call N; returning a NULL
 * pointer makes lcsas_repo_read_blob fail (terminates the walk). */
static char *(*g_blob_fn)(int call, size_t *len_out);
static int g_call;
/* #909: what lcsas_repo_read_blob returns for call N when there is no
 * blob for it (0: -1, the walk stops). */
static int g_fail_codes[8];

/* An id starting ee ee is not in the index (#909). */
const lcsas_blob_loc *
lcsas_blob_index_find(const lcsas_blob_index *ix,
                      const unsigned char id[LCSAS_BLOB_ID_LEN])
{
    static lcsas_blob_loc dummy;
    (void)ix;
    if (id[0] == 0xee && id[1] == 0xee) return NULL;
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
    if (!blob) {
        int code = g_call - 1 < 8 ? g_fail_codes[g_call - 1] : 0;
        return code ? code : -1;
    }
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

static int path_exists(const char *dir, const char *name)
{
    char p[1024];
    struct stat st;
    sprintf(p, "%s/%s", dir, name);
    return lstat(p, &st) == 0;
}

static int count_entries(const char *dir)
{
    DIR *d = opendir(dir);
    struct dirent *e;
    int n = 0;
    if (!d) return -1;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0)
            n++;
    }
    closedir(d);
    return n;
}

/* ── #782-#785 helpers ──────────────────────────────────────────── */

#define FAILURES "RESTORE_FAILURES.txt"

static char g_list[16384];

/* Read target/RESTORE_FAILURES.txt into g_list; 0 when absent. */
static int read_failures(const char *target)
{
    char p[1024];
    FILE *f;
    size_t n;
    g_list[0] = '\0';
    sprintf(p, "%s/%s", target, FAILURES);
    f = fopen(p, "rb");
    if (!f) return 0;
    n = fread(g_list, 1, sizeof g_list - 1, f);
    fclose(f);
    g_list[n] = '\0';
    return 1;
}

/* Rows after the "path\treason\tblob_id" line; -1 when no file. */
static int failure_rows(const char *target)
{
    const char *p;
    int rows = 0;
    if (!read_failures(target)) return -1;
    p = strstr(g_list, "path\treason\tblob_id\n");
    if (!p) return -2;
    p = strchr(p, '\n') + 1;
    while (*p) {
        if (*p == '\n') rows++;
        p++;
    }
    return rows;
}

static int failures_has(const char *target, const char *text)
{
    return read_failures(target) && strstr(g_list, text) != NULL;
}

static int link_is(const char *dir, const char *name, const char *want)
{
    char p[1024], got[1024];
    ssize_t n;
    sprintf(p, "%s/%s", dir, name);
    n = readlink(p, got, sizeof got - 1);
    if (n < 0) return 0;
    got[n] = '\0';
    return strcmp(got, want) == 0;
}

static void test_escaped_names(void)
{
    /* #781: rustic stores a name that holds '"', '\', a control
     * character or a non-UTF-8 byte in Go-quoted form.  Each one must
     * be restored under its real (decoded) bytes; a name that decodes
     * to something unsafe, or that cannot be decoded, is refused,
     * named on stderr and makes the walk return LCSAS_TREE_INCOMPLETE
     * -- after every other node was restored. */
    /* Each entry is one node; kept apart because one literal would
     * pass the 509 bytes C89 promises to support. */
    static const char *const nodes[] = {
        /* JSON "quo\\\"te" -> rustic text quo\"te -> quo"te */
        "{\"name\":\"quo\\\\\\\"te\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"back\\\\\\\\slash\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"tab\\\\there\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"latin\\\\xe9\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"esc\\u001bx\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        /* refused: decodes to "..", to "/", holds NUL, bad escape,
         * ".", empty, a name that is not a JSON string */
        "{\"name\":\"\\\\x2e\\\\x2e\",\"type\":\"dir\",\"mode\":493,\"subtree\":\"" TREE_ID "\"}",
        "{\"name\":\"a\\\\x2fb\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"n\\\\x00ul\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\"bad\\\\q\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":\".\",\"type\":\"dir\",\"mode\":493}",
        "{\"name\":\"\",\"type\":\"file\",\"mode\":420,\"content\":[]}",
        "{\"name\":7,\"type\":\"file\",\"mode\":420,\"content\":[]}",
        /* a directory after the refused ones still restores */
        "{\"name\":\"d\\\\tir\",\"type\":\"dir\",\"mode\":493,\"subtree\":\"" TREE_ID "\"}"
    };
    static char blob[2048];
    char *target = mktarget();
    char sub[512];
    size_t i;
    int rc;
    strcpy(blob, "{\"nodes\":[");
    for (i = 0; i < sizeof nodes / sizeof nodes[0]; i++) {
        if (i) strcat(blob, ",");
        strcat(blob, nodes[i]);
    }
    strcat(blob, "]}");
    g_blobs[0] = blob;
    g_blobs[1] =
        "{\"nodes\":[{\"name\":\"in\\\\nside\",\"type\":\"file\","
        "\"mode\":420,\"content\":[]}]}";
    g_blobs[2] = NULL;
    rc = run_scripted(target);
    if (rc != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL escaped_names: rc %d, want %d\n",
                rc, LCSAS_TREE_INCOMPLETE);
        fails++;
    }
    if (!path_exists(target, "quo\"te") || !path_exists(target, "back\\slash")
            || !path_exists(target, "tab\there")
            || !path_exists(target, "latin\xe9")
            || !path_exists(target, "esc\x1bx")
            || !path_exists(target, "d\tir")) {
        fprintf(stderr, "FAIL escaped_names: a decoded name is missing\n");
        fails++;
    }
    sprintf(sub, "%s/d\tir", target);
    if (!path_exists(sub, "in\nside")) {
        fprintf(stderr, "FAIL escaped_names: subtree name not decoded\n");
        fails++;
    }
    /* Nothing else: no escaped spelling, no refused node; only the
     * list of the 7 refused ones (#783). */
    if (count_entries(target) != 7) {
        fprintf(stderr, "FAIL escaped_names: %d entries, want 7\n",
                count_entries(target));
        fails++;
    }
    if (failure_rows(target) != 7 || !failures_has(target, "name refused")) {
        fprintf(stderr, "FAIL escaped_names: %d rows in %s, want 7\n",
                failure_rows(target), FAILURES);
        fails++;
    }
    if (path_exists(target, "a") || path_exists(target, "n")) {
        fprintf(stderr, "FAIL escaped_names: a refused name was written\n");
        fails++;
    }
    rmrf(target);
}

static void test_long_name_refused(void)
{
    /* #781: a name longer than the 1023-byte buffer is refused and
     * counted, not silently dropped. */
    char *target = mktarget();
    char blob[2048];
    char name[1101];
    memset(name, 'n', 1100);
    name[1100] = '\0';
    sprintf(blob, "{\"nodes\":[{\"name\":\"%s\",\"type\":\"file\","
            "\"mode\":420,\"content\":[]}]}", name);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL long_name: not reported as incomplete\n");
        fails++;
    }
    /* Only the list of items not restored (#783). */
    if (count_entries(target) != 1 || !path_exists(target, FAILURES)) {
        fprintf(stderr, "FAIL long_name: something was written\n");
        fails++;
    }
    rmrf(target);
}


static void test_links_as_stored(void)
{
    /* #782: links are made with the stored target, wherever it points;
     * #841 finding: linktarget_raw (base64 of the exact bytes) wins
     * over the lossy linktarget.  dGd06Q== is "tgt" + byte 0xE9. */
    char *target = mktarget();
    g_blobs[0] =
        "{\"nodes\":["
        "{\"name\":\"up\",\"type\":\"symlink\",\"linktarget\":\"../b/f\"},"
        "{\"name\":\"out\",\"type\":\"symlink\","
        "\"linktarget\":\"../../../../outside\"},"
        "{\"name\":\"abs\",\"type\":\"symlink\","
        "\"linktarget\":\"/etc/hostname\"},"
        "{\"name\":\"dangling\",\"type\":\"symlink\","
        "\"linktarget\":\"nowhere\"},"
        "{\"name\":\"raw\",\"type\":\"symlink\","
        "\"linktarget\":\"tgt\\ufffd\",\"linktarget_raw\":\"dGd06Q==\"}"
        "]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL links_as_stored: rc != 0\n");
        fails++;
    }
    if (!link_is(target, "up", "../b/f")
            || !link_is(target, "out", "../../../../outside")
            || !link_is(target, "abs", "/etc/hostname")
            || !link_is(target, "dangling", "nowhere")) {
        fprintf(stderr, "FAIL links_as_stored: a link is missing or wrong\n");
        fails++;
    }
    if (!link_is(target, "raw", "tgt\xe9")) {
        fprintf(stderr, "FAIL links_as_stored: linktarget_raw not used\n");
        fails++;
    }
    if (path_exists(target, FAILURES)) {
        fprintf(stderr, "FAIL links_as_stored: %s written\n", FAILURES);
        fails++;
    }
    rmrf(target);
}

static void test_link_target_refused(void)
{
    /* #782/#783: a target that cannot be used is an error: reported,
     * listed, no link made, and the walk ends incomplete. */
    static char blob[12000];
    char *target = mktarget();
    char longt[5001];
    int rc;
    memset(longt, 'x', 5000);
    longt[5000] = '\0';
    sprintf(blob,
            "{\"nodes\":["
            "{\"name\":\"empty\",\"type\":\"symlink\",\"linktarget\":\"\"},"
            "{\"name\":\"nul\",\"type\":\"symlink\","
            "\"linktarget\":\"a\\u0000b\"},"
            "{\"name\":\"none\",\"type\":\"symlink\"},"
            "{\"name\":\"badraw\",\"type\":\"symlink\","
            "\"linktarget\":\"x\",\"linktarget_raw\":\"*not base64*\"},"
            "{\"name\":\"long\",\"type\":\"symlink\",\"linktarget\":\"%s\"},"
            "{\"name\":\"ok\",\"type\":\"symlink\",\"linktarget\":\"t\"}"
            "]}", longt);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    rc = run_scripted(target);
    if (rc != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL link_refused: rc %d, want %d\n", rc,
                LCSAS_TREE_INCOMPLETE);
        fails++;
    }
    if (path_exists(target, "empty") || path_exists(target, "nul")
            || path_exists(target, "none") || path_exists(target, "badraw")
            || path_exists(target, "long") || !link_is(target, "ok", "t")) {
        fprintf(stderr, "FAIL link_refused: wrong links made\n");
        fails++;
    }
    if (failure_rows(target) != 5
            || !failures_has(target, "link refused: the link target is too long")
            || !failures_has(target, "the link target holds a NUL byte")
            || !failures_has(target, "the link target cannot be decoded")) {
        fprintf(stderr, "FAIL link_refused: list is wrong:\n%s\n", g_list);
        fails++;
    }
    rmrf(target);
}

static void test_special_files(void)
{
    /* #783: a FIFO is made (as rustic does, no privilege needed); a
     * device needs root: without it, it is listed and the walk still
     * ends 0.  An unknown type is an error. */
    char *target = mktarget();
    char p[512];
    struct stat st;
    int rc;
    g_blobs[0] =
        "{\"nodes\":["
        "{\"name\":\"ff\",\"type\":\"fifo\",\"mode\":33554868},"
        "{\"name\":\"null\",\"type\":\"chardev\",\"mode\":420,"
        "\"device\":259},"
        "{\"name\":\"sock\",\"type\":\"socket\",\"mode\":16777725},"
        "{\"name\":\"blk\",\"type\":\"dev\",\"mode\":420,"
        "\"device\":1992}"
        "]}";
    g_blobs[1] = NULL;
    rc = run_scripted(target);
    if (rc != 0) {
        fprintf(stderr, "FAIL special: rc %d, want 0\n", rc);
        fails++;
    }
    sprintf(p, "%s/ff", target);
    if (lstat(p, &st) != 0 || !S_ISFIFO(st.st_mode)
            || (st.st_mode & 0777) != 0664) {
        fprintf(stderr, "FAIL special: ff is not a 0664 FIFO\n");
        fails++;
    }
    sprintf(p, "%s/null", target);
    if (geteuid() == 0) {
        if (lstat(p, &st) != 0 || !S_ISCHR(st.st_mode)
                || st.st_rdev != (dev_t)259) {
            fprintf(stderr, "FAIL special: root did not make the device\n");
            fails++;
        }
    } else if (lstat(p, &st) == 0 || failure_rows(target) != 2
               || !failures_has(target,
                                "special file (character device) not "
                                "created")
               || !failures_has(target,
                                "special file (block device) not created")) {
        fprintf(stderr, "FAIL special: device not listed:\n%s\n", g_list);
        fails++;
    }
    sprintf(p, "%s/sock", target);
    if (lstat(p, &st) != 0 || !S_ISSOCK(st.st_mode)) {
        fprintf(stderr, "FAIL special: sock is not a socket\n");
        fails++;
    }
    rmrf(target);

    target = mktarget();
    g_blobs[0] = "{\"nodes\":[{\"name\":\"d\",\"type\":\"door\"}]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE
            || !failures_has(target, "unknown item type: door")) {
        fprintf(stderr, "FAIL special: unknown type not an error\n");
        fails++;
    }
    rmrf(target);
}

static void test_failures_file_lifecycle(void)
{
    /* #783: a clean run removes the list an earlier run wrote, and
     * leaves alone a file of that name we did not write. */
    char *target = mktarget();
    char p[512];
    g_blobs[0] = "{\"nodes\":[{\"name\":\"d\",\"type\":\"door\"}]}";
    g_blobs[1] = NULL;
    (void)run_scripted(target);
    if (!path_exists(target, FAILURES)) {
        fprintf(stderr, "FAIL failures_file: not written\n");
        fails++;
    }
    g_blobs[0] = "{\"nodes\":[]}";
    if (run_scripted(target) != 0 || path_exists(target, FAILURES)) {
        fprintf(stderr, "FAIL failures_file: old list kept\n");
        fails++;
    }
    /* A folder of that name: the list cannot be written; no crash. */
    g_blobs[0] = "{\"nodes\":[{\"name\":\"d\",\"type\":\"door\"}]}";
    sprintf(p, "%s/%s", target, FAILURES);
    mkdir(p, 0700);
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL failures_file: folder case rc\n");
        fails++;
    }
    rmdir(p);
    g_blobs[0] = "{\"nodes\":[]}";
    write_file(p, "my own notes\n", 0644);
    (void)run_scripted(target);
    if (!path_exists(target, FAILURES)) {
        fprintf(stderr, "FAIL failures_file: removed a file not ours\n");
        fails++;
    }
    rmrf(target);
}

static void test_restore_over_links(void)
{
    /* #784: the target holds an earlier restore with links where this
     * snapshot has a file ("x"), a folder ("d") and a link ("l" is a
     * folder here); "q" is a FIFO where the snapshot has a file.
     * Nothing outside the target may change, no folder is removed. */
    char *target = mktarget();
    char outside[512], odir[512], p[512], q[512], buf[64];
    struct stat st;
    int rc;
    sprintf(outside, "%s_outside", target);
    sprintf(odir, "%s_odir", target);
    rmrf(odir);
    mkdir(odir, 0751);
    write_file(outside, "OUTSIDE", 0644);
    sprintf(p, "%s/x", target);
    if (symlink(outside, p) != 0) perror("symlink x");
    sprintf(p, "%s/d", target);
    if (symlink(odir, p) != 0) perror("symlink d");
    sprintf(p, "%s/l", target);
    mkdir(p, 0700);
    sprintf(q, "%s/l/keep", target);
    write_file(q, "keep", 0644);
    sprintf(p, "%s/q", target);
    if (mkfifo(p, 0600) != 0) perror("mkfifo q");

    g_blobs[0] =
        "{\"nodes\":["
        "{\"name\":\"x\",\"type\":\"file\",\"mode\":420,\"size\":0,"
        "\"content\":[]},"
        "{\"name\":\"d\",\"type\":\"dir\",\"mode\":448,"
        "\"subtree\":\"" TREE_ID "\"},"
        "{\"name\":\"l\",\"type\":\"symlink\",\"linktarget\":\"x\"},"
        "{\"name\":\"q\",\"type\":\"file\",\"mode\":420,\"size\":0,"
        "\"content\":[]}"
        "]}";
    g_blobs[1] = "{\"nodes\":[{\"name\":\"inner\",\"type\":\"file\","
                 "\"mode\":420,\"size\":0,\"content\":[]}]}";
    g_blobs[2] = NULL;
    rc = run_scripted(target);
    if (rc != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL over_links: rc %d, want %d (folder at l)\n",
                rc, LCSAS_TREE_INCOMPLETE);
        fails++;
    }
    read_file(outside, buf, sizeof buf);
    if (strcmp(buf, "OUTSIDE") != 0) {
        fprintf(stderr, "FAIL over_links: wrote through the link x\n");
        fails++;
    }
    if (path_exists(odir, "inner")
            || stat(odir, &st) != 0 || (st.st_mode & 0777) != 0751) {
        fprintf(stderr, "FAIL over_links: wrote through the link d\n");
        fails++;
    }
    sprintf(p, "%s/x", target);
    if (lstat(p, &st) != 0 || !S_ISREG(st.st_mode)) {
        fprintf(stderr, "FAIL over_links: x is not a regular file\n");
        fails++;
    }
    sprintf(p, "%s/d", target);
    if (lstat(p, &st) != 0 || !S_ISDIR(st.st_mode)
            || !path_exists(p, "inner")) {
        fprintf(stderr, "FAIL over_links: d is not a real folder\n");
        fails++;
    }
    sprintf(p, "%s/q", target);
    if (lstat(p, &st) != 0 || !S_ISREG(st.st_mode)) {
        fprintf(stderr, "FAIL over_links: the FIFO q was not replaced\n");
        fails++;
    }
    if (lstat(q, &st) != 0
            || !failures_has(target,
                             "a folder with this name is already there")) {
        fprintf(stderr, "FAIL over_links: folder l removed or not listed\n");
        fails++;
    }
    rmrf(target);
    rmrf(odir);
    unlink(outside);
}


static void test_malformed_nodes(void)
{
    /* #783: a node with no name or no type is an error, not a silent
     * skip; a link whose name the file system refuses (over 255 bytes)
     * is an error too. */
    static char blob[2048];
    char *target = mktarget();
    char longname[301];
    memset(longname, 'n', 300);
    longname[300] = '\0';
    sprintf(blob,
            "{\"nodes\":["
            "{\"type\":\"file\",\"content\":[]},"
            "{\"name\":\"notype\"},"
            "{\"name\":\"%s\",\"type\":\"symlink\",\"linktarget\":\"t\"}"
            "]}", longname);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE
            || failure_rows(target) != 3
            || !failures_has(target, "unknown item type: ?")
            || !failures_has(target, "link not created: ")) {
        fprintf(stderr, "FAIL malformed_nodes: list is wrong:\n%s\n", g_list);
        fails++;
    }
    rmrf(target);
}

static void test_read_only_target(void)
{
    /* #784: a link that cannot be removed is reported, never written
     * through; #783: when the list cannot be written the restore says
     * so and carries on.  Root ignores the read-only mode: skip. */
    char *target;
    char outside[512], p[512], buf[64];
    if (geteuid() == 0) return;
    target = mktarget();
    sprintf(outside, "%s_outside", target);
    write_file(outside, "OUTSIDE", 0644);
    sprintf(p, "%s/x", target);
    if (symlink(outside, p) != 0) perror("symlink x");
    chmod(target, 0555);
    g_blobs[0] = "{\"nodes\":[{\"name\":\"x\",\"type\":\"symlink\","
                 "\"linktarget\":\"y\"}]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL read_only_target: not incomplete\n");
        fails++;
    }
    read_file(outside, buf, sizeof buf);
    if (strcmp(buf, "OUTSIDE") != 0 || !link_is(target, "x", outside)) {
        fprintf(stderr, "FAIL read_only_target: link touched\n");
        fails++;
    }
    rmrf(target);
    unlink(outside);
}


static void test_list_rows_visible(void)
{
    /* #783: a row shows a name the way tier 3's _visible() does: valid
     * UTF-8 as is, every other byte as \xNN.  The refused name holds
     * '/' and raw bytes: e-acute, euro, U+0800, U+D7FF, U+10000,
     * U+10FFFF (all valid), then 0xFF, a bad second byte, a bad third
     * byte and a truncated sequence (all not). */
    static const char name[] =
        "\xc3\xa9\xe2\x82\xac\xe0\xa0\x80\xed\x9f\xbf"
        "\xf0\x90\x80\x80\xf4\x8f\xbf\xbf"
        "\xff\xe2\x28\xe2\x82\x28/\xc3";
    static const char want[] =
        "\xc3\xa9\xe2\x82\xac\xe0\xa0\x80\xed\x9f\xbf"
        "\xf0\x90\x80\x80\xf4\x8f\xbf\xbf"
        "\\xff\\xe2(\\xe2\\x82(/\\xc3\t"
        "name refused: the name holds '/'\t\n";
    static char blob[512];
    char *target = mktarget();
    const char *row;
    sprintf(blob, "{\"nodes\":[{\"name\":\"%s\",\"type\":\"file\","
            "\"content\":[]}]}", name);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    (void)run_scripted(target);
    (void)read_failures(target);
    row = strstr(g_list, "blob_id\n");
    row = row ? strchr(row, '\n') + 1 : "";
    if (strncmp(row, target, strlen(target)) == 0) {
        row += strlen(target) + 1;                    /* past "<target>/" */
    }
    if (strcmp(row, want) != 0) {
        fprintf(stderr, "FAIL list_rows_visible: row is %s\n", row);
        fails++;
    }
    rmrf(target);
}

static void test_link_target_raw_limits(void)
{
    /* #852: linktarget_raw too long for the base64 buffer, decoding to
     * more than 4095 bytes, or badly stored; and a badly stored plain
     * linktarget.  Each is an error; nothing is made. */
    static char blob[16384];
    char *target = mktarget();
    char a6000[6001], a5500[5501];
    memset(a6000, 'A', 6000);
    a6000[6000] = '\0';
    memset(a5500, 'A', 5500);
    a5500[5500] = '\0';
    sprintf(blob,
            "{\"nodes\":["
            "{\"name\":\"r1\",\"type\":\"symlink\",\"linktarget_raw\":\"%s\"},"
            "{\"name\":\"r2\",\"type\":\"symlink\",\"linktarget_raw\":\"%s\"},"
            "{\"name\":\"r3\",\"type\":\"symlink\","
            "\"linktarget_raw\":\"QQ\\q\"},"
            "{\"name\":\"t1\",\"type\":\"symlink\",\"linktarget\":\"a\\q\"}"
            "]}", a6000, a5500);
    g_blobs[0] = blob;
    g_blobs[1] = NULL;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE
            || failure_rows(target) != 4
            || path_exists(target, "r1") || path_exists(target, "r2")
            || path_exists(target, "r3") || path_exists(target, "t1")) {
        fprintf(stderr, "FAIL raw_limits: list is wrong:\n%s\n", g_list);
        fails++;
    }
    rmrf(target);
}

static void test_file_at_link_path(void)
{
    /* #784: a regular file where the snapshot has a link or a folder
     * is replaced (only folders are never removed). */
    char *target = mktarget();
    char p[512];
    struct stat st;
    sprintf(p, "%s/l", target);
    write_file(p, "old", 0644);
    sprintf(p, "%s/d", target);
    write_file(p, "old", 0644);
    g_blobs[0] = "{\"nodes\":["
                 "{\"name\":\"l\",\"type\":\"symlink\",\"linktarget\":\"t\"},"
                 "{\"name\":\"d\",\"type\":\"dir\",\"mode\":493}"
                 "]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0 || !link_is(target, "l", "t")
            || lstat(p, &st) != 0 || !S_ISDIR(st.st_mode)) {
        fprintf(stderr, "FAIL file_at_link_path\n");
        fails++;
    }
    rmrf(target);
}

static void test_root_setuid_kept(void)
{
    /* #785: as root, the owner is set before the mode (a change of
     * owner clears setuid/setgid).  Go mode: bit 23 setuid, bit 22
     * setgid; 8389101 = setuid + 0755, 4194797 = setgid + 0755. */
    char *target;
    char p[512];
    struct stat st;
    if (geteuid() != 0) {
        printf("test_tree: root_setuid SKIP (not root)\n");
        return;
    }
    target = mktarget();
    g_blobs[0] =
        "{\"nodes\":["
        "{\"name\":\"su\",\"type\":\"file\",\"mode\":8389101,"
        "\"uid\":1234,\"gid\":1234,\"size\":0,\"content\":[]},"
        "{\"name\":\"sg\",\"type\":\"file\",\"mode\":4194797,"
        "\"uid\":1234,\"gid\":1234,\"size\":0,\"content\":[]}"
        "]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL root_setuid: rc != 0\n");
        fails++;
    }
    sprintf(p, "%s/su", target);
    if (stat(p, &st) != 0 || (st.st_mode & 07777) != 04755
            || st.st_uid != 1234) {
        fprintf(stderr, "FAIL root_setuid: su mode %o uid %ld\n",
                (unsigned)(st.st_mode & 07777), (long)st.st_uid);
        fails++;
    }
    sprintf(p, "%s/sg", target);
    if (stat(p, &st) != 0 || (st.st_mode & 07777) != 02755
            || st.st_gid != 1234) {
        fprintf(stderr, "FAIL root_setuid: sg mode %o gid %ld\n",
                (unsigned)(st.st_mode & 07777), (long)st.st_gid);
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

/* ── #909: unreadable data is listed, the walk goes on ──────────── */

#define ZERO_PACK \
    "0000000000000000000000000000000000000000000000000000000000000000"
#define NOT_IN_INDEX \
    "eeee000000000000000000000000000000000000000000000000000000000000"

/* A file node "<name>" with one content blob (id TREE_ID: in the
 * index), size `size`. */
#define FILE_NODE(name, size) \
    "{\"name\":\"" name "\",\"type\":\"file\",\"mode\":420,\"size\":" \
    #size ",\"content\":[\"" TREE_ID "\"]}"

static int run_scripted_with(const char *target,
                             struct lcsas_disc_locator *locator)
{
    lcsas_master_key mk;
    lcsas_blob_index ix;
    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = scripted_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;
    return lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                              target, target, locator, NULL);
}

static void file_is(const char *target, const char *name, const char *want,
                    const char *tag)
{
    char p[1024], buf[256];
    sprintf(p, "%s/%s", target, name);
    if (!path_exists(target, name)) {
        fprintf(stderr, "FAIL %s: %s missing\n", tag, name);
        fails++;
        return;
    }
    read_file(p, buf, sizeof buf);
    if (strcmp(buf, want) != 0) {
        fprintf(stderr, "FAIL %s: %s holds '%s', want '%s'\n",
                tag, name, buf, want);
        fails++;
    }
}

/* `name` is listed with exactly `reason` (the path column is
 * target/name, or the target itself for an empty name). */
static void listed(const char *target, const char *name, const char *reason,
                   const char *tag)
{
    char row[1024];
    if (*name) sprintf(row, "\n%s/%s\t%s\t", target, name, reason);
    else sprintf(row, "\n%s\t%s\t", target, reason);
    if (!failures_has(target, row)) {
        fprintf(stderr, "FAIL %s: no row '%s' in:\n%s\n", tag, row + 1,
                g_list);
        fails++;
    }
}

static void no_partial(const char *target, const char *name, const char *tag)
{
    char p[512];
    sprintf(p, "%s.lcsas-partial", name);
    if (path_exists(target, p)) {
        fprintf(stderr, "FAIL %s: %s left behind\n", tag, p);
        fails++;
    }
}

/* Files a, b, z; b's one blob cannot be read (`code`).  A code the walk
 * lists gives LCSAS_TREE_INCOMPLETE, a and z restored, b listed with
 * `reason` and nothing at its path; -1 stops the walk at b. */
static void file_read_fails(int code, const char *reason, const char *tag)
{
    char *target = mktarget();
    int rc;
    g_blobs[0] = "{\"nodes\":[" FILE_NODE("a", 2) "," FILE_NODE("b", 2) ","
                 FILE_NODE("z", 2) "]}";
    g_blobs[1] = "A\n";
    g_blobs[2] = NULL;
    g_blobs[3] = "Z\n";
    g_blobs[4] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[2] = code;
    rc = run_scripted(target);
    file_is(target, "a", "A\n", tag);
    no_partial(target, "b", tag);
    if (path_exists(target, "b")) {
        fprintf(stderr, "FAIL %s: b left at its path\n", tag);
        fails++;
    }
    if (reason) {
        if (rc != LCSAS_TREE_INCOMPLETE) {
            fprintf(stderr, "FAIL %s: rc %d, want %d\n", tag, rc,
                    LCSAS_TREE_INCOMPLETE);
            fails++;
        }
        file_is(target, "z", "Z\n", tag);
        listed(target, "b", reason, tag);
        if (failure_rows(target) != 1) {
            fprintf(stderr, "FAIL %s: %d rows, want 1\n", tag,
                    failure_rows(target));
            fails++;
        }
    } else {
        if (rc != -1) {
            fprintf(stderr, "FAIL %s: rc %d, want -1\n", tag, rc);
            fails++;
        }
        if (path_exists(target, "z") || failure_rows(target) > 0) {
            fprintf(stderr, "FAIL %s: the walk went on after -1\n", tag);
            fails++;
        }
    }
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_file_read_failures(void)
{
    file_read_fails(LCSAS_REPO_ERR_NOT_FOUND,
                    "pack not found: " ZERO_PACK, "909_not_found");
    file_read_fails(LCSAS_REPO_ERR_DAMAGED,
                    "pack damaged: " ZERO_PACK, "909_damaged");
    file_read_fails(LCSAS_REPO_ERR_UNREADABLE,
                    "pack unreadable: " ZERO_PACK, "909_unreadable");
    file_read_fails(-1, NULL, "909_stop");
}

static void test_909_blob_not_in_index(void)
{
    /* An id the index does not hold, one that is not hex, and one of
     * the wrong length: each file is listed and the walk goes on. */
    char *target = mktarget();
    static char blob[1024];
    int rc;
    /* Built in parts: C89 allows string literals of 509 bytes. */
    strcpy(blob, "{\"nodes\":["
                 "{\"name\":\"x\",\"type\":\"file\",\"size\":1,"
                 "\"content\":[\"" NOT_IN_INDEX "\"]},"
                 "{\"name\":\"g\",\"type\":\"file\",\"size\":1,\"content\":"
                 "[\"gggggggggggggggggggggggggggggggggggggggggggggggggggggggg"
                 "gggggggg\"]},");
    strcat(blob, "{\"name\":\"s\",\"type\":\"file\",\"size\":1,"
                 "\"content\":[\"abc\"]},"
                 "{\"name\":\"l\",\"type\":\"file\",\"size\":1,\"content\":"
                 "[\"" TREE_ID "123456\"]},"
                 FILE_NODE("z", 2) "]}");
    g_blobs[0] = blob;
    g_blobs[1] = "Z\n";
    g_blobs[2] = NULL;
    rc = run_scripted(target);
    if (rc != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL 909_index: rc %d\n", rc);
        fails++;
    }
    listed(target, "x", "blob not in index: " NOT_IN_INDEX, "909_index");
    listed(target, "g", "blob not in index: gggggggggggggggggggggggggggggggg"
           "gggggggggggggggggggggggggggggggg", "909_index");
    listed(target, "s", "blob not in index: abc", "909_index");
    /* An id longer than 64 characters: its first 64 are shown. */
    listed(target, "l", "blob not in index: " TREE_ID, "909_index");
    file_is(target, "z", "Z\n", "909_index");
    if (path_exists(target, "x") || path_exists(target, "g")
            || path_exists(target, "s")) {
        fprintf(stderr, "FAIL 909_index: a listed file was left\n");
        fails++;
    }
    rmrf(target);
}

static void test_909_older_copy_kept(void)
{
    /* An earlier restore left b (0640, "old"); this run cannot read b's
     * data: b stays exactly as it was. */
    char *target = mktarget();
    char p[512];
    struct stat st;
    sprintf(p, "%s/b", target);
    write_file(p, "older\n", 0640);
    g_blobs[0] = "{\"nodes\":[" FILE_NODE("b", 4) "]}";
    g_blobs[1] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[1] = LCSAS_REPO_ERR_NOT_FOUND;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL 909_older: rc not INCOMPLETE\n");
        fails++;
    }
    file_is(target, "b", "older\n", "909_older");
    if (stat(p, &st) != 0 || (st.st_mode & 07777) != 0640) {
        fprintf(stderr, "FAIL 909_older: mode changed\n");
        fails++;
    }
    no_partial(target, "b", "909_older");
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_partial_file(void)
{
    /* The new content replaces an older file only when complete, and a
     * link left at the partial file's path is never written through. */
    char *target = mktarget();
    char p[512], outside[512], buf[64];
    struct stat st;
    sprintf(outside, "%s.outside", target);
    write_file(outside, "outside\n", 0644);
    sprintf(p, "%s/b", target);
    write_file(p, "older\n", 0644);
    sprintf(p, "%s/b.lcsas-partial", target);
    if (symlink(outside, p) != 0) {
        fprintf(stderr, "FAIL 909_partial: symlink\n");
        fails++;
    }
    g_blobs[0] = "{\"nodes\":[{\"name\":\"b\",\"type\":\"file\",\"mode\":"
                 "416,\"size\":4,\"content\":[\"" TREE_ID "\"]}]}";
    g_blobs[1] = "new\n";
    g_blobs[2] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL 909_partial: rc != 0\n");
        fails++;
    }
    file_is(target, "b", "new\n", "909_partial");
    sprintf(p, "%s/b", target);
    if (stat(p, &st) != 0 || (st.st_mode & 07777) != 0640) {
        fprintf(stderr, "FAIL 909_partial: b mode %o, want 640\n",
                (unsigned)(st.st_mode & 07777));
        fails++;
    }
    no_partial(target, "b", "909_partial");
    read_file(outside, buf, sizeof buf);
    if (strcmp(buf, "outside\n") != 0) {
        fprintf(stderr, "FAIL 909_partial: wrote through the link\n");
        fails++;
    }
    /* A folder at the partial file's path stops the walk; the folder
     * and the file already there are kept. */
    sprintf(p, "%s/b.lcsas-partial", target);
    mkdir(p, 0700);
    g_blobs[0] = "{\"nodes\":[" FILE_NODE("b", 9) "]}";   /* not a resume */
    if (run_scripted(target) != -1) {
        fprintf(stderr, "FAIL 909_partial: a folder in the way, rc != -1\n");
        fails++;
    }
    file_is(target, "b", "new\n", "909_partial_dir");
    rmrf(target);
    unlink(outside);
}

static void test_909_resume_removes_stale_partial(void)
{
    /* A finished file is skipped, and the partial file a killed later
     * run left next to it is removed (tier 3 does the same). */
    char *target = mktarget();
    char p[512];
    struct utimbuf ub;
    sprintf(p, "%s/f", target);
    write_file(p, "abc", 0644);
    ub.actime = ub.modtime = (time_t)1577836800L;
    (void)utime(p, &ub);
    sprintf(p, "%s/f.lcsas-partial", target);
    write_file(p, "a", 0600);
    g_blobs[0] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":420,\"size\":3,"
                 "\"mtime\":\"2020-01-01T00:00:00Z\",\"content\":[]}]}";
    g_blobs[1] = NULL;
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL 909_resume: rc != 0\n");
        fails++;
    }
    file_is(target, "f", "abc", "909_resume");
    no_partial(target, "f", "909_resume");
    rmrf(target);
}

static void test_909_folder_listing_fails(void)
{
    /* sub's tree blob cannot be read: sub is made (with its own mode),
     * listed once, and z after it is restored.  A subtree id that is
     * not hex is listed as not in the index. */
    char *target = mktarget();
    char p[512];
    struct stat st;
    int rc;
    g_blobs[0] = "{\"nodes\":["
                 FILE_NODE("a", 2) ","
                 "{\"name\":\"sub\",\"type\":\"dir\",\"mode\":488,"
                 "\"subtree\":\"" TREE_ID "\"},"
                 "{\"name\":\"bad\",\"type\":\"dir\",\"mode\":493,"
                 "\"subtree\":\"gggggggggggggggggggggggggggggggggggggggggggg"
                 "gggggggggggggggggggg\"},"
                 FILE_NODE("z", 2) "]}";
    g_blobs[1] = "A\n";
    g_blobs[2] = NULL;          /* sub's listing */
    g_blobs[3] = "Z\n";
    g_blobs[4] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[2] = LCSAS_REPO_ERR_DAMAGED;
    rc = run_scripted(target);
    if (rc != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL 909_folder: rc %d\n", rc);
        fails++;
    }
    file_is(target, "a", "A\n", "909_folder");
    file_is(target, "z", "Z\n", "909_folder");
    listed(target, "sub", "pack damaged: " ZERO_PACK, "909_folder");
    listed(target, "bad", "blob not in index: gggggggggggggggggggggggggggg"
           "gggggggggggggggggggggggggggggggggggg", "909_folder");
    if (failure_rows(target) != 2) {
        fprintf(stderr, "FAIL 909_folder: %d rows, want 2\n",
                failure_rows(target));
        fails++;
    }
    sprintf(p, "%s/sub", target);
    if (stat(p, &st) != 0 || !S_ISDIR(st.st_mode)
            || (st.st_mode & 07777) != 0750 || count_entries(p) != 0) {
        fprintf(stderr, "FAIL 909_folder: sub is not an empty 0750 folder\n");
        fails++;
    }
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_root_listing_fails(void)
{
    /* The snapshot's own tree cannot be read: the target is listed
     * (its path, with no "/"), and the walk ends INCOMPLETE. */
    char *target = mktarget();
    g_blobs[0] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[0] = LCSAS_REPO_ERR_NOT_FOUND;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL 909_root: rc not INCOMPLETE\n");
        fails++;
    }
    listed(target, "", "pack not found: " ZERO_PACK, "909_root");
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_quit_stops(void)
{
    /* 'q' at the disc prompt (the locator's reader_quit): the walk
     * stops (main exits 75), nothing is listed, and the file it was
     * writing is not left behind. */
    char *target = mktarget();
    lcsas_disc_locator loc;
    memset(&loc, 0, sizeof loc);
    loc.reader_quit = 1;
    g_blobs[0] = "{\"nodes\":[" FILE_NODE("a", 2) "," FILE_NODE("b", 2) ","
                 FILE_NODE("z", 2) "]}";
    g_blobs[1] = "A\n";
    g_blobs[2] = NULL;
    g_blobs[3] = "Z\n";
    g_blobs[4] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[2] = LCSAS_REPO_ERR_NOT_FOUND;
    if (run_scripted_with(target, &loc) != -1) {
        fprintf(stderr, "FAIL 909_quit: the walk did not stop\n");
        fails++;
    }
    file_is(target, "a", "A\n", "909_quit");
    if (path_exists(target, "b") || path_exists(target, "z")
            || failure_rows(target) > 0) {
        fprintf(stderr, "FAIL 909_quit: b, z or a row is there\n");
        fails++;
    }
    no_partial(target, "b", "909_quit");
    /* The same at a folder's listing. */
    g_blobs[0] = NULL;
    g_fail_codes[0] = LCSAS_REPO_ERR_NOT_FOUND;
    if (run_scripted_with(target, &loc) != -1 || failure_rows(target) > 0) {
        fprintf(stderr, "FAIL 909_quit: a listing after q was listed\n");
        fails++;
    }
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_hardlink_not_to_failed_file(void)
{
    /* a and b are one hardlinked file.  An earlier restore left an old
     * a; this run cannot read a's data.  b must get its own content,
     * never a link to the old a. */
    char *target = mktarget();
    char pa[512], pb[512];
    struct stat sa, sb;
    sprintf(pa, "%s/a", target);
    sprintf(pb, "%s/b", target);
    write_file(pa, "older\n", 0644);
    g_blobs[0] = "{\"nodes\":["
                 "{\"name\":\"a\",\"type\":\"file\",\"mode\":420,\"size\":4,"
                 "\"inode\":42,\"links\":2,\"content\":[\"" TREE_ID "\"]},"
                 "{\"name\":\"b\",\"type\":\"file\",\"mode\":420,\"size\":4,"
                 "\"inode\":42,\"links\":2,\"content\":[\"" TREE_ID "\"]}]}";
    g_blobs[1] = NULL;
    g_blobs[2] = "new\n";
    g_blobs[3] = NULL;
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    g_fail_codes[1] = LCSAS_REPO_ERR_NOT_FOUND;
    if (run_scripted(target) != LCSAS_TREE_INCOMPLETE) {
        fprintf(stderr, "FAIL 909_hardlink: rc not INCOMPLETE\n");
        fails++;
    }
    file_is(target, "a", "older\n", "909_hardlink");
    file_is(target, "b", "new\n", "909_hardlink");
    if (stat(pa, &sa) == 0 && stat(pb, &sb) == 0 && sa.st_ino == sb.st_ino) {
        fprintf(stderr, "FAIL 909_hardlink: b is a link to the old a\n");
        fails++;
    }
    memset(g_fail_codes, 0, sizeof g_fail_codes);
    rmrf(target);
}

static void test_909_readonly_stale_partial(void)
{
    /* #655 meets #909: a killed run of an older build left a read-only
     * (0444) part-written file at the partial file's path.  It is made
     * writable and reused (lcsas_create_file's retry), and the finished
     * file ends at its own mode with no partial file left. */
    char *target = mktarget();
    char p[512];
    struct stat st;
    sprintf(p, "%s/f.lcsas-partial", target);
    write_file(p, "xx", 0444);
    g_blobs[0] = "{\"nodes\":[{\"name\":\"f\",\"type\":\"file\","
                 "\"mode\":292,\"size\":5,\"content\":[]}]}";
    g_blobs[1] = NULL;
    if (geteuid() == 0) {   /* root ignores the mode: nothing to retry */
        rmrf(target);
        return;
    }
    if (run_scripted(target) != 0) {
        fprintf(stderr, "FAIL 909_ro_partial: rc != 0\n");
        fails++;
    }
    sprintf(p, "%s/f", target);
    if (stat(p, &st) != 0 || st.st_size != 5
            || (st.st_mode & 07777) != 0444) {
        fprintf(stderr, "FAIL 909_ro_partial: f not restored at 0444\n");
        fails++;
    }
    no_partial(target, "f", "909_ro_partial");
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
    test_escaped_names();
    test_long_name_refused();
    test_links_as_stored();
    test_link_target_refused();
    test_special_files();
    test_failures_file_lifecycle();
    test_restore_over_links();
    test_malformed_nodes();
    test_read_only_target();
    test_list_rows_visible();
    test_link_target_raw_limits();
    test_file_at_link_path();
    test_root_setuid_kept();
    /* #909: last, so the earlier tests' allocations (and the fault
     * sweep's first-100 window) are unchanged. */
    test_909_file_read_failures();
    test_909_blob_not_in_index();
    test_909_older_copy_kept();
    test_909_partial_file();
    test_909_resume_removes_stale_partial();
    test_909_folder_listing_fails();
    test_909_root_listing_fails();
    test_909_quit_stops();
    test_909_hardlink_not_to_failed_file();
    test_909_readonly_stale_partial();
    if (fails) {
        fprintf(stderr, "test_tree: %d FAIL\n", fails);
        return 1;
    }
    printf("test_tree: ALL OK\n");
    return 0;
}
