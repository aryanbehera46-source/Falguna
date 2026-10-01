"""Phase 5 Continuation, Section 14 -- Capability Registry V1, HTTP layer.

Same _LiveServerCase pattern as test_commercial_web.py: a real
TTTHQHandler on a dynamic ephemeral port, proving /api/cs/capabilities/*
actually syncs from the real, production worker roster
(_build_workforce_orchestrator) and enforces profile validation at the
HTTP layer. Synthetic data only.
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


class _LiveHQServerCase(unittest.TestCase):
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


class CapabilityRegistryHTTPTests(_LiveHQServerCase):
    def test_sync_discovers_the_real_production_worker_roster(self):
        status, body = self._post("/api/cs/capabilities/sync", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        names = {c["worker_name"] for c in body["items"]}
        # A handful of the always-registered production workers -- proves
        # this reflects the real _build_workforce_orchestrator roster, not
        # a hand-maintained duplicate list.
        self.assertIn("browser_worker", names)
        self.assertIn("research_worker", names)
        self.assertIn("document_worker", names)

    def test_list_before_sync_is_empty(self):
        status, body = self._get("/api/cs/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])

    def test_can_deliver_true_for_known_production_task_type(self):
        result_status, body = self._get("/api/cs/capabilities/can-deliver?task_type=browser_research")
        self.assertEqual(result_status, 200)
        self.assertTrue(body["can_deliver"])
        self.assertEqual(body["worker_name"], "browser_worker")

    def test_can_deliver_false_for_unknown_task_type(self):
        result_status, body = self._get("/api/cs/capabilities/can-deliver?task_type=interplanetary_logistics")
        self.assertEqual(result_status, 200)
        self.assertFalse(body["can_deliver"])

    def test_can_deliver_requires_task_type(self):
        result_status, body = self._get("/api/cs/capabilities/can-deliver")
        self.assertEqual(result_status, 400)

    def test_set_profile_over_http_then_can_deliver_reflects_it(self):
        self._post("/api/cs/capabilities/sync", {"actor": "Aryan"})
        status, body = self._get("/api/cs/capabilities")
        research = next(c for c in body["items"] if c["worker_name"] == "research_worker")
        status, result = self._post(f"/api/cs/capabilities/{research['id']}/profile", {
            "actor": "Aryan", "availability": "UNAVAILABLE", "known_limitations": "No search provider wired in this environment.",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["availability"], "UNAVAILABLE")
        _, deliver = self._get("/api/cs/capabilities/can-deliver?task_type=research")
        self.assertFalse(deliver["can_deliver"])
        self.assertEqual(deliver["known_limitations"], "No search provider wired in this environment.")

    def test_set_profile_rejects_invalid_availability_over_http(self):
        self._post("/api/cs/capabilities/sync", {"actor": "Aryan"})
        status, body = self._get("/api/cs/capabilities")
        any_cap = body["items"][0]
        status, result = self._post(f"/api/cs/capabilities/{any_cap['id']}/profile", {
            "actor": "Aryan", "availability": "SOMEWHAT",
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
