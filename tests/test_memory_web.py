"""HTTP-level tests for Falguna Memory & Knowledge V2 (falguna/web.py's
/api/memory/* and /api/knowledge/* routes), plus the Chat -> Memory
integration (suggestion capture on a posted message) and a real
process-restart persistence test (Acceptance Demonstration #1).

Same real-server pattern as tests/test_browser_web.py's
_LiveFalgunaServerCase, kept as a local copy per this test suite's own
"no cross-test-file import" convention -- deliberately does not require
Playwright (these routes have nothing to do with the browser runtime), so
it never skips in an environment where Playwright happens to be missing.
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

from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler


class _LiveFalgunaServerCase(unittest.TestCase):
    port = 8845

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
        self._start_server()

    def _start_server(self):
        self.control, self.store = open_control_plane(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), FalgunaHandler)
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def _restart_server(self):
        """Simulates exactly what a real Falguna restart does: the process
        (and this test's in-memory StateStore/objects) goes away entirely
        and a brand new one opens the same on-disk .falguna/state.db --
        Acceptance Demonstration #1's "restart Falguna" step."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self._start_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(40):
            try:
                status, body = self._get("/api/config")
                if body.get("product") == "Falguna Engineering":
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("Falguna server did not become ready")

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class MemoryRouteTests(_LiveFalgunaServerCase):
    def test_create_and_get_a_personal_memory_record(self):
        status, out = self._post("/api/memory", {
            "scope_type": "personal", "kind": "preference", "content": "Aryan wants terse status updates",
            "source_type": "user_stated", "confidence": "user_provided",
        })
        self.assertEqual(status, 201, out)
        status, fetched = self._get(f"/api/memory/{out['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["content"], "Aryan wants terse status updates")
        self.assertEqual(fetched["state"], "active")

    def test_project_scope_rejects_an_unapproved_project_id(self):
        status, out = self._post("/api/memory", {
            "scope_type": "project", "scope_id": "not-a-real-project", "kind": "fact",
            "content": "x", "source_type": "user_stated", "confidence": "user_provided",
        })
        self.assertEqual(status, 400, out)

    def test_project_scope_accepts_the_self_referential_falguna_engineering_project(self):
        status, out = self._post("/api/memory", {
            "scope_type": "project", "scope_id": "falguna-engineering", "kind": "fact",
            "content": "This project's default branch is main", "source_type": "user_stated", "confidence": "verified",
        })
        self.assertEqual(status, 201, out)
        self.assertEqual(out["scope_id"], "falguna-engineering")

    def test_conflict_is_surfaced_as_409_with_candidates(self):
        self._post("/api/memory", {
            "scope_type": "personal", "kind": "preference", "content": "Aryan prefers dark mode everywhere",
            "source_type": "user_stated", "confidence": "user_provided",
        })
        status, out = self._post("/api/memory", {
            "scope_type": "personal", "kind": "preference", "content": "Aryan prefers dark mode everywhere always",
            "source_type": "user_stated", "confidence": "user_provided",
        })
        self.assertEqual(status, 409, out)
        self.assertGreaterEqual(len(out["candidates"]), 1)

    def test_supersede_route_excludes_old_from_listing_and_keeps_history(self):
        _, r1 = self._post("/api/memory", {
            "scope_type": "personal", "kind": "fact", "content": "Aryan's timezone is IST",
            "source_type": "user_stated", "confidence": "user_provided",
        })
        status, r2 = self._post(f"/api/memory/{r1['id']}/supersede", {"content": "Aryan's timezone is now PST"})
        self.assertEqual(status, 201, r2)
        _, listing = self._get("/api/memory?scope_type=personal")
        ids = [r["id"] for r in listing["records"]]
        self.assertIn(r2["id"], ids)
        self.assertNotIn(r1["id"], ids)
        _, history = self._get(f"/api/memory/{r2['id']}/history")
        self.assertEqual([h["id"] for h in history["history"]], [r1["id"], r2["id"]])

    def test_pin_edit_forget_and_purge_round_trip(self):
        _, r = self._post("/api/memory", {
            "scope_type": "personal", "kind": "fact", "content": "a fact to manage",
            "source_type": "user_stated", "confidence": "user_provided",
        })
        status, pinned = self._post(f"/api/memory/{r['id']}/pin", {"pinned": True})
        self.assertEqual(pinned["pinned"], 1)
        status, edited = self._post(f"/api/memory/{r['id']}/edit", {"content": "a corrected fact"})
        self.assertEqual(edited["content"], "a corrected fact")
        status, forgotten = self._post(f"/api/memory/{r['id']}/forget", {"reason": "no longer true"})
        self.assertEqual(forgotten["state"], "deleted")
        status, purged = self._post(f"/api/memory/{r['id']}/purge", {})
        self.assertEqual(status, 200, purged)
        self.assertIsNotNone(purged["purged_at"])

    def test_search_endpoint_isolates_by_project(self):
        self._post("/api/memory", {
            "scope_type": "project", "scope_id": "falguna-engineering", "kind": "fact",
            "content": "Falguna Engineering uses SQLite for local state", "source_type": "user_stated", "confidence": "verified",
        })
        status, own = self._get("/api/memory?scope_type=project&scope_id=falguna-engineering&q=SQLite+local+state")
        self.assertEqual(len(own["records"]), 1)
        status, other = self._get("/api/memory?scope_type=personal&q=SQLite+local+state")
        self.assertEqual(other["records"], [])

    def test_unknown_scope_filter_is_dropped_not_widened(self):
        status, out = self._get("/api/memory?scope_type=project&scope_id=totally-unknown-project")
        self.assertEqual(status, 200)
        self.assertEqual(out["records"], [])
        self.assertEqual(out["scopes"], [])


class KnowledgeRouteTests(_LiveFalgunaServerCase):
    def test_ingest_search_and_forget_round_trip_over_http(self):
        import base64
        content = "Falguna's memory system is project-scoped and provenance-aware. " * 10
        status, doc = self._post("/api/knowledge/documents", {
            "filename": "memory-notes.txt", "mime_type": "text/plain",
            "data_base64": base64.b64encode(content.encode()).decode(),
            "scope_type": "project", "scope_id": "falguna-engineering", "source_type": "upload",
        })
        self.assertEqual(status, 201, doc)
        self.assertEqual(doc["status"], "ready")
        status, listing = self._get("/api/knowledge/documents?scope_type=project&scope_id=falguna-engineering")
        self.assertEqual(len(listing["documents"]), 1)
        status, detail = self._get(f"/api/knowledge/documents/{doc['id']}")
        self.assertEqual(status, 200)
        self.assertGreater(len(detail["chunks"]), 0)
        status, other_project = self._get("/api/knowledge/documents?scope_type=project&scope_id=serviceflow")
        self.assertEqual(other_project["documents"], [])
        status, forgotten = self._post(f"/api/knowledge/documents/{doc['id']}/forget", {"reason": "test cleanup"})
        self.assertEqual(forgotten["status"], "deleted")
        status, detail_after = self._get(f"/api/knowledge/documents/{doc['id']}")
        self.assertEqual(detail_after["chunks"], [])

    def test_unsupported_format_returns_a_clear_status_not_a_failure(self):
        import base64
        status, doc = self._post("/api/knowledge/documents", {
            "filename": "image.png", "mime_type": "image/png",
            "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
            "scope_type": "personal", "source_type": "upload",
        })
        self.assertEqual(status, 201, doc)
        self.assertEqual(doc["status"], "unsupported_format")


class MemorySettingsRouteTests(_LiveFalgunaServerCase):
    def test_get_reports_honest_local_only_defaults(self):
        status, out = self._get("/api/memory/settings")
        self.assertEqual(status, 200)
        self.assertEqual(out["provider"], "none")
        self.assertFalse(out["semantic_retrieval_active"])

    def test_selecting_ollama_without_a_reachable_runtime_stays_honest(self):
        status, out = self._post("/api/memory/settings", {"embedding_provider": "ollama", "ollama_base_url": "http://127.0.0.1:1"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["provider"], "ollama")
        self.assertTrue(out["configured"])
        # configured != available -- Falguna never claims semantic retrieval
        # is active just because a provider was selected in Settings.
        self.assertFalse(out["available"])
        self.assertFalse(out["semantic_retrieval_active"])


class ChatSuggestionCaptureTests(_LiveFalgunaServerCase):
    """Posting a message is enough to trigger suggestion capture -- this
    runs synchronously before the (separately, asynchronously generated)
    reply, so it is fully testable even with no model provider reachable
    in this environment."""

    def test_a_save_worthy_chat_message_produces_a_pending_suggestion(self):
        status, conv = self._post("/api/conversations", {"title": "test"})
        self.assertEqual(status, 201, conv)
        status, posted = self._post(f"/api/conversations/{conv['id']}/messages", {"content": "Please remember that I always deploy on Fridays"})
        self.assertEqual(status, 202, posted)
        status, suggestions = self._get("/api/memory/suggestions?scope_type=personal")
        self.assertEqual(len(suggestions["suggestions"]), 1)
        self.assertEqual(suggestions["suggestions"][0]["suggested_kind"], "instruction")

    def test_accepting_a_suggestion_over_http_creates_a_memory_record(self):
        _, conv = self._post("/api/conversations", {"title": "test"})
        self._post(f"/api/conversations/{conv['id']}/messages", {"content": "Remember that I use tabs not spaces"})
        _, suggestions = self._get("/api/memory/suggestions?scope_type=personal")
        suggestion_id = suggestions["suggestions"][0]["id"]
        status, record = self._post(f"/api/memory/suggestions/{suggestion_id}/accept", {})
        self.assertEqual(status, 201, record)
        _, listing = self._get("/api/memory?scope_type=personal")
        self.assertIn(record["id"], [r["id"] for r in listing["records"]])

    def test_an_ordinary_chat_message_produces_no_suggestion(self):
        _, conv = self._post("/api/conversations", {"title": "test"})
        self._post(f"/api/conversations/{conv['id']}/messages", {"content": "What's on the roadmap this week?"})
        _, suggestions = self._get("/api/memory/suggestions?scope_type=personal")
        self.assertEqual(suggestions["suggestions"], [])


class RestartPersistenceTests(_LiveFalgunaServerCase):
    """Acceptance Demonstration #1: save an approved preference, restart
    Falguna, retrieve it with provenance."""

    def test_a_saved_memory_survives_a_full_server_restart_with_provenance_intact(self):
        status, saved = self._post("/api/memory", {
            "scope_type": "personal", "kind": "preference", "content": "Aryan wants concise commit messages",
            "source_type": "user_stated", "confidence": "user_provided", "actor": "Aryan",
        })
        self.assertEqual(status, 201, saved)
        record_id = saved["id"]

        self._restart_server()  # simulates a real process restart against the same on-disk repo

        status, fetched = self._get(f"/api/memory/{record_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["content"], "Aryan wants concise commit messages")
        self.assertEqual(fetched["confidence"], "user_provided")
        self.assertEqual(fetched["source_type"], "user_stated")
        self.assertEqual(fetched["actor"], "Aryan")
        self.assertEqual(fetched["state"], "active")
        status, hits = self._get("/api/memory?scope_type=personal&q=concise+commit+messages")
        self.assertEqual(status, 200)
        self.assertIn(record_id, [r["id"] for r in hits["records"]])

    def test_knowledge_documents_and_chunks_survive_a_restart(self):
        import base64
        status, doc = self._post("/api/knowledge/documents", {
            "filename": "persisted.txt", "mime_type": "text/plain",
            "data_base64": base64.b64encode(b"This document must still be searchable after a restart. " * 5).decode(),
            "scope_type": "personal", "source_type": "upload",
        })
        self.assertEqual(status, 201, doc)

        self._restart_server()

        status, detail = self._get(f"/api/knowledge/documents/{doc['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["document"]["status"], "ready")
        self.assertGreater(len(detail["chunks"]), 0)
        status, hits = self._get("/api/knowledge/documents?scope_type=personal")
        self.assertIn(doc["id"], [d["id"] for d in hits["documents"]])


if __name__ == "__main__":
    unittest.main()
