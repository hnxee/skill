---
name: evidence-gate
description: >-
  Evidence-backed completion protocol for AI agents. Turns a task into explicit,
  checkable acceptance claims, proves each one with a fresh, reproducible check
  (command exit code, output assertion, or a clearly labelled manual attestation),
  invalidates evidence when the files it covered change, and blocks "done" until
  every claim is proven. Use whenever you are about to say a task is finished,
  fixed, working, passing, deployed or verified; when a task has more than one
  acceptance criterion; when you modified code, config or data and must report the
  outcome; or when the user says "make sure", "verify", "double-check", "prove it",
  "are you sure?", "did you test it?" or "don't tell me it works unless it works".
  Not for pure conversation, brainstorming or questions that change nothing.
license: MIT
metadata:
  version: 1.3.0
  category: reliability
  requires: python>=3.8 (standard library only), optional
  portable: true
---

# Evidence Gate

> **An agent's report is only as good as the evidence behind it.**
> No claim of success without fresh, reproducible proof. No exceptions.

## Why this skill exists

The most damaging failure of today's AI agents is not that they cannot write code
or run tools — it is that they **report success they have not verified**:

- "All tests pass" — when the tests were never run, or ran before the last edit.
- "Fixed the bug" — when only the symptom in one path changed.
- "Done" — when 3 of 5 requirements were silently dropped.
- "It works" — based on reading the code, not executing it.
- Swallowed errors, truncated output read as success, `|| true` hiding failures.

Each of these converts a recoverable problem into a hidden one, and destroys the
trust that makes delegating to an agent worthwhile. This skill installs a small,
strict discipline — **claim → check → evidence → gate → honest report** — that
works in any agent, any language and any repository, with or without its helper
script.

## The Iron Rules

1. **No evidence, no claim.** Never state that something works, passes, is fixed
   or is complete unless a check you ran *after your last relevant change* proves it.
2. **Exit codes and assertions, not vibes.** A check is a command whose exit code
   (and optionally output) decides pass/fail. "The output looked fine" is not a check.
3. **Fresh beats remembered.** Evidence is invalidated by any later edit to the
   files it covers. Re-run, don't recall.
4. **Requirements are enumerated up front.** Every explicit and implicit
   requirement of the request becomes a numbered claim *before* you start working.
   Nothing is dropped silently — it is either proven, failed, or explicitly waived
   with a reason the user can see.
5. **Failures are reported, never hidden.** A failed or skipped check appears in
   the final report with its output. Never weaken, skip, delete or `|| true` a
   check to make it pass.
6. **Label confidence honestly.** Use exactly these levels:
   - `VERIFIED` — an executed check passed after the last change.
   - `ATTESTED` — checked by inspection only (reading a file, viewing a UI); say what you looked at.
   - `UNVERIFIED` — not checked; say why (no access, no test exists, too costly).
   - `FAILED` — checked and does not hold.
7. **Checks must not cause harm.** Verification is read-only or sandboxed by
   default. Never run migrations, deploys, paid API calls, destructive commands or
   anything touching production *just to verify*. If the only possible check is
   risky, mark the claim `UNVERIFIED` and tell the user how to check it.

## The protocol

### Step 1 — Extract claims (before doing the work)

Rewrite the request as a short list of atomic, falsifiable claims. Include
implicit requirements a competent engineer would assume.

```
C1  `parse_date("2024-02-30")` raises ValueError            (explicit)
C2  Existing test suite still passes                         (implicit: no regressions)
C3  New behaviour is covered by a test                       (implicit: maintainability)
C4  No files outside src/dates/ and tests/ were modified     (scope)
```

Good claims are **observable** ("command X exits 0", "file Y contains Z"), not
intentions ("handle dates better"). If a claim cannot be made observable, it will
end up `ATTESTED` or `UNVERIFIED` — decide that now, not at the end.

### Step 2 — Design a check per claim

For each claim, pick the *strongest affordable* check (see
`references/verification-recipes.md`):

| Strength | Kind of check |
|---|---|
| Strongest | Automated test that fails before the change and passes after |
| Strong | Running the real program/command with the real input and asserting on output |
| Medium | Static checks: type checker, linter, build, schema validation, `git diff --stat` for scope |
| Weak | Reading the code/file (→ `ATTESTED`) |
| None | Assumption (→ `UNVERIFIED`) |

