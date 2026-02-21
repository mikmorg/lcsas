"""Tests for the pure-Python AES implementation."""

from __future__ import annotations

from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)


class TestAESKeySchedule:
    """Verify key schedule produces correct number of round keys."""

    def test_aes128_produces_11_round_keys(self):
        key = bytes(16)
        rk = key_schedule(key)
        assert len(rk) == 11
        assert all(len(k) == 16 for k in rk)

    def test_aes256_produces_15_round_keys(self):
        key = bytes(32)
        rk = key_schedule(key)
        assert len(rk) == 15
        assert all(len(k) == 16 for k in rk)

    def test_invalid_key_length(self):
        import pytest
        with pytest.raises(ValueError, match="16 or 32"):
            key_schedule(bytes(24))


class TestAESEncryptBlock:
    """Verify AES-128 and AES-256 ECB against NIST test vectors."""

    def test_aes128_nist_vector(self):
        """FIPS 197 Appendix B — AES-128 test vector."""
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plaintext = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
        expected = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")

        rk = key_schedule(key)
        ciphertext = aes_encrypt_block(plaintext, rk)
        assert ciphertext == expected

    def test_aes256_nist_vector(self):
        """NIST SP 800-38A AES-256 ECB test vector (block 1)."""
        key = bytes.fromhex(
            "603deb1015ca71be2b73aef0857d7781"
            "1f352c073b6108d72d9810a30914dff4"
        )
        plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        expected = bytes.fromhex("f3eed1bdb5d2a03c064b5a7e3db181f8")

        rk = key_schedule(key)
        ciphertext = aes_encrypt_block(plaintext, rk)
        assert ciphertext == expected

    def test_aes128_all_zeros(self):
        """AES-128 with all-zero key and plaintext (known vector)."""
        key = bytes(16)
        plaintext = bytes(16)
        # Known result for AES-128(0, 0)
        expected = bytes.fromhex("66e94bd4ef8a2c3b884cfa59ca342b2e")

        rk = key_schedule(key)
        ciphertext = aes_encrypt_block(plaintext, rk)
        assert ciphertext == expected


class TestAESCTR:
    """Verify AES-CTR mode."""

    def test_ctr_round_trip(self):
        """Encrypting then decrypting yields original plaintext."""
        key = bytes.fromhex(
            "603deb1015ca71be2b73aef0857d7781"
            "1f352c073b6108d72d9810a30914dff4"
        )
        iv = bytes(16)
        plaintext = b"Hello, World! This is a test message for AES-CTR."

        ciphertext = aes_ctr(key, iv, plaintext)
        assert ciphertext != plaintext
        assert len(ciphertext) == len(plaintext)

        decrypted = aes_ctr(key, iv, ciphertext)
        assert decrypted == plaintext

    def test_ctr_nist_vector(self):
        """NIST SP 800-38A AES-256-CTR test vector (block 1)."""
        key = bytes.fromhex(
            "603deb1015ca71be2b73aef0857d7781"
            "1f352c073b6108d72d9810a30914dff4"
        )
        # Initial counter: f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff
        iv = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
        plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        expected = bytes.fromhex("601ec313775789a5b7a7f504bbf3d228")

        ciphertext = aes_ctr(key, iv, plaintext)
        assert ciphertext == expected

    def test_ctr_empty(self):
        """Empty plaintext produces empty ciphertext."""
        key = bytes(32)
        iv = bytes(16)
        assert aes_ctr(key, iv, b"") == b""

    def test_ctr_partial_block(self):
        """CTR handles non-block-aligned data correctly."""
        key = bytes(32)
        iv = bytes(16)
        plaintext = b"Short"
        ct = aes_ctr(key, iv, plaintext)
        assert len(ct) == 5
        assert aes_ctr(key, iv, ct) == plaintext

    def test_ctr_multi_block(self):
        """CTR handles data spanning multiple blocks."""
        key = bytes(32)
        iv = bytes(16)
        plaintext = bytes(range(256)) * 2  # 512 bytes = 32 blocks
        ct = aes_ctr(key, iv, plaintext)
        assert len(ct) == 512
        assert aes_ctr(key, iv, ct) == plaintext
