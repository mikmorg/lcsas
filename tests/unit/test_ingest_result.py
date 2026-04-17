"""Unit tests for IngestionResult dataclass."""

from lcsas.restore.executor import IngestionResult


class TestIngestionResult:
    """Test IngestionResult dataclass properties."""

    def test_ingest_result_creation_with_values(self):
        """Create IngestionResult with explicit values."""
        result = IngestionResult(ingested=10, failed=["sha1", "sha2"])
        assert result.ingested == 10
        assert result.failed == ["sha1", "sha2"]

    def test_ingest_result_default_failed(self):
        """IngestionResult has default empty failed list."""
        result = IngestionResult(ingested=5)
        assert result.ingested == 5
        assert result.failed == []

    def test_ingest_result_access_fields(self):
        """Access IngestionResult fields via attribute notation."""
        result = IngestionResult(ingested=42, failed=["abc123"])
        assert result.ingested == 42
        assert result.failed == ["abc123"]

    def test_ingest_result_is_not_iterable(self):
        """IngestionResult is a dataclass, not iterable (confirms fix)."""
        result = IngestionResult(ingested=3, failed=["x"])
        # This should NOT work: ingested, failed = result
        # But this SHOULD work: result.ingested, result.failed
        try:
            _a, _b = result  # This should raise TypeError
            assert False, "IngestionResult should not be iterable"
        except TypeError as e:
            assert "iterable" in str(e).lower()

    def test_ingest_result_unpack_fields_correctly(self):
        """Correct way to extract values from IngestionResult."""
        result = IngestionResult(ingested=7, failed=["pack1", "pack2", "pack3"])
        # Correct unpacking pattern (used in test_corrupt_disc_failover.py)
        ingested = result.ingested
        failed = result.failed
        assert ingested == 7
        assert len(failed) == 3
        assert "pack1" in failed
