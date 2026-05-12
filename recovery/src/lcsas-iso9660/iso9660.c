/*
 * iso9660.c -- read-only ISO 9660 reader.
 *
 * Strict C89.  Implements ECMA-119:
 *   - Primary Volume Descriptor at LBA 16 (sector size 2048).
 *   - Root directory record inside PVD at byte offset 156.
 *   - Directory traversal: each directory is a contiguous extent of
 *     ISO directory records; LSB first.
 *
 * Numerical fields in ISO 9660 are stored as "both-endian" pairs
 * (LSB-MSB), see ECMA-119 §7.3.3.  We always read the LSB half.
 *
 * File names are stored as ASCII; LCSAS uses level-2 short names plus
 * a ";1" version suffix that we strip on lookup.
 */
#include "iso9660.h"

#include <ctype.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct lcsas_iso {
    int fd;
    unsigned long root_extent;
    unsigned long root_size;
};

static unsigned long
ld32_le(const unsigned char *p)
{
    return ((unsigned long)p[0])
         | ((unsigned long)p[1] <<  8)
         | ((unsigned long)p[2] << 16)
         | ((unsigned long)p[3] << 24);
}

static int
read_sector(int fd, unsigned long lba, unsigned char *out)
{
    off_t off = (off_t)lba * LCSAS_ISO_SECTOR;
    ssize_t n;
    size_t total = 0;
    if (lseek(fd, off, SEEK_SET) == (off_t)-1) return -1;
    while (total < LCSAS_ISO_SECTOR) {
        n = read(fd, out + total, LCSAS_ISO_SECTOR - total);
        if (n < 0) return -1;
        if (n == 0) return -1;
        total += (size_t)n;
    }
    return 0;
}

lcsas_iso *
lcsas_iso_open(const char *path)
{
    int fd;
    unsigned char sector[LCSAS_ISO_SECTOR];
    lcsas_iso *iso;

    fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;

    /* PVD at LBA 16. */
    if (read_sector(fd, 16, sector) < 0) { close(fd); return NULL; }
    /* Type byte must be 1 and signature "CD001". */
    if (sector[0] != 0x01 || memcmp(sector + 1, "CD001", 5) != 0) {
        close(fd); return NULL;
    }

    iso = (lcsas_iso *)malloc(sizeof(*iso));
    if (!iso) { close(fd); return NULL; }
    iso->fd = fd;

    /* Root directory record starts at byte offset 156 within PVD.
     * Within the record: byte 2 (LBA, both-endian, LSB at offset 2),
     * byte 10 (data length, both-endian, LSB at offset 10). */
    iso->root_extent = ld32_le(sector + 156 + 2);
    iso->root_size   = ld32_le(sector + 156 + 10);
    return iso;
}

void
lcsas_iso_close(lcsas_iso *iso)
{
    if (!iso) return;
    if (iso->fd >= 0) close(iso->fd);
    free(iso);
}

/* Strip trailing ";<digits>" from an ISO file identifier. */
static void
strip_version(char *s)
{
    size_t i = strlen(s);
    while (i > 0 && s[i - 1] >= '0' && s[i - 1] <= '9') i--;
    if (i > 0 && s[i - 1] == ';') s[i - 1] = '\0';
    /* Some ISOs leave a trailing '.' for files without extension. */
    if (i > 1 && s[strlen(s) - 1] == '.') s[strlen(s) - 1] = '\0';
}

/* Compare an ISO directory identifier to a target name, case-insensitive
 * (ISO 9660 uppercases by default).  `iso_name` is not NUL-terminated. */
static int
iso_name_matches(const unsigned char *iso_name, size_t iso_len,
                 const char *target)
{
    char buf[256];
    size_t i;
    if (iso_len >= sizeof buf) return 0;
    for (i = 0; i < iso_len; i++) buf[i] = (char)iso_name[i];
    buf[iso_len] = '\0';
    strip_version(buf);
    /* Case-insensitive compare. */
    {
        size_t k;
        size_t bl = strlen(buf);
        size_t tl = strlen(target);
        if (bl != tl) return 0;
        for (k = 0; k < bl; k++) {
            int a = (unsigned char)buf[k];
            int b = (unsigned char)target[k];
            if (a >= 'a' && a <= 'z') a -= 32;
            if (b >= 'a' && b <= 'z') b -= 32;
            if (a != b) return 0;
        }
        return 1;
    }
}

