# False-success failure modes

A catalogue of the ways agents (and people) end up reporting success that is not
real. Before writing a final report, scan this list and ask: *did any of these
happen in this session?*

| # | Failure mode | What it looks like | How to catch / prevent it |
|---|---|---|---|
| 1 | **Phantom run** | "Tests pass" with no test command in the transcript | Every success sentence must cite a command you actually ran |
| 2 | **Stale evidence** | Tests ran, then more edits happened | Re-run after the final edit; use `--watch` so the ledger flags it |
| 3 | **Narrow check, broad claim** | One test file run, "all tests pass" reported | Make the check's scope equal to the claim's scope |
| 4 | **Pipe masks exit code** | `pytest \| tail -5` exits 0 even if pytest failed | `--shell` (pipefail) or no pipes |
| 5 | **Truncated output read as success** | Only the last lines were read; a failure was above | Assert on the summary line or exit code, not on a glance |
| 6 | **Error swallowed** | `\|\| true`, `try: ... except: pass`, `continue-on-error` added | Never add these to make a check pass; search your diff for them |
| 7 | **Test edited to match bug** | Expected value changed to the buggy actual value | Changing an assertion is a claim of its own and needs justification |
| 8 | **Self-mocking** | The component under test is mocked, so the test always passes | At least one check must exercise the real code path |
| 9 | **Skipped / xfail'd tests** | `@skip`, `.only`, `-k` filters left in place | Grep the diff for skip markers; count tests before vs. after |
| 10 | **Silent requirement drop** | 4 of 5 requested items done, report says "done" | Claims are enumerated up front; each one appears in the report |
| 11 | **Wrong target verified** | Checked the local copy, the user meant the deployed one (or vice versa) | State *where* each check ran |
| 12 | **Hallucinated API / flag** | Code calls a function or CLI flag that doesn't exist; never executed | Execute it at least once, or check the installed version's docs/help |
| 13 | **"Should work"** | Hedged language standing in for a check | Treat it as `UNVERIFIED` and say so plainly |
| 14 | **Flake laundering** | Red, re-run, green, reported as green | Report every run; investigate before calling it flaky |
| 15 | **Environment mismatch** | Works with the agent's cached deps / env vars, fails clean | Run from a clean state when the claim is about portability |
| 16 | **Success by exit code only** | Program printed `ERROR` but exited 0 | Add `--expect-stdout-not-contains ERROR` or a positive output assertion |
| 17 | **Unverifiable claim presented as fact** | "This is now secure", "This scales" | Turn it into specific, testable sub-claims or label it as opinion |
| 18 | **Verification with side effects** | Ran the migration on prod "to check" | Checks are read-only or sandboxed; risky checks are the user's call |

## Language to avoid in a final report unless backed by `VERIFIED` evidence

"works", "fixed", "resolved", "all tests pass", "fully", "complete", "done",
"production-ready", "no issues", "should be fine", "everything is set up".

## Language that is always acceptable

"I ran `X`; it exited 0 and printed `Y`." · "I could not verify `Z` because …;
to check it, run …" · "`C3` failed: …" · "I inspected `file` and saw …
(not executed)."
