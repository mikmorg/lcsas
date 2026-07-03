"""Tests for DVDisaster wrapper (mocked subprocess)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from lcsas.ecc.dvdisaster import (
    MIN_EFFECTIVE_REDUNDANCY_PCT,
    RS03_MEDIUM_LADDER_BYTES,
    LcsasEccRunner,
    SubprocessDVDisasterRunner,
    _log_effective_redundancy,
    smallest_fitting_medium_bytes,
)


class TestEffectiveRedundancyWarning:
    """Issue #371: a volume packed nearly to the medium ceiling gets
    almost no RS03 parity.  That dilution must surface as a WARNING, not
    a buried INFO line."""

    def test_thin_redundancy_warns(self, caplog):
        # padded only 1% above the data → below the floor.
        iso = 100_000_000
        with caplog.at_level(logging.INFO, logger="lcsas.ecc.dvdisaster"):
            _log_effective_redundancy(int(iso * 1.01), iso)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "thin redundancy did not warn"
        assert "thin bit-rot protection" in warnings[0].getMessage()

    def test_adequate_redundancy_is_info_only(self, caplog):
        iso = 100_000_000
        pct = MIN_EFFECTIVE_REDUNDANCY_PCT + 10
        with caplog.at_level(logging.INFO, logger="lcsas.ecc.dvdisaster"):
            _log_effective_redundancy(int(iso * (1 + pct / 100)), iso)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        infos = [r for r in caplog.records if r.levelname == "INFO"]
        assert infos and "effective redundancy" in infos[0].getMessage()

    def test_floor_boundary_at_exactly_floor_is_info(self, caplog):
        iso = 100_000_000
        padded = int(iso * (1 + MIN_EFFECTIVE_REDUNDANCY_PCT / 100))
        with caplog.at_level(logging.INFO, logger="lcsas.ecc.dvdisaster"):
            _log_effective_redundancy(padded, iso)
        # exactly-at-floor is acceptable (>= floor) → INFO, not WARNING.
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_lcsas_ecc_tool_label_in_message(self, caplog):
        iso = 100_000_000
        with caplog.at_level(logging.INFO, logger="lcsas.ecc.dvdisaster"):
            _log_effective_redundancy(int(iso * 1.20), iso, tool=" (lcsas-ecc)")
        assert any("(lcsas-ecc)" in r.getMessage() for r in caplog.records)


class TestDVDisasterMocked:
    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_args(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)  # dummy ISO file

        runner.augment_iso(iso, redundancy_pct=20)

        args = mock_run.call_args[0][0]
        assert "dvdisaster" in args[0]
        assert "-mRS03" in args
        assert "-c" in args
        # BURN-07: -n must NOT be passed for RS03 augmented images.  Per the
        # dvdisaster manual, "Setting the redundancy is not possible due to
        # constraints in the format. The codec will automatically choose the
        # size of the smallest fitting medium." — a -n here is a placebo at
        # best (and would mean 20 *roots*, not 20%, in ECC-file mode).
        # Re-adding it must be a deliberate act.
        assert "-n" not in args
        assert "20" not in args
        # augment_iso now works on a temp copy then renames; verify the
        # original path is not passed (temp copy is).
        # Just verify dvdisaster was called.

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_success(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.verify_iso(iso) is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.verify_iso(iso) is False

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_clean_exit_then_verifies_clean(self, mock_run, tmp_path):
        # repair_iso runs `-f` then re-verifies (issue #305). `-f` exits 0,
        # the confirming `-t` verify reports clean -> True.
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.repair_iso(iso) is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_nonzero_exit_but_image_recovered(self, mock_run, tmp_path):
        # The #305 scenario: `-f` exits NONZERO even though it successfully
        # corrected the errors; the confirming `-t` verify reports the image
        # is now clean. repair_iso must trust the verify, not the exit code.
        mock_run.side_effect = [MagicMock(returncode=1), MagicMock(returncode=0)]
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.repair_iso(iso) is True

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_unrecoverable(self, mock_run, tmp_path):
        # `-f` exits nonzero AND the confirming `-t` verify still reports
        # corruption (damage beyond ECC capacity) -> False.
        mock_run.side_effect = [MagicMock(returncode=1), MagicMock(returncode=13)]
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)
        assert runner.repair_iso(iso) is False

    def test_check_binary_raises_when_not_on_path(self):
        """check_binary raises RuntimeError when dvdisaster is not on PATH."""
        runner = SubprocessDVDisasterRunner()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="dvdisaster"),
        ):
            runner.check_binary()

    def test_check_binary_passes_when_on_path(self):
        """check_binary succeeds silently when dvdisaster exists on PATH."""
        runner = SubprocessDVDisasterRunner()
        with patch("shutil.which", return_value="/usr/bin/dvdisaster"):
            runner.check_binary()  # should not raise

    def test_augment_raises_when_insufficient_disk_space(self, tmp_path):
        """augment_iso raises OSError when there is not enough free disk space."""
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "big.iso"
        iso.write_bytes(b"\x00" * 1024)  # 1 KiB ISO

        # Simulate a disk with only 512 bytes free (less than ISO + 1 MiB margin)
        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=512),
            ),
            pytest.raises(OSError, match="Insufficient disk space"),
        ):
            runner.augment_iso(iso)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_succeeds_when_sufficient_disk_space(self, mock_run, tmp_path):
        """augment_iso proceeds normally when disk space is adequate."""
        mock_run.return_value = MagicMock(returncode=0)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        # Simulate 1 GiB free — more than enough
        with patch(
            "lcsas.ecc.dvdisaster.shutil.disk_usage",
            return_value=MagicMock(free=1_073_741_824),
        ):
            runner.augment_iso(iso)  # should not raise

        mock_run.assert_called_once()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_called_process_error_propagates(self, mock_run, tmp_path):
        """augment_iso propagates CalledProcessError from dvdisaster."""
        import subprocess as sp
        mock_run.side_effect = sp.CalledProcessError(1, "dvdisaster")
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(sp.CalledProcessError),
        ):
            runner.augment_iso(iso)

        # Temp file must be cleaned up on failure
        assert not iso.with_suffix(".iso.ecc.tmp").exists()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_timeout_raises(self, mock_run, tmp_path):
        """augment_iso raises RuntimeError when dvdisaster times out."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("dvdisaster", 7200)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            runner.augment_iso(iso, timeout=1)

        # Temp file must be cleaned up on timeout
        assert not iso.with_suffix(".iso.ecc.tmp").exists()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_timeout_raises(self, mock_run, tmp_path):
        """verify_iso raises RuntimeError when dvdisaster times out."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("dvdisaster", 3600)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with pytest.raises(RuntimeError, match="timed out"):
            runner.verify_iso(iso, timeout=1)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_timeout_raises(self, mock_run, tmp_path):
        """repair_iso raises RuntimeError when dvdisaster times out."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("dvdisaster", 3600)
        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with pytest.raises(RuntimeError, match="timed out"):
            runner.repair_iso(iso, timeout=1)

    def test_augment_missing_file_raises(self, tmp_path):
        """augment_iso raises FileNotFoundError for a missing ISO."""
        runner = SubprocessDVDisasterRunner()
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.augment_iso(tmp_path / "absent.iso")

    def test_verify_missing_file_raises(self, tmp_path):
        """verify_iso raises FileNotFoundError for a missing ISO."""
        runner = SubprocessDVDisasterRunner()
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.verify_iso(tmp_path / "absent.iso")

    def test_repair_missing_file_raises(self, tmp_path):
        """repair_iso raises FileNotFoundError for a missing ISO."""
        runner = SubprocessDVDisasterRunner()
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.repair_iso(tmp_path / "absent.iso")


