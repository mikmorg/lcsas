/*
 * tree.c -- recursive tree-blob walker.
 *
 * Mirrors src/lcsas/restore/restic_fallback.py:_restore_tree.
 *
 * Memory model: a tree blob is loaded, parsed, and the loop walks
 * each child in source order.  Subtree recursion is iterative-by-
 * recursion (C stack); for very deep trees this could overflow, but
 * restic trees are usually shallow.  For pathological depths use
 * `ulimit -s unlimited` before invoking.
 */
#include "tree.h"
#include "path.h"
#include "json_q.h"
#include "lcsas_io.h"
#include "hex.h"
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
 * Both are excluded by default.  Operators on a platform with
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

/* Issue #192 — hardlink reconstruction.
 *
 * Restic records hardlinks via a per-node "inode" field: two file
 * nodes sharing the same nonzero inode value represent the same
 * hardlinked file.  Tier-2 (rustic restore) reconstructs them via
 * link(2); without doing the same, tier-1 emits a full content copy
 * for every name, which can multiply restored size by the link
 * factor (e.g. /usr/lib/firmware ~10x).
 *
 * The map is per-restore (not global): a flat array of
 * (inode, restored-path) pairs, populated as the recursion walks
 * the tree.  Growth pattern mirrors blob_index_push in repo.c
 * (cap = 64, double).
 *
 * Windows has no link(2); the map is unused there (Win32 hardlinks
 * would need CreateHardLinkA which isn't worth the porting cost for
 * a tier-1 binary whose Windows story is "best-effort").
 */
typedef struct {
    long long inode;
    char *path;          /* malloc'd; freed by hardlink_map_free */
} hardlink_entry;

typedef struct {
    hardlink_entry *entries;
    size_t count;
    size_t cap;
} hardlink_map;

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
hardlink_map_find(const hardlink_map *m, long long inode)
{
    size_t i;
    if (!m) return NULL;
    for (i = 0; i < m->count; i++) {
        if (m->entries[i].inode == inode) return m->entries[i].path;
    }
    return NULL;
}

static int
hardlink_map_insert(hardlink_map *m, long long inode, const char *path)
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
 * != 0 so non-root recovery still works — the restored files end up
 * owned by the recovery user, matching the documented behaviour.
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
apply_node_xattrs(const char *src, const lcsas_json_tok *toks,
                  long node_idx, const char *path)
{
    long ea_i = lcsas_json_obj_get(src, toks, node_idx,
                                   "extended_attributes");
    long count, n;
    long elem_t;

    if (ea_i < 0) return;
    if (toks[ea_i].type != LCSAS_JSON_ARRAY) return;
    count = toks[ea_i].size;
    if (count <= 0) return;

    elem_t = ea_i + 1;
    for (n = 0; n < count; n++) {
        long name_i, value_i;
        char name_buf[256];
        unsigned char *value_buf = NULL;
        long value_len = 0;
        long name_n;
        size_t enc_len = 0;
        size_t cap;

        /* Skip non-object array entries (defensive). */
        while (toks[elem_t].parent != ea_i) elem_t++;
        if (toks[elem_t].type != LCSAS_JSON_OBJECT) {
            elem_t++;
            continue;
        }

        name_i = lcsas_json_obj_get(src, toks, elem_t, "name");
        value_i = lcsas_json_obj_get(src, toks, elem_t, "value");
        if (name_i < 0 || value_i < 0) {
            elem_t = toks[elem_t].end;
            continue;
        }
        if (toks[name_i].type != LCSAS_JSON_STRING) {
            elem_t = toks[elem_t].end;
            continue;
        }

        name_n = lcsas_json_decode_string(src, &toks[name_i],
                                          name_buf, sizeof name_buf);
        if (name_n < 0 || name_n == 0) {
            elem_t = toks[elem_t].end;
            continue;
        }

        /* Value: a base64-encoded raw byte string.  lcsas_b64_decode
         * has no destination-length parameter, so we MUST size the
         * destination buffer from the encoded length BEFORE decoding
         * to prevent a stack/heap overflow (xattrs can be up to
         * 64 KiB on Linux).  Decoded size <= ceil(enc_len * 3 / 4). */
        if (toks[value_i].type == LCSAS_JSON_STRING) {
            enc_len = (size_t)(toks[value_i].end - toks[value_i].start);
            cap = (enc_len + 3) / 4 * 3 + 8;
            value_buf = (unsigned char *)malloc(cap);
            if (!value_buf) {
                /* Best-effort — skip this xattr if we can't allocate. */
                elem_t = toks[elem_t].end;
                continue;
            }
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
            value_buf = NULL;
        }

        elem_t = toks[elem_t].end;
    }
}
#else
/* Stub when xattr support compiled out — xattrs silently dropped. */
static void
apply_node_xattrs(const char *src, const lcsas_json_tok *toks,
                  long node_idx, const char *path)
{
    (void)src; (void)toks; (void)node_idx; (void)path;
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
    /* Always emit a final line so the operator sees the closing count
     * even if the last tick already fired exactly at completion. */
    emit_progress_line(p);
}

