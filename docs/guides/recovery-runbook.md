# LCSAS — Linux End-to-End Restore Runbook

> **New here?** Start at [RECOVERY_GUIDE.md](../RECOVERY_GUIDE.md) — the
> short entry-point that routes you to the right walkthrough for your OS
> and situation. This runbook is the **detailed Linux + META disc**
> walkthrough it points to. For macOS, Windows, live-USB, or disc-only
> (no META disc) restores, follow the per-OS workflow linked from that
> guide instead.

> **Scenario**: a fire, flood, or ransomware event has destroyed
> the original storage.  You have a box of optical discs labelled
> `LCSAS_META` + `LCSAS_TEST_TINY_2026_XXXX` (or `LCSAS_BD25_…` /
> `LCSAS_CD_…` for production media), a working Linux laptop with
> an optical drive, and the encryption password for at least one
> repository.  This document walks you through restoring, command
> by command.

If anything below behaves differently from what's described, jump
to [troubleshooting.md](../TROUBLESHOOTING.md).

---

## Prerequisites (5 minutes)

You need:

1. **A laptop or desktop** with:
   - Linux x86_64, aarch64, or armv7 (Intel/AMD or ARM); macOS
     Intel or Apple Silicon; or Windows x86_64 with WSL or MinGW.
     (The meta disc ships a per-arch `lcsas-restore` tier-1
     binary for each of these.)
   - An optical drive — internal or USB.  Any reader that can
     mount the disc filesystem is fine; you don't need a writer.
   - Enough free space for the restored data PLUS roughly the
     same again for the opportunistic pack cache.  Pessimistic
     rule of thumb: budget 2.5× the source size.
2. **The discs.**  At minimum the `LCSAS_META` disc plus one
   data disc.  In practice you'll need every data disc; the
   restore script will tell you which ones.  For the first data
   disc, pick the one with the newest label: the latest year in
   the label, then the highest number.  Its disc catalog is the
   fullest, so you swap discs less often.  Any data disc still
   works.
3. **The encryption password** for the repository you're restoring.
   The meta disc itself does NOT contain the password (intentionally
   — that would defeat the encryption).  You're expected to have
   it stored separately: paper, password manager, etc.

---

## Step 1 — Insert the LCSAS_META disc

```sh
# Put LCSAS_META in the drive.  Once it spins up:
sudo mount /dev/sr0 /mnt
ls /mnt
```

You should see, among other things, `restore.sh` and
`README_RESTORE.md`.  If the disc is labelled `LCSAS_TEST_TINY_…`
or similar, eject it and find the one labelled `LCSAS_META` first —
that's the recovery toolchain.  If you have more than one
`LCSAS_META` disc, use the newest; if its tools fail, use the one
burned closest in date to your data discs.

> The meta disc is the smallest one in your stack (~50–200 MB).
> Don't confuse it with the data discs (each one ~25 GB on BD25
> production media).

---

## Step 2 — Run the restore script

```sh
sh /mnt/restore.sh ~/restored/ latest
```

That's it.  Two positional arguments:

| Arg | Meaning | Default |
|---|---|---|
| 1 | `TARGET_DIR` — where restored files land | `/tmp/restored` |
| 2 | `SNAPSHOT_ID` or the word `latest` | `latest` |

The script will:

- Copy itself, this computer's recovery programs and the backup sets'
  metadata into its tools folder, `~/lcsas-restore-tools`, and run
  from there, so the meta disc can come out of the drive.  It prints
  how much it copies first; on a slow drive this takes a few minutes.
- Auto-discover repository metadata (per-repository `keys/` +
  `index/` pairs) on the meta disc and on any currently-mounted data
  disc.

---

## Step 3 — Answer the prompts

You'll see these, in this order:

### `Take the META disc out and put in your NEWEST data disc`

When no data disc is in yet, the script asks for your newest data
disc: the latest year in its label, then the highest number (for
example `LCSAS_TEST_TINY_2026_0003` is newer than
`LCSAS_TEST_TINY_2026_0002`).  Its list of snapshots is the newest, so
`latest` really is the latest.

