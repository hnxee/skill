# Final report template

Lead with the outcome. Put failures *above* successes. Keep it short — the evidence
table carries the detail. `ledger.py report --format md` generates the table for you.

---

```markdown
**Outcome:** <COMPLETE | PARTIAL | BLOCKED> — <one sentence on what is now true>.

<If not COMPLETE, one or two sentences on what is missing and why, first.>

| ID | Claim | Status | Evidence |
|----|-------|--------|----------|
| C1 | <falsifiable statement> | VERIFIED | `<command>` → exit 0, `<decisive output line>` |
| C2 | <...> | FAILED | `<command>` → exit 1: `<error line>` |
| C3 | <...> | ATTESTED | Inspected `<file/view>`; observed <...> (not executed) |
| C4 | <...> | UNVERIFIED | <why it could not be checked> |
| C5 | <...> | WAIVED | <reason the user agreed to / needs to know> |

**What you should do next** (only if something is not VERIFIED):
- To verify C4 yourself: `<exact command or steps>`
- C2 needs a decision: <options>

**Changed files:** `<path>`, `<path>` (<n> files; nothing else touched)
```

---

## Rules for the report

1. **Outcome word is derived from the gate**, not from how the work felt:
   - `COMPLETE` — gate open (all `VERIFIED`, or `WAIVED` with a visible reason).
   - `PARTIAL` — some claims `FAILED`, `STALE`, `UNVERIFIED` or `ATTESTED`.
   - `BLOCKED` — the core claim cannot be achieved or checked without user input.
2. **Every `VERIFIED` row names the command.** If you can't name it, it isn't verified.
3. **Quote the decisive output line**, not a paraphrase ("`26 passed in 0.81s`").
4. **No orphan successes**: anything stated as working in the prose must have a row.
5. **Say where checks ran** when it is ambiguous (local / container / staging).
6. **Do not pad.** No victory laps, no restating the task. Evidence speaks.

## Minimal form (for small tasks)

> Done: `parse_date` now rejects `2024-02-30`. Verified with
> `pytest tests/test_parse.py -q` → `12 passed` (run after the last edit).
> Not verified: behaviour in the deployed API (no access); check with `curl …`.
