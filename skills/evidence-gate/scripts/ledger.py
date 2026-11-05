#!/usr/bin/env python3
"""
evidence-gate ledger: a dependency-free evidence ledger and completion gate
for AI agents (and humans).

Workflow:  init -> claim -> verify/attest/waive -> gate -> report

Design principles
  * Standard library only (Python 3.8+). No network access, no telemetry.
  * Commands run locally, without a shell unless --shell is passed.
  * Stored output is truncated and scrubbed of common secret patterns.
  * Evidence can "watch" files/directories; if they change after a check,
    the evidence becomes STALE and the gate closes.
  * State lives in one JSON file (default: .evidence/ledger.json), written
    atomically.

Exit codes:  0 = success / gate open
             1 = check failed / gate closed
             2 = usage or ledger error

Run `python3 ledger.py --help` or `python3 ledger.py <command> --help`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.0.0"

LEDGER_SCHEMA = 1
DEFAULT_LEDGER = os.path.join(".evidence", "ledger.json")
ENV_LEDGER = "EVIDENCE_LEDGER"

TAIL_LINES = 40
TAIL_CHARS = 8000
MAX_WATCH_FILES = 50000
SKIP_DIRS = {".git", ".hg", ".svn", ".evidence", "__pycache__", "node_modules",
             ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox", ".idea"}

# Claim states
VERIFIED = "VERIFIED"
ATTESTED = "ATTESTED"
UNVERIFIED = "UNVERIFIED"
FAILED = "FAILED"
STALE = "STALE"
WAIVED = "WAIVED"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class LedgerError(Exception):
    """Raised for user-facing errors (bad ids, missing ledger, ...)."""


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #

_REDACTIONS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "[REDACTED GITLAB TOKEN]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED API KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED GOOGLE KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED JWT]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[A-Z0-9_]*)"
                r"(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3"), r"\1\2\3[REDACTED]\3"),
]


def redact(text: str) -> str:
    """Mask common credential formats so they never land in the ledger."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def tail(text: str, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    parts = text.splitlines()
    clipped = "\n".join(parts[-lines:]) if len(parts) > lines else text.rstrip("\n")
    if len(clipped) > chars:
        clipped = clipped[-chars:]
    omitted = len(parts) - min(len(parts), lines)
    if omitted > 0:
        clipped = "[... {} earlier line(s) omitted ...]\n{}".format(omitted, clipped)
    return redact(clipped)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def ledger_path(arg: Optional[str]) -> str:
    return arg or os.environ.get(ENV_LEDGER) or DEFAULT_LEDGER


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path: str) -> str:
    """Content fingerprint of a file or directory tree ('MISSING' if absent)."""
    if os.path.isfile(path):
        return "file:" + _sha256_file(path)
    if os.path.isdir(path):
        h = hashlib.sha256()
        count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                try:
                    digest = _sha256_file(full)
                except OSError:
                    digest = "UNREADABLE"
                h.update(rel.encode("utf-8", "surrogateescape") + b"\0" + digest.encode() + b"\n")
                count += 1
                if count > MAX_WATCH_FILES:
                    raise LedgerError("watch path {!r} has more than {} files; watch a narrower path"
                                      .format(path, MAX_WATCH_FILES))
        return "dir:{}:{}".format(count, h.hexdigest())
    return "MISSING"


def snapshot(paths: List[str]) -> Dict[str, str]:
    return {p: fingerprint(p) for p in paths}


def changed_watches(watch: Dict[str, str]) -> List[str]:
    return [p for p, fp in sorted(watch.items()) if fingerprint(p) != fp]


# --------------------------------------------------------------------------- #
# Ledger persistence
# --------------------------------------------------------------------------- #

def load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise LedgerError("no ledger at {!r}; run `init` first (or pass --ledger)".format(path))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LedgerError("cannot read ledger {!r}: {}".format(path, exc))
    if data.get("schema") != LEDGER_SCHEMA:
        raise LedgerError("unsupported ledger schema {!r}".format(data.get("schema")))
    return data


