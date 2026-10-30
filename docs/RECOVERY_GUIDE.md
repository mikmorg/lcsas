# LCSAS — Recovery Guide

> **Audience:** Anyone who has LCSAS backup discs and needs to get their
> files back.  Start here; this guide routes you to the right
> step-by-step walkthrough for your OS and situation.

---

## Before You Start

You need **three things** to restore your files:

| What | Where to find it |
|------|------------------|
| **The backup discs** | A disc binder or case.  Look for discs labeled `LCSAS_*`. |
| **The encryption password** | A separate USB drive, paper printout, or password manager.  Check `START_HERE.txt` on any disc for hints left by the owner. |
| **A computer** | Linux, macOS, or Windows — see the route below. |

> **No password?**  Stop here.  The data **cannot** be restored
> without the password — by anyone, ever.  Search thoroughly before
> giving up: home safe, bank safe deposit box, attorney's office,
> shared password manager.

> **Password split into share cards?**  If `PASSWORD_INFO.txt` on any disc
> says the password is split, you must reconstruct it first: gather
> the stated number of share cards (printed pages titled
> "SHARE CARD"; older cards say "KEY SHARE") and run the combiner
> bundled on the META disc —
> `recovery/bin/<machine>/lcsas-keyshare card1.txt card2.txt`
> (`<machine>` is `x86_64` for most PCs; on Windows, run
> `lcsas-keyshare.exe`, see the Windows walkthrough).  If the binary
> won't run, the fallback is `python3 keyshare_combine.py card1.txt
> card2.txt`.  Either prints the password — then continue below as
> normal.

---

## Which walkthrough should I follow?

Pick the row that matches your situation, then follow the linked doc.

| Your computer | Your starting state | Walkthrough |
|---|---|---|
| Linux (any distro) | You have the discs + the META disc + a working OS | [docs/guides/recovery-runbook.md](guides/recovery-runbook.md) |
| macOS (Intel or Apple Silicon) | You have the discs + the META disc + a working OS | [docs/workflows/restore-host-macos.md](workflows/restore-host-macos.md) |
| Windows | You have the discs + the META disc + a working OS | [docs/workflows/restore-windows.md](workflows/restore-windows.md) |
| No working OS | Lost the host machine entirely | [docs/workflows/restore-live-usb.md](workflows/restore-live-usb.md) (use another computer or a live-Linux USB — the discs are NOT bootable) |
| Any OS with Python | You only have one or two data discs (no META disc) | [docs/workflows/restore-disc-only.md](workflows/restore-disc-only.md) (tier-3 Python fallback) |
| Linux with LCSAS installed | Your archival machine is alive and just want files back | [docs/workflows/restore-host-linux.md](workflows/restore-host-linux.md) (`lcsas restore` — easy mode) |

If none of those fit your situation, see
[the WORKFLOWS matrix](WORKFLOWS.md) for the full list.

---

## The 30-second version (Linux + META disc)

For the most common case, the entire restore is:

```sh
sudo mount /dev/sr0 /mnt                       # insert the newest META disc, mount
sh /mnt/restore.sh ~/restored/ latest          # start restore
# answer "Repository: <name>" and "Password: <yours>"
# first data disc: the one with the newest label (see below)
# when prompted, eject + insert the named data disc, press Enter
# repeat for each data disc the script asks for
```

**Which discs to start with.**  If you have more than one META disc,
use the newest; if its tools fail, use the one burned closest in date
to your data discs.  For the first data disc, pick the one with the
newest label: the latest year in the label, then the highest number.
That disc's catalog is the fullest, so you swap discs less often.  Any
data disc still works.

When you see `RESTORE COMPLETE`, your files are in `~/restored/`.
[recovery-runbook.md](guides/recovery-runbook.md) has the long form with
troubleshooting and guidance for archives with several repositories.

---

## Troubleshooting (OS-agnostic)

### "Permission denied" when running restore.sh

The disc may be mounted without execute permission:

```sh
# Option 1: invoke via sh (always works)
sh /mnt/restore.sh ~/restored/ latest

