/*
 * tree.c -- recursive tree-blob walker.
 *
 * Mirrors src/lcsas/restore/restic_fallback.py:_restore_tree.
 *
 * Memory model: a tree blob is loaded, parsed, and the loop walks
 * each child in source order.  Subtree recursion is iterative-by-
 * recursion (C stack).  Each frame holds a few KB of fixed buffers
 * plus the parsed tokens + decrypted blob (freed only after the
 * children return), so depth is bounded to keep the walk inside the
 * default 8 MB stack: recursion is capped at lcsas_tree_max_depth
 * (default 1000) and a deeper tree fails loud with a named error
 * instead of a SIGSEGV (T1C-04).  An heir with a genuinely deeper
 * layout raises LCSAS_MAX_TREE_DEPTH and the stack ulimit, or falls
 * back to tier-2 (rustic).
 */
/* mknod() and S_IFSOCK are XSI (#783: special files). */
#if !defined(_WIN32) && !defined(_XOPEN_SOURCE)
#  define _XOPEN_SOURCE 700
#endif
#include "tree.h"
#include "path.h"
#include "json_q.h"
#include "lcsas_io.h"
#include "hex.h"
#include "disc_locator.h"
#include "posix_compat.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "b64.h"

/* xattr support is Linux-only by default.  macOS has its own xattr
 * call surface (sys/xattr.h with different fn signatures, plus
 * com.apple.* namespace semantics) and the zig cross-build SDK
 * doesn't ship the header anyway; Windows has no xattr concept.
 * Both are excluded by default.  A build for a platform with
 * Linux-style xattrs but a non-Linux predefined macro can pass
 * `-DLCSAS_FORCE_XATTR` to opt back in; pass `-DLCSAS_NO_XATTR` to
 * force the no-op stub on any platform.  The restore still works
 * everywhere — xattrs are silently dropped on no-xattr targets,
 * matching the behaviour for non-root uid/gid below. */
#if (defined(__linux__) || defined(LCSAS_FORCE_XATTR)) \
    && !defined(LCSAS_NO_XATTR)
#  include <sys/xattr.h>
#  define LCSAS_HAS_XATTR 1
#else
#  define LCSAS_HAS_XATTR 0
#endif

#define LCSAS_PROGRESS_DEFAULT_BLOBS_PER_TICK 16ULL
#define LCSAS_PROGRESS_DEFAULT_BYTES_PER_TICK (1024ULL * 1024ULL)

/* T1C-04: hard cap on tree-walk recursion depth.  Each recursion frame
 * costs ~6 KB of stack (fixed name/type/path buffers) plus the live
 * tokens + decrypted blob held until the children return, so an
 * unbounded walk SIGSEGVs on a pathologically deep tree.  1000 levels
 * ~= 6 MB stack, inside the default 8 MB ulimit with margin, and the
 * 4096-byte node_path buffer bounds restorable depth to roughly that
 * order anyway.  main.c lets LCSAS_MAX_TREE_DEPTH override it (escape
 * hatch for a genuinely deeper layout, paired with `ulimit -s
 * unlimited`). */
int lcsas_tree_max_depth = 1000;

/* Issue #192 — hardlink reconstruction.
 *
 * Restic records hardlinks via the per-node "inode", "device_id" and
 * "links" fields: two file nodes with links > 1 and the same nonzero
 * (device_id, inode) pair represent the same hardlinked file.
 *
 * Issue #653 — the inode number alone is NOT a hardlink key.  rustic
 * records "inode" on every file node, and a snapshot that spans more
 * than one filesystem repeats inode numbers.  Keying on the inode
 * alone turned unrelated files into link()s to whichever file was
 * restored first (wrong content, exit 0).  Only nodes with links > 1
 * enter the map, and the key is (device_id, inode); this also keeps
 * the map as small as the number of hardlinked files.  Tier-2 (rustic restore) reconstructs them via
 * link(2); without doing the same, tier-1 emits a full content copy
 * for every name, which can multiply restored size by the link
 * factor (e.g. /usr/lib/firmware ~10x).
 *
 * The map is per-restore (not global): a flat array of
 * (device, inode, restored-path) triples, populated as the recursion walks
 * the tree.  Growth pattern mirrors blob_index_push in repo.c
 * (cap = 64, double).
 *
 * Windows has no link(2); the map is unused there (Win32 hardlinks
 * would need CreateHardLinkA which isn't worth the porting cost for
 * a tier-1 binary whose Windows story is "best-effort").
 */
typedef struct {
    long long device;    /* "device_id" (0 when absent) */
    long long inode;
    char *path;          /* malloc'd; freed by hardlink_map_free */
} hardlink_entry;

typedef struct {
    hardlink_entry *entries;
    size_t count;
    size_t cap;
} hardlink_map;

/* Issues #781, #783 -- items that are not restored.
 *
 * Every node the walk does not restore is named on stderr in a
 * "NOT RESTORED: <path> (<reason>)" line and gets a row in
 * RESTORE_FAILURES.txt under the restore target (the same file, row
 * format and reasons as tier 3).  There are three kinds:
 *
 *   NR_FAILED   an error (a refused name, a link target that cannot be
 *               used, an unknown item type, a folder in the way, data
 *               that cannot be read, #909).  The walk carries on, and
 *               the restore ends "finished with errors"
 *               (LCSAS_TREE_INCOMPLETE, exit 4).
 *   NR_SPECIAL  a pipe, socket or device this system would not create
 *               (a device needs root; Windows has none of them).  It
 *               holds no data: listed, exit code unaffected.
 *   NR_LINK     a symlink the target file system cannot hold (FAT32,
 *               exFAT, most SMB shares; Windows tier 1 never makes
 *               links).  Listed, exit code unaffected.
 */
#define NR_FAILED  0
#define NR_SPECIAL 1
#define NR_LINK    2

typedef struct {
    unsigned long count[3];
    char **rows;           /* "path\treason\t" (blob id empty), malloc'd */
    size_t nrows;
    size_t cap;
    unsigned long rows_lost;   /* rows dropped on an allocation failure */
} nr_report;

#define RESTORE_FAILURES_NAME "RESTORE_FAILURES.txt"

/* Length of the valid UTF-8 sequence that starts at s (1 to 4), or 0
 * when the bytes there are not one (overlong forms, surrogates and
 * code points above U+10FFFF are not valid). */
static size_t
utf8_seq_len(const unsigned char *s, size_t n)
{
    unsigned char c = s[0];
    unsigned char lo = 0x80, hi = 0xBF;
    size_t need, k;
    if (c < 0x80) return 1;
    if (c >= 0xC2 && c <= 0xDF) need = 2;
    else if (c >= 0xE0 && c <= 0xEF) need = 3;
    else if (c >= 0xF0 && c <= 0xF4) need = 4;
    else return 0;
    if (c == 0xE0) lo = 0xA0;
    if (c == 0xED) hi = 0x9F;
    if (c == 0xF0) lo = 0x90;
    if (c == 0xF4) hi = 0x8F;
    if (n < need) return 0;
    if (s[1] < lo || s[1] > hi) return 0;
    for (k = 2; k < need; k++) {
        if (s[k] < 0x80 || s[k] > 0xBF) return 0;
    }
    return need;
}

/* Copy `n` bytes of a name, path or reason to `out` (when not NULL)
 * so that it cannot move the cursor, hide text or split a row: a
 * control character, DEL, or a byte that is not part of valid UTF-8 is
 * written as \xNN.  Tier 3's _visible() shows them the same way.
 * Returns the length written. */
static size_t
visible_text(char *out, const char *s, size_t n)
{
    static const char hexd[] = "0123456789abcdef";
    size_t i = 0, o = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        size_t k = utf8_seq_len((const unsigned char *)s + i, n - i);
        if (c < 0x20 || c == 0x7F || k == 0) {
            if (out) {
                out[o] = '\\';
                out[o + 1] = 'x';
                out[o + 2] = hexd[c >> 4];
                out[o + 3] = hexd[c & 15];
            }
            o += 4;
            i++;
        } else {
            if (out) memcpy(out + o, s + i, k);
            o += k;
            i += k;
        }
    }
    return o;
}

/* "<dir>/<name>\t<reason>\t" with each part made visible; malloc'd,
 * or NULL when out of memory.  A NULL `name` makes `dir` the whole
 * path (#909: a folder whose listing cannot be read, the restore
 * target itself included).  *path_len gets the length of the path
 * part. */
static char *
nr_row(const char *dir, const char *name, size_t name_len,
       const char *reason, size_t *path_len)
{
    size_t dl = strlen(dir), rl = strlen(reason);
    size_t pn = visible_text(NULL, dir, dl)
                + (name ? 1 + visible_text(NULL, name, name_len) : 0);
    size_t total = pn + 1 + visible_text(NULL, reason, rl) + 1;
    char *row = (char *)malloc(total + 1);
    size_t o = 0;
    if (!row) return NULL;
    o += visible_text(row + o, dir, dl);
    if (name) {
        row[o++] = '/';
        o += visible_text(row + o, name, name_len);
    }
    row[o++] = '\t';
    o += visible_text(row + o, reason, rl);
    row[o++] = '\t';
    row[o] = '\0';
    *path_len = pn;
    return row;
}

