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

from falguna.chat import ChatError, ChatResponder, ConversationStore, search_missions
from falguna.gateway import OpenAICompatibleGateway
from falguna.models import RunPolicy, WorkerResult
from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler, INDEX_HTML
from falguna.workers import ScriptedWorker


def git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class ConversationStoreTests(unittest.TestCase):
    """Persistence layer only -- no HTTP, no model transport. Proves the new
    conversations/chat_messages/conversation_handoffs tables (additive to the
    existing schema) behave correctly through StateStore, and that a handoff
    row only ever references a run_id the real control plane produced."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.chat = ConversationStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_list_rename_and_archive_conversation(self):
        conversation_id = self.chat.create_conversation("  My first chat  ")
        self.assertEqual(self.chat.get_conversation(conversation_id)["title"], "My first chat")
        self.assertEqual(self.chat.get_conversation(conversation_id)["status"], "ACTIVE")

        self.chat.rename_conversation(conversation_id, "Renamed")
        self.assertEqual(self.chat.get_conversation(conversation_id)["title"], "Renamed")
        with self.assertRaises(ChatError):
            self.chat.rename_conversation(conversation_id, "   ")
        with self.assertRaises(ChatError):
            self.chat.rename_conversation("does-not-exist", "x")

        listed = self.chat.list_conversations()
        self.assertEqual([c["id"] for c in listed], [conversation_id])

        self.chat.archive_conversation(conversation_id)
        self.assertEqual(self.chat.get_conversation(conversation_id)["status"], "ARCHIVED")
        self.assertEqual(self.chat.list_conversations(), [])

    def test_list_conversations_carries_a_truncated_last_message_preview(self):
        # Additive presentation data only for the sidebar -- it must reflect
        # the most recent message and never affect ordering or identity.
        conversation_id = self.chat.create_conversation("Chat")
        self.assertEqual(self.chat.list_conversations()[0]["last_message_preview"], "")
        self.chat.add_message(conversation_id, "user", "hello there")
        self.assertEqual(self.chat.list_conversations()[0]["last_message_preview"], "hello there")
        self.chat.add_message(conversation_id, "assistant", "a" * 200)
        preview = self.chat.list_conversations()[0]["last_message_preview"]
        self.assertEqual(len(preview), 141)
        self.assertTrue(preview.endswith("\u2026"))

    def test_add_message_updates_conversation_and_rejects_empty_user_text(self):
        conversation_id = self.chat.create_conversation("Chat")
        before = self.chat.get_conversation(conversation_id)["updated_at"]
        message = self.chat.add_message(conversation_id, "user", "hello there")
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["content"], "hello there")
        after = self.chat.get_conversation(conversation_id)["updated_at"]
        self.assertGreaterEqual(after, before)
        self.assertEqual(len(self.chat.list_messages(conversation_id)), 1)
        with self.assertRaises(ChatError):
            self.chat.add_message(conversation_id, "user", "   ")
        with self.assertRaises(ChatError):
            self.chat.add_message(conversation_id, "system", "not allowed")
        # An assistant message is allowed to carry an error instead of content.
        errored = self.chat.add_message(conversation_id, "assistant", "", error="MODEL_UNAVAILABLE: no codex")
        self.assertEqual(errored["error"], "MODEL_UNAVAILABLE: no codex")

    def test_handoff_links_a_real_run_produced_by_the_real_control_plane(self):
        # This is the safety-relevant seam: a handoff row must reference a run_id
        # that ControlPlane.create_mission/start actually produced -- it must
        # never be usable to fabricate or mutate run state.
        conversation_id = self.chat.create_conversation("Fix the bug")
        policy = RunPolicy()
        ids = self.control.create_mission("fix", "set value to 2", self.repo, policy)

        def edit(worktree, requirement, run_id):
            return WorkerResult(True, "no-op", 0)

        run_id = self.control.start(ids["task_id"], ScriptedWorker(edit), "scripted", "none", policy, force_stop_after="WORKTREE_READY")
        self.chat.record_handoff(conversation_id, run_id, "set value to 2")

        handoffs = self.chat.list_handoffs(conversation_id)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["run_id"], run_id)
        self.assertEqual(handoffs[0]["objective"], "set value to 2")
        self.assertEqual([h["conversation_id"] for h in self.chat.handoffs_for_run(run_id)], [conversation_id])
        # The run itself is untouched by the handoff -- it is a real row from
        # the real control plane, not something conversation_handoffs created.
        self.assertIsNotNone(self.store.get("runs", run_id))

    def test_search_conversations_matches_title_and_message_content(self):
        matching = self.chat.create_conversation("Refactor the login flow")
        other = self.chat.create_conversation("Unrelated topic")
        self.chat.add_message(other, "user", "mentions login somewhere in the body")
        hits = {r["id"] for r in self.chat.search_conversations("login")}
        self.assertEqual(hits, {matching, other})
        self.assertEqual(self.chat.search_conversations(""), [])
        self.assertEqual(self.chat.search_conversations("nothing matches this"), [])

    def test_search_missions_matches_mission_title_and_objective(self):
        policy = RunPolicy()
        ids = self.control.create_mission("Improve caching", "add an LRU cache to the lookup", self.repo, policy)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(lambda *_: WorkerResult(True, "ok", 0)), "scripted", "none", policy, force_stop_after="WORKTREE_READY")
        hits = search_missions(self.store, "caching")
        self.assertEqual([h["run_id"] for h in hits], [run_id])
        self.assertEqual(hits[0]["type"], "mission")
        self.assertEqual(search_missions(self.store, "nothing here matches"), [])


class ChatResponderTests(unittest.TestCase):
    """Unit tests against the same fake-transport pattern test_bootstrap.py
    already uses for StructuredEditWorker/ModelSemanticReviewer -- proves
    ChatResponder speaks the identical schema/transport contract without
    requiring a real authenticated Codex CLI."""

    def gateway(self):
        return OpenAICompatibleGateway("test-model", "http://127.0.0.1:1/v1", "")

    def test_reply_parses_structured_content_and_records_usage(self):
        def response(config, payload, timeout):
            self.assertEqual(payload["response_format"]["json_schema"]["name"], "chat_reply")
            self.assertEqual(payload["messages"][0]["role"], "system")
            return {
                "choices": [{"message": {"content": json.dumps({"reply": "Sure, tell me more.", "suggested_objective": None})}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}},
            }
        outcome = ChatResponder(self.gateway(), response, "test-model").reply([{"role": "user", "content": "hi"}])
        self.assertEqual(outcome["reply"], "Sure, tell me more.")
        self.assertIsNone(outcome["suggested_objective"])
        self.assertEqual(outcome["model_call"]["purpose"], "chat")
        self.assertGreater(outcome["model_call"]["cost_usd"], 0)

    def test_reply_surfaces_a_suggested_objective(self):
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": json.dumps({"reply": "Here is a plan.", "suggested_objective": "Fix the null pointer in checkout"})}}]}
        outcome = ChatResponder(self.gateway(), response, "test-model").reply([{"role": "user", "content": "the checkout page crashes"}])
        self.assertEqual(outcome["suggested_objective"], "Fix the null pointer in checkout")

    def test_transport_failure_raises_chat_error_not_a_crash(self):
        def response(config, payload, timeout):
            raise OSError("TRANSPORT_FAILURE: boom")
        with self.assertRaises(ChatError):
            ChatResponder(self.gateway(), response, "test-model").reply([{"role": "user", "content": "hi"}])

    def test_refusal_raises_chat_error(self):
        def response(config, payload, timeout):
            return {"choices": [{"message": {"refusal": "cannot help with that"}}]}
        with self.assertRaises(ChatError):
            ChatResponder(self.gateway(), response, "test-model").reply([{"role": "user", "content": "hi"}])

    def test_malformed_json_raises_chat_error(self):
        def response(config, payload, timeout):
            return {"choices": [{"message": {"content": "not json"}}]}
        with self.assertRaises(ChatError):
            ChatResponder(self.gateway(), response, "test-model").reply([{"role": "user", "content": "hi"}])


class _LiveFalgunaServerCase(unittest.TestCase):
    """Boots a real repo + a real FalgunaHandler HTTP server on a scratch port,
    mirroring the pattern test_hq_web.py already uses for TTTHQHandler."""

    port = 8797

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "falguna@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Falguna Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.control, self.store = open_control_plane(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), FalgunaHandler)
        self.server.app_root = self.repo
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

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
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2) as resp:
            return resp.status, json.loads(resp.read())

    def _get_status(self, path):
        try:
            self._get(path)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class ChatHttpLayerTests(_LiveFalgunaServerCase):
    def test_conversation_create_list_and_fetch_round_trip_over_real_http(self):
        status, created = self._post("/api/conversations", {"title": "Investigate the flaky test"})
        self.assertEqual(status, 201)
        conversation_id = created["id"]

        status, listed = self._get("/api/conversations")
        self.assertEqual(status, 200)
        self.assertTrue(any(c["id"] == conversation_id for c in listed["conversations"]))

        status, detail = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["conversation"]["title"], "Investigate the flaky test")
        self.assertEqual(detail["messages"], [])
        self.assertEqual(detail["handoffs"], [])

        self.assertEqual(self._get_status("/api/conversations/does-not-exist"), 404)

    def test_posting_a_message_without_an_authenticated_codex_runtime_degrades_gracefully(self):
        # No `codex` executable is installed in this test environment. The
        # message endpoint must still persist the user's message and return
        # 200 with a clearly-errored assistant turn -- never a 500, and never
        # a fabricated reply.
        status, created = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = created["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "hello Falguna"})
        self.assertEqual(status, 200)
        self.assertEqual(out["message"]["content"], "hello Falguna")
        self.assertIsNotNone(out["assistant"]["error"])
        self.assertIn("MODEL_UNAVAILABLE", out["assistant"]["error"])

        status, detail = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(len(detail["messages"]), 2)
        self.assertEqual(self._get_status(f"/api/conversations/{conversation_id}/messages"), 404)  # GET not allowed on this path

        status, err = self._post("/api/conversations/does-not-exist/messages", {"content": "hi"})
        self.assertEqual(status, 404)

        status, err = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "   "})
        self.assertEqual(status, 400)

    def test_rename_conversation_over_http(self):
        status, created = self._post("/api/conversations", {"title": "Old title"})
        conversation_id = created["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/rename", {"title": "New title"})
        self.assertEqual(status, 200)
        status, detail = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(detail["conversation"]["title"], "New title")

    def test_search_finds_conversation_by_title(self):
        self._post("/api/conversations", {"title": "Rate limiting investigation"})
        status, out = self._get("/api/search?q=rate+limiting")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["type"] == "conversation" for r in out["results"]))
        status, empty = self._get("/api/search?q=")
        self.assertEqual(empty["results"], [])

    def test_settings_reports_model_and_preserved_boundaries_without_exposing_a_policy_editor(self):
        status, out = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("model", out)
        self.assertTrue(any("Human-only merge decision" in b for b in out["boundaries"]))
        self.assertTrue(any("Chat has no tools" in b for b in out["boundaries"]))

    def test_handoff_rejects_a_conversation_that_does_not_exist(self):
        status, out = self._post("/api/conversations/does-not-exist/handoff", {"project_id": "falguna-engineering", "objective": "irrelevant, conversation missing"})
        self.assertEqual(status, 404)

    def test_handoff_enforces_the_same_approved_project_guardrail_as_a_direct_mission(self):
        # Chat -> Work handoff must go through _launch exactly like /api/runs --
        # proven here by getting the identical validation error for an
        # unapproved project on both endpoints.
        status, created = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = created["id"]
        status, handoff_err = self._post(f"/api/conversations/{conversation_id}/handoff", {"project_id": "not-a-real-project", "objective": "a bounded objective sentence"})
        self.assertEqual(status, 400)
        status, direct_err = self._post("/api/runs", {"project": "not-a-real-project", "objective": "a bounded objective sentence"})
        self.assertEqual(status, 400)
        self.assertEqual(handoff_err["error"], direct_err["error"])

    def test_run_view_exposes_conversation_handoffs_for_the_work_timeline(self):
        conversation_id = ConversationStore(self.store).create_conversation("Chat")
        policy = RunPolicy()
        ids = self.control.create_mission("fix", "set value to 2", self.repo, policy)
        run_id = self.control.start(ids["task_id"], ScriptedWorker(lambda *_: WorkerResult(True, "ok", 0)), "scripted", "none", policy, force_stop_after="WORKTREE_READY")
        ConversationStore(self.store).record_handoff(conversation_id, run_id, "set value to 2")
        status, out = self._get(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["conversation_handoffs"]), 1)
        self.assertEqual(out["conversation_handoffs"][0]["conversation_id"], conversation_id)


if __name__ == "__main__":
    unittest.main()
