"""Tests for restore/executor.py — cache assembly and restore execution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lcsas.restore.executor import RestoreExecutor


@pytest.fixture
def mock_rustic():
    return MagicMock()


@pytest.fixture
def executor(mock_rustic):
    return RestoreExecutor(mock_rustic)


@pytest.fixture
def metadata_source(tmp_path):
    """Create a fake metadata source with index/snapshots/keys/config."""
    src = tmp_path / "metadata_src"
    for subdir in ["index", "snapshots", "keys"]:
        d = src / subdir
        d.mkdir(parents=True)
        (d / "data.json").write_text('{"test": true}')
    (src / "config").write_text('{"version": 2}')
    return src


# =========================================================================
# prepare_cache()
# =========================================================================


class TestPrepareCache:
    def test_creates_cache_structure(self, executor, metadata_source, tmp_path):
        """Creates cache dir with data/, index/, snapshots/, keys/, config."""
        cache = tmp_path / "cache"
        executor.prepare_cache(cache, metadata_source)

        assert cache.is_dir()
        assert (cache / "data").is_dir()
        assert (cache / "index").is_dir()
        assert (cache / "snapshots").is_dir()
        assert (cache / "keys").is_dir()
        assert (cache / "config").is_file()

    def test_copies_metadata_contents(self, executor, metadata_source, tmp_path):
        """Metadata files are actually copied."""
        cache = tmp_path / "cache"
        executor.prepare_cache(cache, metadata_source)

        assert (cache / "index" / "data.json").read_text() == '{"test": true}'
        assert (cache / "config").read_text() == '{"version": 2}'

    def test_idempotent_does_not_overwrite(self, executor, metadata_source, tmp_path):
        """Calling prepare_cache twice doesn't overwrite existing dirs."""
        cache = tmp_path / "cache"
        executor.prepare_cache(cache, metadata_source)

        # Modify a file to prove it's not overwritten
        (cache / "index" / "data.json").write_text("MODIFIED")

        executor.prepare_cache(cache, metadata_source)
        assert (cache / "index" / "data.json").read_text() == "MODIFIED"

    def test_missing_subdir_raises(self, executor, tmp_path):
        """Missing source subdirectories raise FileNotFoundError with a clear message."""
        cache = tmp_path / "cache"
        source = tmp_path / "partial_metadata"
        source.mkdir()
        # Only create index/, skip snapshots/ and keys/
        (source / "index").mkdir()
        (source / "index" / "data.json").write_text("{}")

        with pytest.raises(FileNotFoundError, match="snapshots"):
            executor.prepare_cache(cache, source)

    def test_missing_all_subdirs_raises(self, executor, tmp_path):
        """Completely missing metadata raises FileNotFoundError."""
        cache = tmp_path / "cache"
        source = tmp_path / "empty_metadata"
        source.mkdir()

        with pytest.raises(FileNotFoundError, match="every disc"):
            executor.prepare_cache(cache, source)

    def test_missing_config_raises(self, executor, tmp_path):
        """Missing repository config raises FileNotFoundError with a clear message."""
        cache = tmp_path / "cache"
        source = tmp_path / "meta_no_config"
        source.mkdir()
        # Create all metadata subdirs but no config file
        for subdir in ("index", "snapshots", "keys"):
            (source / subdir).mkdir()

        with pytest.raises(FileNotFoundError, match="config"):
            executor.prepare_cache(cache, source)


# =========================================================================
# ingest_volume()
# =========================================================================