1. Eject the meta disc (`sudo eject /dev/sr0`, or the drive's button).
2. Insert the newest data disc.  If it does not open by itself, mount
   it: `sudo mount /dev/sr0 /mnt`.
3. Press Enter.  The script prints the full label it found.

`S` then Enter skips this and uses the copies on the meta disc (they
may lack the latest backups).  `Q` then Enter stops (exit code 65).

### `Choose a repository (number or name):` (only on archives with several repositories)

If your archive has more than one repository (e.g. `alpha` and `bravo`),
the script lists them by name and asks you to choose:

```
Multiple repositories on this archive:
  1) alpha
  2) bravo (password split: any 2 of 3 share cards)
Choose a repository (number or name):
```

Type the number (`1`), the name (`alpha`) or the id (its folder name
under `metadata/`) and press Enter.

To pre-select non-interactively, set `LCSAS_REPO=alpha` in your
environment before launching the script.

### `Password:`

Type the encryption password for the chosen repository.  What you
type is not shown.  The script checks it before the restore starts:
an empty or wrong password is asked for again, and `Q` then Enter
stops (exit code 77).  If you'd rather not type it, put it in a file
and set `LCSAS_PWFILE=/path/to/file` (or pass `--key FILE`).

When the repository's password is split into share cards, the script
asks for the cards instead: type the words of each card on one line.
`P` then Enter there lets you type the password itself.

### The disc box

The recovery binary prints a box whenever it needs a pack that
isn't on the disc currently in your drive:

```
+----------------------------------------------------------+
| Pack 4f3e2a1b9c8d0e7f... is required for the next file.  |
| It is on any disc labelled:                              |
|   LCSAS_TEST_TINY_2026_0003                              |
| ...                                                      |
| Insert the right disc and press ENTER to retry.          |
| Type 'q' then ENTER to abort.                            |
+----------------------------------------------------------+
```

At that prompt:

1. **Eject the current disc** from your drive (button, eject
   key, or `eject /dev/sr0` from another terminal).
2. **Insert the disc named in the prompt.**
3. **Press Enter** at the prompt (on Linux a disc that mounts by
   itself is found without Enter).  The binary will rescan
   `/mnt` and any other mount points in `LCSAS_MOUNT_DIRS`,
   find the pack, copy that whole disc into the cache,
   and continue.

If you accidentally insert the wrong disc, the binary prints
`(still not found -- check the disc label and try again)` and
waits.  Eject and try again.

Type `q` at any disc prompt to stop the restore cleanly (exit code
75); see [Aborting / re-running](#aborting--re-running).

---

## Step 4 — Wait for `restore complete`

The binary prints periodic progress on stderr:

```
[lcsas-restore] progress: 12/30 blobs, 0 MB
[lcsas-restore] progress: 24/30 blobs, 1 MB
[lcsas-restore] restore complete
```

When you see `[lcsas-restore] restore complete`, your files are at the
path you gave as `TARGET_DIR` (e.g. `~/restored/`).  Check with
`ls -laR ~/restored/`.

If the archive holds more than one repository, the script then asks
`Restore another backup set from these discs? [y/N]` (see
[Restoring several repositories](#restoring-several-repositories)).
`N` or Enter ends the run and deletes the tools folder.

---

## Step 5 — Check the restore

Two cheap sanity checks anyone can do without rustic / lcsas
expertise:

```sh
# 1. File count looks reasonable
find ~/restored -type f | wc -l

# 2. Spot-check that a file you expect is there and non-empty
ls -la ~/restored/path/to/known/file
```

If the owner kept a `manifest.sha256` of the original data
(highly recommended; see "Production-readiness checklist" in the
README), compare:

```sh
(cd ~/restored && sha256sum -c /path/to/manifest.sha256)
```

---

## Restoring several repositories

After a restore that succeeds, the script asks:

```
Restore another backup set from these discs? [y/N]
```

Type `y`.  It asks for a folder (Enter takes a new folder beside
the first, e.g. `~/restored-2`: two repositories restored into one
folder would be mixed together), then shows the list of repositories
again and asks for the next one's password.  The meta disc is not
needed again, and the discs already copied into the pack cache are
not asked for again.  `N` or Enter ends the run; only then is the
tools folder, with its pack cache, deleted.

To restore the repositories in separate runs instead, run the script
once per repository.  Each run's pack cache is deleted when it
succeeds, so the second run asks for the discs again unless both
runs share a cache folder you name, which is never removed for you:

```sh
mkdir -p ~/shared-pack-cache
LCSAS_PACK_CACHE_DIR=~/shared-pack-cache \
    sh /mnt/restore.sh ~/restored-alpha/ latest
LCSAS_PACK_CACHE_DIR=~/shared-pack-cache \
    sh /mnt/restore.sh ~/restored-bravo/ latest
```

The second run finds most packs already cached from the first run.

---

## One optical drive (most common case)

If you have only one optical drive, you'll be doing the eject /
insert / press-Enter dance for each unique disc.  The pack cache
defaults ON (`LCSAS_PACK_CACHE_DIR=auto`) so you only swap to each
data disc **once** — the first time the binary sees a data disc it
copies the whole disc into `~/lcsas-restore-tools/pack-cache/` (RAM
on a live USB stick), and later packs from the same disc resolve
from the cache.  The cache needs about as much free space as the
discs it copies.

If your home folder is constrained, see the cache-size warnings in
[troubleshooting.md](../TROUBLESHOOTING.md#warning-cache-filesystem-at-x-is-10-free-disabling-further-drains).

---

## Several optical drives (faster)

If you have multiple optical drives or are restoring from pre-ripped
ISO files on disk:

```sh
# Loop-mount every ISO under /mnt, one folder each, and run restore
# from the META image: it auto-discovers all of them
n=0
for iso in /path/to/iso-collection/*.iso; do
    n=$((n + 1))
    sudo mkdir -p "/mnt/iso$n"
    sudo mount -o ro,loop "$iso" "/mnt/iso$n"
done
ls /mnt/iso*/restore.sh              # the META image is the one listed
sh /mnt/isoN/restore.sh ~/restored/ latest   # N from the line above
```

The script's auto-discovery walks `LCSAS_MOUNT_DIRS` (default
`/Volumes:/media/<user>:/media:/mnt:/run/media/<user>:/run/media`)
and each folder's direct children, and adds every mount it finds with
a `data/` subdirectory as a pack source.  A mount anywhere else (say a
`mktemp -d` folder under `/tmp`) is not found: mount under one of those
folders, or set `LCSAS_MOUNT_DIRS` to the folder that holds the
mounts.  With a data disc already mounted it does not ask for the
newest one: it uses the newest it finds.  No swap prompts needed.

---

## Aborting / re-running

Type `q` at any disc prompt to stop (exit code 75), or press Ctrl-C.
Your `TARGET_DIR` is left as-is (no cleanup), and the tools folder
stays, with the discs already copied into its pack cache.  The
script prints the command that carries on:

```sh
sh ~/lcsas-restore-tools/resume.sh
```

It needs no meta disc, asks only for the password (or the share
cards) again (it never stores the password), skips the files that are
already complete and does not ask again for the discs it already
copied.  Re-running the first command from the meta disc works too.
To give up, delete `~/lcsas-restore-tools` yourself.

---

## When `restore.sh` won't work — fallbacks

If `restore.sh` produces an error you can't resolve via
[troubleshooting.md](../TROUBLESHOOTING.md), the meta disc also
ships fallback paths:

- **`recovery/scripts/restore_auto.sh`** — non-interactive POSIX-sh
  version intended for CI pipelines.  Takes two positional arguments
  (`restore_auto.sh RECOVERY_ROOT TARGET_ROOT`) and reads the password
  from the `LCSAS_PWFILE` environment variable (which must be set); it
  restores the latest snapshot of every repository it discovers.  No
  interactive disc-swap prompts — point it at a tree that already holds
  every pack.  Not to be confused with the Bash
  `recovery/scripts/restore-auto.sh`, which has its own flags (Appendix A
  in `README_RESTORE.md`).
- **`recovery/scripts/restore_legacy.sh`** — the older Bash driver.
  Different flag UX:
  `bash recovery/scripts/restore_legacy.sh --key <password-file> --target <dir>
  [--isos <dir>] [--repo <name>] [--snapshot <id>]`.  Useful when you
  already have every ISO extracted to disk (pass `--isos`).
- **Direct invocation of the upstream rustic binary** at
  `tools/bin/rustic-static`.  Requires you to manually assemble
  the pack tree (see [troubleshooting.md](../TROUBLESHOOTING.md#manual-pack-assembly-last-resort-tier-3-fallback)).
- **Tier 3, the Python reader (`standalone_restorer.py`)** (slowest,
  ~1 MB/s).  Requires only Python 3.10+ + standard library.  Run with
  `--repo /path/to/assembled/repo --password-file <file>
  --target <dir>`.

All four are documented in `README_RESTORE.md` on the meta disc.

---

## When even tier 3 won't work — the manual escape hatch

If every recovery tier on the meta disc fails, the disc layout is
documented in `docs/RESTIC_FORMAT_SPEC.md` (also bundled on the
meta disc).  A programmer with that document, the password, and
the data discs can decrypt and reassemble the original files
from scratch in any language.

This is the "50-year survivability" guarantee: the format is
fully specified and the bytes on the disc are forever decryptable
given the password.
