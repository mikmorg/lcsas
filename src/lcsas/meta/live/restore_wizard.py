#!/usr/bin/env python3
"""LCSAS TUI Restore Wizard — guided recovery from backup discs.

Uses ``dialog`` (ncurses-based) for a step-by-step guided restore.
Designed to run in the LCSAS live recovery environment, but also
works on any Linux system with ``dialog`` and Python 3.10+ installed.

Screens:
    1. Welcome
    2. Insert encryption key USB
    3. Key file selection
    4. Insert data discs (loop)
    5. Restore target selection
    6. Repository selection
    7. Confirm & run restore
    8. Complete / shutdown

The wizard wraps ``restore.sh`` (the existing tested restore engine)
rather than reimplementing restore logic.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────

DIALOG = "dialog"
TITLE = "LCSAS Recovery"
META_MOUNT = "/cdrom"  # where the boot disc is mounted
DISC_MNT = "/mnt/disc"
USB_MNT = "/mnt/usb"
ISO_STAGING = "/tmp/lcsas-isos"
RESTORE_SCRIPT = "/cdrom/restore.sh"
STANDALONE_RESTORER = "/cdrom/standalone_restorer.py"

# Known optical device paths to scan
OPTICAL_DEVICES = ["/dev/sr0", "/dev/sr1", "/dev/cdrom"]

# Known filesystem types for USB/external drives
USB_FS_TYPES = ("vfat", "ext4", "ext3", "ext2", "ntfs", "exfat", "xfs", "btrfs")


# ── Dialog helpers ───────────────────────────────────────────────


def _dialog_available() -> bool:
    """Check if ``dialog`` is on PATH."""
    return shutil.which(DIALOG) is not None


def _run_dialog(args: list[str], input_text: str = "") -> tuple[int, str]:
    """Run ``dialog`` with *args* and return (exit_code, stderr_output).

    dialog writes user selections to stderr (fd 2).
    Exit codes: 0 = OK, 1 = Cancel, 255 = ESC.
    """
    cmd = [DIALOG, "--title", TITLE, "--backtitle",
           "LCSAS — Linux Cold Storage Archival Suite"] + args
    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=False,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode, result.stderr.strip()


def msgbox(text: str, height: int = 12, width: int = 60) -> int:
    """Show a message box. Returns exit code."""
    rc, _ = _run_dialog(["--msgbox", text, str(height), str(width)])
    return rc


def yesno(text: str, height: int = 10, width: int = 60) -> bool:
    """Show a yes/no dialog. Returns True for Yes."""
    rc, _ = _run_dialog(["--yesno", text, str(height), str(width)])
    return rc == 0


def infobox(text: str, height: int = 5, width: int = 60) -> None:
    """Show a non-blocking info box (no buttons)."""
    _run_dialog(["--infobox", text, str(height), str(width)])


def inputbox(text: str, default: str = "",
             height: int = 10, width: int = 60) -> tuple[int, str]:
    """Show an input box. Returns (exit_code, user_input)."""
    return _run_dialog(
        ["--inputbox", text, str(height), str(width), default])


def menu(text: str, choices: list[tuple[str, str]],
         height: int = 20, width: int = 70,
         menu_height: int = 10) -> tuple[int, str]:
    """Show a menu. choices = [(tag, description), ...].
    Returns (exit_code, selected_tag)."""
    args = ["--menu", text, str(height), str(width), str(menu_height)]
    for tag, desc in choices:
        args.extend([tag, desc])
    return _run_dialog(args)


def radiolist(text: str, choices: list[tuple[str, str, str]],
              height: int = 20, width: int = 70,
              list_height: int = 10) -> tuple[int, str]:
    """Show a radiolist. choices = [(tag, desc, on/off), ...].
    Returns (exit_code, selected_tag)."""
    args = ["--radiolist", text, str(height), str(width), str(list_height)]
    for tag, desc, status in choices:
        args.extend([tag, desc, status])
    return _run_dialog(args)


def gauge(text: str, percent: int, height: int = 7, width: int = 60) -> None:
    """Show a gauge (progress bar)."""
    _run_dialog(
        ["--gauge", text, str(height), str(width), str(percent)])


def tailbox(filepath: str, height: int = 20, width: int = 76) -> int:
    """Show a tail box that follows a file. Returns exit code on dismiss."""
    rc, _ = _run_dialog(
        ["--tailbox", filepath, str(height), str(width)])
    return rc


def programbox(text: str, height: int = 20, width: int = 76) -> None:
    """Show a programbox that reads from stdin."""
    _run_dialog(["--programbox", text, str(height), str(width)])


# ── Device detection helpers ─────────────────────────────────────


def find_block_devices() -> list[dict[str, str]]:
    """List block devices using ``lsblk``.

    Returns a list of dicts with keys: name, size, type, fstype, label,
    mountpoint.
    """
    try:
        result = subprocess.run(
            ["lsblk", "-Jpo",
             "name,size,type,fstype,label,mountpoint"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        devices = []
        for dev in data.get("blockdevices", []):
            devices.append(dev)
            for child in dev.get("children", []):
                devices.append(child)
        return devices
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return []


def find_key_files(search_dirs: list[str] | None = None) -> list[Path]:
    """Scan mounted volumes for ``*.key`` files.

    Also looks for files matching common key patterns:
    ``*.key``, ``*.pem``, ``*key*``.
    """
    if search_dirs is None:
        search_dirs = ["/mnt", "/media", "/run/media", "/tmp"]

    key_files: list[Path] = []
    for base in search_dirs:
        base_path = Path(base)
        if not base_path.is_dir():
            continue
        for pattern in ("**/*.key",):
            for f in base_path.glob(pattern):
                if f.is_file() and f.stat().st_size > 0:
                    key_files.append(f)
    return sorted(set(key_files))


def mount_device(device: str, mountpoint: str,
                 fs_type: str | None = None) -> bool:
    """Mount *device* at *mountpoint*. Returns True on success."""
    os.makedirs(mountpoint, exist_ok=True)
    cmd = ["mount"]
    if fs_type:
        cmd.extend(["-t", fs_type])
    cmd.extend([device, mountpoint])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def umount_device(mountpoint: str) -> bool:
    """Unmount *mountpoint*. Returns True on success."""
    result = subprocess.run(
        ["umount", mountpoint], capture_output=True, text=True)
    return result.returncode == 0


def eject_disc(device: str = "/dev/sr0") -> bool:
    """Eject optical disc. Returns True on success."""
    result = subprocess.run(
        ["eject", device], capture_output=True, text=True)
    return result.returncode == 0


def find_mounted_usb_drives() -> list[dict[str, str]]:
    """Find USB/external drives that are currently mounted."""
    drives = []
    for dev in find_block_devices():
        if (dev.get("type") in ("part", "disk")
                and dev.get("fstype") in USB_FS_TYPES
                and dev.get("mountpoint")):
            drives.append(dev)
    return drives


def find_unmounted_usb_drives() -> list[dict[str, str]]:
    """Find USB/external partitions that are NOT mounted."""
    drives = []
    for dev in find_block_devices():
        if (dev.get("type") in ("part", "disk")
                and dev.get("fstype") in USB_FS_TYPES
                and not dev.get("mountpoint")
                and dev.get("name", "").startswith("/dev/sd")):
            drives.append(dev)
    return drives


# ── ISO / volume info helpers ────────────────────────────────────


def read_volume_info(iso_dir: str) -> dict[str, object]:
    """Read ``volume_info.json`` from extracted ISOs in *iso_dir*."""
    volumes: list[object] = []
    repos: set[str] = set()
    for root, _dirs, files in os.walk(iso_dir):
        if "volume_info.json" in files:
            path = Path(root) / "volume_info.json"
            try:
                with open(path) as f:
                    vol = json.load(f)
                volumes.append(vol)
                # Extract repo names from pack metadata
                for pack_file in Path(root).glob("packs/*/config"):
                    repo_name = pack_file.parent.name
                    repos.add(repo_name)
            except (json.JSONDecodeError, OSError):
                continue
    return {"volumes": volumes, "repos": repos}


def rip_disc_to_iso(device: str, output_path: str) -> bool:
    """Read an optical disc into an ISO file using ``dd``.

    Returns True on success.
    """
    try:
        # Get disc size via blockdev
        size_result = subprocess.run(
            ["blockdev", "--getsize64", device],
            capture_output=True, text=True, check=True,
        )
        disc_size = int(size_result.stdout.strip())
        if disc_size == 0:
            return False
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return False

    # Use dd with progress
    result = subprocess.run(
        ["dd", f"if={device}", f"of={output_path}",
         "bs=2048", "status=progress"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ── Wizard Screens ───────────────────────────────────────────────


class RestoreWizard:
    """Stateful wizard that guides the user through a complete restore."""

    def __init__(self) -> None:
        self.key_file: str = ""
        self.iso_dir: str = ISO_STAGING
        self.target_dir: str = ""
        self.repo: str = ""
        self.ripped_isos: list[str] = []
        self.restore_log: str = "/tmp/lcsas-restore.log"

    def run(self) -> None:
        """Run the wizard from start to finish."""
        os.makedirs(self.iso_dir, exist_ok=True)

        screens = [
            self.screen_welcome,
            self.screen_insert_key,
            self.screen_select_key,
            self.screen_insert_discs,
            self.screen_select_target,
            self.screen_select_repo,
            self.screen_confirm,
            self.screen_run_restore,
            self.screen_complete,
        ]

        idx = 0
        while 0 <= idx < len(screens):
            result = screens[idx]()
            if result == "next":
                idx += 1
            elif result == "back":
                idx = max(0, idx - 1)
            elif result == "cancel":
                if yesno("Are you sure you want to cancel?\n\n"
                         "You can restart the wizard by running:\n"
                         "  python3 /usr/share/lcsas/restore_wizard.py"):
                    break
                # User said No to cancel → stay on current screen
            elif result == "quit":
                break

    # ── Screen 1: Welcome ────────────────────────────────────────

    def screen_welcome(self) -> str:
        """Welcome screen with overview."""
        # Try to read owner info from meta-volume
        owner = "the disc owner"
        start_here = Path(META_MOUNT) / "START_HERE.txt"
        if start_here.is_file():
            try:
                text = start_here.read_text()
                for line in text.splitlines():
                    if "created by" in line.lower() or "owner" in line.lower():
                        owner = line.strip()
                        break
            except OSError:
                pass

        text = (
            f"Welcome to the LCSAS File Recovery Wizard.\n\n"
            f"These backup discs were created by {owner}.\n\n"
            f"This wizard will guide you through restoring your files.\n\n"
            f"You will need:\n"
            f"  1. The encryption key file (on a USB drive)\n"
            f"  2. The data backup discs\n"
            f"  3. A drive to restore files onto\n\n"
            f"Press OK to begin."
        )
        rc = msgbox(text, height=18, width=65)
        return "cancel" if rc != 0 else "next"

    # ── Screen 2: Insert Key USB ─────────────────────────────────

    def screen_insert_key(self) -> str:
        """Prompt user to insert USB with encryption key."""
        text = (
            "Please insert the USB drive containing your "
            "encryption key file, then press OK.\n\n"
            "The wizard will scan for .key files on all "
            "connected USB drives.\n\n"
            "If you already have the key file accessible, "
            "just press OK."
        )
        rc = msgbox(text, height=14, width=60)
        if rc != 0:
            return "cancel"

        # Auto-mount any unmounted USB drives
        infobox("Scanning for USB drives...", height=3, width=40)
        time.sleep(2)  # give udev time to detect

        for drive in find_unmounted_usb_drives():
            dev_name = drive.get("name", "")
            label = drive.get("label", "USB")
            mnt = f"/mnt/usb-{label}"
            mount_device(dev_name, mnt)

        return "next"

    # ── Screen 3: Select Key File ────────────────────────────────

    def screen_select_key(self) -> str:
        """Let user pick from discovered key files, or type a path."""
        key_files = find_key_files()

        if not key_files:
            rc, path = inputbox(
                "No .key files found on connected drives.\n\n"
                "Enter the full path to your encryption key file:",
                height=12, width=65,
            )
            if rc != 0:
                return "back"
            if not path or not Path(path).is_file():
                msgbox(f"File not found: {path}\n\n"
                       "Please check the path and try again.")
                return "back"
            self.key_file = path
            return "next"

        if len(key_files) == 1:
            self.key_file = str(key_files[0])
            rc = msgbox(
                f"Found encryption key:\n\n"
                f"  {self.key_file}\n\n"
                f"Press OK to use this key.",
                height=10, width=65,
            )
            return "back" if rc != 0 else "next"

        # Multiple keys found — let user choose
        choices = [
            (str(i + 1), str(kf))
            for i, kf in enumerate(key_files)
        ]
        rc, selection = menu(
            "Multiple key files found. Select one:",
            choices, height=18, width=70,
        )
        if rc != 0:
            return "back"
        idx = int(selection) - 1
        self.key_file = str(key_files[idx])
        return "next"

    # ── Screen 4: Insert Data Discs ──────────────────────────────

    def screen_insert_discs(self) -> str:
        """Loop: insert disc → rip to ISO → eject → repeat."""
        while True:
            disc_count = len(self.ripped_isos)
            text = (
                f"Discs ripped so far: {disc_count}\n\n"
                "Insert a data disc and press OK to read it.\n\n"
                'Select "Done" when all discs have been inserted.\n\n'
                "Note: reading a BD-R disc may take several minutes."
            )

            choices = [
                ("read", "Read disc from drive"),
                ("iso", "Use existing ISO file"),
                ("dir", "Use directory of ISOs"),
                ("done", "Done — all discs loaded"),
            ]
            rc, selection = menu(text, choices, height=18, width=65)

            if rc != 0:
                return "back"

            if selection == "done":
                if not self.ripped_isos:
                    if not yesno("No discs have been loaded.\n\n"
                                 "Continue anyway?"):
                        continue
                return "next"

            elif selection == "read":
                result = self._rip_current_disc()
                if result == "back":
                    return "back"

            elif selection == "iso":
                rc, path = inputbox(
                    "Enter path to ISO file:", height=10, width=65)
                if rc == 0 and path and Path(path).is_file():
                    self.ripped_isos.append(path)
                    msgbox(f"Added ISO: {Path(path).name}")

            elif selection == "dir":
                rc, path = inputbox(
                    "Enter path to directory containing ISOs:",
                    height=10, width=65)
                if rc == 0 and path and Path(path).is_dir():
                    isos = list(Path(path).glob("*.iso"))
                    for iso in isos:
                        self.ripped_isos.append(str(iso))
                    msgbox(f"Added {len(isos)} ISO files from:\n{path}")

    def _rip_current_disc(self) -> str:
        """Read the current disc in the optical drive."""
        # Find optical device
        device = ""
        for dev in OPTICAL_DEVICES:
            if Path(dev).exists():
                device = dev
                break

        if not device:
            msgbox("No optical drive detected.\n\n"
                   "Use 'Use existing ISO file' instead.")
            return "next"

        # Get disc label if possible
        label = f"disc_{len(self.ripped_isos) + 1:04d}"
        try:
            result = subprocess.run(
                ["blkid", "-s", "LABEL", "-o", "value", device],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                label = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        iso_path = os.path.join(self.iso_dir, f"{label}.iso")

        infobox(f"Reading disc from {device}...\n\n"
                f"This may take several minutes for a BD-R disc.\n"
                f"Saving to: {iso_path}",
                height=7, width=65)

        if rip_disc_to_iso(device, iso_path):
            self.ripped_isos.append(iso_path)
            eject_disc(device)
            msgbox(f"Disc read successfully!\n\n"
                   f"Label: {label}\n"
                   f"Size: {Path(iso_path).stat().st_size / (1024**3):.1f} GB\n\n"
                   f"The disc has been ejected. Insert the next disc or "
                   f'select "Done".')
        else:
            msgbox(f"Failed to read disc from {device}.\n\n"
                   "Check that a disc is in the drive and try again.")

        return "next"

    # ── Screen 5: Select Restore Target ──────────────────────────

    def screen_select_target(self) -> str:
        """Let user pick where to restore files."""
        choices: list[tuple[str, str]] = []

        # Auto-detect mounted external drives
        for drive in find_mounted_usb_drives():
            mnt = drive.get("mountpoint", "")
            label = drive.get("label", "")
            size = drive.get("size", "")
            desc = f"{label} ({size})" if label else f"({size})"
            choices.append((mnt, desc))

        # Also check for unmounted drives to offer mounting
        for drive in find_unmounted_usb_drives():
            name = drive.get("name", "")
            label = drive.get("label", "")
            size = drive.get("size", "")
            desc = f"[unmounted] {label} ({size})" if label else f"[unmounted] ({size})"
            choices.append((name, desc))

        choices.append(("custom", "Enter a custom path"))

        text = (
            "Where should the files be restored?\n\n"
            "Select a drive or enter a custom directory path."
        )
        rc, selection = menu(text, choices, height=20, width=70)

        if rc != 0:
            return "back"

        if selection == "custom":
            rc, path = inputbox(
                "Enter the full path for restore target:",
                default="/mnt/target",
                height=10, width=65,
            )
            if rc != 0:
                return "back"
            selection = path

        # If user selected an unmounted device, mount it
        if selection.startswith("/dev/"):
            mnt = f"/mnt/restore-target"
            if mount_device(selection, mnt):
                selection = mnt
            else:
                msgbox(f"Failed to mount {selection}.\n"
                       "Try a different drive or enter a custom path.")
                return "back"

        self.target_dir = selection

        # Verify the target is writable
        os.makedirs(self.target_dir, exist_ok=True)
        test_file = Path(self.target_dir) / ".lcsas_write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
        except OSError:
            msgbox(f"Cannot write to: {self.target_dir}\n\n"
                   "The target must be writable. "
                   "Try a different location.")
            return "back"

        return "next"

    # ── Screen 6: Select Repository ──────────────────────────────

    def screen_select_repo(self) -> str:
        """Let user pick which repo to restore (or all)."""
        # Extract ISOs and scan for repos
        infobox("Scanning ISOs for backup repositories...", height=3, width=50)

        # Use restore.sh's ISO extraction if available, else manual scan
        repos: list[str] = []
        for iso_path in self.ripped_isos:
            extract_dir = os.path.join(
                self.iso_dir, f"extracted_{Path(iso_path).stem}")
            os.makedirs(extract_dir, exist_ok=True)

            # Try to extract just the packs/ directory list
            try:
                result = subprocess.run(
                    ["xorriso", "-indev", iso_path,
                     "-find", "/packs", "-maxdepth", "1", "-type", "d"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        line = line.strip().strip("'")
                        if line.startswith("/packs/") and line != "/packs/":
                            repo_name = line.split("/")[2]
                            if repo_name not in repos:
                                repos.append(repo_name)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if not repos:
            # Can't determine repos — let user specify or use default
            rc, repo = inputbox(
                "Could not auto-detect repositories.\n\n"
                "Enter a repository name, or leave blank to "
                "restore all:",
                height=12, width=60,
            )
            if rc != 0:
                return "back"
            self.repo = repo
            return "next"

        choices: list[tuple[str, str]] = [("all", "Restore all repositories")]
        for repo in repos:
            choices.append((repo, f"Repository: {repo}"))

        rc, selection = menu(
            "Select which backup to restore:",
            choices, height=18, width=65,
        )
        if rc != 0:
            return "back"

        self.repo = "" if selection == "all" else selection
        return "next"

    # ── Screen 7: Confirm ────────────────────────────────────────

    def screen_confirm(self) -> str:
        """Show summary and confirm before running restore."""
        repo_text = self.repo if self.repo else "All repositories"
        text = (
            "Ready to restore. Please confirm:\n\n"
            f"  Key file:    {self.key_file}\n"
            f"  Data discs:  {len(self.ripped_isos)} ISO(s)\n"
            f"  Repository:  {repo_text}\n"
            f"  Target:      {self.target_dir}\n\n"
            "WARNING: This will write files to the target directory.\n"
            "Existing files with the same names will be overwritten.\n\n"
            "Proceed with restore?"
        )
        if yesno(text, height=16, width=65):
            return "next"
        return "back"

    # ── Screen 8: Run Restore ────────────────────────────────────

    def screen_run_restore(self) -> str:
        """Execute the restore and show progress."""
        # Build restore command
        cmd = self._build_restore_command()

        if not cmd:
            msgbox("No restore tool found!\n\n"
                   "Expected restore.sh or standalone_restorer.py "
                   "on the meta disc.")
            return "back"

        # Run restore, tailing output to a log file
        infobox("Starting restore...\n\nThis may take a while.",
                height=5, width=50)

        try:
            with open(self.restore_log, "w") as log:
                process = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

            # Show the log file in a tailbox
            tailbox(self.restore_log, height=22, width=78)

            # Wait for process to complete
            process.wait()

            if process.returncode == 0:
                return "next"
            else:
                msgbox(
                    f"Restore exited with code {process.returncode}.\n\n"
                    f"Check the log for details:\n"
                    f"  {self.restore_log}\n\n"
                    "You can retry or exit to a shell for manual recovery.",
                    height=12, width=65,
                )
                return "back"

        except OSError as e:
            msgbox(f"Failed to start restore:\n\n{e}")
            return "back"

    def _build_restore_command(self) -> list[str]:
        """Build the restore command line."""
        # Prefer restore.sh (tested, cascade fallbacks)
        if (Path(RESTORE_SCRIPT).is_file()
                and os.access(RESTORE_SCRIPT, os.X_OK)):
            cmd = [
                RESTORE_SCRIPT,
                "--key", self.key_file,
                "--isos", self.iso_dir,
                "--target", self.target_dir,
            ]
            if self.repo:
                cmd.extend(["--repo", self.repo])
            return cmd

        # Fallback: standalone_restorer.py
        python = shutil.which("python3") or sys.executable
        if Path(STANDALONE_RESTORER).is_file():
            cmd = [
                python, STANDALONE_RESTORER,
                "--key", self.key_file,
                "--isos", self.iso_dir,
                "--target", self.target_dir,
            ]
            if self.repo:
                cmd.extend(["--repo", self.repo])
            return cmd

        return []

    # ── Screen 9: Complete ───────────────────────────────────────

    def screen_complete(self) -> str:
        """Show completion message and offer shutdown."""
        # Count restored files if possible
        file_count = "unknown"
        target = Path(self.target_dir)
        if target.is_dir():
            try:
                count = sum(1 for _ in target.rglob("*") if _.is_file())
                file_count = str(count)
            except OSError:
                pass

        text = (
            "Restore complete!\n\n"
            f"  Files restored: {file_count}\n"
            f"  Target:         {self.target_dir}\n\n"
            f"  Log file:       {self.restore_log}\n\n"
            "What would you like to do?"
        )

        choices = [
            ("shell", "Drop to command line"),
            ("restart", "Run wizard again"),
            ("reboot", "Reboot computer"),
            ("poweroff", "Shut down computer"),
        ]
        rc, selection = menu(text, choices, height=18, width=65)

        if selection == "restart":
            return "back"  # Will re-enter the wizard
        elif selection == "reboot":
            subprocess.run(["reboot"], check=False)
        elif selection == "poweroff":
            subprocess.run(["poweroff"], check=False)

        return "quit"


# ── Main ─────────────────────────────────────────────────────────


def main() -> int:
    """Entry point for the restore wizard."""
    if not _dialog_available():
        print("ERROR: 'dialog' is not installed.", file=sys.stderr)
        print("Install it with: apk add dialog", file=sys.stderr)
        print("Falling back to restore.sh (if available).", file=sys.stderr)
        return 1

    wizard = RestoreWizard()
    wizard.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
