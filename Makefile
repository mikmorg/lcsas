.PHONY: dev lint typecheck test-unit test-integration test-e2e test-all coverage clean blind-restore blind-restore-teardown fetch-recovery verify-recovery

dev:
	pip install -e ".[dev]"

lint:
	ruff check src/ tests/

lint-fix:
	ruff check --fix src/ tests/

typecheck:
	mypy src/

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v -m integration

test-e2e:
	pytest tests/e2e -v

test-all: test-unit test-integration test-e2e

coverage:
	pytest tests/ --cov=lcsas --cov-report=html --cov-report=term-missing

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

blind-restore:
	sudo tests/e2e/cdemu_blind_restore/setup.py
	RUN_DIR=/tmp/lcsas-blind-run-$$$$ tests/e2e/cdemu_blind_restore/run.sh
	@last=$$(ls -1dt /tmp/lcsas-blind-run-* 2>/dev/null | head -1); \
		tests/e2e/cdemu_blind_restore/verify.sh "$$last"

blind-restore-teardown:
	sudo tests/e2e/cdemu_blind_restore/teardown.sh

# Populate ~/.cache/lcsas/recovery-binaries/ with the rustic + Python
# tarballs pinned in recovery/UPSTREAM.sha256.  Idempotent; required
# before `lcsas meta build` if cross-platform support is wanted.  Set
# LCSAS_RECOVERY_CACHE to override the cache root.
fetch-recovery:
	sh recovery/scripts/fetch_upstream.sh

# Audit the local cache without downloading.  Reports any missing or
# corrupted entries against recovery/UPSTREAM.sha256 and exits non-zero
# if anything is wrong.  Phase 21.5.b.
verify-recovery:
	sh recovery/scripts/fetch_upstream.sh --verify-only
