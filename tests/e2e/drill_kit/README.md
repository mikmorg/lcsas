# Release-drill kit (v1.0)

Builds the test archive the v1.0 human drills restore from: #476 (manual
Windows restore) and #474 (a non-technical stand-in). The owner decided its
shape on 2026-11-02 (tracker #478):

- **one archive**, default label prefix `LCSAS`, with two repositories:
  - `photos`: its password is **split 2-of-3** into share cards, set in
    its own `[repos.photos]` table (#587);
  - `documents`: an **ordinary password**, with a password card;
- a fixed, **non-personal** corpus generated from a seed, so every build has
  the same files and the same `manifest.json`;
- **two data discs** (CD700) and a **meta disc built from the drill config
  and catalog**, labelled `LCSAS_META` (#588), the way the owner's real meta
  disc will be built.

## What the corpus holds (#834)

Each backup set holds the things restores get wrong, not just plain files
(`corpus.py`):

- names with spaces, accents, an emoji, other scripts, and `' & % ! ( ) [ ]`;
- a deep tree (20 folders), an empty folder (with an empty folder in it),
  an empty file, a read-only folder (`0555`) with a read-only file in it,
  a file with mode `0600`, a hardlink pair, and a long path;
- one file above 100 MB in `photos` (generated, never committed).

Two **profiles**, chosen with `--profile`:

- `windows` (the default): **both** sets are restored on Windows in #476
  and #474, so both hold only names NTFS can store. Their longest path is at
  most 180 characters inside the set, so it stays under 240 in the default
  restore folder (`%USERPROFILE%\Documents\restored-<set>\<set>\`) for a
  user name of up to 20 characters. Windows tier 1 restores longer paths
  since #619, but tier 2 (rustic-static) and Explorer may not, so the
  drill set stays under MAX_PATH.
- `posix` (Linux and macOS): everything above, plus names with a double
  quote, a backslash, a tab, a colon and `* ? < > |`, a trailing dot or
  space, `case.txt` next to `CASE.txt`, `CON.txt`, and a name whose bytes
  are not UTF-8; a path over 300 characters; and symlinks: relative up and
  across (`links/a/up -> ../b/f`), to a folder, absolute, dangling, and one
  whose target is not UTF-8. macOS cannot store the non-UTF-8 name (tiers 1
  and 3 report it as not restored there), and a default (case-insensitive)
  macOS volume cannot hold `case.txt` next to `CASE.txt`: on a Mac expect
  those two to fail with no restore bug behind them.

No FIFO: the tiers make pipes since #859 (rc10), but the corpus has none
yet. `manifest.json` has a `fifo` type for when it does.

**Two snapshots, two burn sessions.** The kit backs each set up, stages a
burn session, then edits one file, deletes one, renames one, changes a mode,
adds files, backs up again and stages a second session. So the restore must
take "latest" from the newest disc (`LCSAS_CD700_<year>_0002`); a restore of
the first snapshot fails the check. There is no disc writer, so each session
is recorded as burned without one (`burn_session(skip_burn=True)`, as the
test fixtures do): a snapshot goes on a disc only when its packs are on
burned discs or on that session's own discs.

## Build it

From a **clean worktree of the rc being drilled**, so the discs carry exactly
that rc's code. Run master's `build_kit.py` with `--project-root` pointing at
that worktree if the rc's own copy of the builder is out of date (rc11's still
called `lcsas key`, fixed in #938). Run it under the lab's build lock: it stages about 580 MiB
with RS03 ECC and builds a full meta disc.

```sh
git worktree add --detach /scratch/lcsas-drill-src v1.0.0-rcN   # the rc being drilled
cd /scratch/lcsas-drill-src
export LCSAS_RECOVERY_CACHE=/scratch/lcsas-recovery-cache   # the upstream cache
/scratch/build-lock-lcsas.sh \
    python3 tests/e2e/drill_kit/build_kit.py --workdir /scratch/lcsas-drill-kit
```

Add `--profile posix` (and a different `--workdir`) for a Linux or macOS
kit. `--small` makes a quick kit with few bulk files (the big file stays) to
check the kit itself; it is not a drill kit.

`--workdir` must be an empty directory **outside the checkout**: it holds
the passwords, the share cards and the catalog, and the repo is public
(HANDOFF rule 1). Nothing it writes is ever committed. It needs `rustic`,
`xorriso` and `dvdisaster`. The corpus has a read-only folder, so remove a
workdir with `chmod -R u+w <workdir>` first.

It drives the real CLI, in the order an owner would:
1. rustic `init` and a first `backup` of each repository;
2. `lcsas init`, `repo add`, `password split --repo photos`, `password card --repo documents`;
3. `scan`, then `stage --media CD700` (the first session's data disc, with ECC),
   recorded as burned;
4. the corpus changes; a second rustic `backup`, `scan` and `stage` (the
   second session's disc), recorded as burned;
5. `manifest.json`, read from the corpus tree as the second backup saw it;
6. `estate card`;
7. `meta build` + `meta verify --strict`;
8. xorriso `-V LCSAS_META` for the meta disc ISO.

## What comes out

| Path in `--workdir` | What it is |
|---|---|
| `iso/LCSAS_CD700_*.iso` | the two data discs, one per burn session |
| `iso/LCSAS_META.iso` | the meta disc |
| `cards/estate-card.txt` | the estate card (it counts the data discs) |
| `cards/photos-shares/*-card.txt` | the share cards; the stand-in gets **2 of the 3** |
| `cards/documents-password-card.txt` | the password card; **write the password** from `secrets/documents.pw` into its box |
| `manifest.json` | every item of each set (name as bytes, type, sha256, mode, link target, hardlink group), and what the newest backup deleted |
| `drill.toml`, `catalog.db`, `corpus/`, `mirror/`, `staging/`, `secrets/` | the drill archive itself (test data only) |

## What a staged CD700 kit does not cover

The kit tests the restore of real LCSAS discs, but not everything an owner's
archive goes through:

- **No real media.** The ISOs are never burned or read back from a disc, so
  burn errors, read errors, drive behaviour and disc ageing are not tested
  (#473 and `recovery/docs/PHYSICAL_DISC_VALIDATION.txt` cover that).
- **No burn.** The sessions are recorded as burned without `lcsas burn`, so
  the burn, verify-after-burn and label steps are not exercised
  (`make verify-burn-e2e` covers them with CDEmu).
- **CD700 only.** No BD25, BD50, BDXL100 or M-DISC capacities, no multi-GB
  volumes, and no ECC sizes other than CD700's.
- **No damage.** The discs are intact; RS03 repair is proved by
  `tests/integration/test_ecc_repair.py`.
- **Small.** About 580 MiB across two discs, not hundreds of discs.

## Hand-out for the stand-in (#474)

The printed estate card, 2 of the 3 photos share cards, the documents password
card, and the discs. Nothing else. Insert the discs into the Windows VM as
**virtual CD media** (`virsh change-media`), never as copied-in ISO files: a
copied file carries Mark-of-the-Web, so SmartScreen would show prompts a real
disc never does.

## Check a restore

```sh
python3 tests/e2e/drill_kit/verify_restore.py /scratch/lcsas-drill-kit/manifest.json <restored dir>
```

Each set is found as the folder named after it (`photos`, `documents`)
that holds the most of its items, so it doesn't matter how deep the restore
nested it. Inside it every path is compared as bytes, and each item is
checked for: being there, its type, its content, its mode, its link target,
hardlinked names being one file, and nothing the newest backup deleted being
back (`OLD`: the restore used an older snapshot). Add `--repo photos` to check
one repository only.

A folder **copied off Windows** keeps no modes, hardlinks or symlinks: check
it with `--no-metadata`, which skips those and says so. Copy it with a tool
that keeps empty folders and Unicode names.

Items in the folder that are not in the manifest are listed as `EXTRA` but
do not fail the check. A wrong or empty folder, a `--repo` the manifest does
not have, or a manifest from a kit built before #834 (`manifest.sha256`) is
an error (exit 2): nothing was compared, so it is never a pass.

Expected failures until their fixes land:

- **Windows tier 1 (#619):** every name that is not ASCII comes back garbled,
  so it shows as `MISSING`, with the garbled name as `EXTRA`.
- **Tiers 1 and 3, symlinks (#782, #852; fixed by #859):** a link target that
  is not UTF-8 comes back with U+FFFD (`LINK`, both tiers), and tier 3 skips
  the up-and-across, to-a-folder and absolute links (`MISSING`).
