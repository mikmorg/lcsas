/*
 * bench_index.c -- blob-index scaling benchmark (Phase 10).
 *
 * Usage: bench_index REPO PASSWORD_FILE [LOOKUPS]
 *
 * Loads the repository's keys and index, then times LOOKUPS (default
 * 1000) random lcsas_blob_index_find calls and prints one line on
 * stderr:
 *
 *   [bench] entries=N load_index_ms=N rss_after_index_kib=N
 *           find_ns_mean=N lookups=N
 *
 * recovery/scripts/scaling_bench.py drives it.  Issue #791: this was
 * the LCSAS_STRESS_LOOKUPS block in lcsas-restore's main.c, a test hook
 * compiled into every shipped binary; it is the same measurement
 * through the same public functions, built only by `make
 * build/bench_index`.  Linux only (clock_gettime, ru_maxrss in KiB).
 */
#include "repo.h"
#include "lcsas_io.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/resource.h>

static long long
ns_between(const struct timespec *a, const struct timespec *b)
{
    return (long long)(b->tv_sec - a->tv_sec) * 1000000000LL
         + (long long)(b->tv_nsec - a->tv_nsec);
}

int
main(int argc, char **argv)
{
    char keys_dir[4096];
    unsigned char *pw = NULL;
    size_t pw_len = 0;
    size_t k = 0;
    lcsas_master_key mk;
    lcsas_blob_index ix;
    struct timespec t0, t1, f0, f1;
    struct rusage ru;
    long n_lookups = 1000;
    long rss_kib = 0;
    long i;
    int rc = 1;

    if (argc < 3 || argc > 4) {
        fprintf(stderr, "usage: %s REPO PASSWORD_FILE [LOOKUPS]\n", argv[0]);
        return 2;
    }
    if (argc == 4) n_lookups = atol(argv[3]);
    if (n_lookups <= 0) n_lookups = 1000;

    if (lcsas_read_file(argv[2], &pw, &pw_len) != 0) {
        fprintf(stderr, "cannot read password file: %s\n", argv[2]);
        return 1;
    }
    while (k < pw_len && pw[k] != '\n') k++;
    if (k > 0 && pw[k - 1] == '\r') k--;
    pw_len = k;

    snprintf(keys_dir, sizeof keys_dir, "%s/keys", argv[1]);
    if (lcsas_repo_load_keys_dir(keys_dir, pw, pw_len, &mk) != 0) {
        fprintf(stderr, "could not open the repository keys\n");
        free(pw);
        return 1;
    }
    free(pw);

    lcsas_blob_index_init(&ix);
    clock_gettime(CLOCK_MONOTONIC, &t0);
    if (lcsas_repo_load_index(argv[1], &mk, &ix) != 0) {
        fprintf(stderr, "index load failed\n");
        goto out;
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    if (getrusage(RUSAGE_SELF, &ru) == 0) rss_kib = ru.ru_maxrss;

    clock_gettime(CLOCK_MONOTONIC, &f0);
    for (i = 0; i < n_lookups; i++) {
        /* Random ids: most miss, but the search cost is what counts. */
        unsigned char probe[32];
        int j;
        for (j = 0; j < 32; j++) probe[j] = (unsigned char)(rand() & 0xff);
        (void)lcsas_blob_index_find(&ix, probe);
    }
    clock_gettime(CLOCK_MONOTONIC, &f1);

    fprintf(stderr,
            "[bench] entries=%lu load_index_ms=%lld "
            "rss_after_index_kib=%ld find_ns_mean=%llu lookups=%ld\n",
            (unsigned long)ix.count, ns_between(&t0, &t1) / 1000000LL,
            rss_kib,
            (unsigned long long)ns_between(&f0, &f1)
                / (unsigned long long)n_lookups,
            n_lookups);
    rc = 0;
out:
    lcsas_blob_index_free(&ix);
    return rc;
}
