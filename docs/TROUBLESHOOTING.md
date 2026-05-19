# LCSAS Recovery — Troubleshooting

Common errors operators hit during disaster recovery, with the
shortest known fix.  If your error isn't here, file an issue
quoting the exact command and message.

---

## `umount: /mnt: target is busy`

**Cause**: tier-1 binary or your shell has the mount open (chdir
inside, open file handle, SQLite catalog).  Used to happen
constantly before commit `c6f89a0` — the catalog SQLite handle
pinned the mount.  The catalog is now copied to the pack cache
before opening, so this is rare.

**Fix (most cases)**: `lsof /mnt` to find the holder.  If it's
your own shell:

```sh
cd / && sudo umount /mnt
```

**Last resort**: `sudo umount -l /mnt` (lazy umount).  The kernel
hides `/mnt` from new lookups and frees it once the last handle
closes.  Safe; the agent in the blind tests reaches for this when
all else fails.

---

## `no restic repo found under /tmp/lcsas-meta/recovery`

**Cause**: the script looked for `keys/` + `index/` in the legacy
location only.  Means either (a) the meta disc you have is a
pre-Phase-22 build, OR (b) you're invoking the wrong recovery
script.

**Fix**: insert any data disc into the drive and mount it at
`/mnt`.  The script's `metadata/<tenant>/` auto-discovery (added
in PR #83) finds the per-tenant repo metadata on any data disc:

```sh
sudo mount /dev/sr0 /mnt
sh restore.sh ~/restored/ latest
```

If you have NO data disc yet and only the meta disc, the meta
disc itself carries per-tenant metadata under `metadata/<tenant>/`
— the script will find it there too.

---

## `Password: [tier 3] falling back to Python (...)` (loops forever)

**Cause**: tier 3 was invoked but couldn't actually decrypt.  The
historical version of this bug was a positional-vs-flag arg
mismatch (fixed in commit `36a836d`).  If you still see this loop
on a current build, something is wrong with the bundled
`standalone_restorer.py`.

**Fix**: opt into tier-fallback explicitly so the script tries
all three tiers in order instead of failing fast on tier 1:

```sh
LCSAS_TIER_FALLBACK=1 sh restore.sh ~/restored/ latest
```

If tier 3 itself is broken, drop to manual rustic invocation:

```sh
/mnt/tools/bin/rustic-static -r /mnt/metadata/alpha \
    --password-file ~/key.txt restore latest ~/restored/
```

(Replace `alpha` and the password-file path with yours.)

---

## `ERROR: zstd frame at X reports invalid size -1`

**Cause**: the lcsas-restore C binary's zstd wrapper was hitting
`ZSTD_CONTENTSIZE_UNKNOWN` and treating it as fatal.  Fixed in
commit `5b045bd` (falls back to `ZSTD_decompressBound`).

**Fix**: you have an old `lcsas-restore`.  Rebuild for your arch
with:

```sh
lcsas recovery build --arch host
```

or use a fresher meta disc.  The fix has been in master since
2026-05-18.

---

## `INSERT DISC: <hash>` (hex hash, no friendly label)

**Cause**: no `--catalog` was supplied to tier-1, so the binary
can't translate pack-hash → disc label.  Happens when you haven't
mounted any data disc that carries a catalog.

**Fix**: mount any data disc.  Each data disc carries a
holographic copy of the catalog at `<disc-root>/catalog.db`.  The
script auto-discovers it and re-picks whichever copy is freshest.

```sh
sudo mount /dev/sr0 /mnt
# Now press Enter at the prompt; the rescan will find both the
# pack and the catalog.
```

---

## `WARNING: cache filesystem at X is <10% free; disabling further drains`

**Cause**: the pack cache (default `${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>/`)
is on a filesystem that's nearly full.  The binary auto-disables
drains rather than risk ENOSPC mid-restore.

**Fix**: point the cache somewhere with more room:

```sh
mkdir -p /home/me/big-disk/lcsas-cache
LCSAS_PACK_CACHE_DIR=/home/me/big-disk/lcsas-cache \
    sh restore.sh ~/restored/ latest
```

Or disable the cache entirely if you have enough RAM/optical
patience to swap discs many times:

```sh
LCSAS_PACK_CACHE_DIR= sh restore.sh ~/restored/ latest
```

---

## Multi-tenant: typed the wrong tenant name

**Cause**: typed `albha` instead of `alpha` at the
`Repository:` prompt.

**Fix**: the script exits 1 with the available tenant list.  Just
re-run.  The `Repository:` prompt only appears on archives with
more than one tenant.

---

## The agent / script gives up before completing

**Cause**: hit max-turns (test fixture) or exhausted patience
(real operator).

**Fix**: the restore is idempotent over rustic-format repos —
just re-run.  The pack cache survives across runs, so the second
attempt benefits from any draining done in the first attempt:

```sh
# Same exact command, the cache is already warm
sh restore.sh ~/restored/ latest
```

For the test fixture: bump `MAX_TURNS` in `tests/e2e/cdemu_blind_restore/run.sh`.

---

## "Where did my restored files go?"

**Cause**: by default the binary preserves the original absolute
path under the target dir.  If your source files were at
`/var/lib/foo/bar.bin`, after restoring to `~/restored/` you'll
find them at `~/restored/var/lib/foo/bar.bin`.

**Fix**: that's intentional.  Move them where you actually want
with `mv`:

```sh
mv ~/restored/var/lib/foo/* /var/lib/foo/
```

---

## Manual pack assembly (last-resort tier-3 fallback)

If `restore.sh` is broken in some way you can't fix and you have
to manually assemble a rustic-format repo by hand:

1. Pick a writable directory `$R` (call it `/tmp/manual-repo`).
2. Copy `keys/`, `index/`, `snapshots/` from any data disc's
   `metadata/<tenant>/` into `$R/`.
3. For every data disc, mount it and copy `data/` into `$R/data/`
   (or `cp -r` over the existing tree; both directories are
   content-addressed so identical packs merge cleanly).
4. Run rustic-static directly against `$R`:

```sh
/mnt/tools/bin/rustic-static -r /tmp/manual-repo \
    --password-file ~/key.txt restore latest ~/restored/
```

This is what `restore-auto.sh` does internally when invoked with
`--disc-cmd "<your-disc-swap-helper>"`.  The end result is
identical to a successful `restore.sh` run — just slower and more
manual.

---

## When everything fails — the bare-metal escape hatch

The on-disc data format is documented in
`docs/RESTIC_FORMAT_SPEC.md`.  A programmer with that document,
your encryption key, and the data discs can decrypt and rebuild
the original files from scratch in any language — Python, Go,
Rust, C, anything.  No LCSAS binaries required.

This is the "50-year survivability" guarantee.  The format won't
rot; the bytes on the disc are forever decryptable given the key
+ the spec.

If you've reached this point, file an issue describing what you
tried.  We want to make sure the next operator doesn't.
