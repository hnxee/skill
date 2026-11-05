"""Self-tests for scripts/ledger.py.

Run from the skill directory:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ledger  # noqa: E402
import stop_hook  # noqa: E402

HAS_GIT = shutil.which("git") is not None
HAS_BASH = shutil.which("bash") is not None

PY = sys.executable


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "ledger.json")
        # Run every test from inside the temp dir so nothing touches the real work tree.
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.prev_cwd)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ledger.main(["--ledger", self.ledger, *argv])
        return code, out.getvalue(), err.getvalue()

    def init(self, *claims):
        self.assertEqual(self.run_cli("init", "--task", "demo task")[0], 0)
        for cid in claims:
            self.assertEqual(self.run_cli("claim", cid, "claim " + cid)[0], 0)

    def states(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        return {c["id"]: c["state"] for c in json.loads(out)["claims"]}


class TestLifecycle(LedgerTestCase):
    def test_gate_closed_without_claims(self):
        self.init()
        code, out, _ = self.run_cli("gate")
        self.assertEqual(code, 1)
        self.assertIn("no claims", out)

    def test_unverified_blocks_gate(self):
        self.init("C1")
        self.assertEqual(self.states(), {"C1": "UNVERIFIED"})
        self.assertEqual(self.run_cli("gate")[0], 1)

    def test_passing_check_opens_gate(self):
        self.init("C1")
        code, out, _ = self.run_cli("verify", "C1", "--", PY, "-c", "print('ok')")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.states(), {"C1": "VERIFIED"})
        self.assertEqual(self.run_cli("gate")[0], 0)

    def test_failing_check_is_recorded_and_blocks(self):
        self.init("C1")
        code, out, _ = self.run_cli("verify", "C1", "--", PY, "-c", "import sys; sys.exit(3)")
        self.assertEqual(code, 1)
        self.assertIn("exit code 3", out)
        self.assertEqual(self.states(), {"C1": "FAILED"})
        self.assertEqual(self.run_cli("gate")[0], 1)

    def test_latest_run_wins(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--", PY, "-c", "import sys; sys.exit(1)")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        self.assertEqual(self.states(), {"C1": "VERIFIED"})

    def test_missing_binary_fails_cleanly(self):
        self.init("C1")
        code, _, _ = self.run_cli("verify", "C1", "--", "definitely-not-a-real-binary-xyz")
        self.assertEqual(code, 1)
        self.assertEqual(self.states(), {"C1": "FAILED"})

    def test_timeout_fails(self):
        self.init("C1")
        code, out, _ = self.run_cli("verify", "C1", "--timeout", "0.5", "--",
                                    PY, "-c", "import time; time.sleep(5)")
        self.assertEqual(code, 1)
        self.assertIn("timed out", out)


class TestAssertions(LedgerTestCase):
    def test_stdout_contains_and_regex(self):
        self.init("C1", "C2", "C3")
        script = "print('3 passed in 0.1s')"
        self.assertEqual(self.run_cli("verify", "C1", "--expect-stdout-contains", "passed",
                                      "--", PY, "-c", script)[0], 0)
        self.assertEqual(self.run_cli("verify", "C2", "--expect-stdout-regex", r"^\d+ passed",
                                      "--", PY, "-c", script)[0], 0)
        self.assertEqual(self.run_cli("verify", "C3", "--expect-stdout-not-contains", "passed",
                                      "--", PY, "-c", script)[0], 1)
        self.assertEqual(self.states(), {"C1": "VERIFIED", "C2": "VERIFIED", "C3": "FAILED"})

    def test_expected_nonzero_exit(self):
        self.init("C1")
        code, _, _ = self.run_cli("verify", "C1", "--expect-exit", "2",
                                  "--", PY, "-c", "import sys; sys.exit(2)")
        self.assertEqual(code, 0)

    def test_shell_mode_uses_pipefail(self):
        if not ledger.shutil.which("bash"):
            self.skipTest("bash not available")
        self.init("C1")
        code, _, _ = self.run_cli("verify", "C1", "--shell", "--", "false | cat")
        self.assertEqual(code, 1, "a failing program must not be masked by the pipe")


class TestBaselineAndStaleness(LedgerTestCase):
    def test_before_label_does_not_count_as_proof(self):
        self.init("C1")
        code, out, _ = self.run_cli("verify", "C1", "--label", "before", "--expect-exit", "1",
                                    "--", PY, "-c", "import sys; sys.exit(1)")
        self.assertEqual(code, 0)
        self.assertIn("BASELINE OK", out)
        self.assertEqual(self.states(), {"C1": "UNVERIFIED"})
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        _, report, _ = self.run_cli("report")
        self.assertIn("red-before-green baseline", report)

    def test_edit_after_check_makes_evidence_stale(self):
        self.init("C1")
        target = os.path.join(self.tmp.name, "src", "module.py")
        os.makedirs(os.path.dirname(target))
        with open(target, "w") as fh:
            fh.write("x = 1\n")
        self.run_cli("verify", "C1", "--watch", os.path.dirname(target), "--", PY, "-c", "pass")
        self.assertEqual(self.states(), {"C1": "VERIFIED"})
        with open(target, "a") as fh:
            fh.write("x = 2\n")
        self.assertEqual(self.states(), {"C1": "STALE"})
        self.assertEqual(self.run_cli("gate")[0], 1)
        self.run_cli("verify", "C1", "--watch", os.path.dirname(target), "--", PY, "-c", "pass")
        self.assertEqual(self.states(), {"C1": "VERIFIED"})

    def test_deleted_watched_file_is_stale(self):
        self.init("C1")
        target = os.path.join(self.tmp.name, "f.txt")
        with open(target, "w") as fh:
            fh.write("a")
        self.run_cli("attest", "C1", "--note", "read it", "--watch", target)
        self.assertEqual(self.states(), {"C1": "ATTESTED"})
        os.remove(target)
        self.assertEqual(self.states(), {"C1": "STALE"})


class TestAttestAndWaive(LedgerTestCase):
    def test_attested_needs_flag(self):
        self.init("C1")
        self.run_cli("attest", "C1", "--note", "inspected README")
        self.assertEqual(self.run_cli("gate")[0], 1)
        self.assertEqual(self.run_cli("gate", "--allow-attested")[0], 0)

    def test_waive_opens_gate_but_is_reported(self):
        self.init("C1", "C2")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        self.run_cli("waive", "C2", "--reason", "needs prod credentials")
        self.assertEqual(self.run_cli("gate")[0], 0)
        _, report, _ = self.run_cli("report")
        self.assertIn("WAIVED", report)
        self.assertIn("needs prod credentials", report)

    def test_new_evidence_clears_waiver(self):
        self.init("C1")
        self.run_cli("waive", "C1", "--reason", "later")
        self.run_cli("verify", "C1", "--", PY, "-c", "import sys; sys.exit(1)")
        self.assertEqual(self.states(), {"C1": "FAILED"})


class TestSafetyAndErrors(LedgerTestCase):
    def test_secrets_are_redacted_in_stored_output(self):
        self.init("C1")
        leak = "token=ghp_" + "a" * 36 + " API_KEY=supersecretvalue sk-" + "b" * 30
        self.run_cli("verify", "C1", "--", PY, "-c", "print({!r})".format(leak))
        with open(self.ledger) as fh:
            raw = fh.read()
        self.assertNotIn("a" * 36, raw)
        self.assertNotIn("supersecretvalue", raw)
        self.assertNotIn("b" * 30, raw)
        self.assertIn("REDACTED", raw)

    def test_redact_connection_string_password(self):
        self.assertEqual(ledger.redact("postgres://user:hunter2@db:5432/x"),
                         "postgres://user:[REDACTED]@db:5432/x")

    def test_output_tail_is_bounded(self):
        text = "\n".join("line %d" % i for i in range(1000))
        clipped = ledger.tail(text)
        self.assertIn("omitted", clipped)
        self.assertIn("line 999", clipped)
        self.assertNotIn("line 10\n", clipped)

    def test_unknown_claim_is_usage_error(self):
        self.init()
        code, _, err = self.run_cli("verify", "NOPE", "--", PY, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("unknown claim", err)

    def test_missing_ledger_is_usage_error(self):
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("init", err)

    def test_init_refuses_to_overwrite(self):
        self.init()
        self.assertEqual(self.run_cli("init", "--task", "again")[0], 2)
        self.assertEqual(self.run_cli("init", "--task", "again", "--force")[0], 0)

    def test_duplicate_claim_requires_update(self):
        self.init("C1")
        self.assertEqual(self.run_cli("claim", "C1", "other")[0], 2)
        self.assertEqual(self.run_cli("claim", "C1", "other", "--update")[0], 0)

    def test_missing_command_is_usage_error(self):
        self.init("C1")
        self.assertEqual(self.run_cli("verify", "C1")[0], 2)


class TestReport(LedgerTestCase):
    def test_markdown_report_marks_incomplete(self):
        self.init("C1", "C2")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        self.run_cli("verify", "C2", "--", PY, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(1)")
        code, report, _ = self.run_cli("report", "--format", "md")
        self.assertEqual(code, 0)
        self.assertIn("**Outcome: INCOMPLETE**", report)
        self.assertIn("| C2 |", report)
        self.assertIn("boom", report)
        self.assertIn("partially complete", report)

    def test_json_report_is_valid(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        _, out, _ = self.run_cli("report", "--format", "json")
        payload = json.loads(out)
        self.assertEqual(payload["gate"], "OPEN")
        self.assertEqual(payload["summary"][0]["state"], "VERIFIED")


class TestVacuousChecks(LedgerTestCase):
    def test_trivial_commands_are_rejected(self):
        self.init("C1")
        for cmd in (["true"], ["echo", "all tests pass"], [":"], ["exit", "0"]):
            code, out, _ = self.run_cli("verify", "C1", "--", *cmd)
            self.assertEqual(code, 1, cmd)
            self.assertIn("vacuous", out)
        self.assertEqual(self.states(), {"C1": "FAILED"})

    def test_masking_patterns_are_rejected(self):
        self.init("C1")
        for script in ("pytest || true", "make test; true", "set +e; make test",
                       "pytest --collect-only", "npm test || exit 0"):
            code, out, _ = self.run_cli("verify", "C1", "--shell", "--", script)
            self.assertEqual(code, 1, script)
            self.assertIn("vacuous", out, script)

    def test_real_checks_are_not_flagged(self):
        for cmd, shell in ((["python3", "-m", "pytest", "-q"], False),
                           (["test", "-e", "README.md"], False),
                           (["git diff --name-only | grep -v src/"], True),
                           (["echo hi > out.txt && cat out.txt"], True)):
            self.assertEqual(ledger.vacuous_reason(cmd, shell), "", cmd)

    def test_expected_failure_baseline_is_not_vacuous(self):
        self.init("C1")
        code, _, _ = self.run_cli("verify", "C1", "--label", "before", "--expect-exit", "1", "--", "false")
        self.assertEqual(code, 0)


class TestReverify(LedgerTestCase):
    def test_reverify_reruns_recorded_checks(self):
        self.init("C1", "C2")
        flag = os.path.join(self.tmp.name, "flag")
        script = "import os,sys; sys.exit(0 if os.path.exists({!r}) else 1)".format(flag)
        self.run_cli("verify", "C1", "--", PY, "-c", script)
        self.run_cli("verify", "C2", "--", PY, "-c", "print('ok')")
        self.assertEqual(self.states(), {"C1": "FAILED", "C2": "VERIFIED"})
        open(flag, "w").close()
        code, out, _ = self.run_cli("reverify")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.states(), {"C1": "VERIFIED", "C2": "VERIFIED"})

    def test_reverify_only_stale_and_reports_missing(self):
        self.init("C1", "C2")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        code, out, _ = self.run_cli("reverify", "--only-stale")
        self.assertEqual(code, 0)
        self.assertIn("re-ran 0 check(s)", out)
        self.assertIn("no recorded check for: C2", out)

    def test_reverify_failure_exits_nonzero(self):
        self.init("C1")
        target = os.path.join(self.tmp.name, "v.txt")
        with open(target, "w") as fh:
            fh.write("good")
        script = "import sys; sys.exit(0 if open({!r}).read()=='good' else 1)".format(target)
        self.run_cli("verify", "C1", "--", PY, "-c", script)
        with open(target, "w") as fh:
            fh.write("bad")
        self.assertEqual(self.run_cli("reverify")[0], 1)
        self.assertEqual(self.states(), {"C1": "FAILED"})


@unittest.skipUnless(HAS_GIT, "git not available")
class TestGitWatch(LedgerTestCase):
    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp.name, check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        with open("app.py", "w") as fh:
            fh.write("x = 1\n")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "init")

    def test_any_worktree_edit_makes_evidence_stale(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        self.assertEqual(self.states(), {"C1": "VERIFIED"})
        with open("app.py", "a") as fh:
            fh.write("x = 2\n")
        self.assertEqual(self.states(), {"C1": "STALE"})

    def test_new_untracked_file_makes_evidence_stale(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        with open("new.py", "w") as fh:
            fh.write("y = 1\n")
        self.assertEqual(self.states(), {"C1": "STALE"})

    def test_ledger_and_caches_do_not_cause_staleness(self):
        self.init("C1", "C2")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        os.makedirs("__pycache__", exist_ok=True)
        with open(os.path.join("__pycache__", "m.pyc"), "wb") as fh:
            fh.write(b"cache")
        self.run_cli("verify", "C2", "--", PY, "-c", "pass")  # rewrites the ledger in the work tree
        self.assertEqual(self.states(), {"C1": "VERIFIED", "C2": "VERIFIED"})

    def test_no_git_watch_opt_out(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--no-git-watch", "--", PY, "-c", "pass")
        with open("app.py", "a") as fh:
            fh.write("x = 3\n")
        self.assertEqual(self.states(), {"C1": "VERIFIED"})


@unittest.skipUnless(os.name == "posix" and HAS_BASH, "POSIX + bash only")
class TestProcessTree(LedgerTestCase):
    def test_timeout_kills_grandchildren(self):
        self.init("C1")
        pidfile = os.path.join(self.tmp.name, "child.pid")
        script = "sleep 30 & echo $! > {}; wait".format(pidfile)
        code, out, _ = self.run_cli("verify", "C1", "--shell", "--timeout", "1", "--", script)
        self.assertEqual(code, 1)
        self.assertIn("timed out", out)
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            # A killed-but-unreaped zombie also counts as dead.
            try:
                with open("/proc/{}/stat".format(pid)) as fh:
                    if fh.read().split()[2] == "Z":
                        alive = False
                        break
            except OSError:
                pass
            time.sleep(0.1)
        self.assertFalse(alive, "grandchild survived the timeout")


class TestStopHook(LedgerTestCase):
    def hook(self, payload, *argv):
        out = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with contextlib.redirect_stdout(out):
                code = stop_hook.main(["--project-dir", self.tmp.name, *argv])
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.ledger = os.path.join(self.tmp.name, ".evidence", "ledger.json")
        os.environ.pop(ledger.ENV_LEDGER, None)

    def test_no_ledger_is_a_noop(self):
        self.assertEqual(self.hook({}), (0, ""))
        self.assertEqual(self.hook({}, "--mode", "exit-code")[0], 0)
        self.assertEqual(self.hook({}, "--mode", "exit-code", "--require-ledger")[0], 1)

    def test_closed_gate_blocks_stop_once(self):
        self.init("C1")
        code, out = self.hook({"stop_hook_active": False})
        self.assertEqual(code, 0)
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("C1 [UNVERIFIED]", decision["reason"])
        self.assertEqual(self.hook({"stop_hook_active": True}), (0, ""))

    def test_open_gate_allows_stop(self):
        self.init("C1")
        self.run_cli("verify", "C1", "--", PY, "-c", "pass")
        self.assertEqual(self.hook({}), (0, ""))

    def test_exit_code_mode(self):
        self.init("C1")
        code, out = self.hook({}, "--mode", "exit-code")
        self.assertEqual(code, 1)
        self.assertIn("GATE CLOSED", out)


class TestDisplay(unittest.TestCase):
    def test_short_cmd_collapses_and_truncates(self):
        cmd = ["python3", "-c", "import x\n" + "y = 1\n" * 50]
        text = ledger.short_cmd(cmd)
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), ledger.CMD_DISPLAY_CHARS)
        self.assertTrue(text.endswith("..."))


if __name__ == "__main__":
    unittest.main()
