# LCSAS — Recovery Guide

> **Audience:** Anyone who has LCSAS backup discs and needs to get their
> files back — whether you're technical or not.  Print this guide and
> keep it with your disc binder.

---

## Before You Start

You need **three things** to restore your files:

| What | Where to find it |
|------|------------------|
| **The backup discs** | A disc binder or case.  Look for discs labeled `LCSAS_*`. |
| **The encryption key** | A separate USB drive, paper printout, or password manager.  Check `START_HERE.txt` on any disc for hints left by the archivist. |
| **A Linux computer** | Any PC running Linux, or a virtual machine (see Appendix A). |

> **No encryption key?**  Stop here.  The data **cannot** be recovered
> without the key — by anyone, ever.  Search thoroughly before giving
> up: home safe, bank safe deposit box, attorney's office, shared
> password manager.

---

## Which Scenario Are You In?

| Scenario | What you have | Go to |
|----------|---------------|-------|
| **A — Easiest** | All discs + META disc | Section 1 |
| **B — No META disc** | Data discs only (no disc labeled "META") | Section 2 |
| **C — Expert** | LCSAS already installed on this computer | Section 3 |

If you're not sure, start with **Scenario A**.  Look through your discs
for one labeled "META" — it's the rescue disc containing all tools.

---

## Section 1 — Restore with the META Disc (Easiest)

This is the recommended path.  The META disc contains a script that
does everything automatically.

### Step 1: Get the META disc onto your computer

Insert the META disc into a Blu-ray drive.

**If the computer auto-mounts it** (you see a folder appear):

    Note the path — something like /media/your-name/LCSAS_META

**If it doesn't auto-mount:**

    sudo mount /dev/sr0 /mnt

The META disc is now at `/mnt` (or wherever it mounted).

### Step 2: Copy your data disc ISOs to a folder

If your data discs are **physical discs**, you need to create ISO
images first.  For each data disc:

    # Insert the disc, then:
    dd if=/dev/sr0 of=~/discs/LCSAS_BD25_001.iso bs=1M status=progress

    # Use the label printed on each disc as the filename.
    # Repeat for every data disc.

If you already have `.iso` files (e.g., on a hard drive), skip this —
just note the directory they're in.

### Step 3: Run the restore script

    cd /media/your-name/LCSAS_META      # or wherever the META disc is
    ./restore.sh \
        --key /path/to/your/keyfile \
        --isos ~/discs/ \
        --target ~/restored/

Replace the paths above:
- `--key` → the encryption key file (USB drive, etc.)
- `--isos` → the folder containing your `.iso` files
- `--target` → where you want the restored files to go

The script will:
1. Extract every ISO automatically (tries multiple methods)
2. Find all repositories on the discs
3. Assemble the data and decrypt it
4. Place restored files in `~/restored/`

### Step 4: Verify your files

    ls ~/restored/

You should see a folder for each repository (e.g., `family/`, `work/`).
Browse through to confirm your files are there and openable.

**Done!**  Your files are restored.

---

## Section 2 — Restore Without the META Disc

If you can't find the META disc, you can still restore from any data
disc.  Each data disc carries a standalone Python restore script.

### Step 2a: Get the data disc contents

Mount or extract a data disc (start with the highest-numbered one —
it has the most complete catalog):

    # Mount a physical disc:
    sudo mount /dev/sr0 /mnt

    # Or extract an ISO:
    7z x LCSAS_BD25_003.iso -o/tmp/disc3/

### Step 2b: Check what's on the disc

    cat /mnt/START_HERE.txt          # human-readable overview
    cat /mnt/KEY_INFO.txt            # which key(s) you need

### Step 2c: Prepare a working area