/*
 * Find an entry within a directory at (extent, size).  On match sets
 * *out_extent and *out_size; returns 1 if directory, 2 if file, 0 if
 * not found.
 */
static int
find_entry(lcsas_iso *iso,
           unsigned long dir_extent, unsigned long dir_size,
           const char *name,
           unsigned long *out_extent, unsigned long *out_size)
{
    unsigned char *buf;
    unsigned long sectors = (dir_size + LCSAS_ISO_SECTOR - 1) / LCSAS_ISO_SECTOR;
    unsigned long s;
    size_t off;

    buf = (unsigned char *)malloc(sectors * LCSAS_ISO_SECTOR);
    if (!buf) return -1;
    for (s = 0; s < sectors; s++) {
        if (read_sector(iso->fd, dir_extent + s,
                        buf + s * LCSAS_ISO_SECTOR) < 0) {
            free(buf); return -1;
        }
    }

    off = 0;
    while (off + 33 < (size_t)dir_size) {
        unsigned char rec_len = buf[off];
        if (rec_len == 0) {
            /* Pad to next sector boundary. */
            size_t next = ((off / LCSAS_ISO_SECTOR) + 1) * LCSAS_ISO_SECTOR;
            if (next <= off) break;
            off = next;
            continue;
        }
        if (off + rec_len > (size_t)dir_size) break;
        {
            unsigned long ent_extent = ld32_le(buf + off + 2);
            unsigned long ent_size   = ld32_le(buf + off + 10);
            unsigned char flags      = buf[off + 25];
            unsigned char nlen       = buf[off + 32];
            const unsigned char *nm  = buf + off + 33;
            int is_dir = (flags & 0x02) ? 1 : 0;

            /* Skip self (0x00) and parent (0x01) entries. */
            if (!(nlen == 1 && (nm[0] == 0x00 || nm[0] == 0x01))) {
                if (iso_name_matches(nm, nlen, name)) {
                    *out_extent = ent_extent;
                    *out_size   = ent_size;
                    free(buf);
                    return is_dir ? 1 : 2;
                }
            }
        }
        off += rec_len;
    }
    free(buf);
    return 0;
}

/*
 * Walk a slash-separated absolute path.  Returns the (extent, size)
 * of the final component, and `is_dir`.  Returns 0 on success, -1 on
 * miss.
 */
static int
resolve_path(lcsas_iso *iso, const char *path,
             unsigned long *out_extent, unsigned long *out_size,
             int *is_dir_out)
{
    unsigned long extent = iso->root_extent;
    unsigned long size   = iso->root_size;
    int is_dir = 1;
    size_t i = 0;
    char seg[256];

    if (path[0] == '/') i = 1;

    while (path[i]) {
        size_t k = 0;
        while (path[i] && path[i] != '/' && k < sizeof(seg) - 1) {
            seg[k++] = path[i++];
        }
        seg[k] = '\0';
        if (k == 0) {
            if (path[i] == '/') { i++; continue; }
            break;
        }
        if (!is_dir) return -1;  /* descended into non-dir */

        {
            int rc = find_entry(iso, extent, size, seg, &extent, &size);
            if (rc == 0) return -1;
            if (rc < 0) return -1;
            is_dir = (rc == 1);
        }
        if (path[i] == '/') i++;
    }

    *out_extent = extent;
    *out_size = size;
    *is_dir_out = is_dir;
    return 0;
}

