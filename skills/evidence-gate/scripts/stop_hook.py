#!/usr/bin/env python3
"""
evidence-gate enforcement hook.

Makes the gate automatic instead of relying on the agent remembering it.

Modes
  claude  (default)  Claude Code `Stop` / `SubagentStop` hook. Reads the hook
                     JSON from stdin. If an evidence ledger exists for the
                     project and its gate is CLOSED, prints
                     {"decision": "block", "reason": ...} so the agent must
                     either prove the open claims or report the task as
                     partial. Blocks at most once per stop attempt
                     (`stop_hook_active`), so it can never trap the agent in
                     a loop. Without a ledger it does nothing.
  exit-code          For git hooks (pre-push), CI jobs, other agent
                     frameworks: prints the gate table and exits 1 if the
                     gate is closed, 0 if open or if there is no ledger
                     (pass --require-ledger to fail when it is missing).

Environment
  EVIDENCE_LEDGER               ledger path (default <project>/.evidence/ledger.json)
  EVIDENCE_GATE_ALLOW_ATTESTED  "1" to accept ATTESTED claims
  CLAUDE_PROJECT_DIR            project root provided by Claude Code

Standard library only; never touches the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ledger  # noqa: E402


def _read_hook_input() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(project_dir: str) -> str:
    explicit = os.environ.get(ledger.ENV_LEDGER)
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(project_dir, explicit)
    return os.path.join(project_dir, ledger.DEFAULT_LEDGER)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=["claude", "exit-code"], default="claude")
    parser.add_argument("--require-ledger", action="store_true",
                        help="exit-code mode: fail when no ledger exists")
    parser.add_argument("--project-dir", help="override the project directory")
    args = parser.parse_args(argv)

    hook = _read_hook_input() if args.mode == "claude" else {}
    if hook.get("stop_hook_active"):
        return 0  # already blocked once for this stop; never loop

    project_dir = (args.project_dir or os.environ.get("CLAUDE_PROJECT_DIR")
                   or hook.get("cwd") or os.getcwd())
    path = _resolve(project_dir)
    allow_attested = os.environ.get("EVIDENCE_GATE_ALLOW_ATTESTED") == "1"

    if not os.path.exists(path):
        if args.mode == "exit-code" and args.require_ledger:
            print("evidence-gate: no ledger at {}".format(path))
            return 1
        return 0

    cwd = os.getcwd()
    try:
        os.chdir(project_dir)  # relative paths in messages are relative to the project
        data = ledger.load(path)
        rows = ledger.summarize(data)
    except ledger.LedgerError as exc:
        # A broken ledger must not silently pass, but must not wedge the agent either.
        msg = "evidence-gate: cannot evaluate ledger: {}".format(exc)
        if args.mode == "claude":
            print(json.dumps({"decision": "block", "reason": msg}))
            return 0
        print(msg)
        return 1
    finally:
        os.chdir(cwd)

    is_open = ledger.gate_open(rows, allow_attested)
    if args.mode == "exit-code":
        print("Task: " + data["task"])
        ledger._print_table(rows)
        print("\n" + ledger.gate_message(rows, allow_attested))
        return 0 if is_open else 1

    if is_open:
        return 0
    blockers = [r for r in rows if r["state"] not in
                ({ledger.VERIFIED, ledger.WAIVED} | ({ledger.ATTESTED} if allow_attested else set()))]
    lines = ["evidence-gate: the completion gate for task '{}' is CLOSED.".format(data["task"])]
    for r in blockers:
        lines.append("  - {} [{}] {} -> {}".format(r["id"], r["state"], r["text"], r["reason"]))
    lines += [
        "Before finishing, do ONE of the following:",
        "  1. Prove the open claims (fix, then `ledger.py reverify`), or",
        "  2. `ledger.py defer ID --reason ... --user-check ...` for claims that cannot be checked here,",
        "     then tell the user the task is PARTIAL (or `waive` if the user agreed to skip it), or",
        "  If a claim is STALE only because of a non-deliverable file (e.g. REPORT.md),",
        "  run `ledger.py ignore <file>`; never ignore source, tests or config.",
        "  3. Tell the user plainly that the task is PARTIAL and list what is unproven.",
        "Do not describe the task as complete while any claim above is open.",
    ]
    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output cut short (e.g. `| head`): fail closed, never report success
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)