/* Name one item that is not restored: a "NOT RESTORED" line on stderr,
 * a row for RESTORE_FAILURES.txt, and a count of its kind. */
static void
nr_report_item(nr_report *rep, int kind, const char *dir,
               const char *name, size_t name_len, const char *reason)
{
    size_t plen = 0;
    char *row = nr_row(dir, name, name_len, reason, &plen);
    rep->count[kind]++;
    if (!row) {
        fprintf(stderr, "NOT RESTORED: %s/... (%s)\n", dir, reason);
        rep->rows_lost++;
        return;
    }
    /* The row is "<path>\t<reason>\t": print both parts as shown. */
    fprintf(stderr, "NOT RESTORED: %.*s (%.*s)\n", (int)plen, row,
            (int)(strlen(row) - plen - 2), row + plen + 1);
    if (rep->nrows == rep->cap) {
        size_t ncap = rep->cap ? rep->cap * 2 : 16;
        char **p = (char **)realloc(rep->rows, ncap * sizeof(char *));
        if (!p) {
            free(row);
            rep->rows_lost++;
            return;
        }
        rep->rows = p;
        rep->cap = ncap;
    }
    rep->rows[rep->nrows++] = row;
}

static void
nr_report_free(nr_report *rep)
{
    size_t i;
    for (i = 0; i < rep->nrows; i++) free(rep->rows[i]);
    free(rep->rows);
    rep->rows = NULL;
    rep->nrows = rep->cap = 0;
}

/* Issue #781 -- the escaped text of the node name being decoded.  One
 * buffer for the whole walk (the name is decoded before any recursion
 * and the text is not needed after), so the per-frame stack the depth
 * cap is sized for does not grow. */
static char g_stored_name[1024];

/* Issue #781 -- read the node's "name" and decode it to the raw bytes
 * to create.  The JSON string holds rustic's escaped text (see
 * lcsas_unescape_name), and the path-safety checks run on the DECODED
 * bytes, never on the escaped text.  Returns 0 with the name in
 * `out`; otherwise reports the node as not restored (NR_FAILED) and
 * returns -1. */
static int
decode_node_name(const char *src, const lcsas_json_tok *tok,
                 const char *target_dir, char *out, size_t cap,
                 nr_report *rep)
{
    const char *why = NULL;
    const char *shown = g_stored_name;
    size_t shown_len = 0;
    long n = -1;
    char reason[160];
    if (tok && tok->type == LCSAS_JSON_STRING) {
        n = lcsas_json_decode_string(src, tok, g_stored_name,
                                     sizeof g_stored_name);
    }
    if (n < 0) {
        why = "the name is too long or badly stored";
        if (tok) {
            shown = src + tok->start;
            shown_len = tok->end > tok->start ? tok->end - tok->start : 0;
            if (shown_len > 200) shown_len = 200;
        }
    } else {
        long m;
        shown_len = (size_t)n;
        m = lcsas_unescape_name(g_stored_name, (size_t)n, out, cap);
        if (m < 0) {
            why = "the name cannot be decoded";
        } else {
            why = lcsas_name_refusal(out, (size_t)m);
#ifdef _WIN32
            if (!why) why = lcsas_name_windows_refusal(out, (size_t)m);
#endif
#ifdef __APPLE__
            if (!why && !lcsas_name_is_utf8(out, (size_t)m)) {
                why = "macOS cannot store a name that is not valid UTF-8";
            }
#endif
        }
    }
    if (!why) return 0;
    sprintf(reason, "name refused: %.140s", why);
    nr_report_item(rep, NR_FAILED, target_dir, shown, shown_len, reason);
    return -1;
}

static void
hardlink_map_init(hardlink_map *m)
{
    m->entries = NULL;
    m->count = 0;
    m->cap = 0;
}

static void
hardlink_map_free(hardlink_map *m)
{
    size_t i;
    if (!m || !m->entries) return;
    for (i = 0; i < m->count; i++) free(m->entries[i].path);
    free(m->entries);
    m->entries = NULL;
    m->count = m->cap = 0;
}

/* Lookup: returns the stored path or NULL.  Linear search — the map
 * is bounded by the # of hardlinked files in the snapshot, typically
 * small enough that an O(n) walk per node is cheaper than the alloc/
 * compare overhead of anything fancier. */
static const char *
hardlink_map_find(const hardlink_map *m, long long device, long long inode)
{
    size_t i;
    if (!m) return NULL;
    for (i = 0; i < m->count; i++) {
        if (m->entries[i].inode == inode && m->entries[i].device == device)
            return m->entries[i].path;
    }
    return NULL;
}

static int
hardlink_map_insert(hardlink_map *m, long long device, long long inode,
                    const char *path)
{
    char *dup;
    size_t plen;
    if (!m) return -1;
    if (m->count == m->cap) {
        size_t newcap = m->cap ? m->cap * 2 : 64;
        hardlink_entry *p = (hardlink_entry *)realloc(
            m->entries, newcap * sizeof(hardlink_entry));
        if (!p) return -1;
        m->entries = p;
        m->cap = newcap;
    }
    plen = strlen(path);
    dup = (char *)malloc(plen + 1);
    if (!dup) return -1;
    memcpy(dup, path, plen + 1);
    m->entries[m->count].device = device;
    m->entries[m->count].inode = inode;
    m->entries[m->count].path = dup;
    m->count++;
    return 0;
}

/* Decode the "inode" field on a file node.  Returns 0 and writes
 * *out on success; returns -1 if the field is missing, not a number,
 * or fails to parse.  Per rustic semantics, inode == 0 means "not
 * hardlinked" and is treated the same as absent. */
static int
decode_node_inode(const char *src, const lcsas_json_tok *toks,
                  long node_idx, long long *out)
{
    long ino_i = lcsas_json_obj_get(src, toks, node_idx, "inode");
    long long val = 0;
    if (ino_i < 0 || toks[ino_i].type != LCSAS_JSON_NUMBER) return -1;
    if (lcsas_json_decode_int(src, &toks[ino_i], &val) != 0) return -1;
    if (val == 0) return -1;
    *out = val;
    return 0;
}

/* Issue #653 — the hardlink key of a file node.  Returns 0 and fills
 * (*dev_out, *ino_out) only when the node is a hardlink candidate:
 * "links" > 1 and a nonzero "inode".  "device_id" is optional (0 when
 * absent).  Any field that is present but cannot be decoded makes the
 * node a non-candidate, so a doubtful node gets its own content copy
 * rather than a link to another file's content. */
static int
decode_node_hardlink_key(const char *src, const lcsas_json_tok *toks,
                         long node_idx, long long *dev_out,
                         long long *ino_out)
{
    long links_i = lcsas_json_obj_get(src, toks, node_idx, "links");
    long dev_i;
    long long links = 0;
    long long dev = 0;
    if (links_i < 0 || toks[links_i].type != LCSAS_JSON_NUMBER) return -1;
    if (lcsas_json_decode_int(src, &toks[links_i], &links) != 0) return -1;
    if (links <= 1) return -1;
    if (decode_node_inode(src, toks, node_idx, ino_out) != 0) return -1;
    dev_i = lcsas_json_obj_get(src, toks, node_idx, "device_id");
    if (dev_i >= 0) {
        if (toks[dev_i].type != LCSAS_JSON_NUMBER) return -1;
        if (lcsas_json_decode_int(src, &toks[dev_i], &dev) != 0) return -1;
    }
    *dev_out = dev;
    return 0;
}

/* Issue #188 — preserve mtime from the restic tree node.
 *
 * Decode the "mtime" field on `node_idx` (an RFC 3339 string) into a
 * struct timespec pair where ts[0] = atime, ts[1] = mtime.  Both are
 * set to the same value (the snapshot's mtime) because restic doesn't
 * record a useful atime separately and tier-2 (rustic) sets atime =
 * mtime in practice.
 *
 * Returns 0 on success and fills `ts_out` (a 2-element array).  Returns
 * -1 if the field is missing, malformed, or can't be parsed; the caller
 * should treat that as "skip silently" rather than failing the restore.
 *
 * Unused on Windows (POSIX time semantics don't carry across; the
 * Windows port has no working utimensat shim).
 */

/* Issue #201 — translate restic's Go-encoded mode field into a
 * POSIX 12-bit mode word.
 *
 * Restic stores `mode` using Go's `os.FileMode` bit layout:
 *   - bits 0..8  POSIX rwxr-xr-x  (same as ours)
 *   - bit 20     ModeSticky  (1 << 20)
 *   - bit 22     ModeSetgid  (1 << 22)
 *   - bit 23     ModeSetuid  (1 << 23)
 * Bits 24..31 carry directory/symlink/device/etc. type flags which
 * we don't need (the `type` JSON field already tells us those).
 *
 * Prior tier-1 code masked with `& 07777`, which kept the low 12
 * bits — but the setuid/setgid/sticky bits restic emits live at
 * bits 23/22/20, NOT at POSIX positions 11/10/9.  So those bits
 * were silently dropped.
 *
 * Also accept POSIX-encoded variants (bits 11/10/9) in case a
 * non-rustic tree-blob author chose to encode mode the POSIX way.
 * Permissive OR: whichever encoding has the bit set, we honour it.
 *
 * Defined unconditionally (not behind _WIN32 guard) so the file/
 * dir/hardlink call sites compile on Windows even though the
 * post-content fchmod is itself Linux-only.
 */
