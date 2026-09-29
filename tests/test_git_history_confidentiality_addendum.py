"""Phase 3 focused acceptance audit (this request), Requirement 1 follow-up.

The prior audit's HiddenTestFilesystemConfidentialityAdversarialTests proved a
worker's shell tool could no longer `cat` a hidden test's real WORKTREE path
once it was physically stashed out. This audit went further, per Aryan's
explicit instruction not to treat that as proof against every other access
path, and checked git itself.

Finding (reproduced deterministically below, no live model needed): a plain
`GitWorktreeManager.create()` worktree is created with `git worktree add`,
which shares the SOURCE REPO's object database. If the hidden test was ever
committed to that repo (the realistic case -- it has to be a real file to be
a real acceptance test), then even after the working-tree copy is physically
moved away, `git show HEAD:tests/test_calc.py`, `git log -p -- <path>`, and
`git cat-file --batch-all-objects` (which lists every object physically
present regardless of ref reachability) can all retrieve the untouched
original content from within the worker's own worktree. The stash/restore
mechanism never touched git's object store, only the working directory.

Fix: GitWorktreeManager.create_confidential() (falguna/gitops.py), wired into
ControlPlane._continue()'s CREATED stage (falguna/orchestrator.py) whenever
RunPolicy.hidden_verification_files is set. It builds the worker's worktree
from a fully independent, --no-hardlinks clone (never source_repo itself),
squashes history to one orphan commit that never contained the hidden paths,
deletes every other ref, expires the reflog, and runs `git gc --prune=now
--aggressive` to physically purge the now-unreachable hidden blob from disk.
DefinitionOfDone.verify() (falguna/verification.py) was also updated to
exclude hidden_verification_files from changed-file accounting, since
restoring the real file for verification now makes git report it as a new
untracked path (the scrubbed clone's history never had it) rather than an
unmodified tracked one -- preserving the existing "restoring the hidden file
is invisible to changed-file/review accounting" contract without weakening
the separate, already-tested write-time rejection
(HiddenTestBypassAttemptAdversarialTests) that stops a worker from writing to
it in the first place.

This test uses a fully deterministic ScriptedWorker (no live model, no Codex
CLI) so it always runs in the regression suite, and directly re-runs the
exact git plumbing commands the audit used to first demonstrate the gap,
against the SAME worktree the real orchestrator hands a worker, to prove the
fix actually closes it -- not just that a mechanism exists.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.models import RunPolicy, CommandSpec, WorkerResult, ReviewResult
from falguna.orchestrator import ControlPlane
from falguna.store import StateStore
from falguna.workers import WorkerAdapter


class _AlwaysApproveReviewer:
    """Isolates this test from independent-review semantics (already covered
    elsewhere) so it can focus purely on the git-history confidentiality
    boundary. Mirrors the stub used in this audit's own debug repros."""

    def review(self, requirement, diff, changed, verification_evidence):
        return ReviewResult(True, "stub approve for confidentiality isolation test")


class _GitHistoryProbeWorker(WorkerAdapter):
    """A real WorkerAdapter (not a mock of the orchestrator) that, while it
    has the worktree, runs the exact adversarial git commands this audit used
    to first prove the gap, then makes the legitimate fix."""

    def __init__(self, secret_marker: str):
        self.secret_marker = secret_marker
        self.observed_hidden_missing = None
        self.git_show_failed = None
        self.git_log_empty = None
        self.batch_all_objects_clean = None
        self.batch_all_objects_error = None

    def execute(self, worktree: Path, requirement: str, run_id: str) -> WorkerResult:
        hidden_path = worktree / "tests" / "test_calc.py"
        self.observed_hidden_missing = not hidden_path.exists()

        show = subprocess.run(
            ["git", "-C", str(worktree), "show", "HEAD:tests/test_calc.py"],
            capture_output=True, text=True,
        )
        # "failed" means either the command errored (path not in HEAD's tree,
        # the expected outcome) OR it somehow succeeded without leaking the
        # marker -- either way, the marker must never appear in the output.
        self.git_show_failed = (show.returncode != 0) or (self.secret_marker not in show.stdout)

        log = subprocess.run(
            ["git", "-C", str(worktree), "log", "--all", "--oneline", "--", "tests/test_calc.py"],
            capture_output=True, text=True,
        )
        self.git_log_empty = (log.stdout.strip() == "")

        try:
            batch_check = subprocess.run(
                ["git", "-C", str(worktree), "cat-file", "--batch-check", "--batch-all-objects"],
                capture_output=True, text=True, check=True,
            )
            leaked = False
            for line in batch_check.stdout.splitlines():
                parts = line.split()
                if len(parts) < 2 or parts[1] != "blob":
                    continue
                sha = parts[0]
                blob = subprocess.run(
                    ["git", "-C", str(worktree), "cat-file", "-p", sha],
                    capture_output=True, text=True,
                )
                if self.secret_marker in (blob.stdout or ""):
                    leaked = True
                    break
            self.batch_all_objects_clean = not leaked
        except subprocess.CalledProcessError as exc:
            self.batch_all_objects_error = f"{exc}: {exc.stderr}"
            self.batch_all_objects_clean = False

        (worktree / "calc.py").write_text(
            "def add(a, b):\n    \"\"\"Return the sum of a and b.\"\"\"\n    return a + b\n"
        )
        return WorkerResult(True, "fixed add() and ran git-history confidentiality probes", 0)


class HiddenTestGitHistoryConfidentialityAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="falguna-m1-githistory-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.secret_marker = "PHASE3-GITHISTORY-CONFIDENTIALITY-MARKER"
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    \"\"\"Return the sum of a and b.\"\"\"\n    return a - b\n"
        )
        tests_dir = self.repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\n"
            "class CalcTests(unittest.TestCase):\n"
            "    def test_add_returns_sum_not_difference(self):\n"
            f"        # {self.secret_marker}\n"
            "        self.assertEqual(add(2, 3), 5)\n"
            "        self.assertEqual(add(10, -4), 6)\n"
        )
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=demo@example.com", "-c", "user.name=Demo", "commit", "-q", "-m", "initial"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.state_root = self.tmp / "state"
        self.state_root.mkdir()
        self.store = StateStore(self.state_root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(self.state_root / "audit.jsonl")

    def tearDown(self):
        self.store.close()

    def test_git_plumbing_cannot_retrieve_the_hidden_test_from_the_scrubbed_worktree(self):
        control = ControlPlane(self.store, self.audit, self.state_root, reviewer=_AlwaysApproveReviewer())
        policy = RunPolicy(
            allowed_write_globs=["calc.py"],
            verification_commands=[CommandSpec(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], 60, "unit tests")],
            hidden_verification_files=["tests/test_calc.py"],
            max_attempts=1,
        )
        mission = control.create_mission("git-history confidentiality probe", "fix add() to return the sum", self.repo, policy)
        worker = _GitHistoryProbeWorker(self.secret_marker)
        run_id = control.start(mission["task_id"], worker, "git-history-probe", "n/a", policy)
        run = self.store.get("runs", run_id)

        self.assertTrue(worker.observed_hidden_missing, "the hidden test must be physically absent from the working tree during worker execution")
        self.assertTrue(worker.git_show_failed, "`git show HEAD:<hidden path>` must not leak the hidden test's real content")
        self.assertTrue(worker.git_log_empty, "no commit reachable from the worker's worktree may reference the hidden test path")
        self.assertIsNone(worker.batch_all_objects_error, f"batch-all-objects enumeration itself must succeed cleanly: {worker.batch_all_objects_error}")
        self.assertTrue(worker.batch_all_objects_clean,
                         "no object physically present in the worker's worktree's object store -- reachable or not -- may contain the hidden test's real content")

        self.assertEqual(run["status"], "DONE_CANDIDATE",
                          f"mission should still reach DONE_CANDIDATE once the file is restored for independent verification: status={run['status']!r} error={run.get('error')!r}")

        worktree = Path(run["worktree"])
        restored = worktree / "tests" / "test_calc.py"
        self.assertTrue(restored.is_file(), "the hidden test must be restored into the worktree for independent verification")
        self.assertIn(self.secret_marker, restored.read_text(), "the restored file must be the real, untampered hidden test")

        # Prove the worktree genuinely no longer shares source_repo's object
        # database (the architectural fix itself), not merely that the probe
        # commands above happened to fail for some other reason.
        git_file = (worktree / ".git")
        self.assertTrue(git_file.is_file(), "a worktree's .git is a pointer file, not a directory")
        pointer = git_file.read_text()
        self.assertNotIn(str(self.repo.resolve()), pointer,
                          "the worker's worktree must not be linked to source_repo's own .git directory")

        verification_path = self.state_root / "evidence" / run_id / "verification-attempt-1.json"
        import json
        verification = json.loads(verification_path.read_text())
        self.assertTrue(verification["passed"], "the real, independent verification run of the hidden test must have passed")
        joined = " ".join((r.get("stdout", "") + r.get("stderr", "")) for r in verification["results"])
        self.assertIn("test_add_returns_sum_not_difference", joined, "the hidden test must have genuinely executed during verification")
        # The scrub must not itself have been misreported as a "changed" file
        # that trips scope validation.
        self.assertNotIn("tests/test_calc.py", verification["changed_files"])
        self.assertIn("calc.py", verification["changed_files"])

        approvals = self.store.list("approvals", "run_id=?", (run_id,))
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["status"], "PENDING", "no auto-merge: a human approval must remain pending")
        self.assertEqual(approvals[0]["kind"], "PROTECTED_BRANCH_MERGE")


if __name__ == "__main__":
    unittest.main()
