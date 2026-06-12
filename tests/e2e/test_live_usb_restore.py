"""Live-USB replacement-path drill: Secure-Boot QEMU boot of a CURRENT
signed live image, then a tier-1 restore off the attached meta image
[BOOT-04].

BOOT-01 dropped the never-built bootable-meta-disc stack; the no-OS
recovery story is now "boot any CURRENT live-Linux image, then run
restore.sh from the META disc".  The old story was never tested and
turned out to be fiction (BOOT-08).  This test makes the replacement
*trustworthy* by proving its load-bearing property end to end:

  * OVMF with the Microsoft-keys-enrolled Secure-Boot variables
    (``OVMF_CODE_4M.ms.fd`` / ``OVMF_VARS_4M.ms.fd``) boots the stock,
    vendor-signed Ubuntu Server ISO -- the exact signed shim/GRUB/kernel
    chain the rewritten docs rely on for 2035+ consumer hardware.  The
    guest asserts ``SecureBoot=1`` from efivars onto the serial console.
  * A tiny LCSAS archive (one rustic repo, one data ISO, one production
    meta ISO) is attached as additional cdroms; an autoinstall
    early-command mounts them and runs the documented
    ``sh .../recovery/scripts/restore.sh <target> latest`` invocation.
  * The guest echoes ``LCSAS_LIVE_USB_OK <content-set-hash>`` to the
    serial console; the host asserts the hash matches the source data.

SLOW + opt-in (pattern of ``LCSAS_ECC_REPAIR=1``): gated behind
``LCSAS_LIVE_USB_SMOKE=1``, never runs in the default suite::

    LCSAS_LIVE_USB_SMOKE=1 pytest tests/e2e/test_live_usb_restore.py -v

Fixtures (~3.4 GB Ubuntu ISO) are cached under /scratch/lcsas-fixtures
(override: ``LCSAS_LIVE_USB_FIXTURES``) and pinned by URL + SHA-256 in
``tests/e2e/fixtures/live_usb_pins.txt``; the test REFUSES to run (fails,
not skips) on a checksum mismatch so fixture drift is never silent.

Runs under KVM when /dev/kvm is usable, TCG otherwise (this repo's dev
VM is itself a libvirt guest with no nested KVM -- the test must pass
under TCG; expect ~10-20 min).  GRUB keystrokes are injected over QMP
``sendkey``, synchronised on the serial log: OVMF mirrors its console
onto the serial port, so the GRUB menu/shell are visible there long
before the guest kernel switches to ttyS0 via the injected ``console=``
argument.  A cmdline edit does not break Secure Boot -- only binaries
are signed.  Weekly CI: .github/workflows/live-usb-smoke.yml.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
    pytest.mark.skipif(
        not os.environ.get("LCSAS_LIVE_USB_SMOKE"),
        reason="set LCSAS_LIVE_USB_SMOKE=1 to run the slow live-USB Secure-Boot "
        "QEMU drill (~3.4 GB cached fixture; minutes of VM boot)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
PINS_FILE = REPO_ROOT / "tests" / "e2e" / "fixtures" / "live_usb_pins.txt"

# Big artifacts (pinned ISO cache + work tree with the staged meta volume)
# deliberately do NOT use pytest's tmp_path: on the dev VM that lands on
# the small / partition.  /scratch is the designated big-and-disposable
# area; CI overrides via LCSAS_LIVE_USB_FIXTURES.
FIXTURE_DIR = Path(os.environ.get("LCSAS_LIVE_USB_FIXTURES", "/scratch/lcsas-fixtures"))

QEMU_BIN = "qemu-system-x86_64"
# Secure-Boot-enabled OVMF with Microsoft keys enrolled (ubuntu `ovmf`
# package).  The .ms variants are the property under test: a stock
# consumer machine's firmware trusts exactly this key set.
OVMF_CODE = Path(os.environ.get("LCSAS_OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.ms.fd"))
OVMF_VARS = Path(os.environ.get("LCSAS_OVMF_VARS", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"))

TENANT = "alpha"
NUM_FILES = 6
FILE_BYTES = 80 * 1024  # 6 x 80 KB packs to ~480 KB -> one TEST_TINY volume

# Anchored at line start: subiquity prints the early-command SOURCE to the
# console before running it, so an unanchored search would match the
# `echo "${m}_OK $h"`-style script text (or the set -x trace lines, which
# carry a "+ " prefix) instead of the real emission.  Belt-and-braces, the
# guest script also never spells the markers contiguously in its source.
_OK_RE = re.compile(r"^LCSAS_LIVE_USB_OK ([0-9a-f]{64})", re.M)
_FAIL_RE = re.compile(r"^LCSAS_LIVE_USB_FAIL\b", re.M)
_SB_RE = re.compile(r"^LCSAS_SB_STATE (\S+)", re.M)

# Guest-side early-command.  @TOKENS@ substituted at seed-build time --
# .replace(), not str.format, because the script itself uses {} (find).
# Markers are echoed straight to /dev/ttyS0 so they reach the host even
# if subiquity's own console handling changes.
_GUEST_SCRIPT = """\
set -x
exec >/dev/ttyS0 2>&1
echo LCSAS_EARLY_BEGIN
sb=unknown
for v in /sys/firmware/efi/efivars/SecureBoot-*; do
  [ -e "$v" ] && sb=$(od -An -tu1 -j4 -N1 "$v" | tr -d " ")
