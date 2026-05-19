# LCSAS Disaster Recovery — End-to-End Runbook

> **Scenario**: a fire, flood, or ransomware event has destroyed
> the original storage.  You have a box of optical discs labelled
> `LCSAS_META` + `LCSAS_TEST_TINY_2026_XXXX` (or `LCSAS_BD25_…` /
> `LCSAS_CD_…` for production media), a working Linux laptop with
> an optical drive, and the encryption password for at least one
> tenant repository.  This document walks you through restoring,
> command by command.

If anything below behaves differently from what's described, jump
to [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

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
   restore script will tell you which ones.
3. **The encryption password** for the tenant you're restoring.
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
that's the recovery toolchain.

> The meta disc is the smallest one in your stack (~50–200 MB).
> Don't confuse it with the data discs (each one ~25 GB on BD25
> production media).

---

## Step 2 — Run the recovery script

```sh
sh /mnt/restore.sh ~/restored/ latest
```

That's it.  Two positional arguments:

| Arg | Meaning | Default |
|---|---|---|
| 1 | `TARGET_DIR` — where restored files land | `/tmp/restored` |
| 2 | `SNAPSHOT_ID` or the word `latest` | `latest` |

The script will:

- Copy itself into RAM so you can eject the meta disc safely.
- Auto-discover repository metadata (per-tenant `keys/` + `index/`
  pairs) on the meta disc and on any currently-mounted data disc.

---

## Step 3 — Answer the prompts

You'll see up to three prompts in sequence:

### `Repository:` (only on multi-tenant archives)

If your archive has more than one tenant (e.g. `alpha` and `bravo`),
the script lists them and asks you to choose:

```
Multiple repositories on this archive:
  1) alpha
  2) bravo
Choose a repository (number or name):
```

Type either the number (`1`) or the name (`alpha`) and press Enter.

To pre-select non-interactively, set `LCSAS_REPO=alpha` in your
environment before launching the script.

### `Password:`

Type the encryption password for the chosen tenant.  The script
reads stdin in the clear (POSIX-sh has no silent-read) — that's
expected.  If you'd rather not type it visibly, you can put it in
a file and set `LCSAS_PWFILE=/path/to/file`.

### `INSERT DISC: LCSAS_TEST_TINY_2026_0003`

The recovery binary will print this whenever it needs a pack that
isn't on the disc currently in your drive.  At that prompt:

1. **Eject the current disc** from your drive (button, eject
   key, or `eject /dev/sr0` from another terminal).
2. **Insert the disc named in the prompt.**
3. **Press Enter** at the prompt.  The binary will rescan
   `/mnt` and any other mount points in `LCSAS_MOUNT_DIRS`,
   find the pack, drain the rest of that disc into the cache,
   and continue.

If you accidentally insert the wrong disc, the binary prints
`(still not found — check the disc label and try again)` and
waits.  Eject and try again.

Type `q` at any disc prompt to abort the restore cleanly.

---

## Step 4 — Wait for `RESTORE COMPLETE`

The binary prints periodic progress on stderr:

```
[lcsas-restore] progress: 12/30 blobs, 0.4 MB
[lcsas-restore] progress: 24/30 blobs, 0.7 MB
[lcsas-restore] RESTORE COMPLETE
```

When you see `RESTORE COMPLETE`, your files are at the path you
gave as `TARGET_DIR` (e.g. `~/restored/`).  Verify with
`ls -laR ~/restored/`.

---

## Step 5 — Verify the restore

Two cheap sanity checks anyone can do without rustic / lcsas
expertise:

```sh
# 1. File count looks reasonable
find ~/restored -type f | wc -l

# 2. Spot-check that a file you expect is there and non-empty
ls -la ~/restored/path/to/known/file
```

If your operator kept a `manifest.sha256` of the original data
(highly recommended; see "Production-readiness checklist" in the
README), compare:

```sh
(cd ~/restored && sha256sum -c /path/to/manifest.sha256)
```