static int
restore_file_node(const char *repo_path,
                  const lcsas_master_key *mk,
                  const lcsas_blob_index *ix,
                  const char *src,
                  const lcsas_json_tok *toks,
                  long node_idx,
                  const char *target_path,
                  struct lcsas_disc_locator *locator,
                  lcsas_progress *progress,
                  hardlink_map *hlmap)
{
    long content_idx = lcsas_json_obj_get(src, toks, node_idx, "content");
    int fd;
    long t;
    long blob_count;
    long found = 0;
    int rc = 0;
    unsigned int final_posix_mode = 0644;

    /* Issue #92 — idempotent resume: skip files that are already fully
     * restored.  We compare the on-disk file size against the "size"
     * field recorded in the snapshot tree node.  If they match we
     * assume the file is intact and return early, exactly as rustic
     * does by default on the Python/rustic tier.
     *
     * lcsas_create_file() opens with O_TRUNC, so the check MUST come
     * before that call or we would clobber the file unconditionally.
     *
     * Issue #192 — on a hit, register the path in the hardlink map
     * so subsequent occurrences of the same inode can link() to it.
     * Otherwise a partial-resume restore would never recover the
     * hardlinks even though the first occurrence was already on disk. */
    {
        long size_idx = lcsas_json_obj_get(src, toks, node_idx, "size");
        if (size_idx >= 0 && toks[size_idx].type == LCSAS_JSON_NUMBER) {
            long long expected_size = 0;
            if (lcsas_json_decode_int(src, &toks[size_idx], &expected_size) == 0
                    && expected_size >= 0) {
                struct stat st;
                if (stat(target_path, &st) == 0
                        && S_ISREG(st.st_mode)
                        && (long long)st.st_size == expected_size) {
                    fprintf(stderr,
                            "[lcsas-restore] skipping already-restored: %s\n",
                            target_path);
#ifndef _WIN32
                    if (hlmap) {
                        long long ino = 0;
                        if (decode_node_inode(src, toks, node_idx, &ino) == 0
                                && hardlink_map_find(hlmap, ino) == NULL) {
                            (void)hardlink_map_insert(hlmap, ino, target_path);
                        }
                    }
#endif
                    return 0;
                }
            }
        }
    }

#ifndef _WIN32
    /* Issue #192 — hardlink reconstruction.  If this node's inode
     * matches one we've already restored in this snapshot, link()
     * to the previously-restored path instead of writing a fresh
     * content copy.  Falls through to the normal restore path if
     * the inode is missing, zero, unmapped, or the link() call
     * fails (best-effort: a failed link still gets a content copy). */
    if (hlmap) {
        long long inode = 0;
        if (decode_node_inode(src, toks, node_idx, &inode) == 0) {
            const char *prior = hardlink_map_find(hlmap, inode);
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
                    long mode_idx = lcsas_json_obj_get(src, toks, node_idx,
                                                      "mode");
                    long long mode_value = 0644;
                    if (mode_idx >= 0
                            && toks[mode_idx].type == LCSAS_JSON_NUMBER) {
                        (void)lcsas_json_decode_int(src, &toks[mode_idx],
                                                    &mode_value);
                    }
                    (void)chmod(target_path,
                                (mode_t)mode_from_go_filemode(mode_value));
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
                /* First occurrence: record path so subsequent nodes
                 * with the same inode become link()s. */
                (void)hardlink_map_insert(hlmap, inode, target_path);
            }
        }
    }
