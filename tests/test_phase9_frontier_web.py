import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.web import FalgunaHandler, INDEX_HTML


class Phase9FrontierWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"; self.root.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FalgunaHandler)
        self.server.app_root = self.root
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.tmp.cleanup()

    def request(self, path, body=None, org="ttt-org"):
        headers = {"Content-Type": "application/json", "X-Falguna-Organization": org}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_frontier_ui_and_objective_api_flow(self):
        self.assertIn("renderFrontierView", INDEX_HTML)
        self.assertIn("Persistent objectives", INDEX_HTML)
        status, dashboard = self.request("/api/frontier")
        self.assertEqual(status, 200); self.assertEqual(dashboard["phase"], 9)
        status, created = self.request("/api/frontier/objectives", {
            "title": "Persistent synthetic objective", "description": "Keep safe local work resumable across sessions.",
            "mode": "WORK", "autonomy_level": "ASSIST",
        })
        self.assertEqual(status, 201)
        objective_id = created["objective"]["id"]
        status, planned = self.request(f"/api/frontier/objectives/{objective_id}/plan", {})
        self.assertEqual(status, 200); self.assertEqual(len(planned["nodes"]), 6)
        status, bundle = self.request(f"/api/frontier/objectives/{objective_id}/checkpoint", {})
        self.assertEqual(status, 201); self.assertEqual(bundle["version"], 1)
        self.assertEqual(self.request(f"/api/frontier/objectives/{objective_id}", org="other-org")[0], 404)


if __name__ == "__main__":
    unittest.main()
