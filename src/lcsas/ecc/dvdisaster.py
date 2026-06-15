"""Protocol and implementation for DVDisaster ECC operations."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn, Protocol

from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)

# RS03 augmented images cannot take a redundancy setting; dvdisaster pads
# the image up to the smallest fitting medium and fills the slack with
# parity ("Setting the redundancy is not possible due to constraints in
# the format" — man dvdisaster, -n under "RS03 images").  These are the
# medium sizes dvdisaster targets, in 2048-byte sectors × bytes:
# CD 360,000 · DVD 2,295,104 · DVD9 4,171,712 · BD 12,219,392 ·
# BD-DL 24,438,784 · BDXL-TL 48,878,592 (the BD trio matches
# MediaType.{BD25,BD50,BDXL100}.capacity_bytes exactly).
RS03_MEDIUM_LADDER_BYTES: tuple[int, ...] = (
    737_280_000,        # CD (80 min)
    4_700_372_992,      # DVD single layer
    8_543_666_176,      # DVD9 dual layer
    25_025_314_816,     # BD 25 GB single layer
    50_050_629_632,     # BD 50 GB dual layer
    100_103_356_416,    # BDXL 100 GB triple layer
)


def smallest_fitting_medium_bytes(image_bytes: int) -> int:
    """Padded size of an RS03-augmented image of ``image_bytes`` bytes.

    RS03 augmented mode grows the image to the smallest medium on
    :data:`RS03_MEDIUM_LADDER_BYTES` that fits it; the burn pre-flight
    must budget staging space for the *padded* size, not the raw ISO.
    This is an upper bound: dvdisaster rounds the augmented image down
    to whole RS03 layers (multiples of 255 sectors), so the real result
    lands slightly under the nominal medium size (observed: a CD-sized
    pad is 735,836,160 bytes vs the 737,280,000 nominal).

    Raises:
        ValueError: if the image exceeds the largest RS03 medium.
    """
    for medium_bytes in RS03_MEDIUM_LADDER_BYTES:
        if image_bytes <= medium_bytes:
            return medium_bytes
    raise ValueError(
        f"image of {image_bytes:,} bytes exceeds the largest RS03 medium "
        f"({RS03_MEDIUM_LADDER_BYTES[-1]:,} bytes, BDXL 100 GB)"
    )


class DVDisasterRunner(Protocol):
    """Abstract interface for error correction code operations."""

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
    ) -> None: ...

    def verify_iso(
        self,
        iso_path: Path,
    ) -> bool: ...

    def repair_iso(
        self,
        iso_path: Path,
    ) -> bool: ...


class SubprocessDVDisasterRunner(SubprocessRunnerBase):
    """Real DVDisaster implementation using subprocess."""

    def __init__(
        self,
        dvdisaster_binary: str = "dvdisaster",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(dvdisaster_binary, tmpdir)

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
        timeout: int = 7200,
    ) -> None:
        """Augment an ISO image with RS03 error correction data.

        Operates on a temporary copy to avoid corrupting the ISO if the
        process is interrupted.  On success the augmented copy replaces
        the original atomically via ``os.rename``.

        ``redundancy_pct`` is deprecated and **ignored**: RS03 augmented
        images cannot take a redundancy setting — dvdisaster pads the
        image to the smallest fitting medium (see
        :func:`smallest_fitting_medium_bytes`) and the padding *is* the
        effective redundancy.  The parameter is kept for signature
        stability only.
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")

        # Pre-flight: verify there is enough free space for the temp copy.
        iso_size = iso_path.stat().st_size
        disk_free = shutil.disk_usage(iso_path.parent).free
        # Need one full copy of the ISO plus a 1 MiB safety margin.
        if disk_free < iso_size + 1_048_576:
            raise OSError(
                f"Insufficient disk space to create ECC temp copy of '{iso_path.name}': "
                f"{disk_free:,} bytes free, {iso_size + 1_048_576:,} bytes needed."
            )

        tmp = iso_path.with_suffix(".iso.ecc.tmp")
        try:
            shutil.copy2(str(iso_path), str(tmp))
            # No -n: RS03 augmented images reject a redundancy setting and
            # pad to the smallest fitting medium (man dvdisaster).
            cmd = [
                self._binary,
                "-i", str(tmp),
                "-mRS03",
                "-c",
            ]
            try:
                subprocess.run(
                    cmd, capture_output=True, text=True, check=True,
                    env=self._env(), timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                self._handle_timeout("dvdisaster", "ECC augmentation", exc)
            except subprocess.CalledProcessError as exc:
                self._log_stderr("dvdisaster", exc)
                raise
            # Atomic replace on success
            import os
            os.rename(tmp, iso_path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

        padded_size = iso_path.stat().st_size
        effective_pct = (
            (padded_size - iso_size) / iso_size * 100 if iso_size else 0.0
        )
        _logger.info(
            "RS03 ECC: image padded to %s bytes (~%.0f%% effective redundancy)",
            f"{padded_size:,}", effective_pct,
        )

    def verify_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Verify the ECC integrity of an ISO image."""
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-t",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("dvdisaster", "ECC verification", exc)
        return result.returncode == 0

    def repair_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Attempt to repair a damaged ISO using its embedded ECC data.

        Returns True iff the image is intact *after* the repair attempt.

        dvdisaster's ``-f`` exits NONZERO (observed: 1) even when it
        SUCCESSFULLY corrects errors — its exit code conflates "corrected
        some errors" with "failed to correct", so it is not a reliable
        success signal (issue #305).  Rather than reverse-engineer an
        undocumented, version-specific exit-code matrix, we measure the
        outcome directly: ``-f`` fixes the image in place, then we re-run
        verification and return whether the image now passes.  This is
        version-independent and answers the only question a caller cares
        about — is the disc good now?
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [
            self._binary,
            "-i", str(iso_path),
            "-f",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("dvdisaster", "ECC repair", exc)
        if result.returncode != 0:
            _logger.info(
                "dvdisaster -f exited %d for %s; confirming outcome via verify",
                result.returncode, iso_path.name,
            )
        # Ground truth: the repair succeeded iff the image now verifies clean.
        # (-f fixes in place, so this re-reads the same file -f just wrote.)
        return self.verify_iso(iso_path, timeout=timeout)


class LcsasEccRunner(SubprocessRunnerBase):
    """In-house RS03 verify/repair via the bundled ``lcsas-ecc`` binary.

    Implements the :class:`DVDisasterRunner` protocol so operator-side
    ``lcsas verify`` / ``restore exec`` keep working when no dvdisaster is
    installed — the same C89 RS03 decoder that ships on the meta-volume
    tier-1 path (FMT-01).  The *augment* (encode) side is not implemented
    here: writing parity is still dvdisaster's job until the optional
    ``lcsas-ecc augment`` phase-2 encoder lands.  The format is identical,
    so this reads/repairs any dvdisaster-written RS03 image.

    ``lcsas-ecc`` exit-code contract (see recovery/src/lcsas-ecc/main.c):
      0  success (verify: no damage; fix: fully repaired / no repair needed)
      1  damage found (verify) / uncorrectable codewords remain (fix)
      2  no RS03 ECC header (not an augmented image)
      3  usage / I/O / structural error
    """

    def __init__(
        self,
        ecc_binary: str = "lcsas-ecc",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(ecc_binary, tmpdir)

    def augment_iso(
        self,
        iso_path: Path,
        redundancy_pct: int = 15,
    ) -> None:
        """Not supported: lcsas-ecc is decode-only (verify/repair).

        RS03 *encoding* remains dvdisaster's responsibility in the burn
        pipeline.  Callers needing augment must use
        :class:`SubprocessDVDisasterRunner`.
        """
        raise NotImplementedError(
            "lcsas-ecc does not encode RS03 parity (decode-only); "
            "use dvdisaster for the augment step."
        )

    def verify_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Return True iff the image's RS03 data sectors verify clean.

        A missing ECC header (exit 2) or any structural/I/O error (exit 3)
        is surfaced as a failure with a clear exception, never silently
        reported as "intact" — silent success on an unreadable image would
        defeat the disc-integrity guard.
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [self._binary, "verify", str(iso_path)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("lcsas-ecc", "ECC verification", exc)
        if result.returncode in (0, 1):
            return result.returncode == 0
        self._raise_for_ecc_error("verify", iso_path, result)

    def repair_iso(
        self,
        iso_path: Path,
        timeout: int = 3600,
    ) -> bool:
        """Repair the image in place using its embedded RS03 parity.

        Returns True iff the image is intact after the repair attempt.
        ``lcsas-ecc fix`` repairs atomically (it refuses to write a
        partial repair when damage exceeds RS03 capacity), so its exit
        code is an authoritative success signal — unlike dvdisaster ``-f``
        we do not need a re-verify round trip.
        """
        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file not found: {iso_path}")
        cmd = [self._binary, "fix", str(iso_path)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=self._env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._handle_timeout("lcsas-ecc", "ECC repair", exc)
        if result.returncode in (0, 1):
            if result.returncode != 0:
                _logger.info(
                    "lcsas-ecc fix could not fully repair %s "
                    "(damage exceeds RS03 capacity)",
                    iso_path.name,
                )
            return result.returncode == 0
        self._raise_for_ecc_error("fix", iso_path, result)

    @staticmethod
    def _raise_for_ecc_error(
        op: str,
        iso_path: Path,
        result: subprocess.CompletedProcess[str],
    ) -> NoReturn:
        """Translate a non-{0,1} lcsas-ecc exit into a loud RuntimeError."""
        stderr = (result.stderr or "").strip()
        if result.returncode == 2:
            detail = "no RS03 ECC header (not an augmented image)"
        else:
            detail = stderr or "structural or I/O error"
        raise RuntimeError(
            f"lcsas-ecc {op} failed on '{iso_path.name}' "
            f"(exit {result.returncode}): {detail}"
        )


def select_ecc_runner(
    *,
    require_augment: bool = False,
    tmpdir: Path | None = None,
) -> DVDisasterRunner | None:
    """Pick the ECC runner for an operator-side verify/repair path (FMT-01).

    Selection order:

    1. The real ``dvdisaster`` binary if it is on ``PATH`` — it covers
       encode *and* decode and is the byte-exact tool that wrote the
       parity, so prefer it whenever present.
    2. Otherwise the in-house ``lcsas-ecc`` binary if it is on ``PATH`` —
       a verify/repair-only fallback (decode-only) so a host *without*
       dvdisaster still spends the burned RS03 parity instead of skipping
       ECC entirely.  This is the whole point of FMT-01: the repair half
       of the disc-integrity layer must not depend on an abandoned,
       externally-installed tool.
    3. ``None`` when neither is available — the caller then degrades to a
       portable SHA-256 compare (detect-only) or logs "not verified".

    ``require_augment=True`` restricts the choice to runners that can
    *write* parity (encode): only dvdisaster qualifies, because
    :class:`LcsasEccRunner` is decode-only.  Use this on the burn
    (augment) path so a missing dvdisaster is reported as such rather
    than silently selecting a runner that cannot encode.

    The returned object satisfies the :class:`DVDisasterRunner` protocol;
    callers use ``verify_iso`` / ``repair_iso`` (and ``augment_iso`` only
    when ``require_augment`` was set).
    """
    if shutil.which("dvdisaster") is not None:
        return SubprocessDVDisasterRunner(tmpdir=tmpdir)
    if require_augment:
        # Only dvdisaster can encode; do not fall back to a decode-only
        # runner for the augment path.
        return None
    if shutil.which("lcsas-ecc") is not None:
        return LcsasEccRunner(tmpdir=tmpdir)
    return None
