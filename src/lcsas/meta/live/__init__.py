"""Live bootable recovery environment for LCSAS meta-volumes.

Builds a minimal Alpine Linux live system that boots directly into
a TUI restore wizard.  The live environment includes:

* Alpine Linux kernel + initramfs
* Squashfs root filesystem with bundled LCSAS tools
* GRUB2 bootloader (UEFI) + isolinux (Legacy BIOS)
* ``dialog``-based TUI wizard for guided restore

The live system is integrated into the meta-volume ISO via
El Torito boot records, making the disc bootable on both
UEFI and Legacy BIOS systems.
"""
