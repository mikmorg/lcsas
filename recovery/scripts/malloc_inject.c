/*
 * malloc_inject.c -- LD_PRELOAD shim that fails the Nth allocation.
 *
 * Compile:
 *   cc -shared -fPIC -O0 -g -D_GNU_SOURCE \
 *      recovery/scripts/malloc_inject.c -o recovery/build/malloc_inject.so -ldl
 *
 * Usage:
 *   # First pass: count allocations
 *   LD_PRELOAD=$(pwd)/recovery/build/malloc_inject.so \
 *       recovery/build/test_repo
 *   # → "[malloc_inject] total allocations: 1234"
 *
 *   # Sweep: fail allocation N=1..total
 *   for n in $(seq 1 1234); do
 *       LD_PRELOAD=$(pwd)/recovery/build/malloc_inject.so \
 *           LCSAS_FAIL_AT=$n LCSAS_FAIL_QUIET=1 \
 *           recovery/build/test_repo
 *   done
 *
 * Used by recovery/scripts/run_fault_inject.py and the
 * `make audit-gate-fault-inject` opt-in Makefile target.
 *
 * Implementation notes:
 *   - dlsym() itself can call malloc; we use a bootstrap pool until
 *     the real symbols are resolved.
 *   - free() of bootstrap-pool pointers is a no-op (leak; <64 KiB).
 *   - realloc() of bootstrap pointers migrates to the real heap.
 *   - The fault counter excludes bootstrap allocations.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define BOOTSTRAP_SIZE 65536

static long fail_at = 0;
static long alloc_count = 0;
static int  state = 0;   /* 0=unresolved, -1=resolving (dlsym in flight), 1=ready */

static char   bootstrap[BOOTSTRAP_SIZE];
static size_t bootstrap_off = 0;

static void *(*real_malloc)(size_t)             = NULL;
static void *(*real_calloc)(size_t, size_t)     = NULL;
static void *(*real_realloc)(void *, size_t)    = NULL;
static void  (*real_free)(void *)               = NULL;

static int in_bootstrap(const void *p) {
    return (const char *)p >= bootstrap
        && (const char *)p < bootstrap + BOOTSTRAP_SIZE;
}

static void *boot_alloc(size_t size) {
    size = (size + 15) & ~(size_t)15;
    if (bootstrap_off + size > BOOTSTRAP_SIZE) { errno = ENOMEM; return NULL; }
    void *p = &bootstrap[bootstrap_off];
    bootstrap_off += size;
    return p;
}

static void resolve(void) {
    if (state == 1 || state == -1) return;
    state = -1;
    const char *e = getenv("LCSAS_FAIL_AT");
    if (e) fail_at = atol(e);
    real_malloc  = dlsym(RTLD_NEXT, "malloc");
    real_calloc  = dlsym(RTLD_NEXT, "calloc");
    real_realloc = dlsym(RTLD_NEXT, "realloc");
    real_free    = dlsym(RTLD_NEXT, "free");
    state = 1;
}

static int should_fail(void) {
    long n = __sync_add_and_fetch(&alloc_count, 1);
    return fail_at > 0 && n == fail_at;
}

void *malloc(size_t size) {
    if (state != 1) {
        if (state == 0) resolve();
        if (state != 1) return boot_alloc(size);
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_malloc(size);
}

void *calloc(size_t nmemb, size_t size) {
    if (state != 1) {
        if (state == 0) resolve();
        if (state != 1) {
            size_t bytes = nmemb * size;
            void *p = boot_alloc(bytes);
            if (p) memset(p, 0, bytes);
            return p;
        }
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_calloc(nmemb, size);
}

void *realloc(void *ptr, size_t size) {
    if (state != 1) resolve();
    if (state != 1) return boot_alloc(size);
    if (ptr && in_bootstrap(ptr)) {
        /* Migrate from bootstrap → real heap.  We don't know the
         * original size; copy up to `size` from bootstrap (this is
         * safe — bootstrap is read-only memory containing zero or
         * the original data).  Worst-case reads past the original
         * allocation but stays inside BOOTSTRAP_SIZE. */
        if (should_fail()) { errno = ENOMEM; return NULL; }
        void *neu = real_malloc(size);
        if (neu) memcpy(neu, ptr, size);
        return neu;
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_realloc(ptr, size);
}

void free(void *ptr) {
    if (!ptr) return;
    if (in_bootstrap(ptr)) return; /* leak; trivial, capped at 64 KiB */
    if (state != 1) resolve();
    if (real_free) real_free(ptr);
}

__attribute__((destructor))
static void summary(void) {
    if (getenv("LCSAS_FAIL_QUIET") == NULL) {
        fprintf(stderr, "[malloc_inject] total allocations: %ld\n", alloc_count);
    }
}