class TestSmallestFittingMedium:
    """RS03 medium ladder used by the staging pre-flight (BURN-07)."""

    CD, DVD, DVD9, BD25, BD50, BDXL100 = RS03_MEDIUM_LADDER_BYTES

    def test_smallest_fitting_medium_ladder(self):
        # 1 byte → CD
        assert smallest_fitting_medium_bytes(1) == self.CD
        # exactly CD → CD; one past → DVD
        assert smallest_fitting_medium_bytes(self.CD) == self.CD
        assert smallest_fitting_medium_bytes(self.CD + 1) == self.DVD
        # one past DVD9 → BD25
        assert smallest_fitting_medium_bytes(self.DVD9 + 1) == self.BD25
        # one past BD50 → BDXL100
        assert smallest_fitting_medium_bytes(self.BD50 + 1) == self.BDXL100
        # beyond the largest medium → loud failure
        with pytest.raises(ValueError, match="largest RS03 medium"):
            smallest_fitting_medium_bytes(self.BDXL100 + 1)

    def test_ladder_is_sector_aligned_and_ascending(self):
        # dvdisaster sizes media in 2048-byte sectors; the ladder must be
        # strictly ascending or the smallest-fit search breaks.
        for size in RS03_MEDIUM_LADDER_BYTES:
            assert size % 2048 == 0
        assert list(RS03_MEDIUM_LADDER_BYTES) == sorted(
            set(RS03_MEDIUM_LADDER_BYTES)
        )


