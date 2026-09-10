import json
from pathlib import Path
from typing import Optional


MILESTONES = {
    "CREATED": "Mission accepted",
    "WORKTREE_READY": "Isolated worktree ready",
    "WORKER_COMPLETE": "Implementation complete",
    "VERIFIED": "Verification passed",
    "DONE_CANDIDATE": "Independent review passed; human decision required",
}

PHASE_LABELS = {
    "CREATED": "Planning",
    "WORKTREE_READY": "Inspecting",
    "WORKER_COMPLETE": "Editing complete",
    "REPAIR_COMPLETE": "Bounded repair complete",
    "VERIFIED": "Testing passed",
    "DONE_CANDIDATE": "Review passed",
}


def classify_failure(run: dict) -> Optional[dict]:
    status = run["status"]
    error = (run.get("error") or "").lower()
    if status not in {"FAILED", "QUARANTINED"}:
        return None
    explicit = next((name for name in (
        "MODEL_UNSUPPORTED", "TRANSPORT_FAILURE", "PATCH_AMBIGUOUS", "PATCH_STALE", "PATCH_NOOP",
        "CONTEXT_TOO_LARGE", "PROFILE_STALE", "VERIFY_COMMAND_INVALID", "SCOPE_EXPANSION_REQUIRED",
        "REVIEW_FAILURE",
    ) if name.lower() in error), None)
    if explicit:
        category = explicit
    elif status == "QUARANTINED":
        category = "SECURITY_CONTAINMENT" if any(word in error for word in ("path", "write", "command", "network", "protected")) else "BUDGET_STOP"
    elif "definition of done" in error or "verification" in error:
        category = "VERIFICATION_FAILURE"
    elif "permission" in error or "approval" in error:
        category = "PERMISSION_BLOCK"
    elif "worker" in error or "model" in error or "review" in error:
        category = "MODEL_FAILURE"
    elif "input" in error or "not found" in error:
        category = "HUMAN_INPUT_REQUIRED"
    else:
        category = "TOOL_FAILURE"
    actions = {
        "SECURITY_CONTAINMENT": "Inspect the blocked operation; do not weaken policy.",
        "BUDGET_STOP": "Review recorded cost and set a new cap only with human approval.",
        "VERIFICATION_FAILURE": "Inspect failing checks and request a bounded repair.",
        "PERMISSION_BLOCK": "Obtain the required human decision or permission.",
        "MODEL_FAILURE": "Inspect worker/reviewer output; retry only within the bound.",
        "HUMAN_INPUT_REQUIRED": "Provide the missing bounded input.",
        "TOOL_FAILURE": "Repair the local tool or environment, then resume.",
        "MODEL_UNSUPPORTED": "Select a supported authenticated runtime model; no mission retry is needed.",
        "TRANSPORT_FAILURE": "Inspect the preserved stdout and stderr, repair transport, then resume.",
        "PATCH_AMBIGUOUS": "Regenerate a patch with unique surrounding anchors.",
        "PATCH_STALE": "Re-read the current file and regenerate the stale patch.",
        "PATCH_NOOP": "Regenerate a patch that makes a concrete change.",
        "CONTEXT_TOO_LARGE": "Narrow the bounded objective or approved file scope.",
        "PROFILE_STALE": "Update the approved project profile to match the repository layout.",
        "VERIFY_COMMAND_INVALID": "Repair the approved native verification binding before execution.",
        "SCOPE_EXPANSION_REQUIRED": "Approve the newly required files before continuing.",
        "REVIEW_FAILURE": "Inspect independent review findings and request a bounded repair.",
    }
    return {"category": category, "message": run.get("error"), "action": actions[category]}