class TestIngestVolume:
    def _setup_volume(self, tmp_path, layout="flat"):
        """Create a simulated mounted volume with pack files."""
        mount = tmp_path / "volume"
        data_dir = mount / "data"
        data_dir.mkdir(parents=True)

        sha1 = "a" * 64
        sha2 = "b" * 64
        sha3 = "c" * 64

        if layout == "flat":
            (data_dir / sha1).write_bytes(b"pack1_data")
            (data_dir / sha2).write_bytes(b"pack2_data")
        elif layout == "two_level":
            (data_dir / sha1[:2]).mkdir()
            (data_dir / sha1[:2] / sha1).write_bytes(b"pack1_data")
            (data_dir / sha2[:2]).mkdir()
            (data_dir / sha2[:2] / sha2).write_bytes(b"pack2_data")

        return mount, [sha1, sha2, sha3]

    def test_flat_layout_ingest(self, executor, tmp_path):
        """Ingest packs from flat data layout."""
        mount, shas = self._setup_volume(tmp_path, layout="flat")
        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(cache, mount, [shas[0], shas[1]], verify=False)
        assert result.ingested == 2
        assert (cache / "data" / shas[0][:2] / shas[0]).exists()
        assert (cache / "data" / shas[1][:2] / shas[1]).exists()

    def test_two_level_layout_ingest(self, executor, tmp_path):
        """Ingest packs from two-level hash-prefix layout."""
        mount, shas = self._setup_volume(tmp_path, layout="two_level")
        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(cache, mount, [shas[0], shas[1]], verify=False)
        assert result.ingested == 2

    def test_missing_pack_not_counted(self, executor, tmp_path):
        """Pack not on volume is not counted."""
        mount, shas = self._setup_volume(tmp_path, layout="flat")
        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(cache, mount, [shas[2]], verify=False)  # sha3 doesn't exist
        assert result.ingested == 0

    def test_already_cached_skipped(self, executor, tmp_path):
        """Pack already in cache is skipped (not re-copied)."""
        mount, shas = self._setup_volume(tmp_path, layout="flat")
        cache = tmp_path / "cache"
        (cache / "data").mkdir(parents=True)
        # Pre-populate cache with sha1 in two-level layout
        prefix = cache / "data" / shas[0][:2]
        prefix.mkdir(parents=True)
        (prefix / shas[0]).write_bytes(b"already_here")

        result = executor.ingest_volume(cache, mount, [shas[0], shas[1]], verify=False)
        assert result.ingested == 1  # only sha2 ingested
        # sha1 should NOT be overwritten
        assert (cache / "data" / shas[0][:2] / shas[0]).read_bytes() == b"already_here"

    def test_mixed_found_and_missing(self, executor, tmp_path):
        """Returns correct count when some packs found, some not."""
        mount, shas = self._setup_volume(tmp_path, layout="flat")
        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(cache, mount, [shas[0], shas[2]], verify=False)
        assert result.ingested == 1  # sha1 found, sha3 missing

    def test_duplicate_required_packs(self, executor, tmp_path):
        """Duplicate SHA-256 entries should not double-count ingested packs."""
        mount, shas = self._setup_volume(tmp_path, layout="flat")
        cache = tmp_path / "cache"
        cache.mkdir()

        # Pass sha1 twice
        result = executor.ingest_volume(
            cache, mount, [shas[0], shas[0]], verify=False,
        )
        # First copy ingested; second skipped (already exists)
        assert result.ingested == 1


# =========================================================================
# execute_restore()
# =========================================================================


class TestExecuteRestore:
    def test_delegates_to_rustic_runner(self, executor, mock_rustic, tmp_path):
        """Should call rustic.restore() with correct arguments."""
        cache = tmp_path / "cache"
        target = tmp_path / "target"
        pw_file = tmp_path / "password.txt"
        pw_file.write_text("testpass")

        executor.execute_restore(cache, "abc123", target, pw_file)

        mock_rustic.restore.assert_called_once_with(
            snapshot_id="abc123",
            repo_path=cache,
            password_file=pw_file,
            target_path=target,
        )


# =========================================================================
# verify_iso() — ECC integration
# =========================================================================


class TestVerifyISO:
    def test_no_ecc_runner_returns_true(self, executor, tmp_path):
        """Without an ECC runner, verify_iso always returns True."""
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"fake iso")
        assert executor.verify_iso(iso) is True

    def test_no_ecc_runner_logs_info_not_debug(self, executor, tmp_path, caplog):
        """Without an ECC runner, verify_iso logs at INFO level (not DEBUG)."""
        import logging
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"fake iso")
        with caplog.at_level(logging.DEBUG):
            executor.verify_iso(iso)
        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("not verified" in r.message.lower() or "ECC" in r.message for r in info_msgs), \
            f"Expected INFO log about ECC skip, got: {[r.message for r in caplog.records]}"

    def test_ecc_verify_passes(self, mock_rustic, tmp_path):
        """ECC verify_iso returns True when ECC passes."""
        ecc = MagicMock()
        ecc.verify_iso.return_value = True
        ex = RestoreExecutor(mock_rustic, ecc_runner=ecc)

        iso = tmp_path / "test.iso"
        iso.write_bytes(b"fake iso")
        assert ex.verify_iso(iso) is True
        ecc.verify_iso.assert_called_once_with(iso)

    def test_ecc_verify_fails_repair_succeeds(self, mock_rustic, tmp_path):
        """When ECC verify fails but repair succeeds, returns True."""
        ecc = MagicMock()
        ecc.verify_iso.return_value = False
        ecc.repair_iso.return_value = True
        ex = RestoreExecutor(mock_rustic, ecc_runner=ecc)

        iso = tmp_path / "test.iso"
        iso.write_bytes(b"fake iso")
        assert ex.verify_iso(iso) is True
        ecc.repair_iso.assert_called_once_with(iso)

    def test_ecc_verify_and_repair_both_fail(self, mock_rustic, tmp_path):
        """When both ECC verify and repair fail, returns False."""
        ecc = MagicMock()
        ecc.verify_iso.return_value = False
        ecc.repair_iso.return_value = False
        ex = RestoreExecutor(mock_rustic, ecc_runner=ecc)

        iso = tmp_path / "test.iso"
        iso.write_bytes(b"fake iso")
        assert ex.verify_iso(iso) is False


# =========================================================================
# collect_failures mode
# =========================================================================