done
echo "LCSAS_SB_STATE $sb"
mount_by_label() {
  ml_i=0
  while [ "$ml_i" -lt 30 ]; do
    if [ -e "/dev/disk/by-label/$1" ] \\
       && mount -o ro "/dev/disk/by-label/$1" "$2"; then
      return 0
    fi
    for ml_d in /dev/sr0 /dev/sr1 /dev/sr2 /dev/sr3; do
      [ -b "$ml_d" ] || continue
      if [ "$(blkid -o value -s LABEL "$ml_d" 2>/dev/null)" = "$1" ] \\
         && mount -o ro "$ml_d" "$2"; then
        return 0
      fi
    done
    sleep 2
    ml_i=$((ml_i + 1))
  done
  echo "LCSAS_MOUNT_FAIL $1"
  return 1
}
mkdir -p /media/lcsas-meta /media/lcsas-data /restored
mount_by_label LCSAS_META /media/lcsas-meta
mount_by_label "@DATA_LABEL@" /media/lcsas-data
export LCSAS_PASSWORD='@PASSWORD@'
export LCSAS_REPO='@TENANT@'
export LCSAS_ALLOW_TMPFS_TARGET=1
m=LCSAS_LIVE_USB
if sh /media/lcsas-meta/recovery/scripts/restore.sh /restored latest \\
      </dev/null >/run/lcsas-restore.log 2>&1; then
  h=$(find /restored -type f -exec sha256sum {} + \\
      | awk '{print $1}' | sort | sha256sum | cut -d" " -f1)
  echo "${m}_OK $h"
else
  rc=$?
  echo "${m}_FAIL rc=$rc"
  tail -n 80 /run/lcsas-restore.log
