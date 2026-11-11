"""``lcsas burn-iso --verify`` reads the disc back and compares it with the
ISO (#664), and ``--verify-only`` does that for a disc burned earlier (#673).

The "drive" is a plain file that holds the burned image: the real
``read_device_sha256`` reads it as it would read ``/dev/srN``.  Only the
xorriso calls are mocked (burn, ``-check_media``, the volume ID and the
blank check), so the host's optical drive is never touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.cli.main import main
from lcsas.iso.xorriso import MediaStatus

_RUNNER = "lcsas.iso.xorriso.SubprocessXorrisoRunner"
_LABEL = "VOL_READBACK_0001"
_SECTOR = 2048


@pytest.fixture
def iso(tmp_path) -> Path:
    """A sector-aligned image of 64 sectors with varied content."""
    path = tmp_path / "staged" / f"{_LABEL}.iso"
    path.parent.mkdir()
    path.write_bytes(bytes(i % 251 for i in range(64 * _SECTOR)))
    return path


@contextlib.contextmanager
def _drive(*, check_media: bool = True,
           disc_id: str = _LABEL) -> Iterator[dict[str, MagicMock]]:
    with contextlib.ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"{_RUNNER}.{name}", **kw))
            for name, kw in (
                ("burn_iso", {}),
                ("verify_disc", {"return_value": check_media}),
                ("read_disc_volume_id", {"return_value": disc_id}),
                ("media_status", {"return_value": MediaStatus.BLANK}),
            )
        }
        yield mocks


def _run(iso: Path, device: Path, out: Path, *extra: str) -> int:
    return main([
        "burn-iso", str(iso), "--device", str(device), "--label", _LABEL,
        "--emit-receipt", str(out), "--location", "Offsite_Safe", *extra,
    ])


def _receipt(out: Path) -> dict[str, object]:
    data = json.loads((out / f"{_LABEL}_Offsite_Safe.json").read_text())
    assert isinstance(data, dict)
    return data


class TestBurnIsoReadBack:
    def test_matching_disc_passes_and_records_the_hash_read_back(self, tmp_path, iso):
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes())
        out = tmp_path / "receipts"

        with _drive():
            assert _run(iso, device, out) == 0

        receipt = _receipt(out)
        want = hashlib.sha256(iso.read_bytes()).hexdigest()
        assert receipt["verify_passed"] is True
        assert receipt["iso_sha256"] == want
        assert receipt["device_sha256"] == want
        assert receipt["verify_only"] is False

    def test_disc_that_differs_from_the_iso_fails(self, tmp_path, iso, capsys):
        """The review's experiment: part of the image zeroed.  -check_media
        and the volume ID still pass (mocked True here); the read-back
        does not."""
        data = bytearray(iso.read_bytes())
        data[20 * _SECTOR:40 * _SECTOR] = bytes(20 * _SECTOR)
        device = tmp_path / "drive.img"
        device.write_bytes(bytes(data))
        out = tmp_path / "receipts"

        with _drive():
            assert _run(iso, device, out) == 1

        receipt = _receipt(out)
        assert receipt["verify_passed"] is False
        assert receipt["device_sha256"] == hashlib.sha256(bytes(data)).hexdigest()
        assert receipt["device_sha256"] != receipt["iso_sha256"]
        assert "does not match the ISO" in capsys.readouterr().out

    def test_reads_exactly_the_iso_length(self, tmp_path, iso):
        """A drive reports more than the image (padding after it): only the
        ISO's own length is read and hashed."""
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes() + b"\xff" * (8 * _SECTOR))
        out = tmp_path / "receipts"

        with _drive():
            assert _run(iso, device, out) == 0

        assert _receipt(out)["device_sha256"] == _receipt(out)["iso_sha256"]

    def test_disc_shorter_than_the_iso_fails(self, tmp_path, iso):
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes()[: 32 * _SECTOR])
        out = tmp_path / "receipts"

        with _drive():
            assert _run(iso, device, out) == 1

        receipt = _receipt(out)
        assert receipt["verify_passed"] is False
        assert receipt["device_sha256"] is None

    def test_unreadable_disc_is_not_read_back(self, tmp_path, iso):
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes())
        out = tmp_path / "receipts"

        with _drive(check_media=False), patch(
            "lcsas.burn.device_verify.read_device_sha256",
        ) as reader:
            assert _run(iso, device, out) == 1

        reader.assert_not_called()
        assert _receipt(out)["verify_passed"] is False

    def test_wrong_disc_is_not_read_back(self, tmp_path, iso):
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes())
        out = tmp_path / "receipts"

        with _drive(disc_id="SOME_OTHER_VOLUME"), patch(
            "lcsas.burn.device_verify.read_device_sha256",
        ) as reader:
            assert _run(iso, device, out) == 1

        reader.assert_not_called()
        assert _receipt(out)["device_sha256"] is None

    def test_read_back_without_a_receipt_still_sets_the_exit_code(self, tmp_path, iso):
        data = bytearray(iso.read_bytes())
        data[-1] ^= 0xFF
        device = tmp_path / "drive.img"
        device.write_bytes(bytes(data))

        with _drive():
            assert main(["burn-iso", str(iso), "--device", str(device),
                         "--label", _LABEL]) == 1

    def test_no_verify_reads_nothing_back(self, tmp_path, iso):
        out = tmp_path / "receipts"
        with _drive() as mocks, patch(
            "lcsas.burn.device_verify.read_device_sha256",
        ) as reader:
            assert _run(iso, tmp_path / "no-drive", out, "--no-verify") == 0

        reader.assert_not_called()
        mocks["verify_disc"].assert_not_called()
        receipt = _receipt(out)
        assert receipt["verify_passed"] is None
        assert receipt["device_sha256"] is None


class TestVerifyOnly:
    def test_checks_the_disc_without_burning(self, tmp_path, iso):
        device = tmp_path / "drive.img"
        device.write_bytes(iso.read_bytes())
        out = tmp_path / "receipts"

        with _drive() as mocks:
            assert _run(iso, device, out, "--verify-only") == 0

        mocks["burn_iso"].assert_not_called()
        # The disc already holds data: the blank check must not refuse it.
        mocks["media_status"].assert_not_called()
        receipt = _receipt(out)
        assert receipt["verify_only"] is True
        assert receipt["verify_passed"] is True
        assert receipt["device_sha256"] == receipt["iso_sha256"]

    def test_a_disc_that_differs_fails(self, tmp_path, iso):
        data = bytearray(iso.read_bytes())
        data[40 * _SECTOR] ^= 0x01
        device = tmp_path / "drive.img"
        device.write_bytes(bytes(data))
        out = tmp_path / "receipts"

        with _drive() as mocks:
            assert _run(iso, device, out, "--verify-only") == 1

        mocks["burn_iso"].assert_not_called()
        assert _receipt(out)["verify_passed"] is False

    def test_cannot_be_combined_with_no_verify(self, tmp_path, iso, capsys):
        with _drive() as mocks:
            rc = _run(iso, tmp_path / "drive.img", tmp_path / "receipts",
                      "--verify-only", "--no-verify")

        assert rc == 1
        mocks["burn_iso"].assert_not_called()
        assert not (tmp_path / "receipts").exists()
        assert "--verify-only" in capsys.readouterr().out
