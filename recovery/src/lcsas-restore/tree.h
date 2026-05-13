/*
 * tree.h -- recursive tree-blob walker.
 *
 * Reads a tree blob, iterates its nodes, and materializes files,
 * directories, and symlinks under `target_dir`.
 */
#ifndef LCSAS_TREE_H
#define LCSAS_TREE_H

#include "repo.h"

struct lcsas_disc_locator;

int lcsas_tree_restore(const char *repo_path,
                       const lcsas_master_key *mk,
                       const lcsas_blob_index *ix,
                       const char *tree_id_hex,
                       const char *target_dir,
                       const char *target_root, /* used for symlink safety */
                       struct lcsas_disc_locator *locator);

#endif
