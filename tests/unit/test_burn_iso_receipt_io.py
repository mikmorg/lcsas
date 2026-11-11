"""I/O-contract tests for ``lcsas burn-iso --emit-receipt`` (issues #517, #675).

The receipt is the only durable evidence a remote burn site hands back, so
two properties matter and were untested:

* ``--emit-receipt`` into a path whose parent directories do not exist yet
  must create them (``mkdir(parents=True)``) and write the receipt at the
  exact path given;
* the receipt must be flushed *and* ``fsync``'d before the command returns --
  a power cut right after "Receipt written" must not lose it.

The xorriso runner is fully mocked (including ``media_status`` and
``read_disc_volume_id``), so is the device read-back, and an unplugged
device name is passed, so the
host's real optical drive is never touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.exceptions import BurnError
from lcsas.iso.xorriso import MediaStatus

_RUNNER = "lcsas.iso.xorriso.SubprocessXorrisoRunner"
_FAKE_DEVICE = "/dev/lcsas-test-no-such-drive"
_LABEL = "VOL_RECEIPT_0001"
_ISO_BYTES = b"not-really-an-iso" * 100


@contextlib.contextmanager
def _mock_xorriso(*, verify: bool = True, burn_error: Exception | None = None):
    """Mock every xorriso touchpoint cmd_burn_iso uses, and the #664 device
    read-back (it returns the ISO's hash: the disc matches)."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(
            "lcsas.burn.device_verify.read_device_sha256",
            return_value=hashlib.sha256(_ISO_BYTES).hexdigest(),
        ))
        burn = stack.enter_context(patch(f"{_RUNNER}.burn_iso", side_effect=burn_error))
        stack.enter_context(patch(f"{_RUNNER}.verify_disc", return_value=verify))
        stack.enter_context(patch(f"{_RUNNER}.media_status", return_value=MediaStatus.BLANK))
        stack.enter_context(patch(f"{_RUNNER}.read_disc_volume_id", return_value=_LABEL))
        yield burn


@pytest.fixture
def iso(tmp_path) -> Path:
    path = tmp_path / "staged" / f"{_LABEL}.iso"
    path.parent.mkdir()
    path.write_bytes(_ISO_BYTES)
    return path


def _burn_iso(iso: Path, receipt: Path | str) -> int:
    return main([
        "burn-iso", str(iso),
        "--device", _FAKE_DEVICE,
        "--label", _LABEL,
        "--location", "Offsite_Safe",
        "--session", "sess-1",
        "--emit-receipt", str(receipt),
    ])


# ---------------------------------------------------------------------------
# --emit-receipt into a directory tree that does not exist yet
# ---------------------------------------------------------------------------