class TestLcsasEccRunner:
    """In-house lcsas-ecc verify/repair runner (FMT-01).

    The exit-code contract (recovery/src/lcsas-ecc/main.c):
      0 ok · 1 damage/uncorrectable · 2 no header · 3 usage/I-O.
    """

    def _iso(self, tmp_path):
        iso = tmp_path / "disc.iso"
        iso.write_bytes(b"\x00" * 1024)
        return iso

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_clean_calls_subcommand(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        runner = LcsasEccRunner()
        iso = self._iso(tmp_path)
        assert runner.verify_iso(iso) is True
        args = mock_run.call_args[0][0]
        assert "lcsas-ecc" in args[0]
        assert args[1] == "verify"
        assert args[2] == str(iso)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_damage_returns_false(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stderr="DAMAGE")
        runner = LcsasEccRunner()
        assert runner.verify_iso(self._iso(tmp_path)) is False

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_no_header_raises_not_silent_success(self, mock_run, tmp_path):
        # Exit 2 (not an augmented image) must be loud, never reported as
        # "intact" — that would defeat the disc-integrity guard.
        mock_run.return_value = MagicMock(returncode=2, stderr="no RS03 header")
        runner = LcsasEccRunner()
        with pytest.raises(RuntimeError, match="no RS03 ECC header"):
            runner.verify_iso(self._iso(tmp_path))

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_io_error_raises(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=3, stderr="short read")
        runner = LcsasEccRunner()
        with pytest.raises(RuntimeError, match="exit 3"):
            runner.verify_iso(self._iso(tmp_path))

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_success(self, mock_run, tmp_path):
        # Unlike dvdisaster -f, lcsas-ecc fix is authoritative (atomic): a
        # single call, exit 0 == repaired.  No re-verify round trip.
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        runner = LcsasEccRunner()
        iso = self._iso(tmp_path)
        assert runner.repair_iso(iso) is True
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert args[1] == "fix"

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_uncorrectable_returns_false(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stderr="uncorrectable")
        runner = LcsasEccRunner()
        assert runner.repair_iso(self._iso(tmp_path)) is False

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_no_header_raises(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=2, stderr="")
        runner = LcsasEccRunner()
        with pytest.raises(RuntimeError, match="no RS03 ECC header"):
            runner.repair_iso(self._iso(tmp_path))

    def test_augment_invokes_lcsas_ecc(self, tmp_path):
        """FMT-01 phase 2: augment_iso is now implemented (was a decode-only
        NotImplementedError) -- it shells out to ``lcsas-ecc augment`` and
        atomically replaces the original with the augmented image."""
        iso = self._iso(tmp_path)

        def _fake_run(cmd, **kw):
            # The encoder writes the --out path; create it so the atomic
            # rename onto the original succeeds under the mock.
            out = cmd[cmd.index("--out") + 1]
            with open(out, "wb") as fh:
                fh.write(b"\x00" * 4096)
            return MagicMock(returncode=0)

        with (
            patch("lcsas.ecc.dvdisaster.subprocess.run",
                  side_effect=_fake_run) as mock_run,
            patch("lcsas.ecc.dvdisaster.shutil.disk_usage",
                  return_value=MagicMock(free=1_073_741_824)),
        ):
            LcsasEccRunner().augment_iso(iso)  # must not raise
        cmd = mock_run.call_args[0][0]
        assert "augment" in cmd and "--out" in cmd

    def test_verify_missing_file_raises(self, tmp_path):
        runner = LcsasEccRunner()
        with pytest.raises(FileNotFoundError):
            runner.verify_iso(tmp_path / "absent.iso")

    def test_augment_missing_file_raises(self, tmp_path):
        runner = LcsasEccRunner()
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.augment_iso(tmp_path / "absent.iso")

    def test_repair_missing_file_raises(self, tmp_path):
        runner = LcsasEccRunner()
        with pytest.raises(FileNotFoundError, match="ISO file not found"):
            runner.repair_iso(tmp_path / "absent.iso")

    def test_augment_image_too_large_raises_oserror(self, tmp_path):
        """An ISO larger than the biggest RS03 medium → ValueError inside
        smallest_fitting_medium_bytes, re-raised as OSError so the burn
        pre-flight surfaces it as a disk/IO problem."""
        iso = self._iso(tmp_path)
        runner = LcsasEccRunner()
        with (
            patch(
                "lcsas.ecc.dvdisaster.smallest_fitting_medium_bytes",
                side_effect=ValueError("image exceeds the largest RS03 medium"),
            ),
            pytest.raises(OSError, match="largest RS03 medium"),
        ):
            runner.augment_iso(iso)

    def test_augment_insufficient_disk_space_raises(self, tmp_path):
        """augment_iso budgets the *padded* full-medium size; too little free
        space → OSError before the subprocess is ever launched."""
        iso = self._iso(tmp_path)
        runner = LcsasEccRunner()
        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=512),
            ),
            pytest.raises(OSError, match="Insufficient disk space to augment"),
        ):
            runner.augment_iso(iso)

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_timeout_raises(self, mock_run, tmp_path):
        """augment_iso raises RuntimeError when lcsas-ecc times out, and
        cleans up the temp file."""
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="lcsas-ecc", timeout=1)
        iso = self._iso(tmp_path)
        runner = LcsasEccRunner()
        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            runner.augment_iso(iso, timeout=1)
        assert not iso.with_suffix(".iso.ecc.tmp").exists()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_augment_nonzero_exit_raises_and_cleans_up(self, mock_run, tmp_path):
        """A non-zero lcsas-ecc augment exit is surfaced via
        _raise_for_ecc_error, and the partial temp file is removed."""
        iso = self._iso(tmp_path)

        def _fake_run(cmd, **kw):
            out = cmd[cmd.index("--out") + 1]
            with open(out, "wb") as fh:
                fh.write(b"\x00" * 4096)
            return MagicMock(returncode=3, stderr="encode error")

        runner = LcsasEccRunner()
        with (
            patch("lcsas.ecc.dvdisaster.subprocess.run", side_effect=_fake_run),
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=1_073_741_824),
            ),
            pytest.raises(RuntimeError, match="lcsas-ecc augment failed"),
        ):
            runner.augment_iso(iso)
        assert not iso.with_suffix(".iso.ecc.tmp").exists()

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_repair_timeout_raises(self, mock_run, tmp_path):
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="lcsas-ecc", timeout=1)
        runner = LcsasEccRunner()
        with pytest.raises(RuntimeError, match="timed out"):
            runner.repair_iso(self._iso(tmp_path))

    @patch("lcsas.ecc.dvdisaster.subprocess.run")
    def test_verify_timeout_raises(self, mock_run, tmp_path):
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="lcsas-ecc", timeout=1)
        runner = LcsasEccRunner()
        with pytest.raises(RuntimeError, match="timed out"):
            runner.verify_iso(self._iso(tmp_path))

    def test_satisfies_dvdisaster_protocol(self):
        from lcsas.ecc.dvdisaster import DVDisasterRunner

        runner: DVDisasterRunner = LcsasEccRunner()
        assert hasattr(runner, "augment_iso")
        assert hasattr(runner, "verify_iso")
        assert hasattr(runner, "repair_iso")