#else
    (void)hlmap;
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
    {
        long mode_idx = lcsas_json_obj_get(src, toks, node_idx, "mode");
        long long mode_value = 0644;
        if (mode_idx >= 0 && toks[mode_idx].type == LCSAS_JSON_NUMBER) {
            (void)lcsas_json_decode_int(src, &toks[mode_idx], &mode_value);
        }
        final_posix_mode = mode_from_go_filemode(mode_value);
        fd = lcsas_create_file(target_path, final_posix_mode);
    }
    if (fd < 0) {
        /* Issue #221 — classify ENOSPC / EDQUOT explicitly so the
         * operator knows the target filesystem ran out of room
         * rather than seeing a generic "file restore failed". */
        int saved_errno = errno;
        if (saved_errno == ENOSPC || saved_errno == EDQUOT) {
            fprintf(stderr,
                    "ERROR: target directory out of space "
                    "(path=%s, errno=%d)\n",
                    target_path, saved_errno);
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

    for (t = content_idx + 1; content_idx >= 0 && found < blob_count; t++) {
        if (toks[t].parent == content_idx
                && toks[t].type == LCSAS_JSON_STRING) {
            unsigned char id[32];
            const lcsas_blob_loc *loc;
            unsigned char *blob = NULL;
            size_t blob_len = 0;

            found++;

            if (toks[t].size != 64) { rc = -1; break; }
            if (lcsas_hex_decode(src + toks[t].start, 32, id) != 0) {
                rc = -1; break;
            }
            loc = lcsas_blob_index_find(ix, id);
            if (!loc) {
                fprintf(stderr, "blob not in index: %.64s\n",
                        src + toks[t].start);
                rc = -1; break;
            }
            if (lcsas_repo_read_blob(repo_path, mk, loc, locator,
                                     &blob, &blob_len) != 0) {
                rc = -1; break;
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
                     * position) tells the operator how far the
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
     * original (and with tier-2's restored output). */
    {
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
     * for security; setting them here is the last touch before
     * close so they stick.  Sticky is unaffected but harmless to
     * re-apply.  Only chmods if the file was created in this call
     * (rc still 0). */
    if (rc == 0) {
        int chmod_rc = fchmod(fd, (mode_t)final_posix_mode);
        (void)chmod_rc;
    }
#endif

#ifndef _WIN32
    /* Issue #188 — restore the snapshot's mtime onto the file we just
     * wrote.  futimens() updates timestamps via the open fd, which
     * avoids a race with anything that might rename the file under us
     * (and dodges the symlink-deref question entirely). */
    {
        struct timespec ts[2];
        if (decode_node_mtime(src, toks, node_idx, ts) == 0) {
            (void)futimens(fd, ts);
        }
    }
    /* Issues #189 + #190 — uid/gid + xattrs (best-effort).
     * Ownership applied AFTER content + mode so the file exists at
     * the path; xattrs go last so a setxattr-time hook won't observe
     * a partially-restored file. */
    apply_node_ownership(src, toks, node_idx, target_path);
    apply_node_xattrs(src, toks, node_idx, target_path);
#endif

    close(fd);
    return rc;
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
                     hardlink_map *hlmap)
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

    if (lcsas_hex_decode(tree_id_hex, 32, tree_id) != 0) return -1;
    loc = lcsas_blob_index_find(ix, tree_id);
    if (!loc) {
        fprintf(stderr, "tree blob not found: %s\n", tree_id_hex);
        return -1;
    }
    if (lcsas_repo_read_blob(repo_path, mk, loc, locator,
                             &blob, &blob_len) != 0) {
        return -1;
    }

    /* Trees can be large; allocate a generous token buffer. */
    toks = (lcsas_json_tok *)malloc(sizeof(lcsas_json_tok) * 65536);
    if (!toks) { free(blob); return -1; }

    ntoks = lcsas_json_parse((const char *)blob, blob_len, toks, 65536);
    if (ntoks <= 0 || toks[0].type != LCSAS_JSON_OBJECT) goto out;

    nodes_arr = lcsas_json_obj_get((char *)blob, toks, 0, "nodes");
    if (nodes_arr < 0 || toks[nodes_arr].type != LCSAS_JSON_ARRAY) {
        rc = 0; goto out;
    }
    node_count = toks[nodes_arr].size;

    /* mkdir target_dir if needed. */
    lcsas_mkdir_p(target_dir);

    for (t = nodes_arr + 1; found < node_count; t++) {
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
            long lt_i = lcsas_json_obj_get((char *)blob, toks, t, "linktarget");
            char name_buf[1024];
            char type_buf[32];
            char node_path[4096];

            if (name_i < 0 || type_i < 0) continue;
            if (lcsas_json_decode_string((char *)blob, &toks[name_i],
                                         name_buf, sizeof name_buf) < 0)
                continue;
            if (lcsas_json_decode_string((char *)blob, &toks[type_i],
                                         type_buf, sizeof type_buf) < 0)
                continue;

            /* Path traversal safety: name must be a plain basename. */
            if (lcsas_path_safe_name(name_buf) != 0) {
                fprintf(stderr, "skip unsafe name: %s\n", name_buf);
                continue;
            }
            {
                size_t i;
                int has_slash = 0;
                for (i = 0; name_buf[i]; i++) {
                    if (name_buf[i] == '/') { has_slash = 1; break; }
                }
                if (has_slash) {
                    fprintf(stderr, "skip name with slash: %s\n", name_buf);
                    continue;
                }
            }

            snprintf(node_path, sizeof node_path, "%s/%s", target_dir, name_buf);

            if (strcmp(type_buf, "file") == 0) {
                if (restore_file_node(repo_path, mk, ix,
                                      (char *)blob, toks, t, node_path,
                                      locator, progress, hlmap) != 0) {
                    fprintf(stderr, "file restore failed: %s\n", node_path);
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
                 * field.  Match tier-2 (rustic) parity. */
                {
                    long dmode_i = lcsas_json_obj_get((char *)blob, toks,
                                                     t, "mode");
                    long long dmode_v = 0755;
                    if (dmode_i >= 0
                            && toks[dmode_i].type == LCSAS_JSON_NUMBER) {
                        (void)lcsas_json_decode_int((char *)blob,
                                                    &toks[dmode_i], &dmode_v);
                    }
#ifndef _WIN32
                    (void)chmod(node_path,
                                (mode_t)mode_from_go_filemode(dmode_v));
#endif
                }
                if (subtree_i >= 0
                        && toks[subtree_i].type == LCSAS_JSON_STRING
                        && toks[subtree_i].size == 64) {
                    char sub_hex[65];
                    memcpy(sub_hex, (char *)blob + toks[subtree_i].start, 64);
                    sub_hex[64] = '\0';
                    if (tree_restore_recurse(repo_path, mk, ix, sub_hex,
                                             node_path, target_root,
                                             locator, progress,
                                             hlmap) != 0) {
                        goto out;
                    }
                }
#ifndef _WIN32
                /* Issue #188 — apply dir mtime AFTER recursing into the
                 * subtree.  Otherwise creating children re-bumps the
                 * parent's mtime to the restore-time clock. */
                {
                    struct timespec ts[2];
                    if (decode_node_mtime((char *)blob, toks, t, ts) == 0) {
                        (void)utimensat(AT_FDCWD, node_path, ts, 0);
                    }
                }
                /* Issues #189 + #190 — uid/gid + xattrs on the dir.
                 * Same ordering rationale as mtime: applied AFTER
                 * children are restored so we don't fight the
                 * recursive descent. */
                apply_node_ownership((char *)blob, toks, t, node_path);
                apply_node_xattrs((char *)blob, toks, t, node_path);
#endif
            } else if (strcmp(type_buf, "symlink") == 0) {
                if (lt_i >= 0) {
                    char tgt[1024];
                    if (lcsas_json_decode_string((char *)blob, &toks[lt_i],
                                                 tgt, sizeof tgt) < 0) continue;
                    if (lcsas_path_safe_symlink(target_root,
                                                target_dir, tgt) != 0) {
                        fprintf(stderr, "skip unsafe symlink %s -> %s\n",
                                node_path, tgt);
                        continue;
                    }
                    /* Atomic create: unlink first if exists. */
                    unlink(node_path);
                    if (symlink(tgt, node_path) != 0) {
                        int saved_errno = errno;
                        if (saved_errno == ENOSPC
                                || saved_errno == EDQUOT) {
                            fprintf(stderr,
                                    "ERROR: target directory out of space "
                                    "(symlink path=%s, errno=%d)\n",
                                    node_path, saved_errno);
                        } else {
                            fprintf(stderr, "symlink failed: %s -> %s\n",
                                    node_path, tgt);
                        }
                    }
#ifndef _WIN32
                    else {
                        /* Issue #188 — set the symlink's OWN mtime,
                         * not the target's (AT_SYMLINK_NOFOLLOW).
                         * Best-effort: not all filesystems / kernels
                         * support this; ignore failure. */
                        struct timespec ts[2];
                        if (decode_node_mtime((char *)blob, toks, t, ts) == 0) {
                            (void)utimensat(AT_FDCWD, node_path, ts,
                                            AT_SYMLINK_NOFOLLOW);
                        }
                        /* Issues #189 + #190 — symlink uid/gid +
                         * xattrs.  apply_node_ownership uses
                         * lchown() so the link's own ownership is
                         * set (not the target's).  Similarly
                         * lsetxattr() on a symlink path operates
                         * on the link itself when the kernel
                         * supports it. */
                        apply_node_ownership((char *)blob, toks, t, node_path);
                        apply_node_xattrs((char *)blob, toks, t, node_path);
                    }
#endif
                }
            } else {
                fprintf(stderr, "skip unsupported node type %s: %s\n",
                        type_buf, name_buf);
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

/*
 * Public entry point.  Owns the per-restore hardlink map: allocates,
 * passes it through the recursive walk, frees on return.  The map is
 * INTENTIONALLY internal to tree.c so the public API stays stable.
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
    int rc;
    hardlink_map_init(&hlmap);
    rc = tree_restore_recurse(repo_path, mk, ix, tree_id_hex,
                              target_dir, target_root,
                              locator, progress, &hlmap);
    hardlink_map_free(&hlmap);
    return rc;
}