class TestEmitReceiptCreatesParents:
    def test_nested_missing_dirs_are_created(self, tmp_path, iso):
        receipt = tmp_path / "out" / "site-b" / "2026" / "receipt.json"
        assert not (tmp_path / "out").exists()  # nothing pre-exists

        with _mock_xorriso():
            rc = _burn_iso(iso, receipt)

        assert rc == 0
        for d in (tmp_path / "out", tmp_path / "out" / "site-b", receipt.parent):
            assert d.is_dir()
        # The last component is the receipt FILE, not another directory.
        assert receipt.is_file()
        data = json.loads(receipt.read_text())
        assert data["volume_label"] == _LABEL
        assert data["location"] == "Offsite_Safe"
        assert data["session_id"] == "sess-1"
        assert data["device"] == _FAKE_DEVICE
        assert data["verify_passed"] is True
        assert data["iso_sha256"] == hashlib.sha256(_ISO_BYTES).hexdigest()
        assert data["iso_size_bytes"] == len(_ISO_BYTES)
        # Nothing else was dropped next to it (no auto-named sibling file).
        assert [p.name for p in receipt.parent.iterdir()] == ["receipt.json"]

    def test_single_missing_parent_is_created(self, tmp_path, iso):
        receipt = tmp_path / "receipts" / "r.json"

        with _mock_xorriso():
            assert _burn_iso(iso, receipt) == 0

        assert json.loads(receipt.read_text())["volume_label"] == _LABEL

    def test_existing_parent_dir_is_fine(self, tmp_path, iso):
        (tmp_path / "already").mkdir()
        receipt = tmp_path / "already" / "r.json"

        with _mock_xorriso():
            assert _burn_iso(iso, receipt) == 0

        assert receipt.is_file()

    def test_existing_receipt_of_the_same_disc_is_overwritten(self, tmp_path, iso):
        """A re-run for the same volume and location replaces its receipt."""
        receipt = tmp_path / "r.json"
        receipt.write_text(json.dumps({
            "volume_label": _LABEL, "location": "Offsite_Safe",
            "verify_passed": False,
        }))

        with _mock_xorriso():
            assert _burn_iso(iso, receipt) == 0

        assert json.loads(receipt.read_text())["verify_passed"] is True

    def test_existing_receipt_of_another_volume_is_refused(self, tmp_path, iso, capsys):
        """#675: another volume's receipt is kept, and the refusal comes
        before the burn, so no disc is left without a receipt.  (This test
        used to expect the overwrite.)"""
        receipt = tmp_path / "r.json"
        stale = json.dumps({"volume_label": "STALE", "location": "Offsite_Safe"})
        receipt.write_text(stale)

        with _mock_xorriso() as burn:
            rc = _burn_iso(iso, receipt)

        assert rc == 1
        burn.assert_not_called()
        assert receipt.read_text() == stale
        out = capsys.readouterr().out
        assert "refusing to overwrite" in out
        assert "STALE" in out


# ---------------------------------------------------------------------------
# #675: a --emit-receipt folder that does not exist yet
# ---------------------------------------------------------------------------


class TestEmitReceiptMissingFolder:
    def test_trailing_separator_makes_a_folder(self, tmp_path, iso):
        target = tmp_path / "receipts"
        with _mock_xorriso():
            assert _burn_iso(iso, f"{target}{os.sep}") == 0

        assert target.is_dir()
        written = target / f"{_LABEL}_Offsite_Safe.json"
        assert json.loads(written.read_text())["volume_label"] == _LABEL

    def test_name_without_extension_makes_a_folder(self, tmp_path, iso):
        target = tmp_path / "receipts"
        with _mock_xorriso():
            assert _burn_iso(iso, str(target)) == 0

        assert target.is_dir()
        assert [p.name for p in target.iterdir()] == [f"{_LABEL}_Offsite_Safe.json"]

    def test_three_burns_leave_three_receipts(self, tmp_path):
        """The reported failure: each burn overwrote one file named
        'receipts', so only the last receipt survived."""
        target = tmp_path / "receipts"
        for n in range(3):
            label = f"VOL_RECEIPT_000{n}"
            iso = tmp_path / f"{label}.iso"
            iso.write_bytes(_ISO_BYTES)
            with _mock_xorriso(), patch(
                f"{_RUNNER}.read_disc_volume_id", return_value=label,
            ):
                assert main([
                    "burn-iso", str(iso), "--device", _FAKE_DEVICE,
                    "--label", label, "--location", "Offsite_Safe",
                    "--emit-receipt", str(target),
                ]) == 0

        assert sorted(p.name for p in target.iterdir()) == [
            f"VOL_RECEIPT_000{n}_Offsite_Safe.json" for n in range(3)
        ]

    def test_existing_file_without_extension_is_still_the_file(self, tmp_path, iso):
        """An existing file is never turned into a folder; it is replaced
        only when it holds this disc's receipt."""
        target = tmp_path / "receipt"
        target.write_text(json.dumps(
            {"volume_label": _LABEL, "location": "Offsite_Safe"}))
        with _mock_xorriso():
            assert _burn_iso(iso, str(target)) == 0

        assert target.is_file()
        assert json.loads(target.read_text())["verify_passed"] is True

    def test_failed_burn_leaves_no_receipt_and_creates_no_dirs(self, tmp_path, iso):
        """A receipt must only ever exist for a burn that actually ran."""
        receipt = tmp_path / "out" / "deep" / "receipt.json"

        with _mock_xorriso(burn_error=BurnError("xorriso exploded")) as burn:
            rc = _burn_iso(iso, receipt)

        burn.assert_called_once()
        assert rc == 1
        assert not receipt.exists()
        assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# fsync of the receipt
