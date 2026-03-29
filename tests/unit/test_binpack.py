"""Tests for the bin packing algorithm."""

from __future__ import annotations

import pytest

from lcsas.binpack.algorithm import estimate_volumes_needed, first_fit_decreasing


class TestFirstFitDecreasing:
    def test_empty_list(self):
        selected, remaining = first_fit_decreasing([], capacity=1000)
        assert selected == []
        assert remaining == []

    def test_single_item_fits(self):
        items = [("a", 500)]
        selected, remaining = first_fit_decreasing(items, capacity=1000)
        assert selected == [("a", 500)]
        assert remaining == []

    def test_single_item_too_large(self):
        items = [("a", 2000)]
        selected, remaining = first_fit_decreasing(items, capacity=1000)
        assert selected == []
        assert remaining == [("a", 2000)]

    def test_exact_fit(self):
        items = [("a", 500), ("b", 500)]
        selected, remaining = first_fit_decreasing(items, capacity=1000)
        assert len(selected) == 2
        assert remaining == []

    def test_sorts_largest_first(self):
        items = [("small", 100), ("big", 900), ("med", 500)]
        selected, remaining = first_fit_decreasing(items, capacity=1000)
        # Should pick big (900) + small (100) = 1000
        selected_ids = {s[0] for s in selected}
        assert "big" in selected_ids
        assert "small" in selected_ids

    def test_reserved_space(self):
        items = [("a", 800)]
        selected, remaining = first_fit_decreasing(items, capacity=1000, reserved=300)
        # Usable = 700, item is 800 -> doesn't fit
        assert selected == []
        assert remaining == [("a", 800)]

    def test_many_small_items(self):
        items = [(f"p{i}", 10) for i in range(100)]
        selected, remaining = first_fit_decreasing(items, capacity=500)
        total = sum(s for _, s in selected)
        assert total <= 500
        assert len(selected) == 50

    def test_test_tiny_capacity(self):
        """Simulate packing into TEST_TINY (1 MB) media."""
        capacity = 1_048_576
        items = [(f"pack_{i}", 100_000) for i in range(15)]
        selected, remaining = first_fit_decreasing(items, capacity=capacity)
        total = sum(s for _, s in selected)
        assert total <= capacity
        assert len(selected) == 10  # 10 * 100KB = 1MB
        assert len(remaining) == 5

    def test_zero_capacity(self):
        items = [("a", 1)]
        selected, remaining = first_fit_decreasing(items, capacity=0)
        assert selected == []
        assert remaining == [("a", 1)]

    def test_oversized_item_emits_warning(self, caplog):
        """When the largest item exceeds usable capacity, a warning is logged."""
        import logging

        items = [("huge_pack", 2_000_000), ("small", 100)]
        with caplog.at_level(logging.WARNING, logger="lcsas.binpack.algorithm"):
            selected, remaining = first_fit_decreasing(items, capacity=1_000_000)

        # Only the oversized pack ends up in remaining; small fits
        selected_ids = {i[0] for i in selected}
        remaining_ids = {i[0] for i in remaining}
        assert "huge_pack" in remaining_ids
        assert "small" in selected_ids
        assert any("huge_pack" in r.message for r in caplog.records)
        assert any("cannot fit" in r.message.lower() for r in caplog.records)


class TestEstimateVolumes:
    def test_zero_data(self):
        assert estimate_volumes_needed(0, 1000) == 0

    def test_fits_in_one(self):
        assert estimate_volumes_needed(500, 1000) == 1

    def test_needs_two(self):
        assert estimate_volumes_needed(1500, 1000) == 2

    def test_exact_boundary(self):
        assert estimate_volumes_needed(1000, 1000) == 1

    def test_with_ecc_overhead(self):
        # 1000 capacity, 20% ECC -> 800 usable
        assert estimate_volumes_needed(1600, 1000, ecc_overhead_pct=20) == 2

    def test_with_reserved(self):
        # 1000 capacity, 200 reserved -> 800 usable
        assert estimate_volumes_needed(1600, 1000, reserved=200) == 2

    def test_invalid_usable(self):
        with pytest.raises(ValueError, match="No usable capacity"):
            estimate_volumes_needed(100, 100, reserved=200)
