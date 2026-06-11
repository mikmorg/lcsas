# Live-USB Recovery (No Working Operating System)

> The *no-OS* recovery journey: the operator (or heir) has the LCSAS
> discs but no computer with a working operating system. **LCSAS discs
> are NOT bootable** — the route is any other working computer, or a
> current live-Linux USB stick booted on the dead machine. This is the
> on-disc `recovery/docs/BOOT.txt` procedure in repo-docs form (owned by
> plan BOOT-01; the live-USB path itself is validated under BOOT-04).

Historical note: earlier docs routed this scenario to "boot the meta
disc directly". No disc any build ever produced was bootable — the boot
scaffolding was never built, and was dropped in 2026-06 and quarantined
to `experimental/boot/` (see `experimental/boot/README.md` for the
decision record and the revival precondition in BOOT-08).

Sibling docs for gentler scenarios:

- `docs/workflows/restore-host-linux.md` — working Linux host
- `docs/workflows/restore-windows.md` — working Windows host
- `docs/workflows/restore-disc-only.md` — single-disc spot recovery
- `docs/workflows/recovery-toolchain.md` — binary cascade architecture

---

## Option 1 — Use any other computer

Any working computer with an optical drive (internal or USB) can run
the recovery: a friend's machine, a library computer, a second-hand
laptop. Insert the META disc, open `START_HERE.txt`, and follow the
section for that machine's operating system (Windows → `restore.bat` /
`RECOVER_WINDOWS.txt`; macOS or Linux → `restore.sh` / `RECOVER.txt`).

## Option 2 — Boot a live-Linux USB stick on the dead machine

**Prerequisites:** any other working computer for a few minutes (to
prepare the stick), a USB stick of 8 GB or more, and an optical drive
on the dead machine (internal or USB).

1. On any working machine, download a current mainstream Linux live
   desktop image. **Ubuntu Desktop LTS** is the concrete example; any
   current major Linux distribution works.
2. Write the image to the USB stick using the distribution's official
   instructions (search: *create Ubuntu live USB* — the official tools
   run on Windows, macOS, and Linux).
3. Boot the dead machine from the stick: press the boot-menu key at
   power-on (commonly F12, F2, Esc, or Del) and pick the USB stick.
4. Choose **"Try Ubuntu"** (run without installing). You now have a
   full Linux desktop running from RAM.
5. Insert the LCSAS META disc. Most live systems auto-mount it when you
   click the disc in the file manager; otherwise:

   ```sh
   sudo mkdir -p /mnt
   sudo mount -o ro /dev/sr0 /mnt
   ```

6. Run the recovery:

   ```sh
   sh /mnt/recovery/scripts/restore.sh /home/ubuntu/restored latest
   ```

   The script prompts for the archive password and walks through data
   disc swaps; `recovery/docs/RECOVER.txt` on the disc is the full
   operator manual from this point.
7. **Copy the restored files to a real disk before powering off** — a
   live system's home directory lives in RAM. Either copy out of
   `/home/ubuntu/restored`, or pass a path on a mounted disk as the
   target instead (mind tmpfs capacity for large restores). Best
   practice in a live-USB session: restore directly to a plugged-in
   external drive, not the live system's home folder — `restore.sh`
   detects a RAM-backed (tmpfs) target and asks for confirmation
   before continuing.

## Why this works decades out

A current live image is maintained by an OS vendor *today*: it is
Secure-Boot signed (modern firmware accepts it without configuration
changes) and carries current hardware drivers (it boots on machines
that did not exist when the discs were burned). The LCSAS recovery
binaries are static and need nothing from the host beyond a kernel —
the live system only has to boot and mount the disc. Always download a
**current** image rather than archiving one next to the discs: an old
image rots against new hardware; a current one never does. (Validation
of this replacement path is BOOT-04's deliverable.)

## Source refs

- On-disc procedure: `recovery/docs/BOOT.txt` (same steps, plain text)
- Decision-flow routing: `recovery/docs/RECOVER.txt` ("Do you have a
  working OS?")
- Routing guard test:
  `tests/recovery_hardening/test_no_boot_deadend_routing.py`
- Docs-vs-CLI contract gate:
  `tests/recovery_hardening/test_boot_docs_reality.py`
- Dropped boot scaffolding: `experimental/boot/README.md`