static unsigned int
mode_from_go_filemode(long long m)
{
    unsigned int posix = (unsigned int)(m & 0777);
    if (m & ((long long)1 << 23)) posix |= 04000;  /* setuid (Go) */
    if (m & ((long long)1 << 22)) posix |= 02000;  /* setgid (Go) */
    if (m & ((long long)1 << 20)) posix |= 01000;  /* sticky (Go) */
    if (m & 04000) posix |= 04000;                 /* setuid (POSIX) */
    if (m & 02000) posix |= 02000;                 /* setgid (POSIX) */
    if (m & 01000) posix |= 01000;                 /* sticky (POSIX) */
    return posix;
}

/* The node's "mode" as a POSIX mode word, or `dflt` when absent or not
 * a number.  The one mode decoder for files, hard links, folders
 * and special files (#791: it was written out three times). */
static unsigned int
node_posix_mode(const char *src, const lcsas_json_tok *toks, long idx,
                unsigned int dflt)
{
    long mode_i = lcsas_json_obj_get(src, toks, idx, "mode");
    long long v = 0;
    if (mode_i < 0 || toks[mode_i].type != LCSAS_JSON_NUMBER) return dflt;
    if (lcsas_json_decode_int(src, &toks[mode_i], &v) != 0) return dflt;
    return mode_from_go_filemode(v);
}

#ifndef _WIN32
static int
decode_node_mtime(const char *src, const lcsas_json_tok *toks,
                  long node_idx, struct timespec ts_out[2])
{
    long mt_i = lcsas_json_obj_get(src, toks, node_idx, "mtime");
    char buf[64];
    long long sec = 0;
    long nsec = 0;

    if (mt_i < 0 || toks[mt_i].type != LCSAS_JSON_STRING) return -1;
    if (lcsas_json_decode_string(src, &toks[mt_i], buf, sizeof buf) < 0)
        return -1;
    if (lcsas_parse_iso8601_utc(buf, &sec, &nsec) != 0) return -1;

    ts_out[0].tv_sec = (time_t)sec;
    ts_out[0].tv_nsec = nsec;
    ts_out[1].tv_sec = (time_t)sec;
    ts_out[1].tv_nsec = nsec;
    return 0;
}

/* Write a blob to fd, lseek-ing past zero runs >= 4 KiB to leave
 * holes instead of materialising zeros.  Threshold matches
 * btrfs/ext4 default block size: smaller runs aren't worth the
 * extra syscall + potential fragmentation. */
#ifndef _WIN32
static int
write_blob_sparse(int fd, const unsigned char *buf, size_t len)
{
    const size_t HOLE_MIN = 4096;
    size_t i = 0;
    while (i < len) {
        size_t zstart = i;
        size_t zend;
        size_t zlen;
        /* Find the next zero. */
        while (zstart < len && buf[zstart] != 0) zstart++;
        /* Write the non-zero prefix [i, zstart). */
        if (zstart > i) {
            if (lcsas_write_exact(fd, buf + i, zstart - i) != 0)
                return -1;
        }
        if (zstart >= len) return 0;
        /* Measure the zero run. */
        zend = zstart;
        while (zend < len && buf[zend] == 0) zend++;
        zlen = zend - zstart;
        if (zlen >= HOLE_MIN) {
            /* Long zero run — seek past it, leaving a hole. */
            if (lseek(fd, (off_t)zlen, SEEK_CUR) == (off_t)-1)
                return -1;
        } else {
            /* Short zero run — fragmenting it isn't worth it. */
            if (lcsas_write_exact(fd, buf + zstart, zlen) != 0)
                return -1;
        }
        i = zend;
    }
    return 0;
}
#endif

/* Issue #189 — restore the snapshot's owner (uid) + group (gid).
 *
 * Restic stores uid/gid as integer fields on every tree node.  Only
 * root (or a process with CAP_CHOWN) can actually change ownership;
 * non-root attempts return EPERM.  We silently no-op when geteuid()
 * != 0 so a non-root restore still works — the restored files end up
 * owned by the account running it, matching the documented behaviour.
 *
 * Use lchown() (NOT chown) so the symlink's own uid/gid is set, not
 * the target's.  For files/dirs the two are equivalent.
 */
static void
apply_node_ownership(const char *src, const lcsas_json_tok *toks,
                     long node_idx, const char *path)
{
    long uid_i, gid_i;
    long long uid_v = -1, gid_v = -1;

    if (geteuid() != 0) return;

    uid_i = lcsas_json_obj_get(src, toks, node_idx, "uid");
    gid_i = lcsas_json_obj_get(src, toks, node_idx, "gid");
    if (uid_i >= 0 && toks[uid_i].type == LCSAS_JSON_NUMBER) {
        (void)lcsas_json_decode_int(src, &toks[uid_i], &uid_v);
    }
    if (gid_i >= 0 && toks[gid_i].type == LCSAS_JSON_NUMBER) {
        (void)lcsas_json_decode_int(src, &toks[gid_i], &gid_v);
    }
    if (uid_v < 0 || gid_v < 0) return;

    /* glibc's warn_unused_result on lchown fires through (void)
     * casts — assign-then-ignore is the workaround. */
    {
        int chown_rc = lchown(path, (uid_t)uid_v, (gid_t)gid_v);
        (void)chown_rc;
    }
}

/* Issue #190 — restore extended attributes from a tree node.
 *
 * Restic stores them as `extended_attributes`: an array of objects
 * each with a `name` (string) and `value` (base64-encoded raw bytes).
 * Many xattrs are namespaced — only `user.*` is writable by an
 * unprivileged process; `security.*` and `trusted.*` need root /
 * CAP_SYS_ADMIN.  We do best-effort restoration: any setxattr
 * failure is silent (errno EACCES is the common case for non-root +
 * non-user.* namespaces).
 *
 * Compiled out entirely when LCSAS_NO_XATTR is defined or _WIN32 is
 * set — the binary still restores file content and mode in that
 * case, matching the LIKELY-to-be-non-portable nature of xattrs.
 */
#if LCSAS_HAS_XATTR
static void
apply_node_xattrs(const char *src, const lcsas_json_tok *toks, long ntoks,
                  long node_idx, const char *path)
{
    long ea_i = lcsas_json_obj_get(src, toks, node_idx,
                                   "extended_attributes");
    long count, found = 0;
    long j;

    if (ea_i < 0) return;
    if (toks[ea_i].type != LCSAS_JSON_ARRAY) return;
    count = toks[ea_i].size;
    if (count <= 0) return;

    /* Walk the array by TOKEN INDEX, never by byte offset.  json_q's
     * `.end` is an exclusive byte offset into `src`, NOT a token index
     * (json_q.h:35), so advancing `j` by it would run off the end of
     * toks[].  Instead, scan tokens forward and treat each direct child
     * of the array (parent == ea_i) as one element; nested subtree
     * tokens are skipped by the parent filter.  Bounded by both ntoks
     * and the declared element count — the same idiom as the content
     * and nodes walks above (tree.c ~658, ~869). */
    for (j = ea_i + 1; j < ntoks && found < count; j++) {
        long name_i, value_i, name_n;
        char name_buf[256];
        unsigned char *value_buf = NULL;
        long value_len = 0;

        if (toks[j].parent != ea_i) continue;
        found++;
        if (toks[j].type != LCSAS_JSON_OBJECT) continue;

        name_i = lcsas_json_obj_get(src, toks, j, "name");
        value_i = lcsas_json_obj_get(src, toks, j, "value");
        if (name_i < 0 || value_i < 0) continue;
        if (toks[name_i].type != LCSAS_JSON_STRING) continue;

        name_n = lcsas_json_decode_string(src, &toks[name_i],
                                          name_buf, sizeof name_buf);
        if (name_n <= 0) continue;

        /* Value: a base64-encoded raw byte string.  lcsas_b64_decode
         * has no destination-length parameter, so we MUST size the
         * destination buffer from the encoded length BEFORE decoding
         * to prevent a stack/heap overflow (xattrs can be up to
         * 64 KiB on Linux).  Decoded size <= ceil(enc_len * 3 / 4). */
        if (toks[value_i].type == LCSAS_JSON_STRING) {
            size_t enc_len =
                (size_t)(toks[value_i].end - toks[value_i].start);
            size_t cap = (enc_len + 3) / 4 * 3 + 8;
            value_buf = (unsigned char *)malloc(cap);
            if (!value_buf) continue;  /* best-effort: skip on OOM */
            value_len = lcsas_b64_decode(
                src + toks[value_i].start, enc_len, value_buf);
            if (value_len < 0 || (size_t)value_len > cap) value_len = 0;
        }

        /* lsetxattr matches lchown semantics — operates on the
         * symlink itself, not the target. */
        if (value_buf) {
            (void)lsetxattr(path, name_buf, value_buf,
                            (size_t)value_len, 0);
            free(value_buf);
        }
    }
}
#else
/* Stub when xattr support compiled out — xattrs silently dropped. */
static void
apply_node_xattrs(const char *src, const lcsas_json_tok *toks, long ntoks,
                  long node_idx, const char *path)
{
    (void)src; (void)toks; (void)ntoks; (void)node_idx; (void)path;
}
#endif

