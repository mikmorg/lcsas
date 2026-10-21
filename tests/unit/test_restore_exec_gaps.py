"""Gap tests for ``lcsas restore exec`` (issue #517).

Pins the two ``restore exec`` Gaps bullets in
``docs/workflows/restore-host-linux.md``:

* "No test verifies the ``ShutdownManager`` cleans the temp cache on SIGINT
  mid-ingest" -- (a) a ``KeyboardInterrupt`` raised mid-fetch removes the
  auto-created temp cache (and only that: an explicit ``--cache-dir`` is the
  operator's and survives, which is what makes a re-run resumable), and a real
  SIGINT / SIGTERM delivered mid-ingest runs the registered cleanup *before*
  the signal is re-raised -- for SIGTERM that is the only thing that removes the
  cache, because the re-raised default action kills the process without ever
  reaching the ``finally:``;
* "No CLI-level test covers the ``select_ecc_runner()`` wiring inside
  ``restore exec``" -- (b) the runner ``select_ecc_runner()`` returns is the one
  the executor uses to verify (and, on failure, repair) the volume's ISO, and
  its verdict decides whether the restore proceeds.

Everything runs against fakes (no rustic, xorriso, dvdisaster or optical
device), the same way ``test_cli_restore.py`` drives ``cmd_restore_exec``.
"""

from __future__ import annotations

import argparse
import hashlib
import signal
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.cli.main import cmd_restore_exec
from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import ensure_location
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.restore.executor import RestoreError, RestoreExecutor
from lcsas.rustic.types import RestorePlan
from lcsas.utils.labels import generate_uuid

_LABEL = "VOL_001"
_PACK = "aa" * 32
_SELECT_ECC = "lcsas.ecc.dvdisaster.select_ecc_runner"


class _Env:
    """A catalog with one pack on one VERIFIED volume, plus fake collaborators."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.staging = tmp / "staging"
        self.staging.mkdir()
        self.volumes = tmp / "volumes"
        pack_dir = self.volumes / _LABEL / "data" / _PACK[:2]
        pack_dir.mkdir(parents=True)
        (pack_dir / _PACK).write_bytes(b"pack-bytes")
        self.iso = self.volumes / f"{_LABEL}.iso"  # sibling the CLI looks for

        mirror = tmp / "mirror"
        for sub in ("index", "snapshots", "keys"):
            (mirror / sub).mkdir(parents=True)
        (mirror / "config").write_text("{}")
        self.key = tmp / "key"
        self.key.write_bytes(b"secret")

        self.select = MagicMock()
        self.conn = get_memory_connection()
        create_all(self.conn)
        register_repo(self.conn, "family", "family", str(mirror), "")
        pack = register_pack(self.conn, _PACK, 1024, "family")
        self.volume = create_volume(
            self.conn, _LABEL, generate_uuid(), "TEST_TINY", 1_000_000,
            "Home_Shelf", "VERIFIED",
        )
        bulk_link_packs(self.conn, self.volume.volume_id, [pack.pack_id])

        self.runner = MagicMock()
        self.runner.restore_dry_run.return_value = RestorePlan(
            snapshot_id="snap1", required_pack_hashes=[_PACK],
        )
        self.config = MagicMock()
        self.config.db_path = Path(":memory:")
        self.config.staging_path = self.staging
        self.config.repositories = {
            "family": MagicMock(mirror_path=mirror, password_file=self.key),
        }

    def args(self, **overrides) -> argparse.Namespace:
        ns = {
            "config": Path("config.toml"), "db": None, "command": "restore",
            "restore_command": "exec", "repo": "family", "snapshot_id": "snap1",
            "target_path": self.tmp / "restored", "password_file": self.key,
            "cache_dir": None, "volume_dir": self.volumes, "iso_dir": None,
            "skip_verify": True,
        }
        ns.update(overrides)
        return argparse.Namespace(**ns)

    def run(self, args: argparse.Namespace, ecc: object | None = None) -> int:
        """Run ``cmd_restore_exec``; ``select_ecc_runner()`` yields ``ecc``.

        ``self.select`` is the mock standing in for ``select_ecc_runner`` so a
        test can assert it was consulted.  The default (``None``) is the
        "no ECC tool on this host" answer, which keeps the signal tests from
        probing for real dvdisaster / lcsas-ecc binaries.
        """
        self.select = MagicMock(return_value=ecc)
        with (
            patch("lcsas.config.settings.load_config", return_value=self.config),
            patch("lcsas.db.connection.get_connection", return_value=self.conn),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner",
                  return_value=self.runner),
            patch("lcsas.utils.subprocess.check_binary_version"),
            patch(_SELECT_ECC, self.select),
        ):
            return cmd_restore_exec(args)

    def temp_caches(self) -> list[Path]:
        return sorted(self.staging.glob("lcsas-restore-*"))


@pytest.fixture
def env(tmp_path) -> _Env:
    return _Env(tmp_path)


@pytest.fixture
def signals_restored() -> Iterator[None]:
    """Put SIGINT/SIGTERM handlers back no matter what a test did to them."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# (a) interrupt cleanup
