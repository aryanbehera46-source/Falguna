"""Phase 5 Continuation, Section 16 -- Delivery Capacity & Scheduling V1,
HTTP layer. Proves /api/cs/capacity is reachable and returns the real,
evidence-based snapshot over HTTP. Synthetic data only.
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


class CapacityHTTPTests(unittest.TestCase):
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

    def test_capacity_snapshot_over_http_with_no_data_is_available(self):
        status, body = self._get("/api/cs/capacity")
        self.assertEqual(status, 200)
        self.assertEqual(body["recommendation"], "AVAILABLE")
        self.assertIn("thresholds", body)
        self.assertIn("recommendation_reason", body)


if __name__ == "__main__":
    unittest.main()