#endif

void
lcsas_progress_init(lcsas_progress *p, unsigned long long total_hint)
{
    if (!p) return;
    p->enabled = 1;
    p->total_blob_hint = total_hint;
    p->blobs_done = 0;
    p->bytes_done = 0;
    p->last_tick_blobs = 0;
    p->last_tick_bytes = 0;
    p->blobs_per_tick = LCSAS_PROGRESS_DEFAULT_BLOBS_PER_TICK;
    p->bytes_per_tick = LCSAS_PROGRESS_DEFAULT_BYTES_PER_TICK;
}

static void
emit_progress_line(const lcsas_progress *p)
{
    /* Render bytes as integer MB to keep the format `\d+/\d+` clean
     * for downstream regex matching (no decimal point in the number). */
    unsigned long long mb = p->bytes_done / (1024ULL * 1024ULL);
    fprintf(stderr,
            "[lcsas-restore] progress: %llu/%llu blobs, %llu MB\n",
            p->blobs_done, p->total_blob_hint, mb);
}

void
lcsas_progress_tick(lcsas_progress *p, unsigned long long blob_len)
{
    unsigned long long d_blobs;
    unsigned long long d_bytes;

    if (!p || !p->enabled) return;
    p->blobs_done++;
    p->bytes_done += blob_len;

    d_blobs = p->blobs_done - p->last_tick_blobs;
    d_bytes = p->bytes_done - p->last_tick_bytes;
    if (d_blobs >= p->blobs_per_tick || d_bytes >= p->bytes_per_tick) {
        emit_progress_line(p);
        p->last_tick_blobs = p->blobs_done;
        p->last_tick_bytes = p->bytes_done;
    }
}

void
lcsas_progress_finish(const lcsas_progress *p)
{
    if (!p || !p->enabled) return;
    /* Always emit a final line so the heir sees the closing count
     * even if the last tick already fired exactly at completion. */
    emit_progress_line(p);
}

/* Issue #909 -- a file or folder whose data could not be read is listed
 * (NOT RESTORED line, RESTORE_FAILURES.txt row) and the walk goes on;
 * the restore then ends LCSAS_TREE_INCOMPLETE (exit 4), as tier 3's.
 * restore_file_node returns this once it has listed the file. */
#define TREE_ITEM_LISTED 3

/* Issue #909 -- the RESTORE_FAILURES.txt reason for a blob that
 * lcsas_repo_read_blob could not read (its return code `code`), naming
 * the pack; tier 3 writes the same text.  Returns 0 with the reason in
 * `reason` (96 bytes), or -1 when the restore must stop instead: 'q'
 * (or the end of input) at the disc prompt (exit 75), checked first,
 * or a failure that is not about this item (out of memory). */
static int
read_fail_reason(int code, const unsigned char pack_id[LCSAS_BLOB_ID_LEN],
                 const struct lcsas_disc_locator *locator, char *reason)
{
    const char *what;
    char hex[LCSAS_HEX_BLOB_ID_LEN + 1];
    if (locator && locator->reader_quit) return -1;
    if (code == LCSAS_REPO_ERR_NOT_FOUND) what = "pack not found";
    else if (code == LCSAS_REPO_ERR_DAMAGED) what = "pack damaged";
    else if (code == LCSAS_REPO_ERR_UNREADABLE) what = "pack unreadable";
    else return -1;
    lcsas_hex_encode(pack_id, LCSAS_BLOB_ID_LEN, hex);
    hex[LCSAS_HEX_BLOB_ID_LEN] = '\0';
    sprintf(reason, "%s: %s", what, hex);
    return 0;
}

/* Issue #909 -- "blob not in index: <id>", the id as stored (at most
 * 64 characters of it; the row makes it visible). */
static void
not_in_index_reason(const char *id, size_t len, char *reason)
{
    if (len > LCSAS_HEX_BLOB_ID_LEN) len = LCSAS_HEX_BLOB_ID_LEN;
    sprintf(reason, "blob not in index: %.*s", (int)len, id);
}

/* Issue #784 -- what the snapshot has at a path, for make_way(). */
#define WANT_DIR   0
#define WANT_FILE  1
#define WANT_OTHER 2   /* a symlink or a special file */

static const char *make_way(const char *path, int want);

/* Issue #909 -- the name a file is written under until it is complete
 * (tier 3 uses the same one). */
#define PARTIAL_SUFFIX ".lcsas-partial"

/* Issue #909 -- the partial file's path (the caller keeps a file's path
 * under 4096 bytes) and a NOT RESTORED reason: one buffer each for the
 * whole walk, as g_link_target, not on the stack.  restore_file_node
 * may be inlined into the recursive walk, whose per-frame stack the
 * depth cap is sized for (T1C-04); each is used before any recursion. */
static char g_partial[4096 + sizeof PARTIAL_SUFFIX];
static char g_reason[96];

/* Restore one file node to `target_path`.  The content is written to
 * "<target_path>.lcsas-partial", which gets the node's owner, mode,
 * xattrs and times and is then moved onto `target_path` (#909): a file
 * an earlier restore left there is replaced only by a complete file,
 * and kept as it was when this one fails.  Returns 0; TREE_ITEM_LISTED
 * when the file's data could not be read (it is listed, nothing is left
 * at either path); LCSAS_TREE_DISK_FULL; or -1 (the walk stops). */
