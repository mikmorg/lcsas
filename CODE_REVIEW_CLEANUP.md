# Code Review Cleanup Task

This task guides a comprehensive code review and cleanup pass across the LCSAS codebase. Work through sections systematically, commit after each logical grouping.

## 1. Lint & Type Checking
- [ ] Run `make lint` and fix all ruff violations
- [ ] Run `make typecheck` and resolve mypy errors
- [ ] Check for unused imports (ruff has rules for this)
- [ ] Remove dead code identified by linters

Commit: "fix: resolve lint and type-checking violations"

## 2. Code Duplication (DRY Principle)

### Search for repeated patterns:
- [ ] Grep for identical exception handling blocks — extract to shared helper
- [ ] Look for repeated SQL queries — consider query helper functions
- [ ] Identify repeated validation logic — move to validator functions
- [ ] Find copy-pasted test fixtures — consolidate in conftest.py

### Key files to check:
- src/lcsas/cli/main.py (long command functions)
- src/lcsas/burn/orchestrator.py (parallel staging/burning logic)
- src/lcsas/restore/executor.py (pack ingestion variants)
- src/lcsas/db/*.py (CRUD patterns)

Commit: "refactor: eliminate code duplication via shared helpers"

## 3. Code Smells

### Check for:
- [ ] Functions > 100 lines — consider breaking into smaller pieces
- [ ] Functions with > 5 parameters — use dataclass or dict for grouping
- [ ] Deeply nested if/else (> 3 levels) — extract guard clauses or early returns
- [ ] Magic numbers — replace with named constants
- [ ] Overly broad except clauses (bare `except:` or `except Exception:`) — be specific
- [ ] Mutable default arguments in function signatures
- [ ] Global state or module-level variables (except logging)
- [ ] TOCTOU race conditions in file operations

### Key files to review:
- src/lcsas/cli/main.py (argument parsing, command dispatch)
- src/lcsas/burn/orchestrator.py (complex state transitions)
- src/lcsas/restore/executor.py (error recovery paths)

Commit: "refactor: address code smells (nesting, complexity, magic numbers)"

## 4. Error Handling & Logging

### Review error handling:
- [ ] All exceptions have context (don't swallow errors silently)
- [ ] Error messages are actionable (not just "failed")
- [ ] Logging levels are correct (info, warning, error, debug)
- [ ] Exception types are specific (not bare Exception)
- [ ] Resource cleanup happens in finally blocks or context managers

### Key patterns to check:
- Database transaction rollbacks on error
- Staging/staging cleanup on failure
- File handle cleanup
- Temporary directory cleanup on interrupt

Commit: "fix: improve error handling and logging clarity"

## 5. Test Coverage Gaps

### Identify and add tests for:
- [ ] Edge cases in bin-packing algorithm (empty, single pack, exact fit)
- [ ] Database transaction rollbacks (concurrent writes, errors mid-transaction)
- [ ] File I/O errors (permission denied, disk full, symlink attack)
- [ ] Restore from damaged/incomplete discs
- [ ] Multi-repo consolidation workflows
- [ ] Label generation edge cases (max length, special chars)
- [ ] Parser robustness (malformed JSON, missing fields)

Run: `make coverage` and check for < 80% files

Commit: "test: add missing coverage for edge cases and error paths"

## 6. Documentation & Comments

### Review for clarity:
- [ ] Docstrings on public functions (module, class, method level)
- [ ] Comments explain WHY, not WHAT (code already explains what)
- [ ] Comments are kept in sync with code (outdated comments confuse)
- [ ] Function signatures are clear (good naming, type hints)
- [ ] Complex algorithms have high-level explanation

Remove:
- [ ] "removed X" comments (use git blame)
- [ ] Commented-out code (use git history)
- [ ] TODO/FIXME without context (resolve or document properly)

Commit: "docs: clarify docstrings and remove outdated comments"

## 7. Constants & Configuration

### Consolidate magic values:
- [ ] Move hardcoded strings to named constants (e.g., media type defaults, paths)
- [ ] Move numeric thresholds to config or const module (e.g., timeout values, retry counts)
- [ ] Group related constants into enums or dataclasses
- [ ] Configuration that should be user-editable vs. baked-in defaults

Check files:
- src/lcsas/config/ (settings, media types)
- src/lcsas/burn/, src/lcsas/restore/ (thresholds, timeouts)

Commit: "refactor: extract hardcoded constants to named consts"

## 8. Naming Clarity

### Review for consistency:
- [ ] Variable names are descriptive (not `x`, `tmp`, `data`)
- [ ] Naming conventions are consistent (snake_case for functions, PascalCase for classes)
- [ ] Abbreviations are clear (use `temp_dir` not `tmpd`)
- [ ] Boolean functions/variables start with `is_`, `has_`, `can_`
- [ ] Private methods/functions start with `_`
- [ ] Constants are UPPER_SNAKE_CASE

Commit: "refactor: improve variable and function naming"

## 9. Type Hints & Contracts

### Check for:
- [ ] All public functions have type hints (parameters and return)
- [ ] Complex types are documented (Union, Optional, etc.)
- [ ] Database model fields match schema constraints
- [ ] Function contracts are enforced (preconditions, postconditions)

Run: `make typecheck` again after changes

Commit: "refactor: improve type hints and API contracts"

## 10. Performance & Scalability

### Review for inefficiencies:
- [ ] N+1 database queries (batch queries where possible)
- [ ] Unnecessary file copies (use symlinks/hardlinks where safe)
- [ ] Inefficient string operations (use f-strings, avoid repeated .format())
- [ ] Large objects held in memory (streaming where possible)
- [ ] Regex compilation inside loops (compile once, reuse)

Check files:
- src/lcsas/db/queries.py (query patterns)
- src/lcsas/staging/builder.py (file operations)
- src/lcsas/restore/executor.py (pack ingestion)

Commit: "perf: optimize database queries and file operations"

## 11. Security Review

### Check for:
- [ ] Input validation (CLI args, file paths, database values)
- [ ] Path traversal prevention (symlink checks, path normalization)
- [ ] Command injection prevention (use subprocess properly, no shell=True)
- [ ] Secrets handling (no passwords in logs, no hardcoded keys)
- [ ] Privilege checks where needed (file permissions, directory access)

Commit: "security: add input validation and path safety checks"

## 12. Test Organization & Clarity

### Review test structure:
- [ ] Test names clearly describe what they test
- [ ] Each test tests one thing (not multiple assertions on unrelated behavior)
- [ ] Fixtures are organized in conftest.py (not duplicated per test file)
- [ ] Mock objects are realistic (not oversimplified)
- [ ] Test data is clear (not magic numbers, use named constants)

Commit: "test: improve test organization and clarity"

## 13. CI/Build Process

### Check:
- [ ] Makefile targets are documented
- [ ] build process is reproducible
- [ ] Tests run in CI (GitHub Actions or similar)
- [ ] Lint check enforced in CI
- [ ] Type check enforced in CI

Commit: "ci: ensure lint and type-check pass in CI"

## 14. Deprecations & Migrations

### Identify:
- [ ] Deprecated functions/modules (mark with `@deprecated` if applicable)
- [ ] Old code paths that should be removed
- [ ] Database schema versions (migration strategy if needed)

Commit: "refactor: remove deprecated code and old migration logic"

## 15. Final Verification

- [ ] Run `make lint` → all pass
- [ ] Run `make typecheck` → all pass
- [ ] Run `make test-all` → all pass (unit + integration if tools available)
- [ ] Run `make coverage` → check coverage is adequate
- [ ] Review git log of commits — logical grouping, good messages

Final commit: "chore: code review cleanup pass — lint, DRY, smells, coverage"

---

## Notes

- Work through these sections in order; earlier sections unblock later ones
- Commit after each section to keep changes reviewable
- Run tests after each commit to catch regressions early
- Use `git diff` to review changes before committing
- If unsure whether to refactor something, leave a comment for discussion rather than guessing