unsigned char *
lcsas_iso_read_file(lcsas_iso *iso, const char *path, size_t *out_len)
{
    unsigned long extent;
    unsigned long size;
    int is_dir;
    unsigned long sectors;
    unsigned long s;
    unsigned char *buf;

    if (resolve_path(iso, path, &extent, &size, &is_dir) != 0) return NULL;
    if (is_dir) return NULL;

    sectors = (size + LCSAS_ISO_SECTOR - 1) / LCSAS_ISO_SECTOR;
    buf = (unsigned char *)malloc(sectors * LCSAS_ISO_SECTOR);
    if (!buf) return NULL;
    for (s = 0; s < sectors; s++) {
        if (read_sector(iso->fd, extent + s,
                        buf + s * LCSAS_ISO_SECTOR) < 0) {
            free(buf); return NULL;
        }
    }
    *out_len = size;
    return buf;
}

int
lcsas_iso_stream_file(lcsas_iso *iso, const char *path,
                      lcsas_iso_chunk_cb cb, void *userdata,
                      size_t chunk)
{
    unsigned long extent;
    unsigned long size;
    int is_dir;
    unsigned char *sbuf;
    unsigned long off = 0;

    if (chunk == 0 || chunk % LCSAS_ISO_SECTOR != 0) return -1;
    if (resolve_path(iso, path, &extent, &size, &is_dir) != 0) return -1;
    if (is_dir) return -1;

    sbuf = (unsigned char *)malloc(chunk);
    if (!sbuf) return -1;

    while (off < size) {
        unsigned long want = size - off;
        unsigned long sectors;
        unsigned long i;
        if (want > chunk) want = chunk;
        sectors = (want + LCSAS_ISO_SECTOR - 1) / LCSAS_ISO_SECTOR;
        for (i = 0; i < sectors; i++) {
            if (read_sector(iso->fd, extent + off / LCSAS_ISO_SECTOR + i,
                            sbuf + i * LCSAS_ISO_SECTOR) < 0) {
                free(sbuf); return -1;
            }
        }
        if (cb(userdata, sbuf, (size_t)want) != 0) {
            free(sbuf); return -1;
        }
        off += want;
    }
    free(sbuf);
    return 0;
}

int
lcsas_iso_list_dir(lcsas_iso *iso, const char *dir_path,
                   lcsas_iso_entry_cb cb, void *userdata)
{
    unsigned long extent;
    unsigned long size;
    int is_dir;
    unsigned char *buf;
    unsigned long sectors;
    unsigned long s;
    size_t off = 0;

    if (resolve_path(iso, dir_path, &extent, &size, &is_dir) != 0) return -1;
    if (!is_dir) return -1;

    sectors = (size + LCSAS_ISO_SECTOR - 1) / LCSAS_ISO_SECTOR;
    buf = (unsigned char *)malloc(sectors * LCSAS_ISO_SECTOR);
    if (!buf) return -1;
    for (s = 0; s < sectors; s++) {
        if (read_sector(iso->fd, extent + s,
                        buf + s * LCSAS_ISO_SECTOR) < 0) {
            free(buf); return -1;
        }
    }

    while (off + 33 < (size_t)size) {
        unsigned char rec_len = buf[off];
        if (rec_len == 0) {
            size_t next = ((off / LCSAS_ISO_SECTOR) + 1) * LCSAS_ISO_SECTOR;
            if (next <= off) break;
            off = next;
            continue;
        }
        if (off + rec_len > (size_t)size) break;
        {
            unsigned char flags = buf[off + 25];
            unsigned char nlen  = buf[off + 32];
            const unsigned char *nm = buf + off + 33;
            char name[256];
            int e_is_dir = (flags & 0x02) ? 1 : 0;
            if (!(nlen == 1 && (nm[0] == 0x00 || nm[0] == 0x01))) {
                size_t i;
                size_t copy = (nlen < sizeof name) ? nlen : sizeof name - 1;
                for (i = 0; i < copy; i++) name[i] = (char)nm[i];
                name[copy] = '\0';
                strip_version(name);
                if (cb(userdata, name, e_is_dir) != 0) {
                    free(buf); return -1;
                }
            }
        }
        off += rec_len;
    }
    free(buf);
    return 0;
}
