/*
 * lcsas-iso9660 -- standalone ISO9660 reader.
 *
 * Usage:
 *   lcsas-iso9660 cat IMAGE PATH         # write file to stdout
 *   lcsas-iso9660 ls  IMAGE PATH         # list a directory
 *   lcsas-iso9660 extract IMAGE PATH DST # copy file to DST
 *
 * Provides a kernel-free ISO9660 reader for environments where
 * `mount -o loop` is not available.
 */
#include "iso9660.h"
#include "../lcsas-restore/posix_compat.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

static int
write_chunk(void *ud, const void *buf, size_t len)
{
    int fd = *(int *)ud;
    const unsigned char *p = (const unsigned char *)buf;
    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int
ls_cb(void *ud, const char *name, int is_dir)
{
    (void)ud;
    printf("%c %s\n", is_dir ? 'd' : '-', name);
    return 0;
}

int
main(int argc, char **argv)
{
    lcsas_iso *iso;
    int rc = 1;

    if (argc < 4) {
        fprintf(stderr,
            "usage:\n"
            "  %s cat     IMAGE PATH\n"
            "  %s ls      IMAGE PATH\n"
            "  %s extract IMAGE PATH DST\n",
            argv[0], argv[0], argv[0]);
        return 2;
    }

    iso = lcsas_iso_open(argv[2]);
    if (!iso) {
        fprintf(stderr, "cannot open ISO %s\n", argv[2]);
        return 1;
    }

    if (strcmp(argv[1], "ls") == 0) {
        if (lcsas_iso_list_dir(iso, argv[3], ls_cb, NULL) != 0) {
            fprintf(stderr, "list failed: %s\n", argv[3]);
            goto out;
        }
        rc = 0;
    } else if (strcmp(argv[1], "cat") == 0) {
        int fd = 1;
        if (lcsas_iso_stream_file(iso, argv[3], write_chunk, &fd, 65536) != 0) {
            fprintf(stderr, "stream failed: %s\n", argv[3]);
            goto out;
        }
        rc = 0;
    } else if (strcmp(argv[1], "extract") == 0) {
        int fd;
        if (argc < 5) {
            fprintf(stderr, "extract: missing DST\n");
            goto out;
        }
        fd = open(argv[4], O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd < 0) {
            fprintf(stderr, "cannot open dst %s\n", argv[4]);
            goto out;
        }
        if (lcsas_iso_stream_file(iso, argv[3], write_chunk, &fd, 65536) != 0) {
            fprintf(stderr, "extract failed: %s\n", argv[3]);
            close(fd);
            goto out;
        }
        close(fd);
        rc = 0;
    } else {
        fprintf(stderr, "unknown subcommand: %s\n", argv[1]);
        rc = 2;
    }

out:
    lcsas_iso_close(iso);
    return rc;
}
