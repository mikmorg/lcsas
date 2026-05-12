/*
 * arena.h -- bump allocator.
 *
 * Lifetime: one arena per CLI invocation.  Freed wholesale on exit.
 * Eliminates the need to plumb individual `free()` calls and the
 * associated leak risk.
 */
#ifndef LCSAS_ARENA_H
#define LCSAS_ARENA_H

#include <stddef.h>

typedef struct lcsas_arena_chunk lcsas_arena_chunk;

typedef struct {
    lcsas_arena_chunk *head;
    size_t default_chunk;
} lcsas_arena;

void lcsas_arena_init(lcsas_arena *a, size_t default_chunk);
void *lcsas_arena_alloc(lcsas_arena *a, size_t bytes);
void *lcsas_arena_calloc(lcsas_arena *a, size_t bytes);
char *lcsas_arena_strdup(lcsas_arena *a, const char *s, size_t len);
void lcsas_arena_reset(lcsas_arena *a);

#endif