fi
"""


# ---------------------------------------------------------------------------
# Pin + fixture handling
# ---------------------------------------------------------------------------


def _load_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in PINS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.strip()
    for required in ("ISO_URL", "ISO_SHA256"):
        assert required in pins, f"{PINS_FILE} is missing {required}="
    return pins


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fetch_pinned_iso(pins: dict[str, str]) -> Path:
    """Return the cached live ISO, downloading + verifying if needed.

    A SHA-256 mismatch is a hard FAILURE (not a skip): either the cache
    was tampered with / corrupted, or upstream content drifted under a
    pinned URL.  Both must be loud (plan BOOT-04: no silent fixture drift).
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    iso = FIXTURE_DIR / Path(pins["ISO_URL"]).name
    if not iso.exists():
        part = iso.with_suffix(iso.suffix + ".part")
        with urllib.request.urlopen(pins["ISO_URL"]) as resp, open(part, "wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        part.rename(iso)
    actual = _sha256_file(iso)
    if actual != pins["ISO_SHA256"]:
        pytest.fail(
            f"pinned live ISO checksum mismatch for {iso}:\n"
            f"  expected {pins['ISO_SHA256']} (tests/e2e/fixtures/live_usb_pins.txt)\n"
            f"  actual   {actual}\n"
            "Refusing to run. Delete the cached file to re-download, or bump "
            "the pin deliberately if upstream shipped a new point release."
        )
    return iso


# ---------------------------------------------------------------------------
# Tiny LCSAS archive (one repo -> one data ISO + production meta ISO)
# ---------------------------------------------------------------------------


@dataclass
class _Archive:
    meta_iso: Path
    data_iso: Path
    data_label: str
    password: str
    expected_hash: str


def _content_set_hash(root: Path) -> str:
    """Path-independent content hash, mirroring the guest-side pipeline:
    sha256 of the sorted newline-joined per-file sha256 hex digests."""
    digests = sorted(
        hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    )
    payload = "".join(d + "\n" for d in digests)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _build_archive(work: Path) -> _Archive:
    """Build the LCSAS fixture archive, as the blind-restore harness does
    (tests/e2e/cdemu_blind_restore/setup.py), scaled down to ONE data disc
    so the unattended guest never has to swap media."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.db.connection import get_connection
    from lcsas.db.queries import get_unarchived_packs
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.iso.xorriso import SubprocessXorrisoRunner
    from lcsas.meta.builder import MetaVolumeBuilder
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs
    from lcsas.staging.metadata import MIN_HOLOGRAPHIC_RESERVE_BYTES

    sources = work / "sources" / TENANT
    sources.mkdir(parents=True)
    for i in range(NUM_FILES):
        (sources / f"file{i:02d}.bin").write_bytes(os.urandom(FILE_BYTES))
    expected_hash = _content_set_hash(sources)

    password = os.urandom(16).hex()
    pw_file = work / f"{TENANT}.pw"
    pw_file.write_text(password)
    pw_file.chmod(0o600)

    mirror = work / "mirror" / TENANT
    mirror.mkdir(parents=True)
    env = {
        **os.environ,
        "RUSTIC_REPOSITORY": str(mirror),
        "RUSTIC_PASSWORD_FILE": str(pw_file),
    }
    subprocess.run(["rustic", "init"], env=env, check=True, capture_output=True)
    subprocess.run(
        [
            "rustic", "config",
            "--set-datapack-size", "256KiB",
            "--set-datapack-size-limit", "512KiB",
            "--set-treepack-size", "128KiB",
            "--set-treepack-size-limit", "256KiB",
        ],
        env=env, check=True, capture_output=True,
    )
    subprocess.run(
        ["rustic", "backup", str(sources)], env=env, check=True, capture_output=True
    )

    db_path = work / "catalog.db"
    conn = get_connection(db_path)
    create_all(conn)
    register_repo(conn, TENANT, TENANT, str(mirror))
    conn.commit()
    delta = DeltaAnalyzer(conn, scan_mirror_packs(mirror).packs, repo_id=TENANT)
    delta.register_new_packs()
    conn.commit()

    config = LCSASConfig(
        mirror_base_path=work / "mirror",
        staging_path=work / "staging",
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="LCSAS",
        metadata_reserve_bytes=MIN_HOLOGRAPHIC_RESERVE_BYTES,
        repositories={
            TENANT: RepositoryConfig(
                name=TENANT, mirror_path=mirror, password_file=pw_file
            )
        },
    )

    class _NoOpEcc:
        def augment_iso(self, iso_path: Path, redundancy_pct: int = 15) -> None:
            pass

        def verify_iso(self, iso_path: Path) -> bool:
            return True

        def repair_iso(self, iso_path: Path) -> bool:
            return True

    orchestrator = BurnOrchestrator(config, conn, SubprocessXorrisoRunner(), _NoOpEcc())
    iso_out = work / "iso_out"
    iso_out.mkdir()
    data_isos: list[Path] = []
    labels: list[str] = []
    while get_unarchived_packs(conn):
        manifest = orchestrator.prepare(media_type=MediaType.TEST_TINY)
        iso_path = iso_out / f"{manifest.volume_label}.iso"
        orchestrator.execute(manifest, iso_output=iso_path, skip_burn=True)
        data_isos.append(iso_path)
        labels.append(manifest.volume_label)
    conn.close()
    assert len(data_isos) == 1, (
        f"fixture split across {len(data_isos)} data volumes; the unattended "
        "guest can only mount a fixed set of cdroms -- shrink NUM_FILES/"
        "FILE_BYTES so everything fits one TEST_TINY volume"
    )

    meta_stage = work / "meta_stage"
    meta_stage.mkdir()
    MetaVolumeBuilder(meta_stage, catalog_db_path=db_path).build()
    meta_iso = iso_out / "LCSAS_META.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-V", "LCSAS_META", "-R", "-J",
         "-o", str(meta_iso), str(meta_stage)],
        check=True, capture_output=True,
    )
    return _Archive(
        meta_iso=meta_iso,
        data_iso=data_isos[0],
        data_label=labels[0],
        password=password,
        expected_hash=expected_hash,
    )


def _build_seed_iso(work: Path, archive: _Archive) -> Path:
    """NoCloud (CIDATA) seed carrying the autoinstall early-command."""
    seed_dir = work / "seed"
    seed_dir.mkdir()
    script = (
        _GUEST_SCRIPT
        .replace("@DATA_LABEL@", archive.data_label)
        .replace("@PASSWORD@", archive.password)
        .replace("@TENANT@", TENANT)
    )
    indented = "".join(f"      {line}\n" for line in script.splitlines())
    (seed_dir / "user-data").write_text(
        "#cloud-config\n"
        "autoinstall:\n"
        "  version: 1\n"
        "  early-commands:\n"
        "    - |\n"
        f"{indented}",
        encoding="utf-8",
    )
    (seed_dir / "meta-data").write_text("instance-id: lcsas-live-usb\n", encoding="utf-8")
    seed_iso = work / "seed.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-V", "CIDATA", "-J", "-R",
         "-o", str(seed_iso), str(seed_dir)],
        check=True, capture_output=True,
    )
    return seed_iso


# ---------------------------------------------------------------------------
# QEMU / QMP driving
# ---------------------------------------------------------------------------


class _Qmp:
    """Minimal QMP client -- just enough for blind sendkey injection."""

    def __init__(self, sock_path: Path, connect_timeout: float = 60.0) -> None:
        deadline = time.monotonic() + connect_timeout
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        while True:
            try:
                self._sock.connect(str(sock_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self._fp = self._sock.makefile("rb")
        self._read_obj()  # greeting
        self._cmd("qmp_capabilities")

    def _read_obj(self) -> dict[str, object]:
        line = self._fp.readline()
        if not line:
            raise ConnectionError("QMP socket closed (QEMU died?)")
        return json.loads(line)  # type: ignore[no-any-return]

    def _cmd(self, name: str, **arguments: object) -> None:
        payload: dict[str, object] = {"execute": name}
        if arguments:
            payload["arguments"] = arguments
        self._sock.sendall(json.dumps(payload).encode() + b"\n")
        while True:  # skip async events until our response arrives
            obj = self._read_obj()
            if "return" in obj:
                return
            if "error" in obj:
                raise RuntimeError(f"QMP {name} failed: {obj['error']}")

    def sendkey(self, key: str) -> None:
        self._cmd("human-monitor-command", **{"command-line": f"sendkey {key}"})

    def close(self) -> None:
        self._sock.close()


_CHAR_KEYS = {" ": "spc", "=": "equal", "-": "minus", ".": "dot", "/": "slash"}


def _keys_for(text: str) -> list[str]:
    keys = []
    for ch in text:
        if ch in _CHAR_KEYS:
            keys.append(_CHAR_KEYS[ch])
        elif ch.isupper():
            keys.append(f"shift-{ch.lower()}")
        else:
            keys.append(ch)
    return keys


# Cursor-addressed screen drawing + simple escapes; stripping these from
# the serial log leaves the plain text GRUB rendered.
_ANSI_RE = re.compile(r"\x1b\[[0-9;=?]*[A-Za-z]|\x1b[=>]|\r")


def _serial_text(serial_log: Path) -> str:
    if not serial_log.exists():
        return ""
    return _ANSI_RE.sub("", serial_log.read_text(encoding="utf-8", errors="replace"))


def _wait_serial(serial_log: Path, predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(_serial_text(serial_log)):
            return
        time.sleep(2)
    pytest.fail(
        f"timed out after {timeout:.0f}s waiting for {what}\n"
        f"--- last serial lines ---\n{_tail(serial_log)}"
    )


def _inject_grub_cmdline(
    qmp: _Qmp, serial_log: Path, menu_timeout: float, key_delay: float
) -> None:
    """Drive GRUB over QMP sendkey, synchronised on the serial mirror.

    OVMF multiplexes its console onto the serial port, so the GRUB menu
    and shell are visible in the serial log long before the kernel
    switches to ttyS0.  Wait for the menu, freeze its 30 s countdown
    (Up keeps the selection put), then drop to the GRUB shell ('c') and
    type the casper boot commands directly.  Each command is typed only
    after the previous ``grub>`` prompt returned: key injection into a
    busy GRUB (reading a ~70 MB initrd off the cdrom) gets dropped, and
    blind menu-editor line navigation proved fragile (a trailing blank
    editor line put the appended args on the initrd line).  A cmdline
    typed at the shell does not violate Secure Boot -- only binaries are
    signature-checked, and the signed shim still verifies the kernel
    that ``linux`` loads.
    """
    _wait_serial(
        serial_log,
        lambda t: "Try or Install" in t,
        menu_timeout,
        "the GRUB menu on the serial console",
    )
    qmp.sendkey("up")  # any key freezes the countdown; Up keeps entry 1 selected
    time.sleep(1.0)
    qmp.sendkey("c")

    def type_line(line: str) -> None:
        for key in _keys_for(line):
            qmp.sendkey(key)
            time.sleep(key_delay)
        qmp.sendkey("ret")

    def prompt_count(text: str) -> int:
        return text.count("grub>")

    _wait_serial(serial_log, lambda t: prompt_count(t) >= 1, 60, "the GRUB shell prompt")
    type_line("linux /casper/vmlinuz autoinstall console=ttyS0 ---")
    _wait_serial(
        serial_log, lambda t: prompt_count(t) >= 2, 300,
        "the kernel to load (grub> prompt #2)",
    )
    type_line("initrd /casper/initrd")
    _wait_serial(
        serial_log, lambda t: prompt_count(t) >= 3, 600,
        "the initrd to load (grub> prompt #3)",
    )
    type_line("boot")


def _tail(path: Path, lines: int = 100) -> str:
    if not path.exists():
        return "<no serial output at all>"
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------


def test_live_usb_secure_boot_restore():
    if shutil.which(QEMU_BIN) is None:
        pytest.skip(f"{QEMU_BIN} not installed (apt install qemu-system-x86)")
    if not (OVMF_CODE.is_file() and OVMF_VARS.is_file()):
        pytest.skip(
            f"Secure-Boot OVMF (.ms.fd) not found at {OVMF_CODE} / {OVMF_VARS} "
            "(apt install ovmf, or point LCSAS_OVMF_CODE/LCSAS_OVMF_VARS at them)"
        )

    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    # TCG (no nested KVM on the dev VM) is several times slower than KVM:
    # scale both the GRUB-appearance window and the overall deadline.
    menu_timeout = 60.0 if kvm else 240.0
    total_deadline = (12 * 60) if kvm else (25 * 60)
    key_delay = 0.15 if kvm else 0.4

    iso = _fetch_pinned_iso(_load_pins())

    work = FIXTURE_DIR / "live-usb-work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    archive = _build_archive(work)
    seed_iso = _build_seed_iso(work, archive)

    vars_copy = work / "OVMF_VARS.ms.fd"
    shutil.copy2(OVMF_VARS, vars_copy)
    target_img = work / "target.img"
    with open(target_img, "wb") as f:
        f.truncate(8 << 30)  # sparse install target for subiquity's defaults
    serial_log = work / "serial.log"
    qmp_sock = work / "qmp.sock"

    qemu_cmd = [
        QEMU_BIN,
        "-name", "lcsas-live-usb-smoke",
        "-M", "q35",
        "-m", "4096",
        "-smp", "4",
        "-accel", "kvm" if kvm else "tcg,thread=multi",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={vars_copy}",
        "-drive", f"media=cdrom,format=raw,file={iso}",
        "-drive", f"media=cdrom,format=raw,file={archive.meta_iso}",
        "-drive", f"media=cdrom,format=raw,file={archive.data_iso}",
        "-drive", f"media=cdrom,format=raw,file={seed_iso}",
        "-drive", f"if=virtio,format=raw,file={target_img}",
        "-nic", "user,model=virtio-net-pci",
        "-serial", f"file:{serial_log}",
        "-qmp", f"unix:{qmp_sock},server,nowait",
        "-display", "none",
    ]

    started = time.monotonic()
    proc = subprocess.Popen(qemu_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        qmp = _Qmp(qmp_sock)
        _inject_grub_cmdline(qmp, serial_log, menu_timeout, key_delay)

        marker: str | None = None
        while time.monotonic() - started < total_deadline:
            if proc.poll() is not None:
                _, stderr = proc.communicate(timeout=10)
                pytest.fail(
                    f"QEMU exited early (rc={proc.returncode}): "
                    f"{stderr.decode(errors='replace')[-2000:]}\n"
                    f"--- last serial lines ---\n{_tail(serial_log)}"
                )
            text = _serial_text(serial_log)
            if _OK_RE.search(text) or _FAIL_RE.search(text):
                marker = text
                break
            time.sleep(5)

        if marker is None:
            pytest.fail(
                f"no LCSAS_LIVE_USB_{{OK,FAIL}} marker on the serial console within "
                f"{total_deadline}s ({'KVM' if kvm else 'TCG'}). If the log below is "
                "near-empty, the blind GRUB keystroke injection most likely missed "
                f"the menu.\n--- last serial lines ---\n{_tail(serial_log)}"
            )

        # Secure Boot must be ENFORCED, not merely tolerated: the guest read
        # the SecureBoot efivar and echoed its value.  0 means someone booted
        # non-.ms firmware -- exactly the regression this drill exists to catch.
        assert "1" in _SB_RE.findall(marker), (
            "guest did not report SecureBoot=1 -- the live image booted without "
            "Secure Boot enforcement (wrong OVMF code/vars?).\n"
            f"--- last serial lines ---\n{_tail(serial_log)}"
        )

        ok = _OK_RE.search(marker)
        assert ok, (
            "restore.sh failed inside the live guest "
            "(LCSAS_LIVE_USB_FAIL):\n"
            f"--- last serial lines ---\n{_tail(serial_log, 150)}"
        )
        assert ok.group(1) == archive.expected_hash, (
            f"restored content-set hash {ok.group(1)} != expected "
            f"{archive.expected_hash} -- restore completed but produced wrong data"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)

    shutil.rmtree(work)  # keep the work tree only when something failed above
