"""Phase 9 Frontier & Independence control plane.

This module deliberately coordinates the proven FALGUNA subsystems instead of
replacing them. It owns durable intent, execution-graph state, leases,
approvals, evidence, model/evaluation promotion, connectors, continuity and
the Independence Gate. Existing mission/research/browser/memory services stay
the execution systems of record; TTT HQ stays the commercial system of record.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .audit import AuditLog
from .store import StateStore, utcnow


MODES = ("WORK", "CODE", "RESEARCH", "BROWSER", "DATA", "TTT_INTERNAL", "MARKETS_RESEARCH")
AUTONOMY_LEVELS = ("ASK", "ASSIST", "EXECUTE_WITH_APPROVAL", "AUTONOMOUS_WITHIN_POLICY", "COMPANY_AUTONOMOUS")
OBJECTIVE_STATUSES = ("DRAFT", "READY", "RUNNING", "PAUSED", "NEEDS_APPROVAL", "COMPLETED", "FAILED", "STOPPED")
NODE_STATUSES = ("PENDING", "BLOCKED", "READY", "RUNNING", "NEEDS_APPROVAL", "COMPLETED", "FAILED", "CANCELLED")
EFFORT_MODES = ("AUTO", "FAST", "BALANCED", "DEEP", "MAXIMUM", "AUTONOMOUS")
MODEL_STATES = ("CANDIDATE", "EVALUATED", "APPROVED", "RETIRED")
SENSITIVE_ACTIONS = {
    "PAYMENT", "REFUND", "PAYOUT", "BENEFICIARY_CHANGE", "LEGAL_COMMITMENT",
    "PRODUCTION_DEPLOY", "EXTERNAL_OUTREACH", "MAJOR_SPEND", "SECURITY_CRITICAL",
    "LIVE_TRADE", "PUSH", "MERGE", "DNS_CHANGE",
}
SAFE_ACTIONS = {"READ", "PLAN", "ANALYZE", "LOCAL_EDIT", "LOCAL_TEST", "LOCAL_BROWSER", "CHECKPOINT", "REPORT"}
AGENT_ROLES = {
    "LEAD": ["plan", "route", "synthesize", "quality_gate"],
    "CODE": ["inspect_repo", "edit", "test", "diff_review"],
    "RESEARCH": ["source_collection", "citation", "contradiction_tracking"],
    "QA": ["test", "browser_evidence", "regression_reasoning"],
    "SECURITY": ["threat_review", "permission_review", "prompt_injection_review"],
    "DATA": ["profile", "query", "chart", "anomaly_review"],
    "DESIGN": ["artifact_review", "accessibility_review"],
    "SALES": ["draft_only", "qualification", "handoff"],
    "FINANCE_ANALYSIS": ["analysis_only", "risk_review", "no_money_movement"],
}


class FrontierError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _require_choice(value: str, choices: Iterable[str], field: str) -> str:
    clean = str(value or "").strip().upper()
    if clean not in choices:
        raise FrontierError(f"{field} must be one of {', '.join(choices)}")
    return clean


class AutonomyPolicy:
    """Hard policy boundary. Consequential actions never inherit autonomy."""

    @staticmethod
    def decision(autonomy_level: str, action_class: str) -> Dict[str, Any]:
        level = _require_choice(autonomy_level, AUTONOMY_LEVELS, "autonomy_level")
        action = str(action_class or "").strip().upper()
        if action in SENSITIVE_ACTIONS:
            return {"allowed": False, "approval_required": True, "reason": "Hard-bound consequential action"}
        if action not in SAFE_ACTIONS:
            return {"allowed": False, "approval_required": True, "reason": "Unknown action class fails closed"}
        if level in {"ASK", "ASSIST"} and action not in {"READ", "PLAN", "ANALYZE", "REPORT"}:
            return {"allowed": False, "approval_required": True, "reason": f"{level} does not permit execution"}
        if level == "EXECUTE_WITH_APPROVAL" and action in {"LOCAL_EDIT", "LOCAL_BROWSER"}:
            return {"allowed": False, "approval_required": True, "reason": "Execution approval required"}
        return {"allowed": True, "approval_required": False, "reason": "Allowed within local policy"}


class FrontierControlPlane:
    def __init__(self, store: StateStore, audit: AuditLog):
        self.store = store
        self.audit = audit

    def _org_row(self, table: str, record_id: str, organization_id: str) -> Dict[str, Any]:
        row = self.store.get(table, record_id)
        if not row or row.get("organization_id") != organization_id:
            raise FrontierError("Record not found in this organization")
        return row

    def ensure_default_agents(self, organization_id: str, actor: str = "system") -> List[Dict[str, Any]]:
        existing = self.store.list("p9_agents", "organization_id=?", (organization_id,))
        by_role = {row["role"]: row for row in existing}
        for role, capabilities in AGENT_ROLES.items():
            if role in by_role:
                continue
            agent_id = self.store.create("p9_agents", {
                "organization_id": organization_id, "role": role,
                "display_name": role.replace("_", " ").title(),
                "capabilities_json": _json(capabilities),
                "permissions_json": _json({"scope": "objective", "external_actions": False}),
                "status": "AVAILABLE", "current_node_id": None,
                "created_at": utcnow(), "updated_at": utcnow(),
            })
            by_role[role] = self.store.get("p9_agents", agent_id)
        return sorted(by_role.values(), key=lambda row: row["role"])

    def create_objective(self, organization_id: str, title: str, description: str, mode: str,
                         autonomy_level: str = "ASSIST", owner: str = "Aryan",
                         schedule: Optional[Dict[str, Any]] = None,
                         stop_conditions: Optional[List[Dict[str, Any]]] = None,
                         limits: Optional[Dict[str, Any]] = None,
                         source_type: Optional[str] = None, source_id: Optional[str] = None) -> Dict[str, Any]:
        title, description = str(title or "").strip(), str(description or "").strip()
        if not title or not description:
            raise FrontierError("title and description are required")
        mode = _require_choice(mode, MODES, "mode")
        autonomy = _require_choice(autonomy_level, AUTONOMY_LEVELS, "autonomy_level")
        schedule = schedule or {"kind": "MANUAL"}
        if schedule.get("kind") not in {"MANUAL", "INTERVAL", "CRON", "EVENT"}:
            raise FrontierError("Unsupported schedule kind")
        objective_id = self.store.create("p9_objectives", {
            "organization_id": organization_id, "title": title, "description": description,
            "mode": mode, "status": "READY", "autonomy_level": autonomy,
            "schedule_json": _json(schedule), "stop_conditions_json": _json(stop_conditions or []),
            "limits_json": _json(limits or {"max_cost_usd": 0, "external_actions": False}),
            "source_type": source_type, "source_id": source_id, "owner": owner,
            "last_checkpoint_at": None, "heartbeat_at": None, "next_action": "Plan execution graph",
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        self._event(objective_id, organization_id, "OBJECTIVE_CREATED", {"mode": mode, "autonomy": autonomy}, owner)
        self.audit.append("P9_OBJECTIVE_CREATED", {"objective_id": objective_id, "organization_id": organization_id, "mode": mode, "actor": owner})
        return self.objective_bundle(objective_id, organization_id)

    def add_node(self, objective_id: str, organization_id: str, title: str, node_type: str,
                 dependencies: Optional[List[str]] = None, agent_role: Optional[str] = None,
                 tool_name: Optional[str] = None, action_class: str = "ANALYZE",
                 inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        objective = self._org_row("p9_objectives", objective_id, organization_id)
        dependencies = list(dict.fromkeys(dependencies or []))
        for dep in dependencies:
            row = self._org_row("p9_graph_nodes", dep, organization_id)
            if row["objective_id"] != objective_id:
                raise FrontierError("Dependencies must belong to the same objective")
        policy = AutonomyPolicy.decision(objective["autonomy_level"], action_class)
        status = "BLOCKED" if dependencies else ("NEEDS_APPROVAL" if policy["approval_required"] else "READY")
        assigned_agent_id = None
        if agent_role:
            role = agent_role.upper()
            agents = {row["role"]: row for row in self.ensure_default_agents(organization_id)}
            if role not in agents:
                raise FrontierError("Unknown agent role")
            assigned_agent_id = agents[role]["id"]
        node_id = self.store.create("p9_graph_nodes", {
            "objective_id": objective_id, "organization_id": organization_id,
            "node_type": str(node_type or "TASK").upper(), "title": str(title or "").strip(),
            "status": status, "dependencies_json": _json(dependencies),
            "assigned_agent_id": assigned_agent_id, "tool_name": tool_name,
            "approval_class": str(action_class or "").upper(), "input_json": _json(inputs or {}),
            "output_json": None, "attempt": 0, "lease_owner": None, "lease_expires_at": None,
            "error": None, "created_at": utcnow(), "updated_at": utcnow(),
        })
        if policy["approval_required"]:
            self.store.create("p9_approvals", {
                "objective_id": objective_id, "node_id": node_id, "organization_id": organization_id,
                "action_class": str(action_class).upper(), "status": "PENDING",
                "request_reason": policy["reason"], "decided_by": None,
                "decision_reason": None, "decided_at": None,
                "created_at": utcnow(), "updated_at": utcnow(),
            })
        self._event(objective_id, organization_id, "NODE_ADDED", {"node_id": node_id, "status": status}, "system", node_id)
        return self.store.get("p9_graph_nodes", node_id)

    def plan_standard_graph(self, objective_id: str, organization_id: str) -> List[Dict[str, Any]]:
        objective = self._org_row("p9_objectives", objective_id, organization_id)
        if self.store.list("p9_graph_nodes", "objective_id=?", (objective_id,)):
            raise FrontierError("Objective already has an execution graph")
        inspect = self.add_node(objective_id, organization_id, "Inspect context and constraints", "INSPECT", agent_role="LEAD")
        research = self.add_node(objective_id, organization_id, "Collect evidence and resolve uncertainty", "RESEARCH", [inspect["id"]], "RESEARCH")
        execute_role = "CODE" if objective["mode"] == "CODE" else ("DATA" if objective["mode"] == "DATA" else "LEAD")
        execute = self.add_node(objective_id, organization_id, "Execute the bounded work", objective["mode"], [research["id"]], execute_role, action_class="LOCAL_EDIT" if objective["mode"] == "CODE" else "ANALYZE")
        qa = self.add_node(objective_id, organization_id, "Verify results and capture evidence", "QA", [execute["id"]], "QA", action_class="LOCAL_TEST")
        security = self.add_node(objective_id, organization_id, "Review permissions and security boundaries", "SECURITY", [execute["id"]], "SECURITY")
        report = self.add_node(objective_id, organization_id, "Produce checkpoint and finished deliverables", "REPORT", [qa["id"], security["id"]], "LEAD", action_class="REPORT")
        return [inspect, research, execute, qa, security, report]

    def _refresh_blocked(self, objective_id: str, organization_id: str) -> None:
        rows = self.store.list("p9_graph_nodes", "objective_id=?", (objective_id,))
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            if row["status"] != "BLOCKED":
                continue
            deps = _load(row["dependencies_json"], [])
            if deps and all(by_id.get(dep, {}).get("status") == "COMPLETED" for dep in deps):
                self.store.update("p9_graph_nodes", row["id"], status="READY")

    def claim_node(self, node_id: str, organization_id: str, worker_id: str, lease_seconds: int = 300) -> Dict[str, Any]:
        node = self._org_row("p9_graph_nodes", node_id, organization_id)
        objective = self._org_row("p9_objectives", node["objective_id"], organization_id)
        if objective["status"] in {"STOPPED", "COMPLETED"}:
            raise FrontierError("Objective is not runnable")
        if node["status"] != "READY":
            raise FrontierError("Node is not ready")
        expires = (datetime.now(timezone.utc) + timedelta(seconds=max(30, min(int(lease_seconds), 3600)))).isoformat()
        if not self.store.compare_and_set("p9_graph_nodes", node_id, {"status": "READY"}, status="RUNNING", lease_owner=worker_id, lease_expires_at=expires, attempt=int(node["attempt"] or 0) + 1, error=None):
            raise FrontierError("Node was claimed by another worker")
        self.store.update("p9_objectives", objective["id"], status="RUNNING", heartbeat_at=utcnow(), next_action=node["title"])
        self._event(objective["id"], organization_id, "NODE_CLAIMED", {"worker_id": worker_id, "lease_expires_at": expires}, worker_id, node_id)
        return self.store.get("p9_graph_nodes", node_id)

    def complete_node(self, node_id: str, organization_id: str, worker_id: str,
                      output: Dict[str, Any], evidence: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        node = self._org_row("p9_graph_nodes", node_id, organization_id)
        if node["status"] != "RUNNING" or node["lease_owner"] != worker_id:
            raise FrontierError("Only the active lease owner can complete this node")
        self.store.update("p9_graph_nodes", node_id, status="COMPLETED", output_json=_json(output), lease_owner=None, lease_expires_at=None)
        for item in evidence or []:
            self.record_evidence(node["objective_id"], organization_id, item.get("kind", "RESULT"), item.get("summary", ""), node_id=node_id, uri=item.get("uri"), sha256=item.get("sha256"), provenance=item.get("provenance"), confidence=item.get("confidence", "OBSERVED"))
        self._refresh_blocked(node["objective_id"], organization_id)
        nodes = self.store.list("p9_graph_nodes", "objective_id=?", (node["objective_id"],))
        completed = bool(nodes) and all(row["status"] == "COMPLETED" for row in nodes)
        self.store.update("p9_objectives", node["objective_id"], status="COMPLETED" if completed else "RUNNING", heartbeat_at=utcnow(), next_action="Objective complete" if completed else "Continue ready graph nodes")
        self._event(node["objective_id"], organization_id, "NODE_COMPLETED", {"output_digest": _digest(output)}, worker_id, node_id)
        return self.objective_bundle(node["objective_id"], organization_id)

    def recover_expired_leases(self, organization_id: str, now: Optional[str] = None) -> List[str]:
        now = now or utcnow()
        recovered = []
        for node in self.store.list("p9_graph_nodes", "organization_id=? AND status=?", (organization_id, "RUNNING")):
            if node.get("lease_expires_at") and node["lease_expires_at"] < now:
                self.store.update("p9_graph_nodes", node["id"], status="READY", lease_owner=None, lease_expires_at=None, error="Worker lease expired; safely requeued")
                self._event(node["objective_id"], organization_id, "NODE_RECOVERED", {"prior_worker": node.get("lease_owner")}, "recovery", node["id"])
                recovered.append(node["id"])
        return recovered

    def decide_approval(self, approval_id: str, organization_id: str, decision: str,
                        actor: str, reason: str) -> Dict[str, Any]:
        approval = self._org_row("p9_approvals", approval_id, organization_id)
        if approval["status"] != "PENDING":
            raise FrontierError("Approval has already been decided")
        decision = _require_choice(decision, ("APPROVED", "REJECTED"), "decision")
        if not actor.strip() or not reason.strip():
            raise FrontierError("Attributed actor and reason are required")
        self.store.update("p9_approvals", approval_id, status=decision, decided_by=actor, decision_reason=reason, decided_at=utcnow())
        # Hard-bound actions remain non-executable even after recording approval:
        # a future dedicated executor must verify its own action-time approval.
        node_status = "CANCELLED" if decision == "REJECTED" else ("NEEDS_APPROVAL" if approval["action_class"] in SENSITIVE_ACTIONS else "READY")
        self.store.update("p9_graph_nodes", approval["node_id"], status=node_status)
        self._event(approval["objective_id"], organization_id, "APPROVAL_DECIDED", {"approval_id": approval_id, "decision": decision, "action_class": approval["action_class"]}, actor, approval["node_id"])
        return self.store.get("p9_approvals", approval_id)

    def emergency_stop(self, objective_id: str, organization_id: str, actor: str, reason: str) -> Dict[str, Any]:
        self._org_row("p9_objectives", objective_id, organization_id)
        if not reason.strip():
            raise FrontierError("Emergency stop reason is required")
        for node in self.store.list("p9_graph_nodes", "objective_id=?", (objective_id,)):
            if node["status"] not in {"COMPLETED", "FAILED", "CANCELLED"}:
                self.store.update("p9_graph_nodes", node["id"], status="CANCELLED", lease_owner=None, lease_expires_at=None, error=f"Stopped: {reason}")
        self.store.update("p9_objectives", objective_id, status="STOPPED", next_action="Human review required")
        self._event(objective_id, organization_id, "EMERGENCY_STOP", {"reason": reason}, actor)
        self.audit.append("P9_EMERGENCY_STOP", {"objective_id": objective_id, "organization_id": organization_id, "actor": actor, "reason": reason})
        return self.objective_bundle(objective_id, organization_id)

    def record_evidence(self, objective_id: str, organization_id: str, kind: str, summary: str,
                        node_id: Optional[str] = None, uri: Optional[str] = None,
                        sha256: Optional[str] = None, provenance: Optional[Dict[str, Any]] = None,
                        confidence: str = "OBSERVED") -> Dict[str, Any]:
        self._org_row("p9_objectives", objective_id, organization_id)
        if node_id:
            node = self._org_row("p9_graph_nodes", node_id, organization_id)
            if node["objective_id"] != objective_id:
                raise FrontierError("Evidence node belongs to another objective")
        evidence_id = self.store.create("p9_evidence", {
            "objective_id": objective_id, "node_id": node_id, "organization_id": organization_id,
            "kind": str(kind).upper(), "uri": uri, "sha256": sha256,
            "summary": str(summary).strip(), "provenance_json": _json(provenance or {}),
            "confidence": str(confidence).upper(), "created_at": utcnow(),
        })
        return self.store.get("p9_evidence", evidence_id)

    def create_continuity_bundle(self, objective_id: str, organization_id: str) -> Dict[str, Any]:
        state = self.objective_bundle(objective_id, organization_id)
        state["generated_at"] = utcnow()
        prior = self.store.list("p9_continuity_bundles", "objective_id=?", (objective_id,))
        bundle_id = self.store.create("p9_continuity_bundles", {
            "organization_id": organization_id, "objective_id": objective_id,
            "version": len(prior) + 1, "state_json": _json(state), "sha256": _digest(state),
            "created_at": utcnow(),
        })
        self.store.update("p9_objectives", objective_id, last_checkpoint_at=utcnow())
        return self.store.get("p9_continuity_bundles", bundle_id)

    def register_model(self, organization_id: str, model_id: str, version: str,
                       provider_kind: str, ownership: str, capabilities: Dict[str, Any],
                       constraints: Optional[Dict[str, Any]] = None,
                       dataset_provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        asset_id = self.store.create("p9_model_assets", {
            "organization_id": organization_id, "provider_kind": provider_kind,
            "model_id": model_id, "version": version, "ownership": ownership,
            "capabilities_json": _json(capabilities), "constraints_json": _json(constraints or {}),
            "dataset_provenance_json": _json(dataset_provenance or {}), "status": "CANDIDATE",
            "evaluation_summary_json": _json({}), "approved_by": None,
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        return self.store.get("p9_model_assets", asset_id)

    def record_benchmark(self, organization_id: str, program: str, subject_type: str,
                         subject_id: str, suite_version: str, dimensions: Dict[str, Any],
                         evidence: List[Dict[str, Any]], passed: bool,
                         external_audit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not evidence:
            raise FrontierError("Benchmark evidence is required")
        benchmark_id = self.store.create("p9_benchmark_runs", {
            "organization_id": organization_id, "program": program,
            "subject_type": subject_type, "subject_id": subject_id,
            "suite_version": suite_version, "dimensions_json": _json(dimensions),
            "evidence_json": _json(evidence), "passed": 1 if passed else 0,
            "external_audit_json": _json(external_audit or {}), "created_at": utcnow(),
        })
        return self.store.get("p9_benchmark_runs", benchmark_id)

    def promote_model(self, model_asset_id: str, organization_id: str, target_status: str,
                      actor: str) -> Dict[str, Any]:
        asset = self._org_row("p9_model_assets", model_asset_id, organization_id)
        target = _require_choice(target_status, MODEL_STATES, "target_status")
        allowed = {"CANDIDATE": {"EVALUATED"}, "EVALUATED": {"APPROVED", "RETIRED"}, "APPROVED": {"RETIRED"}, "RETIRED": set()}
        if target not in allowed[asset["status"]]:
            raise FrontierError("Invalid model promotion transition")
        benchmarks = self.store.list("p9_benchmark_runs", "organization_id=? AND subject_type=? AND subject_id=?", (organization_id, "MODEL", model_asset_id))
        if target in {"EVALUATED", "APPROVED"} and not benchmarks:
            raise FrontierError("A model cannot be promoted without benchmark evidence")
        if target == "APPROVED" and not any(row["passed"] for row in benchmarks):
            raise FrontierError("A model cannot be approved without a passing evaluation")
        summary = {"runs": len(benchmarks), "passing": sum(int(row["passed"]) for row in benchmarks)}
        self.store.update("p9_model_assets", model_asset_id, status=target, evaluation_summary_json=_json(summary), approved_by=actor if target == "APPROVED" else asset.get("approved_by"))
        return self.store.get("p9_model_assets", model_asset_id)

    def route_model(self, organization_id: str, task: Dict[str, Any], effort: str = "AUTO") -> Dict[str, Any]:
        effort = _require_choice(effort, EFFORT_MODES, "effort")
        candidates = self.store.list("p9_model_assets", "organization_id=? AND status=?", (organization_id, "APPROVED"))
        privacy = str(task.get("privacy", "HYBRID")).upper()
        scored = []
        for row in candidates:
            caps = _load(row["capabilities_json"], {})
            constraints = _load(row["constraints_json"], {})
            if privacy == "LOCAL_ONLY" and row["ownership"] not in {"FALGUNA_OWNED", "LOCAL"}:
                continue
            required = set(task.get("required_capabilities") or [])
            supported = {key for key, value in caps.items() if value}
            if not required.issubset(supported):
                continue
            score = len(required) * 20 + float(caps.get("quality", 0)) * (2 if effort in {"DEEP", "MAXIMUM"} else 1)
            score -= float(constraints.get("cost_rank", 0)) * (2 if effort == "FAST" else 1)
            score -= float(constraints.get("latency_rank", 0)) * (2 if effort == "FAST" else 0.5)
            if row["ownership"] == "FALGUNA_OWNED":
                score += 5
            scored.append((score, row))
        if not scored:
            raise FrontierError("No approved model satisfies the requested capability and privacy policy")
        score, row = sorted(scored, key=lambda item: (-item[0], item[1]["model_id"]))[0]
        return {"asset_id": row["id"], "model_id": row["model_id"], "version": row["version"], "ownership": row["ownership"], "effort": effort, "score": score, "reason": "Highest policy-compatible capability score"}

    def register_plugin(self, organization_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        required = {"name", "version", "tools", "scopes"}
        if not required.issubset(manifest):
            raise FrontierError("Plugin manifest requires name, version, tools and scopes")
        scopes = manifest.get("scopes")
        if not isinstance(scopes, list) or not scopes or any(scope in {"*", "admin", "root"} for scope in scopes):
            raise FrontierError("Plugin scopes must be explicit and least-privilege")
        for tool in manifest.get("tools") or []:
            endpoint = str(tool.get("endpoint", ""))
            if endpoint and urlparse(endpoint).scheme not in {"http", "https", "mcp"}:
                raise FrontierError("Plugin tool endpoint scheme is not allowed")
        live = bool(manifest.get("live_external_actions"))
        status = "PENDING_APPROVAL" if live else "SANDBOXED"
        plugin_id = self.store.create("p9_plugins", {
            "organization_id": organization_id, "name": manifest["name"], "version": manifest["version"],
            "manifest_json": _json(manifest), "scopes_json": _json(scopes),
            "approval_policy": "EXPLICIT_ACTION_TIME" if live else "POLICY_BOUND",
            "status": status, "sandbox_json": _json({"timeout_seconds": min(int(manifest.get("timeout_seconds", 30)), 120), "network": bool(manifest.get("network", False)), "secrets_forwarded": False}),
            "created_at": utcnow(), "updated_at": utcnow(),
        })
        return self.store.get("p9_plugins", plugin_id)

    def record_independence_gate(self, organization_id: str, repo_task_id: str,
                                 dimensions: Dict[str, bool], evidence: List[Dict[str, Any]],
                                 external_audit: Dict[str, Any]) -> Dict[str, Any]:
        required = {"correct_implementation", "tests", "diff_review", "checkpoint", "security_review"}
        if dimensions.get("browser_relevant"):
            required.add("browser_evidence")
        passed = all(bool(dimensions.get(key)) for key in required) and bool(external_audit)
        return self.record_benchmark(organization_id, "INDEPENDENCE_GATE", "REPO_TASK", repo_task_id, "p9-v1", dimensions, evidence, passed, external_audit)

    def independence_summary(self, organization_id: str) -> Dict[str, Any]:
        runs = self.store.list("p9_benchmark_runs", "organization_id=? AND program=?", (organization_id, "INDEPENDENCE_GATE"))
        passed = [row for row in runs if row["passed"]]
        return {"runs": len(runs), "passed": len(passed), "repeatable": len(passed) >= 3, "independent": len(passed) >= 3, "status": "MEASURED_NOT_PROVEN" if len(passed) < 3 else "GATE_SATISFIED"}

    def ttt_context_summary(self, organization_id: str) -> Dict[str, Any]:
        """Read-only governed view; never copies or mutates commercial state."""
        def count(table: str) -> int:
            try:
                return int(self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE organization_id=?", (organization_id,)).fetchone()[0])
            except Exception:
                return 0
        return {"organization_id": organization_id, "authoritative_system": "TTT_HQ", "access": "READ_ONLY_GOVERNED", "counts": {"intakes": count("p8_intakes"), "opportunities": count("p8_opportunities"), "assignments": count("p8_assignments"), "commercial_identities": count("p6_commercial_identities")}, "commercial_mutation_allowed": False}

    def objective_bundle(self, objective_id: str, organization_id: str) -> Dict[str, Any]:
        objective = self._org_row("p9_objectives", objective_id, organization_id)
        decode = dict(objective)
        for key in ("schedule_json", "stop_conditions_json", "limits_json"):
            decode[key[:-5]] = _load(decode.pop(key), {} if key != "stop_conditions_json" else [])
        nodes = self.store.list("p9_graph_nodes", "objective_id=? AND organization_id=?", (objective_id, organization_id))
        for node in nodes:
            node["dependencies"] = _load(node.pop("dependencies_json"), [])
            node["input"] = _load(node.pop("input_json"), {})
            node["output"] = _load(node.pop("output_json"), None)
        return {
            "objective": decode, "nodes": nodes,
            "evidence": self.store.list("p9_evidence", "objective_id=? AND organization_id=?", (objective_id, organization_id)),
            "approvals": self.store.list("p9_approvals", "objective_id=? AND organization_id=?", (objective_id, organization_id)),
            "events": self.store.list("p9_events", "objective_id=? AND organization_id=?", (objective_id, organization_id)),
        }

    def dashboard(self, organization_id: str) -> Dict[str, Any]:
        objectives = self.store.list("p9_objectives", "organization_id=?", (organization_id,))
        objectives.sort(key=lambda row: row["updated_at"], reverse=True)
        return {
            "phase": 9, "status": "IN_PROGRESS", "objectives": objectives,
            "agents": self.ensure_default_agents(organization_id),
            "models": self.store.list("p9_model_assets", "organization_id=?", (organization_id,)),
            "plugins": self.store.list("p9_plugins", "organization_id=?", (organization_id,)),
            "independence_gate": self.independence_summary(organization_id),
            "ttt_context": self.ttt_context_summary(organization_id),
            "live_external_actions_enabled": False, "live_trading_enabled": False,
        }

    def _event(self, objective_id: str, organization_id: str, event_type: str,
               payload: Dict[str, Any], actor: str, node_id: Optional[str] = None) -> str:
        return self.store.create("p9_events", {
            "objective_id": objective_id, "node_id": node_id,
            "organization_id": organization_id, "event_type": event_type,
            "payload_json": _json(payload), "actor": actor, "created_at": utcnow(),
        })
