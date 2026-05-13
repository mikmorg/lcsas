/*
 * iso9660.h -- read-only ISO 9660 (level 2) directory/file reader.
 *
 * Spec: ECMA-119 / ISO 9660:1988.
 * Supports:
 *   - Primary Volume Descriptor at LBA 16
 *   - Path table or root directory record traversal
 *   - File extraction (extent + length)
 *   - Joliet supplementary descriptor (Unicode names) -- detected but
 *     not preferred; we read the primary descriptor for compatibility
 *
 * Does NOT support:
 *   - UDF (used for some BD-R variants); LCSAS uses pure ISO 9660
 *   - Rock Ridge extensions (long names) -- LCSAS file names fit
 *     within ISO 9660 level 2 (31-char limit) by design
 */
#ifndef LCSAS_ISO9660_H
#define LCSAS_ISO9660_H

#include <stddef.h>

#define LCSAS_ISO_SECTOR  2048

typedef struct lcsas_iso lcsas_iso;

/*
 * Open an ISO image (file path).  Returns NULL on error.
 */
lcsas_iso *lcsas_iso_open(const char *path);

void lcsas_iso_close(lcsas_iso *iso);

/*
 * Read a file from the image by absolute path (e.g. "/recovery/scripts/restore.sh").
 * Returns malloc'd buffer (caller frees) and writes length to *out_len.
 * Returns NULL on miss / error.
 */
unsigned char *lcsas_iso_read_file(lcsas_iso *iso, const char *path,
                                   size_t *out_len);

/*
 * Stream a file: returns 0 on success and writes the file's bytes via
 * `cb` in `chunk` sized pieces.  Useful for files too large to buffer.
 */
typedef int (*lcsas_iso_chunk_cb)(void *userdata,
                                  const void *buf, size_t len);
int lcsas_iso_stream_file(lcsas_iso *iso, const char *path,
                          lcsas_iso_chunk_cb cb, void *userdata,
                          size_t chunk);

/*
 * Walk a directory listing.  `dir_path` is "/" or "/path/to/dir".
 * The callback receives the entry name and a flag indicating whether
 * it is a directory.  Returns 0 on success.
 */
typedef int (*lcsas_iso_entry_cb)(void *userdata,
                                  const char *name, int is_dir);
int lcsas_iso_list_dir(lcsas_iso *iso, const char *dir_path,
                       lcsas_iso_entry_cb cb, void *userdata);

#endif