static int
restore_file_node(const char *repo_path,
                  const lcsas_master_key *mk,
                  const lcsas_blob_index *ix,
                  const char *src,
                  const lcsas_json_tok *toks,
                  long ntoks,
                  long node_idx,
                  const char *target_path,
                  struct lcsas_disc_locator *locator,
                  lcsas_progress *progress,
                  hardlink_map *hlmap,
                  nr_report *rep)
{
    long content_idx = lcsas_json_obj_get(src, toks, node_idx, "content");
    int fd;
    long t;
    long blob_count;
    long found = 0;
    int rc = 0;
    unsigned int final_posix_mode = 0644;
    char *partial = g_partial;
    char *reason = g_reason;
    const char *why;
    /* #909: a hardlink key is recorded only once its file is complete,
     * so a later name never links to a file that failed. */
    int hl_record = 0;
    long long hl_dev = 0, hl_ino = 0;

    sprintf(partial, "%s" PARTIAL_SUFFIX, target_path);

    /* Issue #92 — idempotent resume: skip files that are already fully
     * restored.  We compare the on-disk file size against the "size"
     * field recorded in the snapshot tree node.  If they match we
     * assume the file is intact and return early, exactly as rustic
     * (tier 2) does by default.
     *
     * lcsas_create_file() opens with O_TRUNC, so the check MUST come
     * before that call or we would clobber the file unconditionally.
     *
     * Issue #192 — on a hit, register the path in the hardlink map
     * so subsequent occurrences of the same inode can link() to it.
     * Otherwise a partial-resume restore would never restore the
     * hardlinks even though the first occurrence was already on disk. */
    {
        long size_idx = lcsas_json_obj_get(src, toks, node_idx, "size");
        if (size_idx >= 0 && toks[size_idx].type == LCSAS_JSON_NUMBER) {
            long long expected_size = 0;
            if (lcsas_json_decode_int(src, &toks[size_idx], &expected_size) == 0
                    && expected_size >= 0) {
                struct stat st;
                int same = 0;
                /* #784: lstat -- a symlink at the path is never taken
                 * for this file (the walk has removed one already). */
#ifndef _WIN32
                if (lstat(target_path, &st) == 0
#else
                if (stat(target_path, &st) == 0
#endif
                        && S_ISREG(st.st_mode)
                        && (long long)st.st_size == expected_size) {
                    same = 1;
#ifndef _WIN32
                    /* Issue #661 — size alone does not prove the file
                     * is this snapshot's file: a run of a DIFFERENT
                     * snapshot into the same folder would keep the old
                     * content of every same-size file.  A finished file
                     * carries the node's mtime (applied last, below),
                     * so require that too, to within a second.  Issue
                     * #787 -- FAT keeps times in 2-second steps, so a
                     * file with an odd-second mtime comes back 1 s off
                     * on a FAT or exFAT stick; a tolerance of less than
                     * 2 s lets a resume skip it.  Windows tier 1 does
                     * not restore mtimes, so it stays size-only. */
                    {
                        struct timespec nts[2];
                        if (decode_node_mtime(src, toks, node_idx, nts) == 0) {
                            long long d = (long long)st.st_mtime
                                        - (long long)nts[1].tv_sec;
                            if (d >= 2 || d <= -2) same = 0;
                        }
                    }
#endif
                }
                if (same) {
                    int pk = lcsas_entry_kind(partial);
                    fprintf(stderr,
                            "[lcsas-restore] skipping already-restored: %s\n",
                            target_path);
                    /* #909: a partial file a later, stopped run left
                     * is not needed (tier 3 removes it too). */
                    if (pk == LCSAS_ENTRY_FILE || pk == LCSAS_ENTRY_LINK) {
                        (void)unlink(partial);
                    }
#ifndef _WIN32
                    if (hlmap) {
                        long long dev = 0, ino = 0;
                        if (decode_node_hardlink_key(src, toks, node_idx,
                                                     &dev, &ino) == 0
                                && hardlink_map_find(hlmap, dev, ino)
                                       == NULL) {
                            (void)hardlink_map_insert(hlmap, dev, ino,
                                                      target_path);
                        }
                    }
#endif
                    return 0;
                }
            }
        }
    }

#ifndef _WIN32
    /* Issue #192 — hardlink reconstruction.  If this node's
     * (device_id, inode) key (links > 1 only, #653) matches one we've
     * already restored in this snapshot, link()
     * to the previously-restored path instead of writing a fresh
     * content copy.  Falls through to the normal restore path if
     * the inode is missing, zero, unmapped, or the link() call
     * fails (best-effort: a failed link still gets a content copy). */
    if (hlmap) {
        long long device = 0, inode = 0;
        if (decode_node_hardlink_key(src, toks, node_idx,
                                     &device, &inode) == 0) {
            const char *prior = hardlink_map_find(hlmap, device, inode);
            if (prior) {
                /* Unlink any pre-existing target; link() refuses to
                 * overwrite, and we may be re-running over a stale
                 * tree (e.g. partial restore). */
                (void)unlink(target_path);
                if (link(prior, target_path) == 0) {
                    /* Apply node mode + mtime via lstat path API.
                     * Hardlinks share inode metadata so this also
                     * touches `prior` — that's the snapshot's
                     * intent (all names point at the same content). */
                    (void)chmod(target_path,
                                (mode_t)node_posix_mode(src, toks, node_idx,
                                                        0644));
                    {
                        struct timespec ts[2];
                        if (decode_node_mtime(src, toks, node_idx, ts) == 0) {
                            (void)utimensat(AT_FDCWD, target_path, ts, 0);
                        }
                    }
                    return 0;
                }
                /* link() failed (cross-device? EXDEV on weird
                 * filesystems?) — fall through to normal restore. */
            } else {
                /* First occurrence: record the path once the file is
                 * complete (below), so subsequent nodes with the same
                 * inode become link()s. */
                hl_record = 1;
                hl_dev = device;
                hl_ino = inode;
            }
        }
    }
#else
    (void)hlmap;
    (void)hl_record; (void)hl_dev; (void)hl_ino;
#endif

    /* Pull the "mode" field from the tree node (restic stores it as
     * Go's os.FileMode; see mode_from_go_filemode).  Default to 0o644
     * if absent or malformed so restored files at least match the
     * common-case rustic behaviour.
     *
     * We capture the translated POSIX mode here and re-apply it
     * AFTER the content + ftruncate (issue #201).  The Linux kernel
     * strips setuid/setgid bits on every write() against a fd as a
     * security measure (preventing privilege-escalation via
     * overwriting a setuid binary).  Setting the bits via fchmod
     * inside lcsas_create_file gets clobbered by the content loop.
     * Setting them ONE LAST TIME after the loop sticks. */
    /* #909, #784: never write through what is at the partial file's
     * path: a link or an old partial file is removed, a folder stops
     * the restore. */
    why = make_way(partial, WANT_FILE);
    if (why) {
        fprintf(stderr, "ERROR: %s: %s\n", partial, why);
        errno = 0;
        return -1;
    }
    {
        final_posix_mode = node_posix_mode(src, toks, node_idx, 0644);
#ifdef _WIN32
        /* Writable while it is written, so a failed one can be removed;
         * lcsas_replace_file makes it read-only when the mode says so. */
        fd = lcsas_create_file(partial, final_posix_mode | 0200);
#else
        fd = lcsas_create_file(partial, final_posix_mode);
#endif
        /* #654: errno now describes only what fails from here on (the
         * resume stat and the hardlink unlink above leave ENOENT). */
        if (fd >= 0) errno = 0;
    }
    if (fd < 0) {
        /* Issue #221 — classify ENOSPC / EDQUOT explicitly so the
         * heir knows the target filesystem ran out of room
         * rather than seeing a generic "file restore failed". */
        int saved_errno = errno;
        if (saved_errno == ENOSPC || saved_errno == EDQUOT) {
            fprintf(stderr,
                    "ERROR: target directory out of space "
                    "(path=%s, errno=%d)\n",
                    target_path, saved_errno);
            errno = saved_errno;
            return LCSAS_TREE_DISK_FULL;
        }
        return -1;
    }

    if (content_idx < 0 || toks[content_idx].type != LCSAS_JSON_ARRAY) {
        /* Empty content -> empty file.  Fall through to the timestamp
         * block so an empty file still carries the snapshot mtime. */
        blob_count = 0;
    } else {
        blob_count = toks[content_idx].size;
    }

    for (t = content_idx + 1;
         content_idx >= 0 && t < ntoks && found < blob_count; t++) {
        if (toks[t].parent == content_idx
                && toks[t].type == LCSAS_JSON_STRING) {
            unsigned char id[32];
            const lcsas_blob_loc *loc;
            unsigned char *blob = NULL;
            size_t blob_len = 0;

            found++;

            /* #909: an id that is not 64 hex digits is not in the
             * index either (tier 3 lists it the same way). */
            loc = NULL;
            if (toks[t].size == 64
                    && lcsas_hex_decode(src + toks[t].start, 32, id) == 0) {
                loc = lcsas_blob_index_find(ix, id);
            }
            if (!loc) {
                fprintf(stderr, "blob not in index: %.*s\n",
                        (int)(toks[t].size < 64 ? toks[t].size : 64),
                        src + toks[t].start);
                not_in_index_reason(src + toks[t].start,
                                    (size_t)toks[t].size, reason);
                rc = TREE_ITEM_LISTED; break;
            }
            {
                int brc = lcsas_repo_read_blob(repo_path, mk, loc, locator,
                                               &blob, &blob_len);
                if (brc != 0) {
                    rc = read_fail_reason(brc, loc->pack_id, locator, reason)
                         == 0 ? TREE_ITEM_LISTED : -1;
                    break;
                }
            }
            {
#ifndef _WIN32
                /* Issue #193 — sparse-aware write: long zero runs
                 * become holes via lseek, short runs and non-zero
                 * bytes write normally.  Files restored from a
                 * sparse source (VM images, pre-allocated DB
                 * extents) end up with st_blocks similar to
                 * rustic-restore's output instead of dense
                 * materialisation. */
                int write_rc = write_blob_sparse(fd, blob, blob_len);
#else
                int write_rc = lcsas_write_exact(fd, blob, blob_len);
#endif
                if (write_rc != 0) {
                    /* Issue #221 — classify ENOSPC / EDQUOT
                     * specifically; this is the dominant
                     * environmental failure on a long restore.
                     * `bytes_written` (from the post-fail seek
                     * position) tells the heir how far the
                     * restore got before the target filled. */
                    int saved_errno = errno;
                    if (saved_errno == ENOSPC || saved_errno == EDQUOT) {
                        off_t cur = lseek(fd, 0, SEEK_CUR);
                        fprintf(stderr,
                                "ERROR: target directory out of space "
                                "(path=%s, bytes_written=%lld, "
                                "errno=%d)\n",
                                target_path,
                                (long long)(cur >= 0 ? (long long)cur : -1),
                                saved_errno);
                        errno = saved_errno;
                        free(blob); rc = LCSAS_TREE_DISK_FULL; break;
                    }
                    free(blob); rc = -1; break;
                }
            }
            lcsas_progress_tick(progress, (unsigned long long)blob_len);
            free(blob);
        }
        if (toks[t].start >= toks[content_idx].end) break;
    }

#ifndef _WIN32
    /* Issue #193 — if the last operation in the content loop was an
     * lseek past zero bytes at end-of-file, the file's logical size
     * doesn't reflect the trailing hole.  ftruncate it to the
     * snapshot's declared size to guarantee size parity with the
     * original (and with tier-2's restored output).
     *
     * Issue #245 — guard with rc==0.  Previously this fired even
     * when the blob loop bailed out with rc=-1 (transient pack
     * read failure, mid-disc-swap, etc.), padding the partial
     * file with a sparse hole up to expected_size.  That artifact
     * then survived because the #92 idempotent-resume check sees a
     * file at the right size and skips it next pass.  Symptom:
     * one random file silently restored as 130 KiB of zeros on
     * every blind-restore run.  On failure we'd rather leave the
     * file at its actual written length so resume re-attempts it. */
    if (rc == 0) {
        long size_idx = lcsas_json_obj_get(src, toks, node_idx, "size");
        if (size_idx >= 0 && toks[size_idx].type == LCSAS_JSON_NUMBER) {
            long long expected_size = 0;
            if (lcsas_json_decode_int(src, &toks[size_idx], &expected_size) == 0
                    && expected_size >= 0) {
                int ftrc = ftruncate(fd, (off_t)expected_size);
                (void)ftrc;
            }
        }
    }
    /* Issue #201 — re-apply mode (incl. setuid/setgid/sticky) AFTER
     * content writes.  The kernel strips setuid+setgid on write()
     * for security; setting them after the last write makes them
     * stick.  Only when the file was written in full (rc still 0).
     *
     * Issue #785 — the order is owner, mode, xattrs, times (rustic's
     * order).  A change of owner clears setuid/setgid on Linux, so the
     * owner goes first and the mode after it; the times go last.
     *
     * Issue #188 — futimens() sets the times through the open fd.
     *
     * Issue #909 — all of it on the partial file; a rename keeps it. */
    if (rc == 0) {
        struct timespec ts[2];
        apply_node_ownership(src, toks, node_idx, partial);
        {
            int chmod_rc = fchmod(fd, (mode_t)final_posix_mode);
            (void)chmod_rc;
        }
        apply_node_xattrs(src, toks, ntoks, node_idx, partial);
        if (decode_node_mtime(src, toks, node_idx, ts) == 0) {
            (void)futimens(fd, ts);
        }
    }
#endif

    /* Windows will not rename or remove an open file: close first. */
    close(fd);
    if (rc == 0) {
        if (lcsas_replace_file(partial, target_path, final_posix_mode) != 0) {
            int saved_errno = errno;
            (void)unlink(partial);
            errno = saved_errno;
            return -1;
        }
#ifndef _WIN32
        if (hl_record) {
            (void)hardlink_map_insert(hlmap, hl_dev, hl_ino, target_path);
        }
#endif
        return 0;
    }
    /* #909: nothing is left at either path; an older copy at
     * target_path is kept as it was.  A full disk gets its space back. */
    {
        int saved_errno = errno;
        (void)unlink(partial);
        errno = saved_errno;
    }
    if (rc == TREE_ITEM_LISTED) {
        nr_report_item(rep, NR_FAILED, target_path, NULL, 0, reason);
    }
    return rc;
}

/* Issue #784 -- make room at `path` (a path below the restore target)
 * for the item the snapshot has there, when the target folder already
 * holds an earlier restore.
 *
 * The rule (tier 3 follows the same one): whatever is in the way is
 * replaced, except a folder.  A folder where the snapshot has a file,
 * a link or a special file is never removed (it may hold anything);
 * that item is reported instead.  A symlink is never followed: one at
 * a file's or a folder's path is removed and the real item made in
 * its place, so no data is written through it.  On Windows a reparse
 * point (symlink, junction, mount point) is reported, not removed.  A
 * regular file where the snapshot has a file is left to the caller
 * (the resume check, then an overwrite).
 *
 * Returns NULL when the path is ready, else the reason it is not. */
static const char *
make_way(const char *path, int want)
{
    switch (lcsas_entry_kind(path)) {
    case LCSAS_ENTRY_NONE:
        return NULL;
    case LCSAS_ENTRY_DIR:
        return want == WANT_DIR
            ? NULL : "a folder with this name is already there";
    case LCSAS_ENTRY_FILE:
        if (want == WANT_FILE) return NULL;
        break;
#ifdef _WIN32
    case LCSAS_ENTRY_LINK:
        return "a link or junction with this name is already there";
#endif
    default:
        break;
    }
    if (unlink(path) != 0 && errno != ENOENT) {
        return "the item already there with this name cannot be removed";
    }
    return NULL;
}

#ifndef _WIN32
/* Issue #785 -- metadata of a folder, link or special file, in
 * rustic's order: owner, mode (not for a link), xattrs, times.  The
 * owner goes before the mode because a change of owner clears
 * setuid/setgid; the times go last.  All best-effort. */
static void
apply_path_meta(const char *src, const lcsas_json_tok *toks, long ntoks,
                long idx, const char *path, int is_link,
                unsigned int posix_mode)
{
    struct timespec ts[2];
    apply_node_ownership(src, toks, idx, path);
    if (!is_link) (void)chmod(path, (mode_t)posix_mode);
    apply_node_xattrs(src, toks, ntoks, idx, path);
    if (decode_node_mtime(src, toks, idx, ts) == 0) {
        (void)utimensat(AT_FDCWD, path, ts,
                        is_link ? AT_SYMLINK_NOFOLLOW : 0);
    }
}
#endif

/* Symlink targets: one buffer each for the whole walk (a target is
 * used before any recursion), so the per-frame stack the depth cap is
 * sized for does not grow.  4095 bytes is the longest target Linux
 * accepts. */
static char g_link_target[4096];
static char g_link_b64[5600];   /* base64 of up to 4095 bytes + slack */

/* Read the node's link target into g_link_target.  rustic stores a
 * target that is not valid UTF-8 twice: "linktarget" with each bad
 * byte replaced by U+FFFD, and "linktarget_raw", the exact bytes in
 * base64.  The raw bytes are used when present, so the link points
 * at the same name as in the backup.  Returns NULL, or the reason the
 * target cannot be used (tier 3 gives the same reasons). */
static const char *
decode_link_target(const char *src, const lcsas_json_tok *toks, long idx)
{
    long raw_i = lcsas_json_obj_get(src, toks, idx, "linktarget_raw");
    long lt_i = lcsas_json_obj_get(src, toks, idx, "linktarget");
    long n;
    if (raw_i >= 0 && toks[raw_i].type == LCSAS_JSON_STRING) {
        long need;
        n = lcsas_json_decode_string(src, &toks[raw_i], g_link_b64,
                                     sizeof g_link_b64);
        if (n < 0) {
            return toks[raw_i].end - toks[raw_i].start
                       >= (long)sizeof g_link_b64 - 1
                ? "the link target is too long"
                : "the link target cannot be decoded";
        }
        need = lcsas_b64_decode(g_link_b64, (size_t)n, NULL);
        if (need < 0) return "the link target cannot be decoded";
        if ((size_t)need >= sizeof g_link_target) {
            return "the link target is too long";
        }
        if (lcsas_b64_decode(g_link_b64, (size_t)n,
                             (unsigned char *)g_link_target) != need) {
            return "the link target cannot be decoded";
        }
        n = need;
    } else if (lt_i >= 0 && toks[lt_i].type == LCSAS_JSON_STRING) {
        n = lcsas_json_decode_string(src, &toks[lt_i], g_link_target,
                                     sizeof g_link_target);
        if (n < 0) {
            return toks[lt_i].end - toks[lt_i].start
                       >= (long)sizeof g_link_target - 1
                ? "the link target is too long"
                : "the link target cannot be decoded";
        }
    } else {
        return "the link has no target";
    }
    g_link_target[n] = '\0';
    if (n == 0) return "the link target is empty";
    if ((size_t)n != strlen(g_link_target)) {
        return "the link target holds a NUL byte";
    }
    return NULL;
}

/* Issue #783 -- the kind of special file a node type names, or NULL. */
static const char *
special_label(const char *type)
{
    if (strcmp(type, "fifo") == 0) return "FIFO";
    if (strcmp(type, "socket") == 0) return "socket";
    if (strcmp(type, "dev") == 0) return "block device";
    if (strcmp(type, "chardev") == 0) return "character device";
    return NULL;
}

/* Make a special file as rustic does (mkfifo, mknod).  A FIFO and a
 * socket need no privilege on Linux; a device needs root. */
static int
make_special(const char *type, const char *path, long long device)
{
#ifdef _WIN32
    (void)type; (void)path; (void)device;
    errno = ENOSYS;
    return -1;
#else
    if (strcmp(type, "fifo") == 0) return mkfifo(path, (mode_t)0600);
    if (strcmp(type, "socket") == 0) {
        return mknod(path, (mode_t)(S_IFSOCK | 0600), (dev_t)0);
    }
    if (strcmp(type, "dev") == 0) {
        return mknod(path, (mode_t)(S_IFBLK | 0600), (dev_t)device);
    }
    return mknod(path, (mode_t)(S_IFCHR | 0600), (dev_t)device);
#endif
}

static int
tree_restore_recurse(const char *repo_path,
                     const lcsas_master_key *mk,
                     const lcsas_blob_index *ix,
                     const char *tree_id_hex,
                     const char *target_dir,
                     const char *target_root,
                     struct lcsas_disc_locator *locator,
                     lcsas_progress *progress,
                     hardlink_map *hlmap,
                     nr_report *rep,
                     int depth)
{
    unsigned char tree_id[32];
    const lcsas_blob_loc *loc;
    unsigned char *blob = NULL;
    size_t blob_len = 0;
    lcsas_json_tok *toks = NULL;
    long ntoks;
    long nodes_arr;
    long node_count, found = 0;
    long t;
    int rc = -1;
    unsigned int dir_final_mode = 0755;  /* #654: applied after children */

    /* T1C-04: fail loud before the C stack overflows.  A SIGSEGV here
     * is indistinguishable from "the recovery software is broken" to a
     * non-technical heir; a named error is recoverable. */
    if (depth > lcsas_tree_max_depth) {
        fprintf(stderr,
            "ERROR: directory tree deeper than %d levels at %s\n"
            "       tier-1 caps recursion to avoid crashing; if this depth is\n"
            "       genuine, re-run with LCSAS_MAX_TREE_DEPTH=<n> and\n"
            "       'ulimit -s unlimited', or use tier-2 (rustic)\n",
            lcsas_tree_max_depth, target_dir);
        return -1;
    }

    /* Issue #909 -- a folder whose listing (tree blob) cannot be read is
     * listed, left empty, and the walk goes on, as in tier 3: the row
     * is the folder itself (the restore target, for the root tree). */
    loc = NULL;
    if (lcsas_hex_decode(tree_id_hex, 32, tree_id) == 0) {
        loc = lcsas_blob_index_find(ix, tree_id);
    }
    if (!loc) {
        fprintf(stderr, "tree blob not found: %s\n", tree_id_hex);
        not_in_index_reason(tree_id_hex, strlen(tree_id_hex), g_reason);
        nr_report_item(rep, NR_FAILED, target_dir, NULL, 0, g_reason);
        return 0;
    }
    {
        int brc = lcsas_repo_read_blob(repo_path, mk, loc, locator,
                                       &blob, &blob_len);
        if (brc != 0) {
            if (read_fail_reason(brc, loc->pack_id, locator, g_reason) != 0) {
                return -1;
            }
            nr_report_item(rep, NR_FAILED, target_dir, NULL, 0, g_reason);
            return 0;
        }
    }

    /* Trees can be large; grow the token buffer on demand (T1C-01).
     * The 1024-token start keeps the per-recursion-frame heap small
     * (~40 KB) for typical directories while still scaling to wide
     * folders / long blob lists. */
    ntoks = lcsas_json_parse_alloc((const char *)blob, blob_len, &toks, 1024);
    if (ntoks == -2) {
        fprintf(stderr,
                "ERROR: tree blob %.64s is too large for tier-1 (a directory "
                "with very many entries,\n       or a file with very many "
                "blobs); use tier-2 (rustic)\n", tree_id_hex);
        free(blob);
        return -1;
    }
    if (ntoks < 0) {
        fprintf(stderr, "ERROR: tree blob %.64s: invalid JSON\n", tree_id_hex);
        free(blob);
        return -1;
    }
    if (toks[0].type != LCSAS_JSON_OBJECT) {
        fprintf(stderr, "ERROR: tree blob %.64s: invalid JSON\n", tree_id_hex);
        goto out;
    }

    nodes_arr = lcsas_json_obj_get((char *)blob, toks, 0, "nodes");
    if (nodes_arr < 0 || toks[nodes_arr].type != LCSAS_JSON_ARRAY) {
        rc = 0; goto out;
    }
    node_count = toks[nodes_arr].size;

    /* mkdir target_dir if needed. */
    lcsas_mkdir_p(target_dir);

    /* T1C-04: bound the walk by ntoks.  A malformed blob (e.g. an
     * invalid token inside the array) can leave `found < node_count`
     * forever while t runs past the parsed-token count — reading
     * toks[t] out of bounds.  The start>=end break below only fires
     * for well-formed tokens, so the ntoks guard is the real backstop. */
    for (t = nodes_arr + 1; t < ntoks && found < node_count; t++) {
        if (!(toks[t].parent == nodes_arr
                  && toks[t].type == LCSAS_JSON_OBJECT)) {
            if (toks[t].start >= toks[nodes_arr].end) break;
            continue;
        }
        found++;

        {
            long name_i = lcsas_json_obj_get((char *)blob, toks, t, "name");
            long type_i = lcsas_json_obj_get((char *)blob, toks, t, "type");
            long subtree_i = lcsas_json_obj_get((char *)blob, toks, t, "subtree");
            char name_buf[1024];
            char type_buf[32];
            char node_path[4096];
            const char *why;
            const char *special;

            /* Issue #781: decode rustic's escaped name, then run the
             * path-safety checks (empty, ".", "..", '/', NUL; on
             * Windows also the characters NTFS cannot store) on the
             * decoded bytes.  A refused name is reported and counted,
             * and the walk carries on with the next node; the restore
             * then ends non-zero (see lcsas_tree_restore). */
            if (decode_node_name((char *)blob,
                                 name_i >= 0 ? &toks[name_i] : NULL,
                                 target_dir, name_buf, sizeof name_buf,
                                 rep) != 0) {
                continue;
            }
            if (type_i < 0
                    || lcsas_json_decode_string((char *)blob, &toks[type_i],
                                                type_buf,
                                                sizeof type_buf) < 0) {
                strcpy(type_buf, "?");
            }

            {
                /* T1C-04: a silently-truncated path restores a file to
                 * the WRONG location — fail loud like every other
                 * tier-1 error rather than corrupt the tree. */
                int plen = snprintf(node_path, sizeof node_path, "%s/%s",
                                    target_dir, name_buf);
                if (plen < 0 || (size_t)plen >= sizeof node_path) {
                    fprintf(stderr,
                            "ERROR: restored path too long (>4095 bytes): "
                            "%.200s...\n", target_dir);
                    goto out;
                }
            }

            special = special_label(type_buf);
            /* Issue #784: never write through what an earlier restore
             * left at this path (see make_way). */
            if (strcmp(type_buf, "file") == 0) {
                why = make_way(node_path, WANT_FILE);
            } else if (strcmp(type_buf, "dir") == 0) {
                why = make_way(node_path, WANT_DIR);
            } else if (strcmp(type_buf, "symlink") == 0 || special) {
                why = make_way(node_path, WANT_OTHER);
            } else {
                /* Issue #783: a type this reader does not know is not
                 * restored, and that is an error. */
                char reason[96];
                sprintf(reason, "unknown item type: %.40s", type_buf);
                nr_report_item(rep, NR_FAILED, target_dir, name_buf,
                               strlen(name_buf), reason);
                continue;
            }
            if (why) {
                nr_report_item(rep, NR_FAILED, target_dir, name_buf,
                               strlen(name_buf), why);
                continue;
            }

            if (strcmp(type_buf, "file") == 0) {
                int frc;
                errno = 0;
                frc = restore_file_node(repo_path, mk, ix,
                                        (char *)blob, toks, ntoks, t,
                                        node_path, locator, progress,
                                        hlmap, rep);
                /* #909: its data could not be read; it is listed. */
                if (frc == TREE_ITEM_LISTED) continue;
                if (frc != 0) {
                    /* Issue #654 — name the system error when there is
                     * one, so "permission denied" or "read-only file
                     * system" is not hidden behind a bare failure. */
                    int saved_errno = errno;
                    if (frc == LCSAS_TREE_DISK_FULL) rc = frc;
                    if (saved_errno != 0) {
                        fprintf(stderr, "file restore failed: %s (%s)\n",
                                node_path, strerror(saved_errno));
                    } else {
                        fprintf(stderr, "file restore failed: %s\n",
                                node_path);
                    }
                    goto out;
                }
            } else if (strcmp(type_buf, "dir") == 0) {
                if (lcsas_mkdir_p(node_path) != 0) {
                    int saved_errno = errno;
                    if (saved_errno == ENOSPC
                            || saved_errno == EDQUOT) {
                        fprintf(stderr,
                                "ERROR: target directory out of space "
                                "(mkdir path=%s, errno=%d)\n",
                                node_path, saved_errno);
                        rc = LCSAS_TREE_DISK_FULL;
                        goto out;
                    }
                    /* Non-ENOSPC mkdir failures stay best-effort:
                     * mkdir_p returns 0 on EEXIST so this is a real
                     * filesystem problem (perms, ENOENT path race,
                     * read-only mount).  Continue and let later
                     * operations surface the underlying issue. */
                }
                /* mkdir_p hardcodes 0700 for intermediates; the leaf
                 * directory's mode comes from the tree node's "mode"
                 * field.  Match tier-2 (rustic) parity.
                 *
                 * Issue #654 — the node's mode is applied AFTER the
                 * children are restored (below, with the mtime).  A
                 * read-only directory (0555, 0500) chmod'ed first made
                 * every child's create fail for a non-root user.  While
                 * the children are written the directory is owner
                 * rwx; this also re-opens a read-only directory left
                 * by an earlier run, so a resume gets past it. */
                dir_final_mode = node_posix_mode((char *)blob, toks, t, 0755);
#ifndef _WIN32
                (void)chmod(node_path, (mode_t)(dir_final_mode | 0700));
#else
                (void)dir_final_mode;
#endif
                if (subtree_i >= 0
                        && toks[subtree_i].type == LCSAS_JSON_STRING
                        && toks[subtree_i].size == 64) {
                    char sub_hex[65];
                    int sub_rc;
                    memcpy(sub_hex, (char *)blob + toks[subtree_i].start, 64);
                    sub_hex[64] = '\0';
                    sub_rc = tree_restore_recurse(repo_path, mk, ix,
                                                  sub_hex, node_path,
                                                  target_root, locator,
                                                  progress, hlmap, rep,
                                                  depth + 1);
                    if (sub_rc != 0) {
                        rc = sub_rc;   /* -1, or LCSAS_TREE_DISK_FULL */
                        goto out;
                    }
                }
#ifndef _WIN32
                /* Issues #654, #188, #189, #190, #785 — the folder's
                 * owner, final mode, xattrs and times, now that every
                 * child exists (creating a child would bump the mtime
                 * again). */
                apply_path_meta((char *)blob, toks, ntoks, t, node_path,
                                0, dir_final_mode);
#endif
            } else if (strcmp(type_buf, "symlink") == 0) {
                /* Issue #782: every link is restored with its stored
                 * target, as rustic does: absolute, relative, pointing
                 * up and out of the restore, or dangling.  Making a
                 * link writes nothing outside the target folder; the
                 * risk is a later write THROUGH a link, which make_way
                 * and O_NOFOLLOW rule out (#784). */
                why = decode_link_target((char *)blob, toks, t);
                if (why) {
                    char reason[96];
                    sprintf(reason, "link refused: %.70s", why);
                    nr_report_item(rep, NR_FAILED, target_dir, name_buf,
                                   strlen(name_buf), reason);
                    continue;
                }
                if (symlink(g_link_target, node_path) != 0) {
                    int saved_errno = errno;
                    if (saved_errno == ENOSPC
                            || saved_errno == EDQUOT) {
                        /* Issue #221 — out-of-space classifier. */
                        fprintf(stderr,
                                "ERROR: target directory out of space "
                                "(symlink path=%s, errno=%d)\n",
                                node_path, saved_errno);
                        rc = LCSAS_TREE_DISK_FULL;
                        goto out;
                    } else if (saved_errno == EPERM
                            || saved_errno == EOPNOTSUPP
                            || saved_errno == ENOSYS) {
                        /* Issue #224 — FAT32/exFAT/SMB targets cannot
                         * store symlinks at all; symlink(2) returns
                         * EPERM / EOPNOTSUPP / ENOSYS depending on the
                         * filesystem driver (and Windows tier 1 never
                         * makes links).  Listed, and the restore goes
                         * on: it is a limit of the target, not an
                         * error in the backup (#783). */
                        nr_report_item(rep, NR_LINK, target_dir, name_buf,
                                       strlen(name_buf),
                                       "link not created: the target file "
                                       "system does not support symlinks");
                    } else {
                        char reason[160];
                        sprintf(reason, "link not created: %.120s",
                                strerror(saved_errno));
                        nr_report_item(rep, NR_FAILED, target_dir,
                                       name_buf, strlen(name_buf), reason);
                    }
                }
#ifndef _WIN32
                else {
                    /* Issues #188, #189, #190, #785 — the link's own
                     * owner (lchown), xattrs and times
                     * (AT_SYMLINK_NOFOLLOW), never its target's. */
                    apply_path_meta((char *)blob, toks, ntoks, t,
                                    node_path, 1, 0);
                }
#endif
            } else {
                /* Issue #783: a FIFO, socket or device.  Made as rustic
                 * makes it; when this system will not (a device needs
                 * root, Windows has none of them) it is listed, and
                 * the exit code is not affected: it holds no data. */
                long long device = 0;
                long dev_i = lcsas_json_obj_get((char *)blob, toks, t,
                                                "device");
                if (dev_i >= 0 && toks[dev_i].type == LCSAS_JSON_NUMBER) {
                    (void)lcsas_json_decode_int((char *)blob, &toks[dev_i],
                                                &device);
                }
                if (make_special(type_buf, node_path, device) != 0) {
                    char reason[200];
#ifdef _WIN32
                    sprintf(reason, "special file (%s) not created: "
                            "this system cannot create it", special);
#else
                    sprintf(reason, "special file (%s) not created: %.120s",
                            special, strerror(errno));
#endif
                    nr_report_item(rep, NR_SPECIAL, target_dir, name_buf,
                                   strlen(name_buf), reason);
                }
#ifndef _WIN32
                else {
                    apply_path_meta((char *)blob, toks, ntoks, t, node_path,
                                    0, node_posix_mode((char *)blob, toks,
                                                       t, 0600));
                }
#endif
            }
        }
        if (toks[t].start >= toks[nodes_arr].end) break;
    }
    rc = 0;

out:
    free(blob);
    free(toks);
    return rc;
}

/* Issue #783 -- the first lines of RESTORE_FAILURES.txt.  Tier 3
 * writes the same text (restic_fallback.py, _FAILURES_HEADER) with its
 * own name on the second line. */
static const char *const failures_header[] = {
    "LCSAS restore: items that were NOT restored.",
    "Written by lcsas-restore (tier 1).  Everything else was restored.",
    "A row whose reason starts \"special file\" is a pipe, socket or",
    "device that this system cannot create.  It holds no data.",
    "A row whose reason starts \"link not created: the target file",
    "system\" is a link that this file system cannot hold.",
    "Every other row is an error: see \"ITEMS NOT RESTORED\" in RESTORE.txt.",
    "",
    "path\treason\tblob_id",
    NULL
};

/* Write RESTORE_FAILURES.txt under `target_root`, one row per item not
 * restored.  With no rows, remove one an earlier run left (only a file
 * that starts with our header), so it never describes an older run. */
static void
write_failures_file(const char *target_root, const nr_report *rep)
{
    char path[4200];
    int fd;
    size_t i;
    int plen = snprintf(path, sizeof path, "%s/%s", target_root,
                        RESTORE_FAILURES_NAME);
    if (plen < 0 || (size_t)plen >= sizeof path) return;
    if (rep->nrows == 0) {
        char head[6];
        int n = -1;
        if (lcsas_entry_kind(path) != LCSAS_ENTRY_FILE) return;
        fd = open(path, O_RDONLY | O_BINARY | O_NOFOLLOW);
        if (fd < 0) return;
        n = (int)read(fd, head, sizeof head);
        close(fd);
        if (n == (int)sizeof head && memcmp(head, "LCSAS ", 6) == 0) {
            (void)unlink(path);
        }
        return;
    }
    if (make_way(path, WANT_FILE) != NULL) {
        fprintf(stderr, "[lcsas-restore] could not write %s\n", path);
        return;
    }
    /* No O_BINARY: on Windows the C library writes CRLF line ends. */
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0644);
    if (fd < 0) {
        fprintf(stderr, "[lcsas-restore] could not write %s (%s)\n",
                path, strerror(errno));
        return;
    }
    for (i = 0; failures_header[i]; i++) {
        if (lcsas_write_exact(fd, failures_header[i],
                              strlen(failures_header[i])) != 0
                || lcsas_write_exact(fd, "\n", 1) != 0) {
            goto fail;
        }
    }
    for (i = 0; i < rep->nrows; i++) {
        if (lcsas_write_exact(fd, rep->rows[i], strlen(rep->rows[i])) != 0
                || lcsas_write_exact(fd, "\n", 1) != 0) {
            goto fail;
        }
    }
    close(fd);
    return;
fail:
    fprintf(stderr, "[lcsas-restore] could not write %s (%s)\n",
            path, strerror(errno));
    close(fd);
}