---

## Restoring multiple tenants

Run the recovery script once per tenant:

```sh
sh /mnt/restore.sh ~/restored-alpha/ latest    # answer 'alpha'
sh /mnt/restore.sh ~/restored-bravo/ latest    # answer 'bravo'
```

Each invocation builds its own pack cache under
`${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>/`.  These persist until you
delete them.  If you're restoring multiple tenants in sequence, you
can speed up runs 2–N by re-pointing them at a shared cache:

```sh
mkdir -p /tmp/shared-pack-cache
LCSAS_PACK_CACHE_DIR=/tmp/shared-pack-cache \
    sh /mnt/restore.sh ~/restored-alpha/ latest
LCSAS_PACK_CACHE_DIR=/tmp/shared-pack-cache \
    sh /mnt/restore.sh ~/restored-bravo/ latest
```

The second run finds most packs already cached from the first run.

---

## Single-drive operators (most common case)

If you have only one optical drive, you'll be doing the eject /
insert / press-Enter dance for each unique disc.  The pack cache
defaults ON (`LCSAS_PACK_CACHE_DIR=auto`) so you only swap to each
data disc **once** — the first contact drains the whole disc's
packs into RAM-backed `/tmp/lcsas-pack-cache.<pid>/`, and
subsequent packs from the same disc resolve from cache.

If `/tmp` is constrained, see the cache-size warnings in
[TROUBLESHOOTING.md](./TROUBLESHOOTING.md#cache-filesystem-is-10-free).

---

## Multi-drive operators (faster)

If you have multiple optical drives or are restoring from pre-ripped
ISO files on disk:

```sh
# Loop-mount every ISO and run restore — it auto-discovers all of them
for iso in /path/to/iso-collection/*.iso; do
    mp=$(mktemp -d)
    sudo mount -o ro,loop "$iso" "$mp"
done
sh /mnt/restore.sh ~/restored/ latest
```

The script's auto-discovery walks `LCSAS_MOUNT_DIRS` (default
`/Volumes:/media/<user>:/media:/mnt:/run/media/<user>`) and adds
every mount it finds with a `data/` subdirectory as a pack source.
No swap prompts needed.

---

## Aborting / re-running

Type `q` at any disc prompt to abort.  Your `TARGET_DIR` will be
left as-is (no cleanup); re-run the same command to retry.  The
pack cache survives across runs and the second attempt benefits
from it immediately.

---

## When `restore.sh` won't work — fallbacks

If `restore.sh` produces an error you can't resolve via
[TROUBLESHOOTING.md](./TROUBLESHOOTING.md), the meta disc also
ships fallback paths:

- **`restore-auto.sh`** — non-interactive, flag-driven version
  intended for CI pipelines.  Reads its disc-swap commands from
  whatever you set `--disc-cmd` to.  See Appendix A in
  `README_RESTORE.md`.
- **`restore_legacy.sh`** — the older Bash driver.  Different
  flag UX (`--key`, `--target`, `--repo`).  Still works.
- **Direct invocation of the upstream rustic binary** at
  `tools/bin/rustic-static`.  Requires you to manually assemble
  the pack tree (see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#manual-pack-assembly)).
- **Pure-Python `standalone_restorer.py`** (slowest, ~1 MB/s).
  Requires only Python 3.10+ + standard library.  Run with
  `--repo /path/to/assembled/repo --password-file <file>
  --target <dir>`.

All four are documented in the per-meta-disc `README_RESTORE.md`.

---

## When even tier 3 won't work — the manual escape hatch

If every recovery tier on the meta disc fails, the disc layout is
documented in `docs/RESTIC_FORMAT_SPEC.md` (also bundled on the
meta disc).  A programmer with that document, the encryption key,
and the data discs can decrypt and reassemble the original files
from scratch in any language.

This is the "50-year survivability" guarantee: the format is
fully specified and the bytes on the disc are forever decryptable
given the key.
