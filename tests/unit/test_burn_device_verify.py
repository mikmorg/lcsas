"""Tests for exact-length device read-back hashing (BURN-04)."""

from __future__ import annotations

import hashlib

import pytest

from lcsas.burn.device_verify import read_device_sha256


class TestReadDeviceSha256:
    def test_exact_length_read(self, tmp_path):
        content = b"\xa5" * 10_000
        dev = tmp_path / "fake_device"
        dev.write_bytes(content)

        got = read_device_sha256(str(dev), len(content))
        assert got == hashlib.sha256(content).hexdigest()

    def test_padding_beyond_length_is_excluded(self, tmp_path):
        """Drive padding past the image length must not affect the hash."""
        content = b"image-bytes" * 100
        dev = tmp_path / "fake_device"
        dev.write_bytes(content + b"\x00" * 2048)

        got = read_device_sha256(str(dev), len(content))
        assert got == hashlib.sha256(content).hexdigest()

    def test_short_read_raises_oserror(self, tmp_path):
        """A device shorter than the recorded image IS a verify failure."""
        dev = tmp_path / "fake_device"
        dev.write_bytes(b"x" * 100)

        with pytest.raises(OSError, match="Short read"):
            read_device_sha256(str(dev), 200)

    def test_short_read_error_carries_device_and_offset(self, tmp_path):
        dev = tmp_path / "fake_device"
        dev.write_bytes(b"x" * 100)

        with pytest.raises(OSError) as excinfo:
            read_device_sha256(str(dev), 200)
        msg = str(excinfo.value)
        assert str(dev) in msg
        assert "100" in msg  # offset reached
        assert "200" in msg  # expected length

    def test_missing_device_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            read_device_sha256(str(tmp_path / "no_such_device"), 10)

    def test_small_chunk_size(self, tmp_path):
        """Chunked reads spanning many iterations hash identically."""
        content = bytes(range(256)) * 10
        dev = tmp_path / "fake_device"
        dev.write_bytes(content)

        got = read_device_sha256(str(dev), len(content), chunk=7)
        assert got == hashlib.sha256(content).hexdigest()

    def test_non_positive_length_rejected(self, tmp_path):
        dev = tmp_path / "fake_device"
        dev.write_bytes(b"data")

        with pytest.raises(ValueError, match="positive"):
            read_device_sha256(str(dev), 0)
        with pytest.raises(ValueError, match="positive"):
            read_device_sha256(str(dev), -5)