/*
 * Public entry point.  Owns the per-restore hardlink map and the list
 * of items not restored: allocates, passes them through the recursive
 * walk, frees on return.  Both are INTERNAL to tree.c so the public
 * API stays stable.
 */
int
lcsas_tree_restore(const char *repo_path,
                   const lcsas_master_key *mk,
                   const lcsas_blob_index *ix,
                   const char *tree_id_hex,
                   const char *target_dir,
                   const char *target_root,
                   struct lcsas_disc_locator *locator,
                   lcsas_progress *progress)
{
    hardlink_map hlmap;
    nr_report rep;
    int rc;
    int walk_done;
    memset(&rep, 0, sizeof rep);
    hardlink_map_init(&hlmap);
    rc = tree_restore_recurse(repo_path, mk, ix, tree_id_hex,
                              target_dir, target_root,
                              locator, progress, &hlmap,
                              &rep, 0);
    walk_done = (rc == 0);
    hardlink_map_free(&hlmap);
    /* Issues #781, #783: say how many items were not restored, of each
     * kind, and where the list is.  Tier 3 prints the same sentences.
     * Only errors make the restore "finished with errors". */
    if (rep.count[NR_SPECIAL] > 0) {
        fprintf(stderr,
                "[lcsas-restore] %lu special file(s) were not restored: "
                "pipes, sockets or devices that this system cannot "
                "create.  They hold no data.\n", rep.count[NR_SPECIAL]);
    }
    if (rep.count[NR_LINK] > 0) {
        fprintf(stderr,
                "[lcsas-restore] %lu link(s) were not restored: the target "
                "file system does not support symlinks.\n",
                rep.count[NR_LINK]);
    }
    if (rep.count[NR_FAILED] > 0) {
        fprintf(stderr,
                "[lcsas-restore] ERROR: %lu item(s) were NOT restored.\n",
                rep.count[NR_FAILED]);
        if (rc == 0) rc = LCSAS_TREE_INCOMPLETE;
    }
    /* A stale RESTORE_FAILURES.txt is removed only after a finished
     * walk, as tier 3 does: a walk that stopped early has not looked at
     * every item, so the old list may still be right.  Rows found
     * before the stop are written either way. */
    if (walk_done || rep.nrows > 0) {
        write_failures_file(target_root, &rep);
    }
    if (rep.nrows > 0) {
        fprintf(stderr, "[lcsas-restore] See %s/%s for the list.\n",
                target_root, RESTORE_FAILURES_NAME);
    }
    if (rep.rows_lost > 0) {
        fprintf(stderr,
                "[lcsas-restore] %lu of them are missing from the list "
                "(out of memory); see the NOT RESTORED lines above.\n",
                rep.rows_lost);
    }
    nr_report_free(&rep);
    return rc;
}