class TestIngestCollectFailures:
    """Tests for ingest_volume with collect_failures=True."""

    def test_collect_failures_returns_result(self, executor, tmp_path):
        """collect_failures=True returns an IngestionResult."""
        from lcsas.restore.executor import IngestionResult

        mount = tmp_path / "volume" / "data"
        mount.mkdir(parents=True)
        sha = "a" * 64
        (mount / sha).write_bytes(b"pack_data")

        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(
            cache, tmp_path / "volume", [sha],
            verify=False, collect_failures=True,
        )
        assert isinstance(result, IngestionResult)
        assert result.ingested == 1
        assert result.failed == []

    def test_corrupt_pack_collected_not_raised(self, executor, tmp_path):
        """Corrupt pack is added to failed list, not raised."""
        mount = tmp_path / "volume" / "data"
        mount.mkdir(parents=True)
        # Use a sha that won't match the content
        fake_sha = "deadbeef" * 8  # 64 chars
        (mount / fake_sha).write_bytes(b"corrupt_data")

        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(
            cache, tmp_path / "volume", [fake_sha],
            verify=True, collect_failures=True,
        )
        assert result.ingested == 0
        assert fake_sha in result.failed
        # The corrupt file should have been removed
        assert not (cache / "data" / fake_sha[:2] / fake_sha).exists()

    def test_corrupt_pack_raises_without_collect(self, executor, tmp_path):
        """Without collect_failures, corrupt pack raises PackCorruptionError."""
        from lcsas.restore.executor import PackCorruptionError

        mount = tmp_path / "volume" / "data"
        mount.mkdir(parents=True)
        fake_sha = "deadbeef" * 8
        (mount / fake_sha).write_bytes(b"corrupt_data")

        cache = tmp_path / "cache"
        cache.mkdir()

        with pytest.raises(PackCorruptionError):
            executor.ingest_volume(
                cache, tmp_path / "volume", [fake_sha],
                verify=True, collect_failures=False,
            )

    def test_mixed_good_and_corrupt(self, executor, tmp_path):
        """Mix of good (verify=False) and corrupt packs."""
        mount = tmp_path / "volume" / "data"
        mount.mkdir(parents=True)

        # Good pack (content matches sha — use verify=False for simplicity)
        good_sha = "a" * 64
        (mount / good_sha).write_bytes(b"good_data")

        # Corrupt pack
        bad_sha = "deadbeef" * 8
        (mount / bad_sha).write_bytes(b"bad_data")

        cache = tmp_path / "cache"
        cache.mkdir()

        # With verify=False, both succeed
        result = executor.ingest_volume(
            cache, tmp_path / "volume", [good_sha, bad_sha],
            verify=False, collect_failures=True,
        )
        assert result.ingested == 2
        assert result.failed == []


# =========================================================================
# verify_cache_completeness()
# =========================================================================

class TestVerifyCacheCompleteness:
    """Tests for the static verify_cache_completeness method."""

    def test_all_present_returns_empty(self, tmp_path):
        """Returns empty list when every required pack exists."""
        cache = tmp_path / "cache"
        data = cache / "data"
        sha1 = "a" * 64
        sha2 = "b" * 64
        for sha in (sha1, sha2):
            d = data / sha[:2]
            d.mkdir(parents=True, exist_ok=True)
            (d / sha).write_bytes(b"pack")

        missing = RestoreExecutor.verify_cache_completeness(
            cache, [sha1, sha2],
        )
        assert missing == []

    def test_missing_packs_returned(self, tmp_path):
        """Returns the SHA-256 hashes of missing packs."""
        cache = tmp_path / "cache"
        data = cache / "data"
        present = "a" * 64
        absent = "b" * 64
        d = data / present[:2]
        d.mkdir(parents=True)
        (d / present).write_bytes(b"pack")

        missing = RestoreExecutor.verify_cache_completeness(
            cache, [present, absent],
        )
        assert missing == [absent]

    def test_empty_required_returns_empty(self, tmp_path):
        """No required packs → nothing missing."""
        cache = tmp_path / "cache"
        (cache / "data").mkdir(parents=True)
        assert RestoreExecutor.verify_cache_completeness(cache, []) == []

    def test_all_missing(self, tmp_path):
        """Everything missing when data/ is empty."""
        cache = tmp_path / "cache"
        (cache / "data").mkdir(parents=True)
        sha1 = "a" * 64
        sha2 = "b" * 64
        missing = RestoreExecutor.verify_cache_completeness(
            cache, [sha1, sha2],
        )
        assert set(missing) == {sha1, sha2}

    def test_no_data_dir(self, tmp_path):
        """All packs missing when data/ directory doesn't exist at all."""
        cache = tmp_path / "cache"
        cache.mkdir()
        # Deliberately do NOT create cache/data/
        sha = "a" * 64
        missing = RestoreExecutor.verify_cache_completeness(cache, [sha])
        assert missing == [sha]

    def test_duplicate_required_packs_handled(self, tmp_path):
        """Duplicate SHA-256 entries don't cause errors."""
        cache = tmp_path / "cache"
        data = cache / "data"
        sha = "a" * 64
        d = data / sha[:2]
        d.mkdir(parents=True)
        (d / sha).write_bytes(b"pack")

        # Pass the same hash twice
        missing = RestoreExecutor.verify_cache_completeness(
            cache, [sha, sha],
        )
        assert missing == []