# ---------------------------------------------------------------------------


class TestInterruptCleanup:
    def test_keyboard_interrupt_mid_ingest_removes_the_temp_cache(
        self, env, signals_restored,
    ):
        seen: dict[str, Path] = {}
        handlers_before = {
            s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)
        }

        def interrupted(self, cache_dir, *_a, **_kw):
            seen["cache"] = cache_dir
            assert cache_dir.is_dir()
            assert env.temp_caches() == [cache_dir]
            raise KeyboardInterrupt

        with (
            patch.object(RestoreExecutor, "ingest_volume", interrupted),
            pytest.raises(KeyboardInterrupt) as excinfo,
        ):
            env.run(env.args())

        # ``excinfo`` still references the traceback, hence cmd_restore_exec's
        # frame and its TemporaryDirectory: if the cache is gone now, the
        # ``finally:`` removed it explicitly -- TemporaryDirectory's own
        # garbage-collection finaliser cannot have done it for us.
        assert excinfo.traceback
        assert seen["cache"].parent == env.staging
        assert not seen["cache"].exists()
        assert env.temp_caches() == []
        env.runner.restore.assert_not_called()
        # The signal handlers ShutdownManager installed were taken back out.
        assert {
            s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)
        } == handlers_before

    def test_keyboard_interrupt_keeps_an_explicit_cache_dir(
        self, env, signals_restored,
    ):
        """``--cache-dir`` is the operator's: whatever was ingested stays, so a
        re-run resumes instead of re-reading every disc."""
        cache = env.tmp / "my-cache"
        survivor = cache / "data" / _PACK[:2] / _PACK
        survivor.parent.mkdir(parents=True)
        survivor.write_bytes(b"already ingested")

        def interrupted(self, cache_dir, *_a, **_kw):
            assert cache_dir == cache
            raise KeyboardInterrupt

        with (
            patch.object(RestoreExecutor, "ingest_volume", interrupted),
            pytest.raises(KeyboardInterrupt),
        ):
            env.run(env.args(cache_dir=cache))

        assert survivor.read_bytes() == b"already ingested"
        assert (cache / "index").is_dir()  # prepare_cache's metadata is kept too
        assert env.temp_caches() == []  # and no temp cache was created beside it

    @pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=lambda s: s.name)
    def test_signal_mid_ingest_runs_the_cleanup_before_re_raising(
        self, env, signals_restored, sig,
    ):
        """What a real Ctrl-C / ``kill`` does mid-ingest.

        ``ShutdownManager._handler`` runs the registered cleanups, restores the
        previous handler, then re-raises the signal.  The previous handler is
        stood in for by ``previous`` below, which records whether the temp
        cache was already gone *at the moment of re-raise* -- i.e. before any
        ``finally:`` could have removed it.  Under a real SIGTERM the previous
        handler is SIG_DFL, which terminates the process; the ``finally:`` never
        runs, so this registered cleanup is the only thing that removes the
        cache.
        """
        class _Delivered(BaseException):
            pass

        state: dict[str, object] = {}

        def previous(signum, _frame):
            state["signum"] = signum
            state["cache_gone_at_reraise"] = not state["cache"].exists()
            raise _Delivered

        signal.signal(sig, previous)

        def signalled(self, cache_dir, *_a, **_kw):
            state["cache"] = cache_dir
            assert cache_dir.is_dir()
            # restore exec took over the signal for the duration of the run
            assert signal.getsignal(sig) not in (previous, signal.SIG_DFL)
            signal.raise_signal(sig)
            raise AssertionError("the signal handler did not interrupt ingest")

        with (
            patch.object(RestoreExecutor, "ingest_volume", signalled),
            pytest.raises(_Delivered),
        ):
            env.run(env.args())

        assert state["signum"] == sig
        assert state["cache_gone_at_reraise"] is True
        assert env.temp_caches() == []
        assert signal.getsignal(sig) is previous  # uninstalled again
        env.runner.restore.assert_not_called()

    def test_completed_restore_removes_the_temp_cache_too(self, env):
        """Control: the happy path also leaves no temp cache behind (so the
        interrupt tests above are not passing on a cache that was never made)."""
        created: list[Path] = []
        real_ingest = RestoreExecutor.ingest_volume

        def spy(self, cache_dir, *a, **kw):
            created.append(cache_dir)
            return real_ingest(self, cache_dir, *a, **kw)

        with patch.object(RestoreExecutor, "ingest_volume", spy):
            rc = env.run(env.args())

        assert rc == 0
        assert len(created) == 1 and created[0].parent == env.staging
        assert not created[0].exists()
        env.runner.restore.assert_called_once()


