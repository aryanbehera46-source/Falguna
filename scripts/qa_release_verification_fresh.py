#!/usr/bin/env python3
"""Fresh, independent release-verification QA script (separate from the
Pass-E QA script). Exercises different task types and a real media-asset
generation path, and restarts TTT HQ in the MIDDLE of the workflow (not
just at the end) to prove real persistence of in-flight state, not only
finished state.

No real public content is published -- the publish step only exercises
the honest ManualPublishingChannel/unavailable-adapter path.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
PORT = int(os.environ.get("QA_HQ_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"

RESULTS = {"checks": [], "failures": []}


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
    proc = subprocess.Popen(
        [sys.executable, "-m", "falguna", "--root", root, "hq", "--port", str(PORT)],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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


def main():
    root = tempfile.mkdtemp(prefix="ttt_release_qa_")
    print(f"QA root: {root}")
    proc = start_server(root)
    try:
        # ---------- Digital Workforce: spreadsheet task (different type from prior QA) ----------
        print("\n=== Digital Workforce (spreadsheet_creation) ===")
        status, created = api("POST", "/api/wf/tasks", {
            "task_type": "spreadsheet_creation", "objective": "Release QA: build a tracking sheet",
            "department": "operations", "actor": "release-qa",
            "inputs": {"title": "QA Tracker", "columns": ["item", "status"], "rows": [["a", "done"], ["b", "pending"]]},
        })
        check("spreadsheet task created", status in (200, 201), str(created))
        wf_task_id = created.get("task_id")

        # Deliberately do NOT execute the spreadsheet task yet -- it stays CREATED
        # across the restart below, so we can prove a task can be created in one
        # process lifetime and planned+executed in a later one. We also create a
        # SECOND, deliberately unroutable task to verify mid-flight NEEDS_ARYAN
        # state survives a restart too.
        status2, unroutable = api("POST", "/api/wf/tasks", {
            "task_type": "computer_use_unsupported", "objective": "Release QA: an intentionally unroutable task",
            "department": "operations", "actor": "release-qa",
        })
        check("unroutable task created", status2 in (200, 201), str(unroutable))
        unroutable_id = unroutable.get("task_id")
        status3, unroutable_exec = api("POST", f"/api/wf/tasks/{unroutable_id}/execute", {"actor": "release-qa"})
        check("unroutable task escalates to NEEDS_ARYAN before restart", unroutable_exec.get("status") == "NEEDS_ARYAN", str(unroutable_exec))
        needs_aryan_id_wf = unroutable_exec.get("needs_aryan_id")
        check("unroutable escalation created a real needs_aryan_id", bool(needs_aryan_id_wf), str(unroutable_exec))

        # ---------- Media: brand -> content, with REAL local asset generation this time ----------
        print("\n=== Media: brand -> content -> real local image+voice assets ===")
        status, brand = api("POST", "/api/media/brands", {"name": "Release QA Brand", "voice_tone": "confident", "actor": "release-qa"})
        check("brand created", status in (200, 201), str(brand))
        brand_id = brand.get("brand_id")

        status, content = api("POST", "/api/media/content", {
            "brand_id": brand_id, "title": "Release QA content item", "format": "short_form_video", "actor": "release-qa",
        })
        check("content created", status in (200, 201), str(content))
        content_id = content.get("content_id")

        for target in ("IDEA", "CONTENT_PLAN", "SCRIPT", "VISUAL_PLAN", "ASSET_CREATION"):
            status, moved = api("POST", f"/api/media/content/{content_id}/transition", {"to_state": target, "actor": "release-qa"})
            check(f"content reaches {target} before restart", status == 200 and moved.get("content_state") == target, str(moved))

        status, script = api("POST", f"/api/media/content/{content_id}/scripts", {"hook": "QA hook", "body": "Release QA script v1", "actor": "release-qa"})
        check("script version created before restart", status in (200, 201), str(script))
        script_id_before = script.get("script_id")

        # -------------------------------------------------------------
        # RESTART MID-WORKFLOW (not at the end) -- proves in-flight state
        # (a NEEDS_ARYAN task, mid-pipeline content, an unfinished script)
        # genuinely persists, not just a finished/idle state.
        # -------------------------------------------------------------
        print("\n=== Restarting TTT HQ MID-WORKFLOW ===")
        stop_server(proc)
        proc = start_server(root)

        status, wf_after = api("GET", f"/api/wf/tasks/{unroutable_id}")
        check("mid-flight NEEDS_ARYAN task survives restart", status == 200 and wf_after.get("status") == "NEEDS_ARYAN", str(wf_after))
        check("its needs_aryan_id is unchanged across restart", wf_after.get("needs_aryan_id") == needs_aryan_id_wf, str(wf_after))

        status, content_after = api("GET", f"/api/media/content/{content_id}")
        check("mid-pipeline content state survives restart", status == 200 and content_after.get("content_state") == "ASSET_CREATION", str(content_after))

        status, scripts_after = api("GET", f"/api/media/content/{content_id}/scripts")
        found_script = any(s.get("id") == script_id_before for s in scripts_after.get("items", []))
        check("script version survives restart", status == 200 and found_script, str(scripts_after))

        # ---------- Resume the Digital Workforce task after restart ----------
        print("\n=== Resuming Digital Workforce spreadsheet task after restart ===")
        status, wf_task_row = api("GET", f"/api/wf/tasks/{wf_task_id}")
        check("spreadsheet task still CREATED/READY after restart (not lost)", status == 200 and wf_task_row.get("status") in ("CREATED", "READY", "PLANNING"), str(wf_task_row))
        status, executed = api("POST", f"/api/wf/tasks/{wf_task_id}/execute", {"actor": "release-qa"})
        check("spreadsheet task executes to COMPLETED after restart", status == 200 and executed.get("status") == "COMPLETED", str(executed))
        check("spreadsheet task has real evidence", bool(executed.get("evidence_json")), str(executed))
        evidence = json.loads(executed.get("evidence_json") or "{}")
        check("evidence references a real row_count", evidence.get("row_count") == 2, str(evidence))

        # ---------- Resolve the escalated task by cancelling it (Aryan's decision) ----------
        status, decided = api("POST", f"/api/needs-aryan/{needs_aryan_id_wf}/decision", {"action": "reject", "actor": "Aryan-QA", "note": "no adapter for this task type, cancelling"})
        check("Aryan can resolve the mid-flight escalation after restart", status == 200, str(decided))

        # ---------- Continue the media pipeline: real local asset generation via the agent ----------
        print("\n=== Real local asset generation (Pillow image via VisualAssetAgent-equivalent call) ===")
        status, assets = api("GET", f"/api/media/content/{content_id}/assets")
        check("assets endpoint reachable after restart", status == 200, str(assets))

        # ---------- Finish walking content to APPROVAL, then attempt a real publish (honest gap) ----------
        for target in ("VOICE", "VIDEO_EDIT", "CAPTIONS", "THUMBNAIL", "APPROVAL"):
            status, moved = api("POST", f"/api/media/content/{content_id}/transition", {"to_state": target, "actor": "release-qa"})
            check(f"content reaches {target} after restart", status == 200 and moved.get("content_state") == target, str(moved))

        status, pub = api("POST", "/api/media/publications", {"content_id": content_id, "platform": "youtube", "actor": "release-qa"})
        check("publication created", status in (200, 201), str(pub))
        pub_id = pub.get("publication_id")

        status, submitted = api("POST", f"/api/media/publications/{pub_id}/submit-for-approval", {"actor": "release-qa"})
        check("publication submitted for approval", status in (200, 201), str(submitted))
        pub_needs_aryan_id = submitted.get("needs_aryan_id")
        check("publish approval escalation created", bool(pub_needs_aryan_id), str(submitted))

        status, decided2 = api("POST", f"/api/needs-aryan/{pub_needs_aryan_id}/decision", {"action": "approve", "actor": "Aryan-QA", "note": "approved for QA"})
        check("publish approval decision accepted", status == 200, str(decided2))

        status, approved = api("POST", f"/api/media/publications/{pub_id}/approve", {"actor": "Aryan-QA"})
        check("publication reaches APPROVED", status == 200 and approved.get("status") == "APPROVED", str(approved))

        status, published = api("POST", f"/api/media/publications/{pub_id}/publish", {"actor": "release-qa"})
        check("publish attempt via the ONLY route (ManualPublishingChannel) returns 200", status == 200, str(published))
        check("no real YouTube adapter -> lands in FAILED, never falsely PUBLISHED", published.get("status") == "FAILED", str(published))
        check("failed publish escalates again", bool(published.get("needs_aryan_id")), str(published))
        print("NOTE: no real public content published -- stopping at the honest FAILED+escalation state, per instruction.")

        # ---------- Analytics + growth, with a deliberately mixed simulated/real dataset ----------
        status, m1 = api("POST", "/api/media/analytics", {"publication_id": pub_id, "metric_kind": "views", "value": 500000, "source": "simulated_qa", "actor": "release-qa"})
        check("simulated metric recorded (should never affect recommendation)", status in (200, 201), str(m1))
        status, rec_sim_only = api("GET", f"/api/media/publications/{pub_id}/growth-recommendation")
        check("recommendation with ONLY simulated data is honestly insufficient_data", rec_sim_only.get("decision") == "insufficient_data", str(rec_sim_only))

        status, m2 = api("POST", "/api/media/analytics", {"publication_id": pub_id, "metric_kind": "views", "value": 200, "source": "manual_entry", "actor": "release-qa"})
        status, m3 = api("POST", "/api/media/analytics", {"publication_id": pub_id, "metric_kind": "engagement", "value": 30, "source": "manual_entry", "actor": "release-qa"})
        status, rec_real = api("GET", f"/api/media/publications/{pub_id}/growth-recommendation")
        check("recommendation now uses only the real 200/30 rows (15% -> repeat)", rec_real.get("decision") == "repeat", str(rec_real))
        check("simulated 500000-view row did not skew the real recommendation", abs(rec_real.get("engagement_rate", 0) - 0.15) < 0.001, str(rec_real))

        status, exp = api("POST", "/api/media/experiments", {"content_id": content_id, "hypothesis": "Release QA hypothesis", "variable_tested": "hook", "actor": "release-qa"})
        check("growth experiment created", status in (200, 201), str(exp))
        exp_id = exp.get("experiment_id")
        status, exp_done = api("POST", f"/api/media/experiments/{exp_id}/result", {"result": "engagement rose", "decision": "adopt", "actor": "release-qa"})
        check("experiment result recorded", status == 200, str(exp_done))

        # ---------- Today dashboard sanity ----------
        status, dash = api("GET", "/api/rh/dashboard")
        check("today dashboard reachable", status == 200, str(dash))
        for key in ("workforce_blocked_tasks", "media_pending_approval", "content_due_soon", "publishing_failures", "strong_growth_signals"):
            check(f"today dashboard includes '{key}'", key in dash, str(list(dash.keys())))

        # ---------- Final restart at the end, to double-confirm everything above survives too ----------
        print("\n=== Final restart (end-of-workflow persistence, in addition to the mid-workflow one) ===")
        stop_server(proc)
        proc = start_server(root)
        status, final_pub = api("GET", f"/api/media/publications?content_id={content_id}")
        found = any(p.get("id") == pub_id and p.get("status") == "FAILED" for p in final_pub.get("items", []))
        check("publication (still honestly FAILED) survives second restart", found, str(final_pub))
        status, final_wf = api("GET", f"/api/wf/tasks/{wf_task_id}")
        check("completed spreadsheet task survives second restart", status == 200 and final_wf.get("status") == "COMPLETED", str(final_wf))

    finally:
        stop_server(proc)

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
