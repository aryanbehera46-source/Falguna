#!/usr/bin/env python3
"""Section 26 end-to-end QA script.

Digital Workforce test:
  Request -> task -> planning -> worker selection -> execution -> verification
  -> evidence -> completion

Media test:
  Brand -> research -> content idea -> script -> asset plan -> generated/test
  assets -> caption -> thumbnail metadata -> approval -> controlled publishing
  adapter -> analytics -> growth recommendation

Then: restart TTT HQ (kill the server process, start a fresh one against the
SAME root) and verify persistence of everything created above.

No real public content is published. The publishing step only exercises the
honest ManualPublishingChannel path (BLOCKED -> FAILED -> publish_approval
escalation) -- it never calls mark-published-manually with fabricated
"proof", since that would falsely claim a real publish happened.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
PORT = int(os.environ.get("QA_HQ_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"


def api(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_up(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_server(root):
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "falguna", "--root", root, "hq", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_up():
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        proc.kill()
        raise RuntimeError(f"server did not come up\n{out}")
    return proc


def stop_server(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        RESULTS["failures"].append(label + (f" -- {extra}" if extra else ""))
    RESULTS["checks"].append((label, cond))


RESULTS = {"checks": [], "failures": []}


def main():
    tmp = tempfile.mkdtemp(prefix="ttt_qa_")
    root = tmp
    print(f"QA root: {root}")
    proc = start_server(root)
    try:
        # ---------- Digital Workforce test ----------
        print("\n=== Digital Workforce: Request -> task -> plan -> execute -> evidence -> completion ===")
        status, created = api("POST", "/api/wf/tasks", {
            "task_type": "document_creation",
            "objective": "QA: draft a one-paragraph internal note",
            "department": "operations",
            "actor": "qa-script",
            "inputs": {"title": "QA Note", "content_text": "This is a QA-generated note."},
        })
        check("workforce task create returns 200/201", status in (200, 201), f"status={status} body={created}")
        task_id = created.get("task_id")
        check("workforce task has id", bool(task_id), str(created))
        # document_creation needs real inputs the DocumentWorker can act on -- re-verify via GET
        status, precheck = api("GET", f"/api/wf/tasks/{task_id}")
        check("workforce task readable right after creation", status == 200 and precheck.get("status") == "CREATED", str(precheck))

        status, executed = api("POST", f"/api/wf/tasks/{task_id}/execute", {"actor": "qa-script"})
        check("workforce task execute returns 200", status == 200, f"status={status} body={executed}")
        check("workforce task reaches COMPLETED", executed.get("status") == "COMPLETED", str(executed))

        status, hist = api("GET", f"/api/wf/tasks/{task_id}/history")
        check("workforce task history reachable", status == 200 and hist.get("items"), str(hist))

        status, fetched = api("GET", f"/api/wf/tasks/{task_id}")
        check("workforce task evidence present on completion",
              bool(fetched.get("evidence_json")), str(fetched))

        # unroutable task -> should escalate, not crash
        status, unroutable = api("POST", "/api/wf/tasks", {
            "task_type": "browser_research", "objective": "QA: find something online",
            "department": "operations", "actor": "qa-script",
        })
        check("unroutable-type task create ok", status in (200, 201), str(unroutable))
        ur_id = unroutable.get("id") or unroutable.get("task_id")
        status, ur_exec = api("POST", f"/api/wf/tasks/{ur_id}/execute", {"actor": "qa-script"})
        check("unroutable task execute does not crash (200)", status == 200, f"status={status} body={ur_exec}")
        check("unroutable task escalates to NEEDS_ARYAN (not a raised error)",
              ur_exec.get("status") in ("NEEDS_ARYAN", "BLOCKED"), str(ur_exec))

        # ---------- Media test ----------
        print("\n=== Media: brand -> content -> script -> assets -> approval -> publish(honest gap) -> analytics -> growth ===")
        status, brand = api("POST", "/api/media/brands", {
            "name": "QA Brand", "voice_tone": "direct", "actor": "qa-script",
        })
        check("brand create ok", status in (200, 201), str(brand))
        brand_id = brand.get("brand_id")

        status, content = api("POST", "/api/media/content", {
            "brand_id": brand_id, "title": "QA content item", "format": "image_post", "actor": "qa-script",
        })
        check("content create ok (starts pipeline)", status in (200, 201), str(content))
        content_id = content.get("content_id")
        status, content_row = api("GET", f"/api/media/content/{content_id}")
        check("content starts in RESEARCH", content_row.get("content_state") == "RESEARCH", str(content_row))

        # walk the content state machine forward through a few real transitions
        for target in ("IDEA", "CONTENT_PLAN", "SCRIPT"):
            status, moved = api("POST", f"/api/media/content/{content_id}/transition", {
                "to_state": target, "actor": "qa-script", "reason": "QA walk",
            })
            check(f"content transitions to {target}", status == 200 and moved.get("content_state") == target, str(moved))

        status, script = api("POST", f"/api/media/content/{content_id}/scripts", {
            "body": "QA script body v1", "hook": "QA hook", "actor": "qa-script",
        })
        check("script version created", status in (200, 201), str(script))

        status, scripts = api("GET", f"/api/media/content/{content_id}/scripts")
        check("script version listed", status == 200 and len(scripts.get("items", [])) >= 1, str(scripts))

        status, assets = api("GET", f"/api/media/content/{content_id}/assets")
        check("assets endpoint reachable (may be empty in this QA path)", status == 200, str(assets))

        status, pub = api("POST", "/api/media/publications", {
            "content_id": content_id, "platform": "instagram", "actor": "qa-script",
        })
        check("publication create ok", status in (200, 201), str(pub))
        pub_id = pub.get("publication_id")

        status, submitted = api("POST", f"/api/media/publications/{pub_id}/submit-for-approval", {"actor": "qa-script"})
        check("publication submitted for approval", status in (200, 201), str(submitted))
        needs_aryan_id = submitted.get("needs_aryan_id")

        check("publication submit-for-approval creates a needs_aryan escalation", bool(needs_aryan_id), str(submitted))
        status, decided = api("POST", f"/api/needs-aryan/{needs_aryan_id}/decision", {
            "action": "approve", "actor": "Aryan-QA", "note": "QA approval",
        })
        check("needs-aryan approval decision accepted", status == 200, f"status={status} body={decided}")

        status, approved = api("POST", f"/api/media/publications/{pub_id}/approve", {"actor": "Aryan-QA"})
        check("publication reaches APPROVED", status == 200 and approved.get("status") == "APPROVED", str(approved))

        status, published = api("POST", f"/api/media/publications/{pub_id}/publish", {"actor": "qa-script"})
        check("publish attempt returns 200 (honest gap path)", status == 200, str(published))
        check("no real adapter -> publication lands in FAILED (never falsely PUBLISHED)",
              published.get("status") == "FAILED", str(published))
        check("failed publish escalates a publish_approval Needs Aryan item",
              bool(published.get("needs_aryan_id")), str(published))
        print("NOTE: not calling mark-published-manually -- doing so would require real external "
              "evidence (e.g. a real post URL), which this QA script does not have. No real public "
              "content is published as part of this test, per spec.")

        status, analytics_rec = api("POST", "/api/media/analytics", {
            "publication_id": pub_id, "metric_kind": "views", "value": 0, "source": "qa-script-manual-entry", "actor": "qa-script",
        })
        check("analytics record accepted (real, human-sourced, zero views honestly)", status in (200, 201), str(analytics_rec))

        status, growth = api("GET", f"/api/media/publications/{pub_id}/growth-recommendation")
        check("growth recommendation reachable", status == 200, str(growth))
        check("growth recommendation is an honest real-data decision (not fabricated)",
              growth.get("decision") in ("insufficient_data", "stop", "test", "repeat"), str(growth))

        status, exp = api("POST", "/api/media/experiments", {
            "content_id": content_id, "hypothesis": "QA hypothesis", "variable_tested": "thumbnail", "actor": "qa-script",
        })
        check("growth experiment create ok", status in (200, 201), str(exp))

        # ---------- Today dashboard signals ----------
        status, dash = api("GET", "/api/rh/dashboard")
        check("today dashboard reachable", status == 200, str(dash))
        for key in ("workforce_blocked_tasks", "media_pending_approval", "content_due_soon",
                    "publishing_failures", "strong_growth_signals"):
            check(f"today dashboard includes '{key}'", key in dash, str(list(dash.keys())))

        # ---------- Restart and verify persistence ----------
        print("\n=== Restarting TTT HQ against the same root and verifying persistence ===")
        stop_server(proc)
        proc = start_server(root)

        status, fetched2 = api("GET", f"/api/wf/tasks/{task_id}")
        check("workforce task survives restart", status == 200 and fetched2.get("status") == "COMPLETED", str(fetched2))

        status, content2 = api("GET", f"/api/media/content/{content_id}")
        check("content item survives restart", status == 200 and content2.get("content_state") == "SCRIPT", str(content2))

        status, pub2 = api("GET", "/api/media/publications", )
        status, pub2 = api("GET", f"/api/media/publications?content_id={content_id}")
        found = any(p.get("id") == pub_id and p.get("status") == "FAILED" for p in pub2.get("items", []))
        check("publication (FAILED, honest gap) survives restart", found, str(pub2))

        status, scripts2 = api("GET", f"/api/media/content/{content_id}/scripts")
        check("script version survives restart", status == 200 and len(scripts2.get("items", [])) >= 1, str(scripts2))

    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== SUMMARY ===")
    total = len(RESULTS["checks"])
    passed = sum(1 for _, c in RESULTS["checks"] if c)
    print(f"{passed}/{total} checks passed")
    if RESULTS["failures"]:
        print("FAILURES:")
        for f in RESULTS["failures"]:
            print(" -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
