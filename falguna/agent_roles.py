"""Phase B: AI Workforce V1 -- five named, persistent agent roles.

Deliberately built as thin, honest adapters over Falguna's EXISTING,
already-tested runtime rather than a second job system:

  * Sales Researcher and Proposal Specialist wrap the real Revenue Hunter
    pipeline (falguna/revenue_hunter.py, falguna/opportunity_agent.py):
    deterministic scoring (QualificationEngine.score -- no network, no
    model call), real opportunity/qualification/proposal rows, real
    lifecycle transitions and Needs Aryan approval items. Neither agent
    ever claims to have contacted a client -- Sales Researcher only reads
    and scores information already on file or supplied as task input.

  * Engineering Agent wraps the real Falguna Engineering control plane
    (falguna/orchestrator.py's ControlPlane, falguna/gitops.py's
    GitWorktreeManager, falguna/workers.py's StructuredEditWorker) via the
    exact seam Revenue Hunter is designed to use for a won job
    (falguna/handoff.py's accept_revenue_hunter_handoff) -- an isolated
    git worktree, a bounded RunPolicy (explicit editable files, explicit
    verification commands, no network by default), and independent
    semantic review. The best an unattended run can reach on its own is
    DONE_CANDIDATE plus a PENDING human merge approval -- nothing here
    ever merges to the target repository's real branch.

  * QA Agent independently re-verifies an Engineering Agent's run: it
    re-executes the same verification command a second time, itself, in
    the same worktree (not just re-reading the first run's self-report),
    and separately reads the on-disk verification/review evidence the
    control plane already wrote, cross-checking the two.

  * Executive Chief of Staff aggregates only real, already-stored data
    (workforce tasks, pipeline, Needs Aryan) into a plain summary -- no
    model call, no invented metrics, no activity reported that isn't
    backed by a real row somewhere.

Every worker follows workforce.py's WorkforceWorker contract: COMPLETED
requires real evidence (enforced by WorkerResult itself); a missing
capability, input, or approval is reported as BLOCKED or FAILED, never
guessed into a false COMPLETED.
"""
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .workforce import WorkerResult, WorkforceWorker
from .revenue_hunter import (
    OpportunityStore, QualificationStore, ProposalStore,
    OpportunityError, ProposalError, generate_proposal_text,
)
from .opportunity_agent import build_qualification_engine, AcquisitionProfileStore
from .documents import DocumentStore
from .models import RunPolicy, CommandSpec
from .gateway import LocalGateway
from .workers import StructuredEditWorker
from .review import ModelSemanticReviewer
from .handoff import accept_revenue_hunter_handoff
from .store import utcnow