**Red-before-green:** for bug fixes, run the reproduction *before* fixing and see
it fail. A check that never failed may not be testing anything.

### Step 3 — Do the work

Normal execution. Keep the claim list visible; add claims if the scope genuinely
grows, and say so.

### Step 4 — Run the checks, record the evidence

Run every check **after the final edit**. Record, per claim: the exact command,
exit code, the decisive lines of output, and the time. If any edit happens after a
check, that check is stale — re-run it.

### Step 5 — Gate

You may only describe the task as complete if **every claim is `VERIFIED`** or
explicitly waived/attested with a stated reason. Otherwise the task is
*partially complete* and you must say so in the first sentence of your report.

### Step 6 — Report honestly

Use the template in `references/report-template.md`. Lead with the outcome, then
the evidence table, then anything failed/unverified and what the user should do
about it. Never bury a failure below a success.

## Using the helper script (optional but recommended)

`scripts/ledger.py` is a dependency-free Python 3.8+ CLI that enforces the
protocol mechanically. It runs commands **locally only**, never touches the
network itself, redacts common secret patterns from stored output, and keeps its
state in a single JSON file (default `.evidence/ledger.json` in the current
directory — add `.evidence/` to `.gitignore` if you don't want it committed).

```bash
L="python3 path/to/evidence-gate/scripts/ledger.py"

$L init --task "Reject impossible dates in parse_date"
$L claim C1 "parse_date('2024-02-30') raises ValueError"
$L claim C2 "Full test suite passes"
$L claim C3 "Only src/dates and tests changed"
$L claim C4 "README documents the new behaviour"
$L claim C5 "Production API rejects the date too"

# Red-before-green: expect the reproduction to fail before the fix
$L verify C1 --expect-exit 1 --label before -- python3 -c "from dates import parse_date; parse_date('2024-02-30')"

# ... make the change ...

$L verify C1 -- python3 -m pytest tests/test_parse.py -q -k impossible
$L verify C2 -- python3 -m pytest -q
$L verify C3 -- $L changed --allow 'src/dates/*' --allow 'tests/*'   # scope check
$L attest C4 --note "Read README section 'Dates'; wording updated"   # weaker evidence
$L defer  C5 --reason "Needs production credentials" \
             --user-check "curl -fsS https://api.example.com/dates?d=2024-02-30"  # stays UNVERIFIED
$L ignore REPORT.md         # non-deliverables you write later must not stale the evidence

# ... any further edit makes the evidence above STALE (git work tree is watched) ...
$L reverify                 # re-run every recorded check after your LAST edit
$L gate                     # exit 0 only if all claims VERIFIED (or waived) -> here: closed by C5
$L report --format md       # outcome PARTIAL + "What you should do next" for C5
```

`defer` vs `waive`: use **`defer`** when a claim matters but cannot be checked in
this environment — it stays `UNVERIFIED`, the gate stays closed, and the report
tells the user exactly how to check it; the honest outcome is *partial*. Use
**`waive`** only when the user agreed the claim is out of scope.

Key behaviours:

- `verify` runs the command **without a shell** by default (pass `--shell` for
  pipelines), with a timeout (`--timeout`, default 600 s).
- Pass/fail is decided by `--expect-exit` (default 0) plus optional
  `--expect-stdout-contains`, `--expect-stdout-regex`, `--expect-stdout-not-contains`,
  `--expect-stderr-contains`, `--expect-output-contains` (stdout or stderr).
- The most recent evidence for a claim decides its state (a `defer` after an
  `attest` makes it `UNVERIFIED` again; a new passing run after a failure makes it `VERIFIED`).
- **Staleness is automatic inside a git work tree**: every check fingerprints the
  work tree (HEAD + content of changed/untracked files, ignoring `.gitignore`d
  files, tool caches and the ledger). Any later edit marks the evidence **STALE**,
  which closes the gate, and the reason names the files that changed. Outside
  git, or to be more precise, add `--watch PATH` (repeatable); opt out with
  `--no-git-watch`. `ignore GLOB` excludes non-deliverables such as your own
  report file — never source, tests or config; ignored patterns are listed in the report.
- `changed [--allow GLOB ...]` lists files changed vs HEAD (caches and the ledger
  excluded) and, with `--allow`, exits 1 if anything outside the scope changed.
- `reverify` re-runs the last recorded check of every claim (or `--only-stale`),
  which is the one-command way to satisfy "verify after the final edit".
- **Vacuous checks are rejected**: `true`, `echo ...`, `exit 0`, `... || true`,
  `; true`, `set +e`, `--collect-only`/`--dry-run` cannot count as evidence. The
  guard is heuristic — it stops the common tricks, not every weak check, so the
  "strongest affordable check" rule still applies.
- Timeouts kill the **whole process tree**, and background processes a check
  leaves behind are cleaned up.
- `--label before` records a red-before-green baseline without counting it as the final proof.
- `gate --allow-attested` lets `ATTESTED` claims pass when inspection is the best
  possible check; the report still labels them `ATTESTED`.
- Exit codes: `0` ok/gate open · `1` gate closed or check failed · `2` usage error.

## Enforcement: make the gate automatic

A protocol the agent can forget is a suggestion. `scripts/stop_hook.py` turns it
into a guard-rail:

- **Claude Code** — register it as a `Stop` (and optionally `SubagentStop`) hook.
  When a ledger exists and its gate is closed, the hook blocks the agent's first
  attempt to finish and tells it exactly which claims are open; the agent must
  prove them, waive them with a reason, or tell the user the task is partial. It
  blocks at most once per stop (`stop_hook_active`), so it can never loop, and it
  does nothing in projects without a ledger.

  ```json
  {
    "hooks": {
      "Stop": [{ "hooks": [{ "type": "command",
        "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/skills/evidence-gate/scripts/stop_hook.py" }] }]
    }
  }
  ```

- **Anything else** (git `pre-push`, CI, other agent frameworks):
  `python3 scripts/stop_hook.py --mode exit-code [--require-ledger]` prints the
  gate and exits 1 when it is closed.

If Python is unavailable, follow the protocol by hand: keep the claim list in your
working notes and paste the evidence table from `references/report-template.md`.

## Triggers — when to apply this skill

Apply it automatically, without being asked, when **any** of these is true:

- You are about to write "done", "fixed", "works", "passes", "complete",
  "deployed", "resolved", "verified" or an equivalent in any language.
- The task changed files, configuration, data or infrastructure.
- The request contains more than one requirement.
- The user expresses doubt or asks for certainty ("are you sure", "did you test it",
  "make sure", "verify", "double-check", "prove it").
- You are handing work to another agent or human who will rely on your report.
- A previous attempt in the same session claimed success and turned out wrong.

Skip it for read-only questions, explanations, brainstorming and casual conversation.

## Anti-patterns that violate the gate

See `references/failure-modes.md` for the full catalogue with detection tips.
The short list:

- Verifying before the last edit, then editing again.
- Running a narrower check than the claim (one test file for "all tests pass").
- Treating a non-zero exit as "probably flaky" without re-running and reporting it.
- Piping into `head`/`tail`/`grep` so the exit code becomes the pipe's, not the program's.
- Trusting a project's test wrapper (`run_tests.sh`, `make test`) without checking it
  can fail: a red-before-green baseline that does not go red exposes it.
- Editing a test's expectations to match buggy output.
- Mocking the very thing the claim is about.
- Reporting a summary number ("98% passing") without naming what failed.
- Saying "should work" — that phrase is an `UNVERIFIED` label in disguise.

## Files in this skill

| Path | Purpose |
|---|---|
| `SKILL.md` | This protocol (the only file an agent strictly needs) |
| `scripts/ledger.py` | Stdlib-only evidence ledger and completion gate |
| `scripts/stop_hook.py` | Enforcement hook (Claude Code `Stop` hook, or exit-code mode for git/CI) |
| `references/verification-recipes.md` | Strongest affordable check per task type |
| `references/failure-modes.md` | Catalogue of false-success patterns and how to catch them |
| `references/report-template.md` | Final report format with evidence table |
| `LICENSE` | MIT license |
| `tests/test_ledger.py` | Self-tests for the scripts (`python3 -m unittest discover -s tests`) |
| `evals/` | A/B benchmark with hidden checks grading correctness *and* report honesty; results and limits in `evals/README.md` |
