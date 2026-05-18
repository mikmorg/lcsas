.PHONY: dev lint typecheck test-unit test-integration test-e2e test-recovery-hardening test-all gate coverage clean blind-restore blind-restore-teardown fetch-recovery verify-recovery build-recovery

# Default target: lint + typecheck + every test tier ending with the
# recovery-hardening gate.  `make` with no args runs the full build
# gate; CI uses the same path.
.DEFAULT_GOAL := gate

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

# Recovery-hardening tests — pedantic gates that exist because every
# bug they catch slipped through unit/integration into a real blind
# run.  Hard-fails the build on any regression.  See
# tests/recovery_hardening/README.md for the per-test catalogue.
test-recovery-hardening:
	pytest tests/recovery_hardening/ -v

# Full test suite.  Recovery-hardening runs LAST: every other tier
# (unit/integration/e2e) must succeed first; the hardening checks are
# the final gate that says "this build is shippable."
test-all: test-unit test-integration test-e2e test-recovery-hardening

# Production build gate.  Composes lint + typecheck + the entire test
# pyramid; the recovery-hardening tier is the final step.  Anything
# that fails here blocks `git push`.
gate: lint typecheck test-all
	@echo "build gate passed."

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

# Cross-build the tier-1 lcsas-restore binary for every target that
# `RecoveryBuilder.cross_build` currently reaches via `zig cc` or
# `<arch>-linux-musl-gcc` (Phase 21.10.b).  armv7 + macOS deferred.
# Requires zig or musl-cross toolchains on PATH.  Skip targets you
# can't build by overriding LCSAS_RECOVERY_ARCHES.
build-recovery:
	@arches="$${LCSAS_RECOVERY_ARCHES:-host x86_64 aarch64 armv7 x86_64-windows x86_64-macos aarch64-macos}"; \
	for a in $$arches; do \
		echo "==> lcsas recovery build --arch $$a"; \
		lcsas recovery build --arch "$$a" || exit 1; \
	done