# Option 2: remount with exec
sudo mount -o remount,exec /mnt
```

### "wrong password" or "unable to open key"

- Double-check you're using the correct password for the repository you
  selected at the `Repository:` prompt.
- If the archive has multiple repositories, they may use different
  passwords.  Check `PASSWORD_INFO.txt` on any disc for which password
  goes with which repository.
- If you have the password in a file (e.g. on a USB drive), set
  `LCSAS_PWFILE=/path/to/file` before launching `restore.sh` to skip
  the interactive prompt.

### "missing pack" or "incomplete data"

The script needs a disc that hasn't been inserted yet.  The disc-swap
prompt names the specific disc it wants:

```
Insert the right disc and press ENTER to retry.
```

Eject the current disc and insert the named one.  On Linux and macOS
the restore notices the disc once your computer has mounted it and
carries on by itself; if nothing happens within a few seconds (or on
Windows), press Enter.  Each disc it finds is named by its label
(`mounted the disc labelled <label> at <path>`), so a wrong disc
shows up straight away, followed by `still not found`.  If a disc is
damaged, check whether another disc with the same label is kept at
another location (the recommended practice is burning ≥2 discs with
each label and keeping them at separate locations; the catalog
records where each disc is).

### The bundled binary won't run on this system

The META disc ships per-target binaries
(see [`docs/CROSS_PLATFORM_META_RFC.md`](CROSS_PLATFORM_META_RFC.md)
for the supported matrix).  If none works on your CPU/OS, the
script automatically falls back to tier 3, the Python reader
(`standalone_restorer.py`), which needs only Python 3.10+.

### Tier 3 (the Python reader) is very slow

The Python fallback runs at ~1 MB/s.  A 100 GB archive takes ~28
hours.  If you have a working system, prefer the C/Rust tiers (the
default).  See [restore-disc-only.md](workflows/restore-disc-only.md)
for the tier-3-only flow.

### Need a working Linux box?

See [Appendix A — Getting Linux](#appendix-a--getting-linux).

For more situations, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Appendix A — Getting Linux

If you don't have a Linux computer, the easiest path:

1. **Download Ubuntu Desktop** from <https://ubuntu.com/download/desktop>
2. **Create a bootable USB** using Rufus (Windows) or balenaEtcher (any OS)
3. **Boot from the USB** — you can "Try Ubuntu" without installing it
4. Open a terminal (Ctrl+Alt+T) and follow
   [recovery-runbook.md](guides/recovery-runbook.md)

Alternatively, use a virtual machine:

- **VirtualBox** (free): <https://www.virtualbox.org/>
- Create a new VM, give it 4 GB RAM, boot the Ubuntu ISO
- The VM has full access to your files and USB drives

> **For Apple Silicon Macs:** use UTM (<https://mac.getutm.app/>) for
> a Linux VM, or just follow
> [restore-host-macos.md](workflows/restore-host-macos.md) directly
> — the META disc ships native aarch64 macOS binaries.

---

## Appendix B — Disc Layout (Reference)

Every LCSAS data disc contains:

```
LCSAS_BD25_2026_0001.iso
├── START_HERE.txt           Read this first (plain English)
├── PASSWORD_INFO.txt        Which password unlocks which repository
├── CONFIG_SUMMARY.txt       Archive settings summary
├── DISC_CARE.txt            How to store discs safely
├── standalone_restorer.py   Tier 3 Python reader (no dependencies)
├── volume_info.json         Machine-readable disc identity
├── catalog.db               Disc catalog (SQLite): which disc holds each pack
├── data/                    Encrypted backup data
│   └── ab/abc123...         Pack files (named by pack ID)
└── metadata/
    └── family/              One folder per repository
        ├── config
        ├── keys/
        ├── index/
        └── snapshots/
```

The **META disc** holds no catalog.  It contains:

```
LCSAS_META.iso
├── restore.sh               Start here.  Interactive restore script.
├── README_RESTORE.md        Detailed restore guide
├── tools/bin/<arch>/        Per-platform recovery binaries
├── lcsas/                   Full LCSAS source code
└── recovery/docs/           On-disc reference (RECOVER.txt, TIERS.txt, …)
```

The META disc ships binaries for six platforms (Linux
x86_64/aarch64/armv7 musl, macOS Intel + Apple Silicon, Windows
x86_64).  See [`docs/CROSS_PLATFORM_META_RFC.md`](CROSS_PLATFORM_META_RFC.md).

---

## Appendix C — Quick Reference Card

Print this and tape it to the inside of your disc binder:

```
┌─────────────────────────────────────────────────────────┐
│          LCSAS BACKUP DISC — QUICK RESTORE              │
│                                                         │
│  1. Find the encryption password                        │
│     (USB / paper / password manager — see               │
│      START_HERE.txt on any disc)                        │
│                                                         │
│  2. Find the newest META disc (labeled "LCSAS_META")    │
│                                                         │
│  3. On a Linux computer, insert META and run:           │
│                                                         │
│     sudo mount /dev/sr0 /mnt                            │
│     sh /mnt/restore.sh ~/restored/ latest               │
│                                                         │
│  4. Answer prompts:                                     │
│     Repository: <name>                                  │
│     Password:   <yours>                                 │
│                                                         │
│  5. First data disc: the one with the newest label      │
│     (latest year, then highest number).  After that,    │
│     when asked, eject + insert the named data disc,     │
│     press Enter.  Repeat per disc.                      │
│                                                         │
│  6. Files appear in ~/restored/                         │
│                                                         │
│  On macOS or Windows?  See RECOVERY_GUIDE.md "Which     │
│  walkthrough should I follow?" table.                   │
│                                                         │
│  No META disc?  See restore-disc-only.md (Python tier). │
│  No working OS?  See restore-live-usb.md (live USB).    │
│  Confused?  Take discs + password to any IT pro.        │
└─────────────────────────────────────────────────────────┘
```