# ---------------------------------------------------------------------------
# (b) select_ecc_runner wiring
# ---------------------------------------------------------------------------


class _FakeEcc:
    """An ECCRunner whose verdicts the test dictates and whose calls it records."""

    def __init__(self, *, verify: bool = True, repair: bool = True) -> None:
        self._verify, self._repair = verify, repair
        self.verified: list[Path] = []
        self.repaired: list[Path] = []

    def verify_iso(self, iso_path: Path) -> bool:
        self.verified.append(iso_path)
        return self._verify

    def repair_iso(self, iso_path: Path) -> bool:
        self.repaired.append(iso_path)
        return self._repair


class TestSelectEccRunnerWiring:
    @pytest.fixture(autouse=True)
    def _iso_beside_the_volume(self, env: _Env) -> None:
        env.iso.write_bytes(b"pretend this is an RS03-augmented ISO")

    def test_the_selected_runner_verifies_the_volumes_iso(self, env):
        ecc = _FakeEcc()

        rc = env.run(env.args(), ecc=ecc)

        assert rc == 0
        env.select.assert_called_once_with()
        assert ecc.verified == [env.iso]
        assert ecc.repaired == []  # verify passed: no repair attempted
        env.runner.restore.assert_called_once()

    def test_a_failed_verify_is_repaired_by_the_same_runner_and_restore_goes_on(
        self, env,
    ):
        ecc = _FakeEcc(verify=False, repair=True)

        rc = env.run(env.args(), ecc=ecc)

        assert rc == 0
        assert ecc.verified == [env.iso]
        assert ecc.repaired == [env.iso]
        env.runner.restore.assert_called_once()

    def test_when_verify_and_repair_both_fail_the_restore_is_refused(self, env):
        ecc = _FakeEcc(verify=False, repair=False)

        with pytest.raises(RestoreError, match="ECC verification and repair both failed"):
            env.run(env.args(), ecc=ecc)

        assert ecc.repaired == [env.iso]
        env.runner.restore.assert_not_called()
        assert env.temp_caches() == []  # and the refused run cleaned up after itself

    def test_no_runner_available_falls_back_to_the_recorded_iso_sha256(self, env):
        """``select_ecc_runner()`` -> ``None``: no ECC call is possible, but the
        catalog-recorded ISO hash still guards the volume (detect-only)."""
        ensure_location(env.conn, "Home_Shelf")
        good = hashlib.sha256(env.iso.read_bytes()).hexdigest()
        add_volume_copy(
            env.conn, env.volume.volume_id, "Home_Shelf", iso_sha256=good,
        )

        rc = env.run(env.args(), ecc=None)

        env.select.assert_called_once_with()
        assert rc == 0
        env.runner.restore.assert_called_once()

    def test_no_runner_and_a_wrong_recorded_hash_refuses_the_restore(self, env):
        ensure_location(env.conn, "Home_Shelf")
        add_volume_copy(
            env.conn, env.volume.volume_id, "Home_Shelf", iso_sha256="0" * 64,
        )

        with pytest.raises(RestoreError, match="ISO SHA-256 mismatch"):
            env.run(env.args(), ecc=None)

        env.runner.restore.assert_not_called()

    def test_no_sibling_iso_means_no_ecc_call_even_with_a_runner(self, env):
        """Documented limit: packs read from a bare directory get per-pack
        SHA-256 only; ECC needs a reachable ``<label>.iso``."""
        env.iso.unlink()
        ecc = _FakeEcc(verify=False, repair=False)  # would refuse if it were asked

        rc = env.run(env.args(), ecc=ecc)

        assert rc == 0
        assert ecc.verified == [] and ecc.repaired == []

