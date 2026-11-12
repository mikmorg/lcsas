"""Tests for lcsas.log."""

from __future__ import annotations


class TestMaskPasswordPath:
    def test_masks_key_file(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/root/keys/family.key") == "***"

    def test_masks_pem_file(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/tmp/cert.pem") == "***"

    def test_passes_normal_path(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/mnt/mirror/data/pack.bin") == "/mnt/mirror/data/pack.bin"