def mission_view(store, state_root: Path, run_id: str) -> dict:
    run = store.get("runs", run_id)
    if not run:
        raise ValueError("run not found")
    task = store.get("tasks", run["task_id"])
    requirement = store.get("requirements", task["requirement_id"])
    mission = store.get("missions", requirement["mission_id"])
    checkpoints = store.list("checkpoints", "run_id=?", (run_id,))
    completed = [MILESTONES[item["stage"]] for item in checkpoints if item["stage"] in MILESTONES]
    current = MILESTONES.get(checkpoints[-1]["stage"], run["status"]) if checkpoints else run["status"]
    progress_events = []
    for item in checkpoints:
        payload = json.loads(item["payload"])
        label = PHASE_LABELS.get(item["stage"])
        if payload.get("completed_milestone_task"):
            label = f"Task {payload['completed_milestone_task']} tested"
        if label:
            progress_events.append({"stage": item["stage"], "label": label, "at": item["created_at"]})
    return {
        "mission": mission["title"],
        "objective": requirement["body"],
        "run_id": run_id,
        "status": run["status"],
        "current_milestone": current,
        "completed_milestones": completed,
        "progress_events": progress_events,
        "attempt": run["attempt"],
        "failure": classify_failure(run),
        "available_controls": (["Resume", "Retry", "Cancel"] if run["status"] in {"PAUSED", "FAILED"} else (["Pause", "Cancel"] if run["status"] in {"PLANNING", "WORKING", "VERIFYING", "REVIEWING"} else [])),
        "timings_ms": {item["stage"]: item["duration_ms"] for item in store.list("mission_timings", "run_id=?", (run_id,))},
    }


def evidence_summary(store, state_root: Path, audit, run_id: str) -> dict:
    view = mission_view(store, state_root, run_id)
    run = store.get("runs", run_id)
    evidence_dir = Path(state_root) / "evidence" / run_id
    verification = _read_json(evidence_dir / "verification.json")
    discovery = _read_json(evidence_dir / "discovery.json")
    review = _read_json(evidence_dir / "review.json")
    approvals = store.list("approvals", "run_id=? AND kind=?", (run_id, "PROTECTED_BRANCH_MERGE"))
    costs = store.list("cost_events", "run_id=?", (run_id,))
    artifacts = store.list("artifacts", "run_id=?", (run_id,))
    native = verification.get("results", [])
    browser = verification.get("browser")
    unresolved = review.get("unresolved_uncertainty", [])
    changed = verification.get("changed_files", [])
    return {
        **view,
        "requirement_coverage": "PASSED" if review.get("dimensions", {}).get("requirement_satisfaction", {}).get("passed") else "NOT_PROVEN",
        "native_tests": [{"label": item.get("label"), "passed": item.get("exit_code") == 0, "exit_code": item.get("exit_code")} for item in native],
        "discovered_file_scope": discovery.get("editable_files", []),
        "discovered_verification_plan": [item.get("argv", []) for item in discovery.get("verification_commands", [])],
        "discovery_confidence": discovery.get("confidence", "NOT_RECORDED"),
        "browser_e2e": "NOT_APPLICABLE" if not browser else ("PASSED" if browser.get("passed") else "FAILED"),
        "independent_review": "PASSED" if review.get("approved") else ("NOT_RUN" if not review else "FAILED"),
        "files_changed": changed,
        "cost_usd": round(sum(float(item["amount_usd"]) for item in costs), 8),
        "risk": "LOW" if run["status"] == "DONE_CANDIDATE" and not unresolved else "REVIEW_REQUIRED",
        "unresolved_issues": unresolved,
        "evidence_hashes_valid": all(_hash_valid(item) for item in artifacts),
        "audit_chain_valid": audit.verify(),
        "merge_approval": approvals[-1]["status"] if approvals else "NOT_REQUESTED",
        "available_actions": ["Approve Merge", "Reject", "Request Changes"] if run["status"] == "DONE_CANDIDATE" else [],
        "protected_main_merge_performed": False,
        "timings_ms": view["timings_ms"],
        "discovery_cache": discovery.get("cache", {}).get("status", "NOT_RECORDED"),
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _hash_valid(artifact: dict) -> bool:
    import hashlib
    path = Path(artifact["path"])
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