Repeat the mount/extract for every data disc.  Then assemble the
pack files and metadata into a single directory:

    # Copy metadata from the newest disc (e.g., disc 3):
    cp -r /tmp/disc3/metadata/REPO_NAME /tmp/cache/

    # Copy the config file:
    cp /tmp/disc3/metadata/REPO_NAME/config /tmp/cache/config

    # Copy pack files from ALL discs:
    for vol in /tmp/disc1 /tmp/disc2 /tmp/disc3; do
        for pack in "$vol"/data/*/*; do
            sha=$(basename "$pack")
            prefix=${sha:0:2}
            mkdir -p /tmp/cache/data/$prefix
            cp -n "$pack" /tmp/cache/data/$prefix/$sha
        done
    done

### Step 2d: Restore using the standalone Python script

Every data disc includes `standalone_restorer.py` — a pure-Python
restore tool with no dependencies beyond Python 3.10+:

    python3 /tmp/disc3/standalone_restorer.py \
        --repo /tmp/cache \
        --password-file /path/to/keyfile \
        --target ~/restored/

> **Note:** This is slower than the normal tool (~1 MB/s) but works on
> any system with Python 3 — including ARM, macOS, or Windows WSL.

### Step 2e: Verify

    ls ~/restored/

Browse your files to confirm.

---

## Section 3 — Restore with LCSAS Installed

If you have LCSAS installed (the archivist's computer or a fresh
install), this is the simplest path:

### From a mounted disc or extracted ISO:

    lcsas restore standalone /mnt/disc ~/restored/ \
        --password-file /path/to/keyfile

LCSAS auto-discovers repositories from the disc's embedded catalog.

### From multiple ISOs:

    lcsas restore standalone /mnt/disc ~/restored/ \
        --password-file /path/to/keyfile \
        --volume-dir /path/to/extracted-isos

### Pick a specific repo or snapshot:

    lcsas restore standalone /mnt/disc ~/restored/ \
        --password-file /path/to/keyfile \
        --repo family --snapshot latest

---

## Troubleshooting

### "Permission denied" when running restore.sh

The disc may be mounted without execute permission:

    # Option 1: Remount with exec
    sudo mount -o remount,exec /mnt

    # Option 2: Copy to local disk
    cp -r /mnt /tmp/lcsas-meta
    cd /tmp/lcsas-meta
    chmod +x restore.sh
    ./restore.sh ...

### "No rustic/restic binary found"

The bundled binary doesn't work on your system (wrong architecture
or missing libraries).  The script should automatically fall back to
the pure-Python restorer.  If not:

    # Use the Python fallback directly:
    python3 standalone_restorer.py \
        --repo /tmp/cache \
        --password-file /path/to/keyfile \
        --target ~/restored/

### "wrong password" or "unable to open key"

- Double-check you're using the correct key file.
- If the archive has multiple repositories, they may use different
  keys.  Check `KEY_INFO.txt` on any disc for which key goes with
  which repository.
- The key is the **file**, not a password you type.  Point `--password-file`
  at the actual file (e.g., the file on the USB drive).

### "missing pack" or "incomplete data"

Some pack files are on a disc you haven't included:

1. Check `catalog.db` on the newest disc to see which volumes contain
   which packs:

       sqlite3 /tmp/disc3/catalog.db \
           "SELECT v.label, COUNT(vp.pack_sha256)
            FROM volumes v JOIN volume_packs vp ON v.id = vp.volume_id
            GROUP BY v.label"

2. Make sure you've extracted **all** data discs, not just the newest.

3. If a physical disc is damaged, check if you have a redundant copy
   at another storage location.

### "cannot extract ISO" (mount, 7z, xorriso all fail)

Install one of these tools:

    # Debian/Ubuntu:
    sudo apt install p7zip-full

    # Or simply mount as root:
    sudo mount -o loop,ro file.iso /mnt

### The restore finished but files look wrong or are missing

- You may have restored an older snapshot.  Try specifying the latest:
  `--snapshot latest`
- Check if there are multiple repositories — you may need to restore
  each one separately.
- Run the restore again into an empty directory to avoid mixing with
  old files.

### Standalone restorer is very slow

This is expected — the pure-Python fallback processes at ~1 MB/s.
For a 100 GB archive, expect ~28 hours.  If speed matters, install
the native `rustic` tool:

    # Download from https://rustic.cli.rs/
    # Or install via package manager:
    cargo install rustic-rs

---

## Appendix A — Getting Linux

If you don't have a Linux computer, here's the easiest way:

1. **Download Ubuntu Desktop** from https://ubuntu.com/download/desktop
2. **Create a bootable USB** using Rufus (Windows) or balenaEtcher (any OS)
3. **Boot from the USB** — you can "Try Ubuntu" without installing it
4. Open a terminal (Ctrl+Alt+T) and follow the steps above

Alternatively, use a virtual machine:
- **VirtualBox** (free): https://www.virtualbox.org/
- Create a new VM, give it 4 GB RAM, boot the Ubuntu ISO
- The VM has full access to your files and USB drives

> **For M1/M2 Mac users:** Use UTM (https://mac.getutm.app/) to run
> an x86_64 Linux VM.  The bundled tools are x86_64 binaries and need
> either native x86_64 Linux or emulation.

---

## Appendix B — Understanding the Disc Layout

Every LCSAS data disc contains:

    LCSAS_BD25_001.iso
    ├── START_HERE.txt           ← Read this first (plain English)
    ├── RESTORE_INSTRUCTIONS.txt ← Technical step-by-step
    ├── KEY_INFO.txt             ← Which key unlocks which data
    ├── CONFIG_SUMMARY.txt       ← Archive settings snapshot
    ├── DISC_CARE.txt            ← How to store discs safely
    ├── standalone_restorer.py   ← Python restore tool (no dependencies)
    ├── volume_info.json         ← Machine-readable disc identity
    ├── catalog.db               ← SQLite catalog of all volumes/packs
    ├── data/                    ← Encrypted backup data
    │   └── ab/abc123...         ← Pack files (named by SHA-256)
    └── metadata/
        └── family/              ← One folder per repository
            ├── config
            ├── keys/
            ├── index/
            └── snapshots/

The **META disc** additionally contains:

    LCSAS_META.iso
    ├── restore.sh               ← Automated restore script
    ├── README_RESTORE.md        ← Detailed restore guide
    ├── README_RESTORE.txt       ← Same, plain text
    ├── tools/
    │   ├── bin/rustic            ← Portable decryption tool
    │   ├── bin/rustic-static     ← Static build (no dependencies)
    │   ├── bin/xorriso           ← ISO extraction tool
    │   ├── bin/python3           ← Portable Python interpreter
    │   └── lib/                  ← Shared libraries
    ├── lcsas/                   ← Full LCSAS source code
    └── docs/
        ├── architecture.md
        ├── RESTIC_FORMAT_SPEC.md ← Data format documentation
        └── DVDISASTER_RS03_FORMAT.md

---

## Appendix C — Quick Reference Card

Print this and tape it to the inside of your disc binder:

```
┌─────────────────────────────────────────────────────────┐
│          LCSAS BACKUP DISC — QUICK RESTORE              │
│                                                         │
│  1. Find the encryption key                             │
│     (USB/paper/safe — see START_HERE.txt on any disc)   │
│                                                         │
│  2. Find the META disc (labeled "META")                 │
│                                                         │
│  3. On a Linux computer, run:                           │
│                                                         │
│     cd /path/to/meta-disc                               │
│     ./restore.sh  --key /path/to/keyfile  \             │
│                   --isos /path/to/isos/   \             │
│                   --target ~/restored/                  │
│                                                         │
│  4. Files appear in ~/restored/                         │
│                                                         │
│  No META disc?  Use standalone_restorer.py on any disc  │
│  No Linux?  See RECOVERY_GUIDE.md Appendix A            │
│  Confused?  Take discs + key to any IT professional     │
└─────────────────────────────────────────────────────────┘
```
