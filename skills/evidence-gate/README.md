# evidence-gate

**A universal skill that stops AI agents from reporting success they have not proven.**

## The problem

Agents today are very good at *doing* and unreliable at *knowing whether what they
did worked*. The most expensive failure in real-world agent use is the confident,
unverified completion report: "all tests pass" when they were never run, "fixed"
when only one path changed, "done" when two of five requirements were silently
dropped. Each one turns a recoverable mistake into a hidden one and erodes the
trust that makes delegation worthwhile.

## The solution

`evidence-gate` installs a strict, lightweight loop:

```
enumerate claims  ->  design a check per claim  ->  do the work
      ->  run checks after the last edit  ->  gate  ->  honest report
```

- Every requirement (explicit *and* implicit) becomes a falsifiable claim.
- Each claim is proven by an executed check (exit code + optional output
  assertions), labelled as inspection-only, explicitly waived, or reported as
  unverified/failed.
- Evidence goes **stale** automatically when the files it covered change.
- "Done" is only allowed when the gate is open.

It works in any agent framework that loads `SKILL.md` files (or simply pasted into
a system prompt), any language and any repository. The optional helper script is
pure Python standard library, runs nothing over the network, and redacts common
secrets from what it stores.

## Layout

```
evidence-gate/
├── SKILL.md                          # the protocol (frontmatter + rules + triggers)
├── README.md                         # this file
├── LICENSE                           # MIT
├── scripts/
│   └── ledger.py                     # evidence ledger & completion gate (stdlib only)
├── references/
│   ├── verification-recipes.md       # strongest affordable check per task type
│   ├── failure-modes.md              # catalogue of false-success patterns
│   └── report-template.md            # final report format
└── tests/
    └── test_ledger.py                # self-tests for ledger.py
```

## Install

Copy the `evidence-gate/` folder into your agent's skills directory, for example:

| Agent | Location |
|---|---|
| Claude Code (project) | `.claude/skills/evidence-gate/` |
| Claude Code (user) | `~/.claude/skills/evidence-gate/` |
| Other agents | wherever the agent loads skills from, or paste `SKILL.md` into its instructions |

No dependencies. Python 3.8+ is needed only for the optional script.

## Quick start (helper script)

```bash
L="python3 skills/evidence-gate/scripts/ledger.py"
$L init --task "Add --json flag to the export command"
$L claim C1 "export --json prints valid JSON"
$L claim C2 "Existing tests still pass"

$L verify C1 --watch src -- sh -c 'mytool export --json | python3 -m json.tool >/dev/null'
$L verify C2 --watch src --watch tests -- python3 -m pytest -q

$L gate && $L report --format md
```

The ledger lives in `.evidence/ledger.json` by default (override with `--ledger` or
`$EVIDENCE_LEDGER`). Add `.evidence/` to your `.gitignore` if you don't want to
commit it.

## Run the self-tests

```bash
cd skills/evidence-gate && python3 -m unittest discover -s tests -v
```

## License

MIT
