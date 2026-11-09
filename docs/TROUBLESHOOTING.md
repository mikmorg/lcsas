# LCSAS Restore — Troubleshooting

Common errors people hit during a restore after a disaster, with the
shortest known fix.  If your error isn't here, file an issue
quoting the exact command and message.

---

## `umount: /mnt: target is busy`

**Cause**: tier-1 binary or your shell has the mount open (chdir
inside, open file handle, SQLite catalog).  Used to happen
constantly before commit `c6f89a0` — the catalog SQLite handle
pinned the mount.  Since rc6 the script gives tier 1 a copy of the
catalog in its tools folder (`~/lcsas-restore-tools/catalog.db`),
and tier 1 copies a catalog it finds on a disc later into its pack
cache before opening it, so this is rare.

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

## `no restic repository found.`

**Cause**: the script looked for `keys/` + `index/` in the legacy
path only.  Means either (a) the meta disc you have is a
pre-Phase-22 build, OR (b) you're invoking the wrong recovery
script.

**Fix**: insert any data disc into the drive and mount it at
`/mnt`.  The script's `metadata/<repo>/` auto-discovery (added
in PR #83) finds each repository's metadata on any data disc:

```sh
sudo mount /dev/sr0 /mnt
sh restore.sh ~/restored/ latest
```

If you have NO data disc yet and only the meta disc, the meta
disc itself carries repository metadata under `metadata/<repo>/`
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

If tier 3 itself is broken, drop to a manual rustic run.  rustic
needs every pack in one folder, so first assemble the repository
as [Manual pack assembly](#manual-pack-assembly-last-resort-tier-3-fallback)
below says, then run rustic-static against that folder.

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

## `(no disc catalog yet: insert your newest data disc)` (the disc box names no disc)

**Cause**: tier 1 has no catalog, so it cannot turn the pack ID into
a disc label.  This happens when no data disc was in at the start
(you typed S at "put in your NEWEST data disc", or the script did not
ask).  The META disc carries no catalog.

**Fix**: put in your newest data disc (the latest year in its
label, then the highest number) and mount it if it does not open by
itself.  Each data disc carries a disc catalog at
`<disc-root>/catalog.db`; tier 1 picks it up when it looks again.

```sh
sudo mount /dev/sr0 /mnt
# Now press Enter at the prompt; the rescan will find both the
# pack and the catalog.
```

Older recovery binaries said `(no --catalog supplied; cannot
suggest a disc)` here.  The legacy `restore_legacy.sh` prints
`INSERT DISC: <label>` instead (blank for the first disc); there,
insert the disc it names, or your newest data disc when it names
none.

---

## `WARNING: the cache at X has N MB free, too little to copy disc L`

**Cause**: the pack cache (default `pack-cache/` in the tools folder,
`~/lcsas-restore-tools/`, since rc6; before that
`${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>/`) is on a drive without room
for the whole disc plus some to spare (5 % of the drive, at most
1 GB).  The binary does not copy discs that do not fit, rather than
risk filling the drive mid-restore; the restore still finishes, but
may ask for those discs again.  (Before rc7 the rule was "under 10 %
free", and the warning read `cache filesystem at X is <10% free;
disabling further drains`.)

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

## Several repositories: typed the wrong repository name

**Cause**: typed `albha` instead of `alpha` at the
`Choose a repository (number or name):` prompt.

**Fix**: on a terminal the script says
`no repository named albha; choose from: ...` and asks again: type
the number, the name or the id (Q then Enter quits, exit code 65).
Without a terminal (a script, a pipe) it exits 1 with that list.
The prompt only appears on archives with more than one repository.
On Windows, `restore.bat` takes only the number.

---

## The agent / script gives up before completing

**Cause**: hit max-turns (test fixture) or exhausted patience
(real heir).

**Fix**: the restore is idempotent over rustic-format repositories —
just carry on.  A restore that stopped keeps its tools folder, with
the discs already copied into its pack cache, until a restore
succeeds.  Run the command the script printed (no META disc needed):

```sh
sh ~/lcsas-restore-tools/resume.sh
```

Re-running the first command from the META disc works too.  Once a
restore succeeds the tools folder and its pack cache are deleted.

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
to manually assemble a rustic-format repository by hand:

1. Pick a writable directory `$R` (call it `/tmp/manual-repo`).
2. Copy `config`, `keys/`, `index/` and `snapshots/` from your
   NEWEST data disc's `metadata/<id>/` into `$R/` (the newest disc
   knows the newest snapshots; `grep -H '^name=' /mnt/metadata/*/LCSAS_REPO.txt`
   says which `<id>` is which backup set).
3. For every data disc, mount it and copy `data/` into `$R/data/`
   (or `cp -r` over the existing tree; both directories are
   content-addressed so identical packs merge cleanly).
4. Run rustic-static from the META disc (`recovery/bin/<arch>/rustic-static`,
   or `tools/bin/rustic-static`) directly against `$R`:

```sh
/mnt/recovery/bin/<arch>/rustic-static -r /tmp/manual-repo \
    --password-file ~/key.txt restore latest ~/restored/
```

This is essentially what `restore_auto.sh RECOVERY_ROOT TARGET_ROOT`
(with `LCSAS_PWFILE` set) does for you non-interactively when every
pack is already laid out under `RECOVERY_ROOT`.  The end result is
identical to a successful `restore.sh` run — just slower and more
manual.

---

## `Waiting for the catalog lock: held by '...' (pid N, ...)`

Another `lcsas` command holds the catalog lock — commonly a
`lcsas burn` that runs for hours.  Your command is queued and will
proceed automatically when the holder finishes.

- **Ctrl-C is safe for the *waiting* command** — it cancels only the
  command you just started, never the one holding the lock.
- **Do NOT kill the holding process.** The named pid may be mid-burn;
  killing it can abort a burn.  Let it finish, or wait at another time.
- For scripts that must not block forever, pass `--lock-timeout <seconds>`
  (or set `LCSAS_LOCK_TIMEOUT`).  On expiry the command exits with code
  **75** (`EX_TEMPFAIL`) so the caller can retry later, instead of hanging.

The lock lives in `<catalog>.lock`.  **Never delete it manually.**  The
kernel releases the underlying `flock` automatically the instant the
holding process exits (even on a crash or `kill -9`); the file itself is
inert and removing it does not unstick anything — it only discards the
holder-identity stamp the next waiter would have read.

---

## When everything fails — the bare-metal escape hatch

The on-disc data format is documented in
`docs/RESTIC_FORMAT_SPEC.md`.  A programmer with that document,
your password, and the data discs can decrypt and rebuild
the original files from scratch in any language — Python, Go,
Rust, C, anything.  No LCSAS binaries required.

This is the "50-year survivability" guarantee.  The format won't
rot; the bytes on the disc are forever decryptable given the
password + the spec.

If you've reached this point, file an issue describing what you
tried.  We want to make sure the next heir doesn't.
