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


class TestLogToStderr:
    def test_log_goes_to_stderr_inside_and_stdout_after(self, capsys):
        from lcsas.log import get_logger, log_to_stderr, setup_logging

        setup_logging()
        logger = get_logger()
        before = logger.handlers[:]
        with log_to_stderr():
            logger.error("inside the block")
        logger.error("after the block")
        captured = capsys.readouterr()
        assert "inside the block" in captured.err
        assert "inside the block" not in captured.out
        assert "after the block" in captured.out
        assert logger.handlers == before

    def test_handlers_restored_on_error(self):
        import pytest

        from lcsas.log import get_logger, log_to_stderr, setup_logging

        setup_logging()
        before = get_logger().handlers[:]
        with pytest.raises(RuntimeError), log_to_stderr():
            raise RuntimeError("boom")
        assert get_logger().handlers == before