def _inputs(task: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(task["inputs_json"]) if task.get("inputs_json") else {}


_CODING_MODEL_MARKERS = ("coder", "codellama", "starcoder", "deepseek-coder")


def _select_engineering_model(default: str, base_url: str = "http://127.0.0.1:11434") -> str:
    """Capability-aware local model selection for coding tasks (Sprint V1 Milestone 1).

    Deliberately narrow: this only ever looks at the local Ollama endpoint
    already used by EngineeringAgentWorker's LocalGateway. It never touches
    privacy_mode, never considers an external/paid provider, and never fails
    the caller -- any error (Ollama unreachable, malformed response, timeout)
    silently falls back to `default`. Among installed local models it prefers
    the largest one whose name looks coding-specialized (e.g. a pulled
    `qwen2.5-coder` model) over the generic instruct default, so a stronger
    local model is used automatically the moment one is installed -- no
    per-call configuration required, and nothing breaks if none ever is.
    """
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return default
    candidates = []
    for entry in payload.get("models", []) or []:
        name = entry.get("name") or entry.get("model") or ""
        if not name or "embed" in name.lower():
            continue
        if any(marker in name.lower() for marker in _CODING_MODEL_MARKERS):
            candidates.append((int(entry.get("size") or 0), name))
    if not candidates:
        return default
    candidates.sort(reverse=True)  # prefer the largest coding-specialized model installed
    return candidates[0][1]


# ---------------------------------------------------------------------------
# 1. Sales Researcher
# ---------------------------------------------------------------------------

class SalesResearcherWorker(WorkforceWorker):
    """Qualifies a lead using Falguna's existing, deterministic, documented
    scoring engine. Never fabricates a lead or claims external outreach --
    it only scores an opportunity already on file, or one whose fields are
    supplied directly as task input (e.g. a referral Aryan typed in, or a
    simulated-trial brief explicitly labeled as such)."""

    name = "sales_researcher"
    _SUPPORTED = {"sales_lead_qualification"}

    def __init__(self, store, audit, orchestrator=None):
        self.store = store
        self.audit = audit
        self.orchestrator = orchestrator

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        opportunity_id = inputs.get("opportunity_id")
        opp_store = OpportunityStore(self.store, self.audit, orchestrator=self.orchestrator)
        try:
            if not opportunity_id:
                fields = inputs.get("opportunity") or {}
                if not (fields.get("title") or "").strip():
                    return WorkerResult(
                        status="BLOCKED",
                        blockers=["no opportunity_id and no opportunity.title supplied"],
                        next_action="Provide an existing opportunity_id, or an 'opportunity' object with at "
                                    "least a title, in task inputs.",
                        execution_method="API",
                    )
                opportunity_id = opp_store.create(
                    fields, actor=task.get("actor", "system"),
                    source=inputs.get("source", "workforce_sales_researcher"),
                )
            profile = AcquisitionProfileStore(self.store).get()
            engine = build_qualification_engine(profile)
            qualification = QualificationStore(self.store, self.audit, engine, orchestrator=self.orchestrator).qualify(
                opportunity_id, actor=task.get("actor", "system"),
            )
        except OpportunityError as exc:
            return WorkerResult(status="FAILED", next_action=str(exc), execution_method="API")
        return WorkerResult(
            status="COMPLETED",
            result={"opportunity_id": opportunity_id, "qualification": qualification},
            evidence={
                "opportunity_id": opportunity_id,
                "fit_score": qualification["fit_score"],
                "recommendation": qualification["recommendation"],
                "recommendation_detail": qualification["recommendation_detail"],
                "why": qualification["why"],
            },
            confidence="Medium",
            execution_method="API",
        )


# ---------------------------------------------------------------------------
# 2. Proposal Specialist
# ---------------------------------------------------------------------------

class ProposalSpecialistWorker(WorkforceWorker):
    """Drafts a real proposal through the existing ProposalStore (which
    already creates a real Needs Aryan approval item and moves the real
    pipeline stage -- nothing here is ever sent without that approval),
    then adds one structured document covering milestones, acceptance
    criteria, pricing assumptions and risks -- the fields the short pitch
    text does not itself carry."""

    name = "proposal_specialist"
    _SUPPORTED = {"proposal_drafting"}

    def __init__(self, store, audit, documents: DocumentStore, needs_aryan=None, orchestrator=None):
        self.store = store
        self.audit = audit
        self.documents = documents
        self.needs_aryan = needs_aryan
        self.orchestrator = orchestrator

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        opportunity_id = inputs.get("opportunity_id")
        if not opportunity_id:
            return WorkerResult(
                status="BLOCKED", blockers=["no opportunity_id supplied"],
                next_action="Run Sales Researcher on this opportunity first, then pass its opportunity_id here.",
                execution_method="API",
            )
        opportunity = self.store.get("rh_opportunities", opportunity_id)
        if not opportunity:
            return WorkerResult(status="FAILED", next_action=f"opportunity not found: {opportunity_id}", execution_method="API")
        qualification = OpportunityStore(self.store, self.audit).latest_qualification(opportunity_id)
        if not qualification:
            return WorkerResult(
                status="BLOCKED", blockers=["opportunity has not been qualified yet"],
                next_action="Run Sales Researcher (sales_lead_qualification) on this opportunity before drafting a proposal.",
                execution_method="API",
            )
        try:
            pitch = ProposalStore(self.store, self.audit, self.needs_aryan, orchestrator=self.orchestrator).generate(
                opportunity_id, kind=inputs.get("kind", "detailed"), actor=task.get("actor", "system"),
            )
        except ProposalError as exc:
            return WorkerResult(status="FAILED", next_action=str(exc), execution_method="API")

        milestones = inputs.get("milestones") or [
            "M1 -- Kickoff and data/requirements intake",
            "M2 -- Working, client-reviewable environment",
            "M3 -- Client UAT against the acceptance criteria below",
            "M4 -- Production cutover and handover",
        ]
        acceptance_criteria = inputs.get("acceptance_criteria") or [
            f"Delivered work satisfies the scope described in \"{opportunity['title']}\".",
            "Automated tests relevant to the changed area pass.",
            "An independent QA review of the changes has been recorded.",
        ]
        assumptions = inputs.get("pricing_assumptions") or [
            f"Price basis: {qualification.get('suggested_price') or 'to confirm once scope is final'}.",
            f"Timeline basis: {qualification.get('suggested_timeline') or 'to confirm once scope is final'}.",
            "Assumes no scope not described in the opportunity record.",
        ]
        risks = inputs.get("risks") or (
            [qualification["risk_flags"]] if qualification.get("risk_flags") else ["No material risk flags recorded."]
        )
        doc_lines = [
            f"# Proposal -- {opportunity['title']}", "",
            "## Pitch", pitch["content"], "",
            "## Milestones", *[f"- {m}" for m in milestones], "",
            "## Acceptance criteria", *[f"- {a}" for a in acceptance_criteria], "",
            "## Pricing assumptions", *[f"- {a}" for a in assumptions], "",
            "## Risks", *[f"- {r}" for r in risks],
        ]
        doc_id = self.documents.create_document(
            f"Proposal -- {opportunity['title']}", "\n".join(doc_lines), doc_type="report",
            department=task.get("department", "sales"), source_task_id=task["id"], actor="system",
        )
        return WorkerResult(
            status="COMPLETED",
            result={"proposal_id": pitch["proposal_id"], "doc_id": doc_id, "needs_aryan_id": pitch["needs_aryan_id"]},
            evidence={
                "opportunity_id": opportunity_id, "proposal_id": pitch["proposal_id"], "doc_id": doc_id,
                "needs_aryan_id": pitch["needs_aryan_id"], "milestone_count": len(milestones),
                "acceptance_criteria_count": len(acceptance_criteria),
            },
            confidence="Medium",
            execution_method="API",
        )


# ---------------------------------------------------------------------------
# 3. Engineering Agent
# ---------------------------------------------------------------------------

class EngineeringAgentWorker(WorkforceWorker):
    """Runs one bounded, authorized coding task in an isolated git worktree
    through the real Falguna Engineering control plane -- the same
    ControlPlane every other Falguna mission goes through, entered via the
    exact handoff seam Revenue Hunter is designed to use for a won job
    (falguna/handoff.py). It never touches the target repository's real
    working tree or branch, never installs its own network access (the
    policy default is allow_network=False), and never merges -- the best
    outcome is DONE_CANDIDATE plus a PENDING human merge approval that
    lives in the control plane's own `approvals` table."""

    name = "engineering_agent"
    _SUPPORTED = {"engineering_fix"}
    DEFAULT_MODEL = "qwen2.5:1.5b-instruct"

    def __init__(self, control):
        self.control = control

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        missing = [k for k in ("repository", "requirement", "editable_files", "verification_commands") if not inputs.get(k)]
        if missing:
            return WorkerResult(
                status="BLOCKED", blockers=[f"missing required inputs: {missing}"],
                next_action="Provide repository, requirement, editable_files (list of repo-relative paths this "
                            "run may write), and verification_commands (list of {argv,label,timeout_seconds}) "
                            "in task inputs -- this worker never guesses a repository's test command.",
                execution_method="API",
            )
        repository = Path(inputs["repository"])
        if not repository.is_dir() or not (repository / ".git").exists():
            return WorkerResult(status="FAILED", next_action=f"not a local git checkout: {repository}", execution_method="API")

        editable_files = list(inputs["editable_files"])
        commands = [
            CommandSpec(list(c["argv"]), int(c.get("timeout_seconds", 120)), c.get("label", "verify"), c.get("network_mode", "deny"))
            for c in inputs["verification_commands"]
        ]
        protected = [".git/**", ".env", "**/.env", "**/*.env", "render.yaml", "server/**", "falguna/policy.py", "schema/**"]
        # A file the caller explicitly listed as editable is allowed even if it
        # would otherwise match one of the broad protected globs above (e.g. a
        # test file under server/ for a repo whose backend IS in scope) --
        # protected_globs is a default fence, not a way to silently override an
        # explicit, human-reviewed editable_files list.
        protected = [g for g in protected if g not in editable_files]
        policy = RunPolicy(
            allowed_write_globs=editable_files,
            protected_globs=protected,
            allowed_commands=list(inputs.get("allowed_commands") or ["node", "git"]),
            verification_commands=commands,
            max_attempts=int(inputs.get("max_attempts", 2)),
            max_cost_usd=float(inputs.get("max_cost_usd", 0.50)),
            max_changed_files=int(inputs.get("max_changed_files", 6)),
            allow_network=bool(inputs.get("allow_network", False)),
            dependency_node_path=inputs.get("dependency_node_path"),
        )
        payload = {
            "title": inputs.get("title", task["objective"]),
            "requirement": inputs["requirement"],
            "repository": str(repository),
            "source_opportunity_id": inputs.get("source_opportunity_id"),
            "client_name": inputs.get("client_name"),
        }
        try:
            handoff = accept_revenue_hunter_handoff(self.control, payload, policy)
        except ValueError as exc:
            return WorkerResult(status="FAILED", next_action=str(exc), execution_method="API")

        model = inputs.get("model") or _select_engineering_model(self.DEFAULT_MODEL)
        # The default (180s) is tuned for a hosted/GPU-backed gateway. A local
        # CPU-only Ollama model editing a non-trivial file can genuinely take
        # longer per call; let the caller raise it explicitly rather than
        # silently failing every real attempt with "timed out".
        edit_timeout = int(inputs.get("model_timeout_seconds", 300))
        gateway = LocalGateway(model=model)
        worker = StructuredEditWorker(
            gateway=gateway, editable_files=editable_files, timeout_seconds=edit_timeout,
            # CPU-only local inference plus strict JSON-schema-constrained decoding makes
            # embedding a whole large real file directly unworkable (observed: a real
            # ~60KB production file pushed prompt processing past 280-300s with no usable
            # output). Excerpt any oversized editable file down to the requirement-relevant
            # slice before it goes to the model; the on-disk patch match/write still
            # operates on the full real file (falguna/workers.py's own contract).
            always_excerpt_large_files=True,
            excerpt_max_chars=int(inputs.get("excerpt_max_chars", 12000)),
            # A slow CPU-only local model given no output cap was observed generating
            # past 1850 tokens without terminating, silently burning the entire
            # model_timeout_seconds budget and being cancelled with zero usable output
            # (Sprint V1, Milestone 1, Royal Table retrial evidence). The expected
            # output here is a short summary plus 1-3 minimal old/new text patches;
            # 1536 tokens is a generous bound for that and forces a fast, decodable
            # failure (handled by the existing except clause / replan loop) instead of
            # a slow, silent timeout when the model rambles.
            max_tokens=int(inputs.get("max_tokens", 1536)),
        )
        # ControlPlane.__init__ defaults self.reviewer to SemanticIndependentReviewer(),
        # an intentional fail-closed stub whose "unresolved_uncertainty" always rejects
        # every candidate -- it never approves anything, by design, until a real
        # reviewer is wired in. falguna/web.py's own hosted-mission routes do exactly
        # this (control.reviewer = ModelSemanticReviewer(gateway, ...)) before calling
        # start()/resume(); do the same here with the local gateway so a genuinely
        # correct local-model edit can actually reach DONE_CANDIDATE instead of being
        # rejected purely because no reviewer was ever configured.
        self.control.reviewer = ModelSemanticReviewer(gateway, timeout_seconds=edit_timeout)
        try:
            run_id = self.control.start(
                handoff["task_id"], worker, worker_name="structured_edit_local", model=model, policy=policy,
            )
        except Exception as exc:  # the control plane raises on policy/verification/review failure paths
            return WorkerResult(
                status="BLOCKED", blockers=[str(exc)],
                next_action="See the run's own evidence directory and Needs Aryan for the exact failure category.",
                result={"mission_id": handoff["mission_id"], "task_id": handoff["task_id"]},
                execution_method="API",
            )
        run = self.control.store.get("runs", run_id)
        base_result = {
            "mission_id": handoff["mission_id"], "task_id": handoff["task_id"], "run_id": run_id,
            "status": run["status"], "worktree": run.get("worktree"),
        }
        if run["status"] == "DONE_CANDIDATE":
            approvals = self.control.store.list("approvals", "run_id=?", (run_id,))
            return WorkerResult(
                status="COMPLETED",
                result=base_result,
                evidence={
                    **base_result,
                    "approval_status": approvals[-1]["status"] if approvals else None,
                    "evidence_dir": str(self.control.state_root / "evidence" / run_id),
                    "note": "Reached DONE_CANDIDATE with a PENDING human merge approval -- not merged, not deployed.",
                },
                confidence="Medium",
                next_action="Aryan reviews the diff and PENDING approval before anything merges.",
                execution_method="API",
            )
        return WorkerResult(
            status="BLOCKED" if run["status"] in {"AWAITING_APPROVAL", "PAUSED"} else "FAILED",
            result=base_result, blockers=[run.get("error") or f"run ended in {run['status']}"],
            next_action="Inspect the run's evidence directory and Needs Aryan queue for the exact failure.",
            execution_method="API",
        )


# ---------------------------------------------------------------------------
# 4. QA Agent
# ---------------------------------------------------------------------------

class QAAgentWorker(WorkforceWorker):
    """Independently reviews an Engineering Agent run: reads the on-disk
    verification/review evidence the control plane already wrote for that
    run, AND separately re-executes the same verification command itself,
    a second time, in the same worktree -- so a QA pass is a genuinely
    independent check, not a re-statement of Engineering's own self-report.
    Reports FAILED honestly if the independent re-run disagrees with the
    original, or if the underlying run never reached DONE_CANDIDATE."""

    name = "qa_agent"
    _SUPPORTED = {"qa_independent_verification"}

    def __init__(self, control):
        self.control = control

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        inputs = _inputs(task)
        run_id = inputs.get("run_id")
        if not run_id:
            return WorkerResult(status="BLOCKED", blockers=["no run_id supplied"],
                                 next_action="Provide the Engineering Agent run_id to independently verify.",
                                 execution_method="API")
        run = self.control.store.get("runs", run_id)
        if not run:
            return WorkerResult(status="FAILED", next_action=f"run not found: {run_id}", execution_method="API")
        if run["status"] != "DONE_CANDIDATE":
            return WorkerResult(
                status="BLOCKED",
                blockers=[f"run {run_id} is in status {run['status']}, not DONE_CANDIDATE -- nothing to independently verify yet"],
                result={"run_id": run_id, "status": run["status"]},
                execution_method="API",
            )
        evidence_dir = self.control.state_root / "evidence" / run_id
        verification_path = evidence_dir / "verification.json"
        review_path = evidence_dir / "review.json"
        if not verification_path.is_file():
            return WorkerResult(status="FAILED", next_action=f"no verification.json evidence at {verification_path}", execution_method="API")
        original_verification = json.loads(verification_path.read_text())
        original_review = json.loads(review_path.read_text()) if review_path.is_file() else None

        task_row = self.control.store.get("tasks", run["task_id"])
        policy_json = json.loads(task_row["policy_json"])
        worktree = Path(run["worktree"])
        rerun_results = []
        env = {"CI": "1"}
        if policy_json.get("dependency_node_path"):
            env["NODE_PATH"] = policy_json["dependency_node_path"]
        all_passed = True
        for spec in policy_json.get("verification_commands", []):
            argv = spec["argv"] if isinstance(spec, dict) else spec.argv
            label = spec.get("label", "verify") if isinstance(spec, dict) else spec.label
            timeout_seconds = spec.get("timeout_seconds", 120) if isinstance(spec, dict) else spec.timeout_seconds
            try:
                completed = subprocess.run(
                    argv, cwd=worktree, env=env, text=True, capture_output=True, timeout=timeout_seconds,
                )
                passed = completed.returncode == 0
                rerun_results.append({"label": label, "exit_code": completed.returncode, "passed": passed,
                                       "stdout_tail": completed.stdout[-2000:], "stderr_tail": completed.stderr[-2000:]})
            except (subprocess.TimeoutExpired, OSError) as exc:
                passed = False
                rerun_results.append({"label": label, "passed": False, "error": str(exc)})
            all_passed = all_passed and passed
        original_passed = bool(original_verification.get("passed"))
        agrees = all_passed == original_passed
        review_approved = bool(original_review.get("approved")) if original_review else None
        verdict_status = "COMPLETED" if (all_passed and agrees) else "FAILED"
        return WorkerResult(
            status=verdict_status,
            result={"run_id": run_id, "independent_rerun_passed": all_passed, "matches_original_verification": agrees,
                     "review_approved": review_approved},
            evidence={
                "run_id": run_id, "rerun_results": rerun_results,
                "original_verification_passed": original_passed, "review_approved": review_approved,
                "verification_evidence_path": str(verification_path),
                "review_evidence_path": str(review_path) if review_path.is_file() else None,
            },
            confidence="High" if agrees else "Low",
            blockers=[] if verdict_status == "COMPLETED" else ["independent re-run did not pass, or disagreed with the original verification"],
            execution_method="API",
        )


# ---------------------------------------------------------------------------
# 5. Executive Chief of Staff
# ---------------------------------------------------------------------------

class ChiefOfStaffWorker(WorkforceWorker):
    """Summarizes real, already-stored state for Aryan -- workforce task
    counts by status/department, real pipeline stats, and pending Needs
    Aryan items. No model call, no invented numbers: every figure here
    traces to a real row this same session could re-query."""

    name = "chief_of_staff"
    _SUPPORTED = {"executive_summary"}

    def __init__(self, store, audit, documents: DocumentStore, needs_aryan=None):
        self.store = store
        self.audit = audit
        self.documents = documents
        self.needs_aryan = needs_aryan

    def supports(self, task_type: str) -> bool:
        return task_type in self._SUPPORTED

    def execute(self, task: Dict[str, Any]) -> WorkerResult:
        from .workforce import WorkforceTaskStore
        tasks = WorkforceTaskStore(self.store, self.audit).list()
        by_status: Dict[str, int] = {}
        by_department: Dict[str, int] = {}
        for row in tasks:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
            by_department[row["department"]] = by_department.get(row["department"], 0) + 1
        opportunities = self.store.list("rh_opportunities")
        by_stage: Dict[str, int] = {}
        for opp in opportunities:
            by_stage[opp["stage"]] = by_stage.get(opp["stage"], 0) + 1
        pending_needs_aryan = self.needs_aryan.list_pending() if self.needs_aryan is not None else []
        lines = [
            f"# Executive summary -- {utcnow()}", "",
            "## Workforce tasks", f"Total: {len(tasks)}",
            *[f"- {status}: {count}" for status, count in sorted(by_status.items())],
            "", "## By department", *[f"- {dept}: {count}" for dept, count in sorted(by_department.items())],
            "", "## Pipeline (Revenue Hunter)", f"Total opportunities: {len(opportunities)}",
            *[f"- {stage}: {count}" for stage, count in sorted(by_stage.items())],
            "", f"## Needs Aryan (pending): {len(pending_needs_aryan)}",
            *[f"- [{item.get('kind')}] {item.get('title')}" for item in pending_needs_aryan[:10]],
        ]
        content = "\n".join(lines)
        doc_id = self.documents.create_document(
            "Executive summary", content, doc_type="report",
            department=task.get("department", "executive"), source_task_id=task["id"], actor="system",
        )
        return WorkerResult(
            status="COMPLETED",
            result={"doc_id": doc_id, "task_counts": by_status, "department_counts": by_department,
                     "pipeline_counts": by_stage, "needs_aryan_pending": len(pending_needs_aryan)},
            evidence={"doc_id": doc_id, "task_total": len(tasks), "opportunity_total": len(opportunities),
                       "needs_aryan_pending": len(pending_needs_aryan)},
            confidence="High",
            execution_method="API",
        )
