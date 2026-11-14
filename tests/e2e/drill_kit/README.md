# Release-drill kit (v1.0)

Builds the test archive the v1.0 human drills restore from: #476 (manual
Windows restore) and #474 (a non-technical stand-in). The owner decided its
shape on 2026-11-02 (tracker #478):

- **one archive**, default label prefix `LCSAS`, with two repositories:
  - `photos`: its password is **split 2-of-3** into share cards, set in
    its own `[repos.photos]` table (#587);
  - `documents`: an **ordinary password**, with a password card;
- a fixed, **non-personal** corpus generated from a seed, so every build has
  the same files and the same `manifest.sha256`. It is sized for **two CD700
  data discs** (about 630 MiB, incompressible);
- a **meta disc built from the drill config and catalog**, labelled
  `LCSAS_META` (#588), the way the owner's real meta disc will be built.

## Build it

From a **clean worktree of the rc being drilled**, so the discs carry exactly
that rc's code. Run it under the lab's build lock: it stages about 630 MiB with
RS03 ECC and builds a full meta disc.

```sh
git worktree add --detach /scratch/lcsas-drill-src v1.0.0-rc9
cd /scratch/lcsas-drill-src
export LCSAS_RECOVERY_CACHE=/scratch/lcsas-recovery-cache   # the upstream cache
/scratch/build-lock-lcsas.sh \
    python3 tests/e2e/drill_kit/build_kit.py --workdir /scratch/lcsas-drill-kit
```

`--workdir` must be an empty directory **outside the checkout**: it holds the
passwords, the share cards and the catalog, and the repo is public (HANDOFF
rule 1). Nothing it writes is ever committed. It needs `rustic`, `xorriso` and
`dvdisaster`.

It drives the real CLI, in the order an owner would:
1. rustic `init` and `backup` for each repository;
2. `lcsas init`, `repo add`, `key split --repo photos`, `key card --repo documents`;
3. `scan`, then `stage --media CD700` (the data disc ISOs, with ECC);
4. `estate card`;
5. `meta build` + `meta verify --strict`;
6. xorriso `-V LCSAS_META` for the meta disc ISO.

## What comes out

| Path in `--workdir` | What it is |
|---|---|
| `iso/LCSAS_CD700_*.iso` | the two data discs |
| `iso/LCSAS_META.iso` | the meta disc |
| `cards/estate-card.txt` | the estate card. **Write the disc count in by hand** (the volumes are staged, not burned, so the card leaves it blank) |
| `cards/photos-shares/*-card.txt` | the share cards; the stand-in gets **2 of the 3** |
| `cards/documents-password-card.txt` | the password card; **write the password** from `secrets/documents.pw` into its box |
| `manifest.sha256` | sha256 of every corpus file, as `<repo>/<file>` |
| `drill.toml`, `catalog.db`, `mirror/`, `staging/`, `secrets/` | the drill archive itself (test data only) |

## Hand-out for the stand-in (#474)

The printed estate card, 2 of the 3 photos share cards, the documents password
card, and the discs. Nothing else. Insert the discs into the Windows VM as
**virtual CD media** (`virsh change-media`), never as copied-in ISO files: a
copied file carries Mark-of-the-Web, so SmartScreen would show prompts a real
disc never does.

## Check a restore

Copy the restored folder off the VM, then:

```sh
python3 tests/e2e/drill_kit/verify_restore.py /scratch/lcsas-drill-kit/manifest.sha256 <restored dir>
```

Each manifest entry is matched by its `<repo>/<file>` tail, so it doesn't
matter how deep the restore nested the files. Add `--repo photos` to check one
repository only.
