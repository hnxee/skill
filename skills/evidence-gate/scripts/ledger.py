#!/usr/bin/env python3
"""
evidence-gate ledger: a dependency-free evidence ledger and completion gate
for AI agents (and humans).

Workflow:  init -> claim -> verify/attest/waive -> (edit) -> reverify -> gate -> report

Design principles
  * Standard library only (Python 3.8+). No network access, no telemetry.
  * Commands run locally, without a shell unless --shell is passed; in shell
    mode `pipefail` is on so a pipe cannot hide a failing program.
  * Timeouts kill the whole process tree, not just the direct child.
  * Vacuous checks (`true`, `echo ok`, `... || true`, `--collect-only`) are
    rejected: they cannot count as evidence.
  * Evidence goes STALE when what it covered changes: explicit --watch paths,
    and (by default, inside a git work tree) any change to the work tree.
  * Stored output is truncated and scrubbed of common secret patterns.
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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.1.0"

LEDGER_SCHEMA = 1
DEFAULT_LEDGER = os.path.join(".evidence", "ledger.json")
ENV_LEDGER = "EVIDENCE_LEDGER"

TAIL_LINES = 40
TAIL_CHARS = 8000
MAX_WATCH_FILES = 50000
CMD_DISPLAY_CHARS = 100
SKIP_DIRS = {".git", ".hg", ".svn", ".evidence", "__pycache__", "node_modules",
             ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea"}
GIT_KEY = "git:"

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

# Absolute path of the ledger in use; excluded from git work-tree fingerprints
# so that saving the ledger never makes its own evidence stale.
_ACTIVE_LEDGER: Optional[str] = None


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
# Vacuous-check detection
# --------------------------------------------------------------------------- #

_NOOP_PROGRAMS = {"true", ":", "echo", "printf", "exit", "sleep", "yes"}
_MASKING = [
    (re.compile(r"\|\|\s*(?:true|:|exit\s+0|echo\b|printf\b)"), "`|| true`-style fallback masks failures"),
    (re.compile(r"(?:;|&&|\n)\s*(?:true|:|exit\s+0)\s*$"), "command ends by forcing success"),
    (re.compile(r"\bset\s+\+e\b"), "`set +e` disables failure detection"),
    (re.compile(r"(?:^|\s)(?:--collect-only|--co|--dry-run|--list-tests|--listTests)(?:\s|$)"),
     "collect/list/dry-run mode executes nothing"),
]


def vacuous_reason(command: List[str], use_shell: bool) -> str:
    """Return why a check cannot be meaningful evidence, or '' if it may be.

    This is a heuristic guard-rail, not a proof of quality: it catches the
    common ways a check is written so that it always passes.
    """
    text = " ".join(command).strip()
    if not text:
        return "empty command"
    for pattern, why in _MASKING:
        if pattern.search(text):
            return why
    try:
        tokens = shlex.split(text) if use_shell else list(command)
    except ValueError:
        tokens = text.split()
    first = os.path.basename(tokens[0]) if tokens else ""
    has_operator = use_shell and re.search(r"[|;&<>`$]", text) is not None
    if first in _NOOP_PROGRAMS and not has_operator:
        return "`{}` alone proves nothing about the claim".format(first)
    return ""


# --------------------------------------------------------------------------- #
# Fingerprints (staleness detection)
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


def _git(root: str, *args: str) -> Optional[bytes]:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run([git, "-C", root, *args], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_root(cwd: str) -> Optional[str]:
    out = _git(cwd, "rev-parse", "--show-toplevel")
    return os.path.abspath(out.decode().strip()) if out else None


def git_fingerprint(root: str) -> str:
    """Fingerprint of a git work tree: HEAD + content of every changed/untracked file.

    Ignored files (per .gitignore), tool caches (SKIP_DIRS) and the active
    ledger are excluded, so routine test runs do not invalidate evidence.
    """
    if not os.path.isdir(root):
        return "MISSING"
    head = _git(root, "rev-parse", "HEAD")
    pathspec = [".", ":(exclude).evidence"]
    if _ACTIVE_LEDGER:
        rel = os.path.relpath(_ACTIVE_LEDGER, root)
        if not rel.startswith(".."):
            pathspec.append(":(exclude){}".format(rel.replace(os.sep, "/")))
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspec)
    if status is None:
        return "GIT_ERROR"
    h = hashlib.sha256((head or b"NO_HEAD").strip() + b"\n")
    entries = status.split(b"\0")
    i, count = 0, 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:].decode("utf-8", "surrogateescape")
        if code[:1] in (b"R", b"C"):
            i += 1  # skip the rename/copy source path
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        full = os.path.join(root, rel)
        try:
            digest = _sha256_file(full) if os.path.isfile(full) else "ABSENT"
        except OSError:
            digest = "UNREADABLE"
        h.update(code + b" " + rel.encode("utf-8", "surrogateescape") + b"\0" + digest.encode() + b"\n")
        count += 1
        if count > MAX_WATCH_FILES:
            return "TOO_MANY_CHANGES"
    return "git:{}:{}".format(count, h.hexdigest())


def fingerprint(path: str) -> str:
    """Content fingerprint of a file, directory tree or git work tree ('MISSING' if absent)."""
    if path.startswith(GIT_KEY):
        return git_fingerprint(path[len(GIT_KEY):])
    if os.path.isfile(path):
        return "file:" + _sha256_file(path)
    if os.path.isdir(path):
        h = hashlib.sha256()
        count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                full = os.path.join(root, name)
                if _ACTIVE_LEDGER and os.path.abspath(full) == _ACTIVE_LEDGER:
                    continue
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


def watch_keys(paths: List[str], cwd: str, use_git: bool) -> List[str]:
    keys = [os.path.abspath(os.path.join(cwd, p)) for p in paths]
    if use_git:
        root = git_root(cwd)
        if root:
            keys.append(GIT_KEY + root)
    return keys


def snapshot(keys: List[str]) -> Dict[str, str]:
    return {k: fingerprint(k) for k in keys}


def display_key(key: str) -> str:
    if key.startswith(GIT_KEY):
        return "git work tree"
    try:
        rel = os.path.relpath(key)
    except ValueError:
        return key
    return key if rel.startswith("..") else rel


def changed_watches(watch: Dict[str, str]) -> List[str]:
    return [display_key(k) for k, fp in sorted(watch.items()) if fingerprint(k) != fp]


# --------------------------------------------------------------------------- #
# Ledger persistence
# --------------------------------------------------------------------------- #

def load(path: str) -> Dict[str, Any]:
    global _ACTIVE_LEDGER
    if not os.path.exists(path):
        raise LedgerError("no ledger at {!r}; run `init` first (or pass --ledger)".format(path))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LedgerError("cannot read ledger {!r}: {}".format(path, exc))
    if data.get("schema") != LEDGER_SCHEMA:
        raise LedgerError("unsupported ledger schema {!r}".format(data.get("schema")))
    _ACTIVE_LEDGER = os.path.abspath(path)
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

def final_evidence(claim: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in claim.get("evidence", []) if e.get("label") != "before"]


def evaluate(claim: Dict[str, Any]) -> Tuple[str, str]:
    """Return (state, reason) for a claim."""
    if claim.get("waived"):
        return WAIVED, claim["waived"].get("reason", "")
    final = final_evidence(claim)
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

def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_check(command: List[str], use_shell: bool, timeout: float, cwd: Optional[str]) -> Dict[str, Any]:
    if use_shell:
        script = " ".join(command)
        bash = shutil.which("bash")
        popen_args: Any = [bash, "-o", "pipefail", "-c", script] if bash else script
        shell = bash is None
    else:
        popen_args, shell = command, False
    kwargs: Dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # own process group -> killable as a tree
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(popen_args, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kwargs)
    except FileNotFoundError as exc:
        return _result(127, False, b"", str(exc).encode(), start)
    except PermissionError as exc:
        return _result(126, False, b"", str(exc).encode(), start)
    try:
        out, err = proc.communicate(timeout=timeout)
        exit_code: Optional[int] = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        exit_code = None
    if os.name == "posix" and not timed_out:
        _kill_tree(proc)  # reap background children the check left behind
    return _result(exit_code, timed_out, out, err, start)


def _result(exit_code: Optional[int], timed_out: bool, out: bytes, err: bytes, start: float) -> Dict[str, Any]:
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": (out or b"").decode("utf-8", "replace"),
        "stderr": (err or b"").decode("utf-8", "replace"),
        "duration_s": round(time.monotonic() - start, 3),
    }


def check_assertions(result: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]], str]:
    assertions: List[Dict[str, Any]] = []
    stdout = result["stdout"]

    exit_ok = (not result["timed_out"]) and result["exit_code"] == spec["expect_exit"]
    assertions.append({"type": "exit_code", "expected": spec["expect_exit"],
                       "actual": result["exit_code"], "ok": exit_ok})
    for needle in spec.get("expect_stdout_contains") or []:
        assertions.append({"type": "stdout_contains", "value": redact(needle), "ok": needle in stdout})
    for needle in spec.get("expect_stdout_not_contains") or []:
        assertions.append({"type": "stdout_not_contains", "value": redact(needle), "ok": needle not in stdout})
    for pattern in spec.get("expect_stdout_regex") or []:
        try:
            ok = re.search(pattern, stdout, re.M) is not None
        except re.error as exc:
            raise LedgerError("invalid --expect-stdout-regex {!r}: {}".format(pattern, exc))
        assertions.append({"type": "stdout_regex", "value": pattern, "ok": ok})

    failed = [a for a in assertions if not a["ok"]]
    if result["timed_out"]:
        reason = "timed out after {}s".format(spec["timeout"])
    elif failed:
        a = failed[0]
        if a["type"] == "exit_code":
            reason = "exit code {} (expected {})".format(a["actual"], a["expected"])
        else:
            reason = "{} failed: {!r}".format(a["type"], a["value"])
    else:
        reason = ""
    return not failed, assertions, reason


def short_cmd(command: List[str], limit: int = CMD_DISPLAY_CHARS) -> str:
    text = re.sub(r"\s+", " ", " ".join(command)).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def execute(data: Dict[str, Any], cid: str, spec: Dict[str, Any], label: str, note: str) -> Dict[str, Any]:
    """Run one check for claim `cid` according to `spec`, append the evidence, return it."""
    claim = get_claim(data, cid)
    command = spec["command"]
    cwd = spec["cwd"]
    vacuous = "" if spec.get("expect_exit", 0) != 0 else vacuous_reason(command, spec["shell"])
    if vacuous:
        result = _result(None, False, b"", b"", time.monotonic())
        passed, assertions, failure = False, [], "vacuous check rejected: " + vacuous
    else:
        result = run_check(command, spec["shell"], spec["timeout"], cwd)
        passed, assertions, failure = check_assertions(result, spec)
    # Fingerprint AFTER the check ran, so artifacts it produced are part of the baseline.
    watch = snapshot(watch_keys(spec.get("watch") or [], cwd, spec.get("git_watch", True)))
    evidence = {
        "kind": "run",
        "label": label,
        "timestamp": now_iso(),
        "command": [redact(c) for c in command],
        "spec": dict(spec, command=[redact(c) for c in command]),
        "shell": bool(spec["shell"]),
        "cwd": cwd,
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "duration_s": result["duration_s"],
        "assertions": assertions,
        "passed": passed,
        "vacuous": bool(vacuous),
        "failure": failure,
        "stdout_sha256": hashlib.sha256(result["stdout"].encode("utf-8", "replace")).hexdigest(),
        "stdout_tail": tail(result["stdout"]),
        "stderr_tail": tail(result["stderr"]),
        "watch": watch,
        "note": note,
    }
    claim.setdefault("evidence", []).append(evidence)
    claim.pop("waived", None)
    return evidence


def print_run(cid: str, ev: Dict[str, Any]) -> None:
    if ev["label"] == "before":
        verdict = "BASELINE OK (failed as expected)" if ev["passed"] else "BASELINE UNEXPECTED"
    else:
        verdict = "PASS" if ev["passed"] else "FAIL"
    print("[{}] {} :: {}  (exit {}, {}s)".format(verdict, cid, short_cmd(ev["command"], 160),
                                                 ev["exit_code"], ev["duration_s"]))
    if not ev["passed"]:
        print("  reason: " + ev["failure"])
        out = ev["stderr_tail"] or ev["stdout_tail"]
        if out:
            print("  output (tail):\n    " + out.replace("\n", "\n    "))


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
    get_claim(data, args.id)
    if not args.command:
        raise LedgerError("no command given; usage: verify ID [options] -- <command> [args...]")
    spec = {
        "command": list(args.command),
        "shell": bool(args.shell),
        "timeout": args.timeout,
        "cwd": os.path.abspath(args.cwd or os.getcwd()),
        "expect_exit": args.expect_exit,
        "expect_stdout_contains": args.expect_stdout_contains or [],
        "expect_stdout_not_contains": args.expect_stdout_not_contains or [],
        "expect_stdout_regex": args.expect_stdout_regex or [],
        "watch": args.watch or [],
        "git_watch": not args.no_git_watch,
    }
    ev = execute(data, args.id, spec, args.label, args.note or "")
    save(path, data)
    print_run(args.id, ev)
    return EXIT_OK if ev["passed"] else EXIT_FAIL


def cmd_reverify(args: argparse.Namespace) -> int:
    """Re-run the last recorded final check of each claim (after your last edit)."""
    path = ledger_path(args.ledger)
    data = load(path)
    ids = args.ids or list(data["claims"])
    ran, failed, skipped = 0, 0, []
    for cid in ids:
        claim = get_claim(data, cid)
        runs = [e for e in final_evidence(claim) if e["kind"] == "run" and "spec" in e]
        if not runs or claim.get("waived"):
            skipped.append(cid)
            continue
        state, _ = evaluate(claim)
        if args.only_stale and state == VERIFIED:
            continue
        spec = runs[-1]["spec"]
        if any("[REDACTED" in c for c in spec["command"]):
            print("[SKIP] {} :: command contained a redacted secret; re-run it with `verify`".format(cid))
            skipped.append(cid)
            continue
        ev = execute(data, cid, spec, "final", "reverify")
        save(path, data)  # save after each check so a crash loses nothing
        print_run(cid, ev)
        ran += 1
        failed += 0 if ev["passed"] else 1
    # A later check may have changed files covered by an earlier one: report final states.
    rows = [r for r in summarize(data) if r["id"] in ids]
    stale = [r["id"] for r in rows if r["state"] == STALE]
    print("\nre-ran {} check(s): {} failed{}{}".format(
        ran, failed,
        "; no recorded check for: " + ", ".join(skipped) if skipped else "",
        "; STALE after re-run (a check modifies files another check covers): " + ", ".join(stale)
        if stale else ""))
    return EXIT_OK if failed == 0 and not stale else EXIT_FAIL


def cmd_attest(args: argparse.Namespace) -> int:
    path = ledger_path(args.ledger)
    data = load(path)
    claim = get_claim(data, args.id)
    keys = watch_keys(args.watch or [], os.getcwd(), not args.no_git_watch)
    claim.setdefault("evidence", []).append({
        "kind": "attest", "label": "final", "timestamp": now_iso(),
        "note": redact(args.note), "watch": snapshot(keys),
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


def gate_message(rows: List[Dict[str, Any]], allow_attested: bool) -> str:
    if gate_open(rows, allow_attested):
        return "GATE OPEN - every claim is backed by evidence. You may report completion."
    if not rows:
        return "GATE CLOSED - no claims recorded. Enumerate the requirements first."
    ok = {VERIFIED, WAIVED} | ({ATTESTED} if allow_attested else set())
    blockers = [r for r in rows if r["state"] not in ok]
    return ("GATE CLOSED - {} claim(s) block completion: {}\n"
            "Do NOT report this task as complete. Fix and re-verify, or report it as partial."
            .format(len(blockers), ", ".join("{} ({})".format(b["id"], b["state"]) for b in blockers)))


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
        print(gate_message(rows, args.allow_attested))
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
        final = final_evidence(claim)
        ev = final[-1] if final else None
        if r["state"] == WAIVED:
            evidence = "waived: " + r["reason"]
        elif ev and ev["kind"] == "run":
            evidence = "`{}` -> exit {}".format(short_cmd(ev["command"]), ev["exit_code"])
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
        if r["state"] == FAILED and ev and ev["kind"] == "run" and not ev.get("vacuous"):
            output = ev["stderr_tail"] or ev["stdout_tail"] or "(no output)"
            details += ["", "<details><summary>{} failure output</summary>".format(r["id"]), "",
                        "```", output, "```", "</details>"]
        baseline = [e for e in claim.get("evidence", []) if e.get("label") == "before"]
        if baseline and r["state"] == VERIFIED:
            b = baseline[-1]
            details.append("- {}: red-before-green baseline `{}` exited {} before the change.".format(
                r["id"], short_cmd(b["command"]), b["exit_code"]))
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
    s.add_argument("--no-git-watch", action="store_true",
                   help="do not also watch the enclosing git work tree (on by default)")
    s.add_argument("--label", choices=["final", "before"], default="final",
                   help="'before' records a red-before-green baseline that does not count as proof")
    s.add_argument("--shell", action="store_true",
                   help="run COMMAND through bash -o pipefail -c (needed for pipes/redirects)")
    s.add_argument("--timeout", type=float, default=600.0, help="seconds (default 600)")
    s.add_argument("--cwd", help="working directory for the command")
    s.add_argument("--note", help="free-form context stored with the evidence")
    s.set_defaults(func=cmd_verify, command=[])

    s = sub.add_parser("reverify", help="re-run the recorded checks (do this after your last edit)")
    s.add_argument("ids", nargs="*", help="claim ids (default: all)")
    s.add_argument("--only-stale", action="store_true", help="skip claims that are still VERIFIED")
    s.set_defaults(func=cmd_reverify)

    s = sub.add_parser("attest", help="record inspection-only (weaker) evidence")
    s.add_argument("id")
    s.add_argument("--note", required=True, help="what exactly you inspected and observed")
    s.add_argument("--watch", action="append", metavar="PATH")
    s.add_argument("--no-git-watch", action="store_true")
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
    global _ACTIVE_LEDGER
    _ACTIVE_LEDGER = None
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
