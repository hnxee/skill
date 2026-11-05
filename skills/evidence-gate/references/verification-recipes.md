# Verification recipes

Pick the **strongest affordable** check for each claim. "Affordable" means: fast
enough to run after the final edit, safe (no production side effects), and
available in the current environment. Commands below are examples — adapt them to
the project's actual tooling (look for `Makefile`, `package.json` scripts,
`pyproject.toml`, CI config) instead of inventing new ones.

## By task type

### Bug fix
1. **Reproduce first** (red): run the failing input/test and record it with
   `verify ID --label before --expect-exit <non-zero> -- <repro>`.
2. Fix.
3. **Same reproduction now passes** (green): `verify ID -- <repro>`.
4. Add a regression test that encodes the reproduction; verify it.
5. Run the surrounding suite to catch regressions.

If you cannot reproduce the bug, you cannot claim to have fixed it — report the
fix as `UNVERIFIED` with the reasoning.

### New feature
- A test exercising the public interface with realistic input, plus at least one
  edge case (empty, boundary, invalid).
- Run the real entry point once (CLI invocation, HTTP request against a *local*
  server, function call from a REPL one-liner) and assert on the output.
- Build/compile/type-check succeeds.

### Refactor (behaviour must not change)
- The full existing test suite passes **before and after**.
- Public interface unchanged: compare exported symbols, CLI `--help`, or API schema.
- `git diff --stat` matches the intended scope.

### Dependency / config change
- Clean install or lockfile resolution succeeds.
- The app/tool starts and reports the expected version or config value.
- The specific behaviour the config controls is observed (not just "it parses").

### Documentation
- Every command or code sample you wrote is executed once and works.
- Links/paths you reference exist (`test -e path`, link checker if available).
- Rendered output looks correct → usually `ATTESTED` (say what you viewed).

### Data transformation / migration script
- Run against a **local copy or fixture**, never production, to verify.
- Assert row counts, checksums or invariants before vs. after.
- Idempotency: running twice yields the same result.
- Production execution is a separate, user-approved step → mark `UNVERIFIED` or `WAIVED`.

### Frontend / UI
- Build succeeds; unit/component tests pass.
- Headless browser screenshot or DOM assertion of the changed view, if a browser
  is available; otherwise `ATTESTED`/`UNVERIFIED` with instructions to check.

### Performance claim ("faster", "uses less memory")
- Measure before and after with the same input, multiple runs, report the numbers.
  "Should be faster" without a measurement is `UNVERIFIED`.

### Security claim ("input is sanitized", "no secret leaks")
- A test with a hostile input (injection payload, path traversal, oversized input).
- Search the diff/output for the forbidden pattern
  (`--expect-stdout-not-contains`).

### Scope claim ("only touched X")
```bash
ledger.py verify C9 -- python3 path/to/ledger.py changed --allow 'src/feature/*' --allow 'tests/*'
```
`changed` includes untracked files, ignores tool caches and the ledger, and exits 1
if anything outside the allowed globs changed.

## Making checks trustworthy

- **Pin the exit code to the program, not the pipe.** Use `--shell` (which enables
  `pipefail`) or avoid pipes entirely.
- **Assert on content, not on absence of crash.** `--expect-stdout-regex '^OK'`
  beats "it exited 0" for programs that print errors but exit 0.
- **Watch what the claim depends on.** `--watch src --watch tests` so later edits
  invalidate the evidence automatically.
- **Match the check's breadth to the claim's breadth.** "All tests pass" requires
  the whole suite, not the one file you touched.
- **Run from a clean state when it matters**: fresh build dir, no cached artifacts.
- **Flaky?** Re-run up to 3 times and report *all* results. Never report the one
  green run in silence.

## When no safe check exists

Record it with `ledger.py defer ID --reason "..." --user-check "<exact command>"`:
the claim stays `UNVERIFIED`, the outcome is *partial*, and the report tells the
user exactly how to verify it. `waive` is only for claims the user agreed to skip.
That is a complete, honest answer; a guessed
"it works" is not.
