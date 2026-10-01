"""Phase 5 Continuation, Section 22 -- Learning from Outcomes V1, HTTP
layer. Proves /api/cs/outcomes and /api/cs/projects/{id}/outcome are
reachable over HTTP and return the same evidence-based record the
automatic CLOSED-transition hook persists -- not a duplicate
computation. Synthetic data only.
"""

import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.hq_web import TTTHQHandler


class _LiveServerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(100):
            try:
                status, _ = self._get("/api/config")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _approved_service(self):
        status, body = self._post("/api/cs/services", {
            "service_key": "http-outcomes-website", "category": "web_dev", "title": "Business Website V1",
            "customer_description": "A synthetic HTTP-layer outcomes test service.", "pricing_model": "fixed", "delivery_mode": "remote",
        })
        self.assertEqual(status, 201)
        service_id = body["service_id"]
        status, _ = self._post(f"/api/cs/services/{service_id}/approval", {"actor": "Aryan", "approval_status": "APPROVED"})
        self.assertEqual(status, 200)
        return service_id

    def _project_through_to_closed(self, service_id=None):
        status, body = self._post("/api/cs/intakes", {
            "customer_name": "Http Outcomes Co", "requested_outcome": "A new site", "service_id": service_id,
        })
        self.assertEqual(status, 201)
        intake_id = body["intake_id"]
        self._post(f"/api/cs/intakes/{intake_id}/qualify", {"actor": "Aryan", "qualification_status": "QUALIFIED"})
        self._post(f"/api/cs/intakes/{intake_id}/approve", {"actor": "Aryan"})
        status, body = self._post(f"/api/cs/intakes/{intake_id}/convert-to-project", {"actor": "Aryan", "delivery_route": "CUSTOM_BUILD"})
        self.assertEqual(status, 201)
        project_id = body["project_id"]
        for to_status in ("IN_DELIVERY", "QA", "HANDED_OVER", "INVOICED", "CLOSED"):
            status, body = self._post(f"/api/cs/projects/{project_id}/transition", {"actor": "Aryan", "to_status": to_status})
            self.assertEqual(status, 200, body)
        return project_id


class OutcomesHTTPTests(_LiveServerCase):
    def test_outcome_is_recorded_automatically_and_readable_by_project_over_http(self):
        project_id = self._project_through_to_closed()
        status, outcome = self._get(f"/api/cs/projects/{project_id}/outcome")
        self.assertEqual(status, 200)
        self.assertEqual(outcome["project_id"], project_id)
        self.assertEqual(outcome["acceptance"], "ACCEPTED_CLEAN")
        self.assertEqual(outcome["qa_cycle_count"], 1)
        self.assertEqual(outcome["dispute_count"], 0)

    def test_outcome_endpoint_is_404_for_a_project_with_no_outcome_yet(self):
        status, body = self._post("/api/cs/intakes", {"customer_name": "Http No Outcome Co", "requested_outcome": "A site"})
        intake_id = body["intake_id"]
        self._post(f"/api/cs/intakes/{intake_id}/qualify", {"actor": "Aryan", "qualification_status": "QUALIFIED"})
        self._post(f"/api/cs/intakes/{intake_id}/approve", {"actor": "Aryan"})
        status, body = self._post(f"/api/cs/intakes/{intake_id}/convert-to-project", {"actor": "Aryan", "delivery_route": "CUSTOM_BUILD"})
        project_id = body["project_id"]
        status, body = self._get(f"/api/cs/projects/{project_id}/outcome")
        self.assertEqual(status, 404)

    def test_outcomes_list_over_http_filters_by_service_and_acceptance(self):
        service_id = self._approved_service()
        project_id = self._project_through_to_closed(service_id=service_id)
        status, body = self._get(f"/api/cs/outcomes?service_id={service_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["project_id"], project_id)
        status, body = self._get(f"/api/cs/outcomes?service_id={service_id}&acceptance=ACCEPTED_WITH_REWORK")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])

    def test_outcomes_list_over_http_with_no_data_is_empty(self):
        status, body = self._get("/api/cs/outcomes")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])


if __name__ == "__main__":
    unittest.main()