# ---------------------------------------------------------------------------


class _FsyncSpy:
    """Record what each ``os.fsync`` call was pointed at, then do the real sync.

    Raw fd numbers are useless for identification (closed fds get reused), so
    each call is described by the file's (st_dev, st_ino) and its size *at the
    moment of the call*.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []  # (st_dev, st_ino, st_size)
        self._real = os.fsync

    def __call__(self, fd: int) -> None:
        st = os.fstat(fd)  # raises if the fd is already closed -> test fails
        self.calls.append((st.st_dev, st.st_ino, st.st_size))
        self._real(fd)

    def calls_for(self, path: Path) -> list[tuple[int, int, int]]:
        st = path.stat()
        return [c for c in self.calls if c[:2] == (st.st_dev, st.st_ino)]


class TestReceiptIsFsynced:
    def test_receipt_file_is_fsynced_with_all_data_already_flushed(self, tmp_path, iso):
        receipt = tmp_path / "nested" / "receipt.json"
        spy = _FsyncSpy()

        with _mock_xorriso(), patch("os.fsync", side_effect=spy):
            assert _burn_iso(iso, receipt) == 0

        calls = spy.calls_for(receipt)
        assert len(calls) == 1, f"receipt fd fsync'd {len(calls)}x; all calls: {spy.calls}"
        final_size = receipt.stat().st_size
        assert final_size > 0
        # flush() must precede fsync(): at fsync time the kernel already holds
        # every byte, otherwise fsync would persist an empty/partial file.
        assert calls[0][2] == final_size

    def test_fsynced_receipt_is_complete_valid_json(self, tmp_path, iso):
        receipt = tmp_path / "r.json"
        spy = _FsyncSpy()

        with _mock_xorriso(), patch("os.fsync", side_effect=spy):
            _burn_iso(iso, receipt)

        assert spy.calls_for(receipt)
        assert json.loads(receipt.read_text())["iso_sha256"]

    def test_receipt_of_a_failed_verify_is_fsynced_too(self, tmp_path, iso):
        """The evidence of a bad burn is as worth keeping as that of a good one."""
        receipt = tmp_path / "r.json"
        spy = _FsyncSpy()

        with _mock_xorriso(verify=False), patch("os.fsync", side_effect=spy):
            rc = _burn_iso(iso, receipt)

        assert rc == 1  # --verify is on by default
        assert len(spy.calls_for(receipt)) == 1
        assert json.loads(receipt.read_text())["verify_passed"] is False

    def test_directory_target_receipt_is_fsynced_too(self, tmp_path, iso):
        out_dir = tmp_path / "receipts"
        out_dir.mkdir()
        spy = _FsyncSpy()

        with _mock_xorriso(), patch("os.fsync", side_effect=spy):
            assert _burn_iso(iso, out_dir) == 0

        written = out_dir / f"{_LABEL}_Offsite_Safe.json"
        assert written.is_file()
        calls = spy.calls_for(written)
        assert len(calls) == 1
        assert calls[0][2] == written.stat().st_size

    def test_no_receipt_requested_means_no_fsync(self, tmp_path, iso):
        """Proves the spy only sees the receipt's fsync, not unrelated ones."""
        spy = _FsyncSpy()

        with _mock_xorriso(), patch("os.fsync", side_effect=spy):
            rc = main(["burn-iso", str(iso), "--device", _FAKE_DEVICE, "--label", _LABEL])

        assert rc == 0
        assert spy.calls == []
