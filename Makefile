.PHONY: dev lint typecheck test-unit test-integration test-e2e test-recovery-hardening test-all gate coverage clean blind-restore blind-restore-x5 blind-restore-teardown fetch-recovery verify-recovery build-recovery

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
	@if [ "$$LCSAS_BLIND_ACK_COST" != "1" ]; then \
		echo "ERROR: make blind-restore spawns a real Claude sub-agent and" >&2; \
		echo "       typically costs USD ~5 per run.  This target is NOT run" >&2; \
		echo "       on every commit.  To proceed, re-invoke with:" >&2; \
		echo "" >&2; \
		echo "         LCSAS_BLIND_ACK_COST=1 make blind-restore" >&2; \
		echo "" >&2; \
		echo "       For repeated stress-testing, see make blind-restore-x5." >&2; \
		exit 1; \
	fi
	sudo tests/e2e/cdemu_blind_restore/setup.py
	@# 45-minute wall-clock cap.  A correct run on TEST_TINY finishes
	@# in ~15-25 min; anything beyond 45 means the agent is looping
	@# and we'd rather burn 1× max-budget than open-ended hours.
	timeout --foreground 2700 \
		env RUN_DIR=/tmp/lcsas-blind-run-$$$$ \
		tests/e2e/cdemu_blind_restore/run.sh
	@last=$$(ls -1dt /tmp/lcsas-blind-run-* 2>/dev/null | head -1); \
		tests/e2e/cdemu_blind_restore/verify.sh "$$last"

# Stress / flakiness gate.  Runs the blind agent N times in
# sequence; fails if any run fails.  Use before declaring a
# product-UX change "shipped".  Cost: ~5× normal blind-restore.
blind-restore-x5:
	@if [ "$$LCSAS_BLIND_ACK_COST" != "1" ]; then \
		echo "ERROR: blind-restore-x5 costs USD ~25 per invocation." >&2; \
		echo "       Re-invoke with LCSAS_BLIND_ACK_COST=1 to proceed." >&2; \
		exit 1; \
	fi
	@for i in 1 2 3 4 5; do \
		echo "=== blind-restore-x5 attempt $$i/5 ==="; \
		$(MAKE) blind-restore || { \
			echo "FAIL on attempt $$i" >&2; exit 1; \
		}; \
		$(MAKE) blind-restore-teardown; \
	done
	@echo "blind-restore-x5: 5/5 PASS"

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