def save(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def get_claim(data: Dict[str, Any], cid: str) -> Dict[str, Any]:
    try:
        return data["claims"][cid]
    except KeyError:
        known = ", ".join(data["claims"]) or "none"
        raise LedgerError("unknown claim {!r} (known: {})".format(cid, known))


# --------------------------------------------------------------------------- #
# State evaluation
# --------------------------------------------------------------------------- #

def evaluate(claim: Dict[str, Any]) -> Tuple[str, str]:
    """Return (state, reason) for a claim."""
    if claim.get("waived"):
        return WAIVED, claim["waived"].get("reason", "")
    final = [e for e in claim.get("evidence", []) if e.get("label") != "before"]
    runs = [e for e in final if e["kind"] == "run"]
    attests = [e for e in final if e["kind"] == "attest"]
    if runs:
        ev = runs[-1]
        if not ev["passed"]:
            return FAILED, ev.get("failure", "check failed")
        changed = changed_watches(ev.get("watch", {}))
        if changed:
            return STALE, "changed since check: " + ", ".join(changed)
        return VERIFIED, "exit {} at {}".format(ev["exit_code"], ev["timestamp"])
    if attests:
        ev = attests[-1]
        changed = changed_watches(ev.get("watch", {}))
        if changed:
            return STALE, "changed since attestation: " + ", ".join(changed)
        return ATTESTED, ev.get("note", "")
    return UNVERIFIED, "no evidence recorded"


def summarize(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for cid, claim in data["claims"].items():
        state, reason = evaluate(claim)
        rows.append({"id": cid, "text": claim["text"], "state": state, "reason": reason})
    return rows


def gate_open(rows: List[Dict[str, Any]], allow_attested: bool) -> bool:
    if not rows:
        return False
    ok = {VERIFIED, WAIVED} | ({ATTESTED} if allow_attested else set())
    return all(r["state"] in ok for r in rows)


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #

def run_check(command: List[str], use_shell: bool, timeout: float, cwd: Optional[str]) -> Dict[str, Any]:
    if use_shell:
        script = " ".join(command)
        bash = shutil.which("bash")
        argv = [bash, "-o", "pipefail", "-c", script] if bash else None
        popen_args: Any = argv if argv else script
        shell = argv is None
    else:
        popen_args, shell = command, False
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(popen_args, shell=shell, cwd=cwd, capture_output=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        exit_code = proc.returncode
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, exit_code = True, None
        out, err = exc.stdout or b"", exc.stderr or b""
    except FileNotFoundError as exc:
        exit_code, out, err = 127, b"", str(exc).encode()
    except PermissionError as exc:
        exit_code, out, err = 126, b"", str(exc).encode()
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": out.decode("utf-8", "replace") if isinstance(out, bytes) else out,
        "stderr": err.decode("utf-8", "replace") if isinstance(err, bytes) else err,
        "duration_s": round(time.monotonic() - start, 3),
    }


def check_assertions(result: Dict[str, Any], args: argparse.Namespace) -> Tuple[bool, List[Dict[str, Any]], str]:
    assertions: List[Dict[str, Any]] = []
    stdout = result["stdout"]

    exit_ok = (not result["timed_out"]) and result["exit_code"] == args.expect_exit
    assertions.append({"type": "exit_code", "expected": args.expect_exit,
                       "actual": result["exit_code"], "ok": exit_ok})
    for needle in args.expect_stdout_contains or []:
        assertions.append({"type": "stdout_contains", "value": redact(needle), "ok": needle in stdout})
    for needle in args.expect_stdout_not_contains or []:
        assertions.append({"type": "stdout_not_contains", "value": redact(needle), "ok": needle not in stdout})
    for pattern in args.expect_stdout_regex or []:
        try:
            ok = re.search(pattern, stdout, re.M) is not None
        except re.error as exc:
            raise LedgerError("invalid --expect-stdout-regex {!r}: {}".format(pattern, exc))
        assertions.append({"type": "stdout_regex", "value": pattern, "ok": ok})

    failed = [a for a in assertions if not a["ok"]]
    if result["timed_out"]:
        reason = "timed out after {}s".format(args.timeout)
    elif failed:
        a = failed[0]
        if a["type"] == "exit_code":
            reason = "exit code {} (expected {})".format(a["actual"], a["expected"])
        else:
            reason = "{} failed: {!r}".format(a["type"], a["value"])
    else:
        reason = ""
    return not failed, assertions, reason


# --------------------------------------------------------------------------- #
# Sub-commands
# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    if os.path.exists(path) and not args.force:
        raise LedgerError("ledger already exists at {!r}; use --force to start over".format(path))
    save(path, {"schema": LEDGER_SCHEMA, "tool": "evidence-gate/" + __version__,
                "task": args.task, "created_at": now_iso(), "claims": {}})
    print("Initialized evidence ledger at {} for task: {}".format(path, args.task))
    return EXIT_OK


def cmd_claim(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    data = load(path)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", args.id):
        raise LedgerError("claim id must be 1-40 chars of letters, digits, '_', '-', '.'")
    if args.id in data["claims"] and not args.update:
        raise LedgerError("claim {!r} exists; pass --update to change its text".format(args.id))
    if args.id in data["claims"]:
        data["claims"][args.id]["text"] = args.text
        verb = "Updated"
    else:
        data["claims"][args.id] = {"text": args.text, "created_at": now_iso(), "evidence": []}
        verb = "Added"
    save(path, data)
    print("{} claim {}: {}".format(verb, args.id, args.text))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    data = load(path)
    claim = get_claim(data, args.id)
    command = list(args.command)
    if not command:
        raise LedgerError("no command given; usage: verify ID [options] -- <command> [args...]")

    result = run_check(command, args.shell, args.timeout, args.cwd)
    passed, assertions, failure = check_assertions(result, args)
    watch = snapshot(args.watch or [])  # fingerprint AFTER the check ran

    evidence = {
        "kind": "run",
        "label": args.label,
        "timestamp": now_iso(),
        "command": [redact(c) for c in command],
        "shell": bool(args.shell),
        "cwd": os.path.abspath(args.cwd or os.getcwd()),
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "duration_s": result["duration_s"],
        "assertions": assertions,
        "passed": passed,
        "failure": failure,
        "stdout_sha256": hashlib.sha256(result["stdout"].encode("utf-8", "replace")).hexdigest(),
        "stdout_tail": tail(result["stdout"]),
        "stderr_tail": tail(result["stderr"]),
        "watch": watch,
        "note": args.note or "",
    }
    claim.setdefault("evidence", []).append(evidence)
    claim.pop("waived", None)
    save(path, data)

    shown = " ".join(evidence["command"])
    if args.label == "before":
        verdict = "BASELINE OK (failed as expected)" if passed else "BASELINE UNEXPECTED"
    else:
        verdict = "PASS" if passed else "FAIL"
    print("[{}] {} :: {}  (exit {}, {}s)".format(verdict, args.id, shown,
                                                 result["exit_code"], result["duration_s"]))
    if not passed:
        print("  reason: " + failure)
        if evidence["stderr_tail"]:
            print("  stderr (tail):\n    " + evidence["stderr_tail"].replace("\n", "\n    "))
        elif evidence["stdout_tail"]:
            print("  stdout (tail):\n    " + evidence["stdout_tail"].replace("\n", "\n    "))
    return EXIT_OK if passed else EXIT_FAIL


def cmd_attest(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    data = load(path)
    claim = get_claim(data, args.id)
    claim.setdefault("evidence", []).append({
        "kind": "attest", "label": "final", "timestamp": now_iso(),
        "note": redact(args.note), "watch": snapshot(args.watch or []),
    })
    claim.pop("waived", None)
    save(path, data)
    print("[ATTESTED] {} :: {}".format(args.id, args.note))
    return EXIT_OK


def cmd_waive(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    data = load(path)
    claim = get_claim(data, args.id)
    claim["waived"] = {"reason": redact(args.reason), "timestamp": now_iso()}
    save(path, data)
    print("[WAIVED] {} :: {}".format(args.id, args.reason))
    return EXIT_OK


def _print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("(no claims recorded)")
        return
    w_id = max(len("ID"), *(len(r["id"]) for r in rows))
    w_st = max(len("STATE"), *(len(r["state"]) for r in rows))
    print("{:<{}}  {:<{}}  CLAIM".format("ID", w_id, "STATE", w_st))
    for r in rows:
        print("{:<{}}  {:<{}}  {}".format(r["id"], w_id, r["state"], w_st, r["text"]))
        if r["state"] != VERIFIED and r["reason"]:
            print("{:<{}}  {:<{}}  -> {}".format("", w_id, "", w_st, r["reason"]))


def cmd_status(args: argparse.Namespace) -> int:
    data = load(ledger_path(args.ledger))
    rows = summarize(data)
    if args.json:
        print(json.dumps({"task": data["task"], "claims": rows}, indent=2, ensure_ascii=False))
    else:
        print("Task: " + data["task"])
        _print_table(rows)
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    data = load(ledger_path(args.ledger))
    rows = summarize(data)
    is_open = gate_open(rows, args.allow_attested)
    if args.json:
        print(json.dumps({"task": data["task"], "gate": "OPEN" if is_open else "CLOSED",
                          "allow_attested": args.allow_attested, "claims": rows},
                         indent=2, ensure_ascii=False))
    else:
        print("Task: " + data["task"])
        _print_table(rows)
        print()
        if is_open:
            print("GATE OPEN - every claim is backed by evidence. You may report completion.")
        else:
            blockers = [r for r in rows if r["state"] not in
                        ({VERIFIED, WAIVED} | ({ATTESTED} if args.allow_attested else set()))]
            if not rows:
                print("GATE CLOSED - no claims recorded. Enumerate the requirements first.")
            else:
                print("GATE CLOSED - {} claim(s) block completion: {}".format(
                    len(blockers), ", ".join("{} ({})".format(b["id"], b["state"]) for b in blockers)))
                print("Do NOT report this task as complete. Fix and re-verify, or report it as partial.")
    return EXIT_OK if is_open else EXIT_FAIL


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def cmd_report(args: argparse.Namespace) -> int:
    data = load(ledger_path(args.ledger))
    rows = summarize(data)
    if args.format == "json":
        out = dict(data)
        out["summary"] = rows
        out["gate"] = "OPEN" if gate_open(rows, args.allow_attested) else "CLOSED"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return EXIT_OK

    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    is_open = gate_open(rows, args.allow_attested)
    outcome = "COMPLETE" if is_open else "INCOMPLETE"
    tally = ", ".join("{} {}".format(v, k) for k, v in sorted(counts.items())) or "no claims"

    if args.format == "text":
        print("Task: {}\nOutcome: {} ({})".format(data["task"], outcome, tally))
        _print_table(rows)
        return EXIT_OK

    lines = ["### Evidence report: {}".format(_md_escape(data["task"])), "",
             "**Outcome: {}** ({})".format(outcome, tally), "",
             "| ID | Claim | Status | Evidence |", "|---|---|---|---|"]
    details: List[str] = []
    for r in rows:
        claim = data["claims"][r["id"]]
        final = [e for e in claim.get("evidence", []) if e.get("label") != "before"]
        ev = final[-1] if final else None
        if r["state"] == WAIVED:
            evidence = "waived: " + r["reason"]
        elif ev and ev["kind"] == "run":
            evidence = "`{}` -> exit {}".format(" ".join(ev["command"]), ev["exit_code"])
            extra = [a for a in ev["assertions"] if a["type"] != "exit_code"]
            if extra:
                evidence += ", {} output assertion(s) {}".format(
                    len(extra), "passed" if all(a["ok"] for a in extra) else "FAILED")
        elif ev:
            evidence = "inspection: " + ev.get("note", "")
        else:
            evidence = "none"
        if r["state"] in (FAILED, STALE, UNVERIFIED) and r["reason"]:
            evidence += " ({})".format(r["reason"])
        lines.append("| {} | {} | {} | {} |".format(r["id"], _md_escape(r["text"]), r["state"],
                                                    _md_escape(evidence)))
        if r["state"] == FAILED and ev and ev["kind"] == "run":
            output = ev["stderr_tail"] or ev["stdout_tail"] or "(no output)"
            details += ["", "<details><summary>{} failure output</summary>".format(r["id"]), "",
                        "```", output, "```", "</details>"]
        baseline = [e for e in claim.get("evidence", []) if e.get("label") == "before"]
        if baseline and r["state"] == VERIFIED:
            b = baseline[-1]
            details.append("- {}: red-before-green baseline `{}` exited {} before the change.".format(
                r["id"], " ".join(b["command"]), b["exit_code"]))
    lines += details
    if not is_open:
        lines += ["", "> Gate closed: this task must be reported as partially complete."]
    print("\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ledger.py",
        description="Evidence ledger and completion gate: no claim of success without fresh proof.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("--ledger", help="ledger file (default: ${} or {})".format(ENV_LEDGER, DEFAULT_LEDGER))
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("init", help="start a ledger for a task")
    s.add_argument("--task", required=True, help="one-line description of the task")
    s.add_argument("--force", action="store_true", help="overwrite an existing ledger")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("claim", help="add an acceptance claim")
    s.add_argument("id", help="short id, e.g. C1")
    s.add_argument("text", help="atomic, falsifiable statement")
    s.add_argument("--update", action="store_true", help="change the text of an existing claim")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("verify", help="run a check and record it as evidence",
                       usage="%(prog)s ID [options] -- COMMAND [ARGS...]")
    s.add_argument("id")
    s.add_argument("--expect-exit", type=int, default=0, help="required exit code (default 0)")
    s.add_argument("--expect-stdout-contains", action="append", metavar="TEXT")
    s.add_argument("--expect-stdout-not-contains", action="append", metavar="TEXT")
    s.add_argument("--expect-stdout-regex", action="append", metavar="REGEX",
                   help="Python regex searched in stdout (multiline mode)")
    s.add_argument("--watch", action="append", metavar="PATH",
                   help="file/dir whose later modification makes this evidence STALE (repeatable)")
    s.add_argument("--label", choices=["final", "before"], default="final",
                   help="'before' records a red-before-green baseline that does not count as proof")
    s.add_argument("--shell", action="store_true",
                   help="run COMMAND through bash -o pipefail -c (needed for pipes/redirects)")
    s.add_argument("--timeout", type=float, default=600.0, help="seconds (default 600)")
    s.add_argument("--cwd", help="working directory for the command")
    s.add_argument("--note", help="free-form context stored with the evidence")
    s.set_defaults(func=cmd_verify, command=[])

    s = sub.add_parser("attest", help="record inspection-only (weaker) evidence")
    s.add_argument("id")
    s.add_argument("--note", required=True, help="what exactly you inspected and observed")
    s.add_argument("--watch", action="append", metavar="PATH")
    s.set_defaults(func=cmd_attest)

    s = sub.add_parser("waive", help="explicitly exclude a claim, with a reason the user will see")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_waive)

    s = sub.add_parser("status", help="show the state of every claim")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("gate", help="exit 0 only if all claims are proven")
    s.add_argument("--allow-attested", action="store_true", help="accept ATTESTED claims")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_gate)

    s = sub.add_parser("report", help="print a paste-ready evidence report")
    s.add_argument("--format", choices=["md", "text", "json"], default="md")
    s.add_argument("--allow-attested", action="store_true")
    s.set_defaults(func=cmd_report)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` is the command to verify, verbatim.
    command: List[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if command:
        if args.cmd != "verify":
            parser.error("`--` COMMAND is only valid with `verify`")
        args.command = command
    try:
        return args.func(args)
    except LedgerError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
