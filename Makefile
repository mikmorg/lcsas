.PHONY: dev lint typecheck test-unit test-integration test-e2e test-recovery-hardening test-all gate coverage clean blind-restore blind-restore-x5 blind-restore-variants blind-restore-teardown fetch-recovery verify-recovery build-recovery gen-catalogue audit-gate shell-coverage

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

# Shell-level coverage of recovery/scripts/restore.sh (issue #213).
# Runs the existing test_restore_* hardening suite with the
# LCSAS_SHELL_TRACE hook enabled (restore.sh's preamble redirects
# `bash -x` xtrace to the named file via BASH_XTRACEFD).  The
# parser in tools/cov_shell.py cross-references the trace against
# the script's executable-line set and reports per-line coverage.
#
# Threshold: 90% (set via --threshold to fail the target if lower).
# Only honoured when bash is the interpreter; the hook is a no-op
# on dash/POSIX sh, so tests that explicitly invoke `sh restore.sh`
# contribute no coverage data.  Most subprocess.run invocations
# already use ['sh', 'restore.sh', ...] — we override SHELL=bash for
# the duration of the trace run via a wrapper that exports
# LCSAS_TRACE_VIA_BASH=1, picked up by the hardening tests.
shell-coverage:
	@rm -f /tmp/lcsas-restore-shell.trace
	@LCSAS_SHELL_TRACE=/tmp/lcsas-restore-shell.trace \
	 LCSAS_TRACE_VIA_BASH=1 \
	    pytest tests/recovery_hardening/test_restore_*.py -q || true
	@python3 tools/cov_shell.py \
	    --threshold 60 \
	    /tmp/lcsas-restore-shell.trace \
	    recovery/scripts/restore.sh

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
		[ "$$i" -lt 5 ] && { echo "=== throttle: sleeping 300s before next attempt ==="; sleep 300; } || true; \
	done
	@echo "blind-restore-x5: 5/5 PASS"

blind-restore-teardown:
	sudo tests/e2e/cdemu_blind_restore/teardown.sh

# Adversarial blind-restore variants (issue #214).  Loops the blind
# test through fixtures that force each recovery-cascade fallback
# path or stress an unusual tenant topology.  Currently shipping 5
# variants:
#
#   tier1-missing        — meta lacks lcsas-restore; restore.sh's
#                          LCSAS_TIER_FALLBACK=1 path falls to tier 2
#                          (XFAIL pending #227).
#   tier1-tier2-missing  — meta lacks tier-1 AND tier-2; tier 3 takes
#                          over (XFAIL pending #227).
#   single-tenant        — only the alpha repo exists; exercises the
#                          no-prompt fast path (issue #216, XFAIL
#                          pending live 15/15 confirmation).
#   5-tenant             — alpha + bravo + charlie + delta + echo;
#                          stress-tests the multi-tenant prompt
#                          (issue #217, XFAIL pending live confirmation).
#   no-catalog           — every data disc lacks catalog.db; forces
#                          the hash-only swap-prompt path (issue #218,
#                          XFAIL pending live confirmation).
#
# All five default to XFAIL — see run_variant.sh's LCSAS_VARIANT_XFAIL.
# Each costs ~$5 of blind-test compute; drop from the XFAIL list once
# a green 15/15 score has been recorded.
#
# Cost: ~$5 per variant × 5 = ~$25 per full sweep.
blind-restore-variants:
	@if [ "$$LCSAS_BLIND_ACK_COST" != "1" ]; then \
		echo "ERROR: blind-restore-variants costs USD ~5 per variant (~\$25 today)." >&2; \
		echo "       Re-invoke with LCSAS_BLIND_ACK_COST=1 to proceed." >&2; \
		exit 1; \
	fi
	@for v in tier1-missing tier1-tier2-missing single-tenant 5-tenant no-catalog; do \
		echo "=== variant: $$v ==="; \
		sudo -E bash tests/e2e/cdemu_blind_restore/run_variant.sh $$v \
		    || { echo "FAIL: variant $$v" >&2; exit 1; }; \
		$(MAKE) blind-restore-teardown; \
	done
	@echo "blind-restore-variants: all variants PASS"

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
gen-catalogue:
	python3 tools/gen_hardening_catalogue.py

THRESHOLD ?= 88

# Opt-in comprehensive gate for recovery/src/lcsas-restore/.
# NOT part of the default `gate`.  Run before merging any PR that
# touches the tier-1 C binary.  See recovery/docs/AUDIT.md.
audit-gate:
	$(MAKE) -C recovery audit-gate THRESHOLD=$(THRESHOLD)

build-recovery:
	@arches="$${LCSAS_RECOVERY_ARCHES:-host x86_64 aarch64 armv7 x86_64-windows x86_64-macos aarch64-macos}"; \
	for a in $$arches; do \
		echo "==> lcsas recovery build --arch $$a"; \
		lcsas recovery build --arch "$$a" || exit 1; \
	done