class TestSelectEccRunner:
    """select_ecc_runner() picks dvdisaster > lcsas-ecc > None (FMT-01)."""

    @patch("lcsas.ecc.dvdisaster.shutil.which")
    def test_prefers_dvdisaster_when_present(self, mock_which):
        from lcsas.ecc.dvdisaster import select_ecc_runner

        # dvdisaster present (lcsas-ecc would also be, but must not be reached).
        mock_which.side_effect = lambda name: (
            "/usr/bin/dvdisaster" if name == "dvdisaster" else None
        )
        runner = select_ecc_runner()
        assert isinstance(runner, SubprocessDVDisasterRunner)

    @patch("lcsas.ecc.dvdisaster.shutil.which")
    def test_falls_back_to_lcsas_ecc_when_dvdisaster_absent(self, mock_which):
        from lcsas.ecc.dvdisaster import select_ecc_runner

        mock_which.side_effect = lambda name: (
            "/opt/lcsas/lcsas-ecc" if name == "lcsas-ecc" else None
        )
        runner = select_ecc_runner()
        assert isinstance(runner, LcsasEccRunner)

    @patch("lcsas.ecc.dvdisaster.shutil.which")
    def test_returns_none_when_neither_present(self, mock_which):
        from lcsas.ecc.dvdisaster import select_ecc_runner

        mock_which.return_value = None
        assert select_ecc_runner() is None

    @patch("lcsas.ecc.dvdisaster.shutil.which")
    def test_require_augment_accepts_lcsas_ecc(self, mock_which):
        from lcsas.ecc.dvdisaster import select_ecc_runner

        # FMT-01 phase 2: lcsas-ecc can now ENCODE (augment), so with only
        # lcsas-ecc present require_augment=True returns the in-house runner
        # (was None pre-phase-2, when the in-house tool was decode-only).
        mock_which.side_effect = lambda name: (
            "/opt/lcsas/lcsas-ecc" if name == "lcsas-ecc" else None
        )
        runner = select_ecc_runner(require_augment=True)
        assert isinstance(runner, LcsasEccRunner)

    @patch("lcsas.ecc.dvdisaster.shutil.which")
    def test_require_augment_accepts_dvdisaster(self, mock_which):
        from lcsas.ecc.dvdisaster import select_ecc_runner

        mock_which.side_effect = lambda name: (
            "/usr/bin/dvdisaster" if name == "dvdisaster" else None
        )
        runner = select_ecc_runner(require_augment=True)
        assert isinstance(runner, SubprocessDVDisasterRunner)
