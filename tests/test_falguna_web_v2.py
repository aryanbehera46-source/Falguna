"""Tests for Falguna Product Experience V2: the async Chat message-state
pipeline (Sections 3-4), stop/regenerate/edit-resubmit, attachments
(Section 14-15), notifications (Section 13), model/work-mode selection
(Sections 5-6), and the Mission Control / usage / files aggregate
endpoints (Sections 7, 26, 14).

Mirrors the structure and conventions tests/test_chat_web.py and
tests/test_search_web.py already established: a persistence-layer class
(no HTTP), and a live-HTTP class booting a real FalgunaHandler server on a
scratch port. This environment has no authenticated `codex` executable
(same as every other Falguna test file), so every async chat call degrades
to a real FAILED state with a MODEL_UNAVAILABLE error -- that degradation
is itself what several tests below assert on, exactly like
test_chat_web.py's existing "degrades gracefully" test.
"""
import base64
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

from falguna.attachments import AttachmentError, AttachmentStore
from falguna.chat import ChatError, ConversationStore
from falguna.models import RunPolicy, WorkerResult
from falguna.notifications import NotificationStore
from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler, WORK_MODE_SETTINGS
from falguna.workers import ScriptedWorker


class ChatMessageStateTests(unittest.TestCase):
    """Persistence layer only -- proves the explicit PENDING -> GENERATING
    -> COMPLETED/FAILED/CANCELLED state machine (Section 4) and the
    supersede-based regenerate/edit-resubmit mechanics (Section 3)."""

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
        self.control, self.store = open_control_plane(self.repo)
        self.chat = ConversationStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_pending_message_transitions_through_generating_to_completed(self):
        conversation_id = self.chat.create_conversation("Chat")
        pending = self.chat.add_pending_message(conversation_id)
        self.assertEqual(pending["status"], "PENDING")
        self.chat.mark_generating(pending["id"])
        self.assertEqual(self.store.get("chat_messages", pending["id"])["status"], "GENERATING")
        applied = self.chat.complete_message(pending["id"], "final reply", {"cost_usd": 0.0}, None)
        self.assertTrue(applied)
        row = self.store.get("chat_messages", pending["id"])
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["content"], "final reply")

    def test_fail_message_records_error_and_clears_content(self):
        conversation_id = self.chat.create_conversation("Chat")
        pending = self.chat.add_pending_message(conversation_id)
        applied = self.chat.fail_message(pending["id"], "MODEL_UNAVAILABLE: no codex")
        self.assertTrue(applied)
        row = self.store.get("chat_messages", pending["id"])
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("MODEL_UNAVAILABLE", row["error"])

    def test_cancel_message_then_complete_or_fail_is_a_no_op(self):
        # The core race-safety property (Section 3, Stop generation):
        # once a message is CANCELLED, a background worker that finishes
        # later must never be able to overwrite it with a delivered reply
        # or a failure -- the cancel is final.
        conversation_id = self.chat.create_conversation("Chat")
        pending = self.chat.add_pending_message(conversation_id)
        cancelled = self.chat.cancel_message(pending["id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertFalse(self.chat.complete_message(pending["id"], "too late", None, None))
        self.assertFalse(self.chat.fail_message(pending["id"], "too late error"))
        row = self.store.get("chat_messages", pending["id"])
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(row["content"], "")

    def test_cancel_requires_a_generating_or_pending_assistant_message(self):
        conversation_id = self.chat.create_conversation("Chat")
        user_message = self.chat.add_message(conversation_id, "user", "hi")
        with self.assertRaises(ChatError):
            self.chat.cancel_message(user_message["id"])  # not an assistant message
        pending = self.chat.add_pending_message(conversation_id)
        self.chat.complete_message(pending["id"], "done", None, None)
        with self.assertRaises(ChatError):
            self.chat.cancel_message(pending["id"])  # already terminal
        with self.assertRaises(ChatError):
            self.chat.cancel_message("does-not-exist")

    def test_list_messages_excludes_superseded_rows_but_keeps_them_in_storage(self):
        conversation_id = self.chat.create_conversation("Chat")
        first = self.chat.add_message(conversation_id, "user", "first")
        second = self.chat.add_pending_message(conversation_id)
        self.chat.complete_message(second["id"], "reply one", None, None)
        self.assertEqual(len(self.chat.list_messages(conversation_id)), 2)
        self.chat.prepare_regenerate(conversation_id, second["id"])
        visible = self.chat.list_messages(conversation_id)
        self.assertEqual([m["id"] for m in visible], [first["id"]])
        # Still physically present, just no longer visible -- audit trail kept.
        self.assertIsNotNone(self.store.get("chat_messages", second["id"]))
        self.assertEqual(self.store.get("chat_messages", second["id"])["superseded"], 1)

    def test_regenerate_only_allowed_on_the_most_recent_visible_message(self):
        conversation_id = self.chat.create_conversation("Chat")
        older = self.chat.add_pending_message(conversation_id)
        self.chat.complete_message(older["id"], "older reply", None, None)
        self.chat.add_message(conversation_id, "user", "a follow-up")
        with self.assertRaises(ChatError):
            self.chat.prepare_regenerate(conversation_id, older["id"])

    def test_regenerate_rejects_a_still_generating_message(self):
        conversation_id = self.chat.create_conversation("Chat")
        pending = self.chat.add_pending_message(conversation_id)
        self.chat.mark_generating(pending["id"])
        with self.assertRaises(ChatError):
            self.chat.prepare_regenerate(conversation_id, pending["id"])

    def test_edit_user_message_supersedes_everything_after_it(self):
        conversation_id = self.chat.create_conversation("Chat")
        user_message = self.chat.add_message(conversation_id, "user", "original")
        reply = self.chat.add_pending_message(conversation_id)
        self.chat.complete_message(reply["id"], "original reply", None, None)
        edited = self.chat.edit_user_message(conversation_id, user_message["id"], "  revised text  ")
        self.assertEqual(edited["content"], "revised text")
        self.assertIsNotNone(edited["edited_at"])
        visible = self.chat.list_messages(conversation_id)
        self.assertEqual([m["id"] for m in visible], [user_message["id"]])

    def test_edit_rejects_empty_content_and_non_user_messages(self):
        conversation_id = self.chat.create_conversation("Chat")
        user_message = self.chat.add_message(conversation_id, "user", "original")
        with self.assertRaises(ChatError):
            self.chat.edit_user_message(conversation_id, user_message["id"], "   ")
        pending = self.chat.add_pending_message(conversation_id)
        with self.assertRaises(ChatError):
            self.chat.edit_user_message(conversation_id, pending["id"], "not a user message")

    def test_delete_conversation_is_distinct_from_archive_and_both_hide_from_the_active_list(self):
        archived_id = self.chat.create_conversation("To archive")
        deleted_id = self.chat.create_conversation("To delete")
        self.chat.archive_conversation(archived_id)
        self.chat.delete_conversation(deleted_id)
        self.assertEqual(self.store.get("conversations", archived_id)["status"], "ARCHIVED")
        self.assertEqual(self.store.get("conversations", deleted_id)["status"], "DELETED")
        self.assertEqual(self.chat.list_conversations(), [])
        with self.assertRaises(ChatError):
            self.chat.delete_conversation("does-not-exist")


class AttachmentStoreTests(unittest.TestCase):
    """Real filesystem storage under .falguna/attachments/ -- no network,
    no execution of uploaded content."""

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
        self.control, self.store = open_control_plane(self.repo)
        self.attachments = AttachmentStore(self.store, self.repo)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_save_and_read_round_trips_bytes_and_records_a_real_sha256(self):
        import hashlib
        data = b"hello world attachment"
        record = self.attachments.save_base64("notes.txt", "text/plain", base64.b64encode(data).decode())
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.attachments.read_bytes(record["id"]), data)
        stored_path = Path(self.store.path).parent / record["storage_rel_path"]
        self.assertTrue(stored_path.is_file())

    def test_rejects_invalid_base64_and_empty_and_oversized_payloads(self):
        with self.assertRaises(AttachmentError):
            self.attachments.save_base64("x.txt", "text/plain", "not-valid-base64!!!")
        with self.assertRaises(AttachmentError):
            self.attachments.save_base64("x.txt", "text/plain", "")
        huge = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
        with self.assertRaises(AttachmentError):
            self.attachments.save_base64("big.bin", "application/octet-stream", huge)

    def test_filename_is_display_metadata_only_never_a_storage_path(self):
        record = self.attachments.save_base64("../../etc/passwd", "text/plain", base64.b64encode(b"x").decode())
        self.assertEqual(record["filename"], "passwd")  # Path(...).name strips traversal
        self.assertNotIn("..", record["storage_rel_path"])

    def test_attach_to_message_requires_the_attachment_to_belong_to_the_conversation(self):
        record = self.attachments.save_base64("a.txt", "text/plain", base64.b64encode(b"x").decode(), conversation_id="conv-a")
        with self.assertRaises(AttachmentError):
            self.attachments.attach_to_message([record["id"]], "conv-b", "message-1")
        self.attachments.attach_to_message([record["id"]], "conv-a", "message-1")
        self.assertEqual(self.attachments.get(record["id"])["message_id"], "message-1")

    def test_attach_to_message_rejects_an_unknown_attachment_id(self):
        with self.assertRaises(AttachmentError):
            self.attachments.attach_to_message(["does-not-exist"], "conv-a", "message-1")


class NotificationStoreTests(unittest.TestCase):
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
        self.control, self.store = open_control_plane(self.repo)
        self.notifications = NotificationStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_notify_once_dedupes_on_kind_ref_type_ref_id(self):
        first = self.notifications.notify_once("TASK_FAILED", "Mission failed", "detail", "run", "run-1")
        second = self.notifications.notify_once("TASK_FAILED", "Mission failed again", "detail2", "run", "run-1")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # deduped, never spams
        self.assertEqual(len(self.notifications.list_notifications()), 1)

    def test_different_kind_or_ref_is_not_deduped(self):
        self.notifications.notify_once("TASK_FAILED", "a", "", "run", "run-1")
        self.notifications.notify_once("APPROVAL_REQUIRED", "b", "", "run", "run-1")
        self.notifications.notify_once("TASK_FAILED", "c", "", "run", "run-2")
        self.assertEqual(len(self.notifications.list_notifications()), 3)

    def test_unread_count_and_mark_read_and_mark_all_read(self):
        one = self.notifications.notify_once("RESEARCH_COMPLETE", "a", "", "research", "r1")
        self.notifications.notify_once("RESEARCH_COMPLETE", "b", "", "research", "r2")
        self.assertEqual(self.notifications.unread_count(), 2)
        self.notifications.mark_read(one)
        self.assertEqual(self.notifications.unread_count(), 1)
        marked = self.notifications.mark_all_read()
        self.assertEqual(marked, 1)
        self.assertEqual(self.notifications.unread_count(), 0)

    def test_mark_read_rejects_an_unknown_id(self):
        with self.assertRaises(ValueError):
            self.notifications.mark_read("does-not-exist")


class _LiveFalgunaServerCase(unittest.TestCase):
    """Same pattern as test_chat_web.py's _LiveFalgunaServerCase -- kept as
    a local copy so this file has no cross-test-file import."""

    port = 8799

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
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as resp:
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
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _wait_operation(self, token, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, op = self._get(f"/api/operations/{token}")
            if op.get("state") in {"COMPLETE", "FAILED", "CANCELLED"}:
                return op
            time.sleep(0.05)
        self.fail(f"operation {token} never reached a terminal state")


class ChatHttpV2Tests(_LiveFalgunaServerCase):
    port = 8799

    def test_config_lists_real_supported_models_and_work_modes(self):
        status, cfg = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("gpt-5.6-luna", cfg["available_models"])
        self.assertEqual(set(cfg["work_modes"]), set(WORK_MODE_SETTINGS))
        self.assertEqual(cfg["default_work_mode"], "BALANCED")
        self.assertIn("codex_runtime_found", cfg)

    def test_edit_and_resubmit_over_http(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "first draft"})
        self.assertEqual(status, 202)
        self._wait_operation(out["operation"])
        status, detail = self._get(f"/api/conversations/{conversation_id}")
        user_message_id = detail["messages"][0]["id"]

        status, out = self._post(f"/api/conversations/{conversation_id}/messages/{user_message_id}/edit", {"content": "revised draft"})
        self.assertEqual(status, 202)
        self._wait_operation(out["operation"])
        status, detail2 = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(len(detail2["messages"]), 2)  # old pair superseded, not accumulated
        self.assertEqual(detail2["messages"][0]["content"], "revised draft")

        status, err = self._post(f"/api/conversations/{conversation_id}/messages/does-not-exist/edit", {"content": "x"})
        self.assertEqual(status, 400)

    def test_regenerate_over_http_only_targets_the_latest_message(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "hello"})
        self._wait_operation(out["operation"])
        status, detail = self._get(f"/api/conversations/{conversation_id}")
        assistant_id = detail["messages"][1]["id"]

        status, out = self._post(f"/api/conversations/{conversation_id}/messages/{assistant_id}/regenerate", {})
        self.assertEqual(status, 202)
        self._wait_operation(out["operation"])
        status, detail2 = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(len(detail2["messages"]), 2)

        # Regenerating a message that is no longer the latest is rejected.
        status, err = self._post(f"/api/conversations/{conversation_id}/messages/{assistant_id}/regenerate", {})
        self.assertEqual(status, 400)

    def test_stop_generation_marks_the_message_cancelled_when_still_pending(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "hi"})
        pending_id = out["assistant"]["id"]
        status, cancelled = self._post(f"/api/conversations/{conversation_id}/messages/{pending_id}/stop", {})
        # Either it was still PENDING/GENERATING (200, CANCELLED) or the
        # no-codex environment already failed it a moment earlier (400) --
        # both are legitimate outcomes of the same real state machine.
        self.assertIn(status, (200, 400))
        if status == 200:
            self.assertEqual(cancelled["status"], "CANCELLED")

    def test_model_and_work_mode_overrides_round_trip_and_validate(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/model", {"model": "not-a-real-model"})
        self.assertEqual(status, 400)
        status, out = self._post(f"/api/conversations/{conversation_id}/model", {"model": "gpt-5.6-terra"})
        self.assertEqual(status, 200)
        self.assertEqual(out["model_override"], "gpt-5.6-terra")
        status, out = self._post(f"/api/conversations/{conversation_id}/work-mode", {"work_mode": "deep"})
        self.assertEqual(status, 200)
        self.assertEqual(out["work_mode"], "DEEP")
        # An unrecognized work mode falls back to the documented default rather than erroring.
        status, out = self._post(f"/api/conversations/{conversation_id}/work-mode", {"work_mode": "ludicrous"})
        self.assertEqual(status, 200)
        self.assertEqual(out["work_mode"], "BALANCED")

    def test_archive_and_delete_over_http(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        status, out = self._post(f"/api/conversations/{conversation_id}/archive", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "ARCHIVED")
        status, listing = self._get("/api/conversations")
        self.assertNotIn(conversation_id, [c["id"] for c in listing["conversations"]])
        status, out = self._post(f"/api/conversations/{conversation_id}/delete", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "DELETED")


class AttachmentHttpTests(_LiveFalgunaServerCase):
    port = 8800

    def test_upload_download_and_associate_with_a_message(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        conversation_id = conv["id"]
        payload = base64.b64encode(b"attachment bytes here").decode()
        status, att = self._post(f"/api/conversations/{conversation_id}/attachments", {"filename": "notes.txt", "content_type": "text/plain", "data_base64": payload})
        self.assertEqual(status, 201)

        status, out = self._post(f"/api/conversations/{conversation_id}/messages", {"content": "see attached", "attachment_ids": [att["id"]]})
        self.assertEqual(status, 202)
        self._wait_operation(out["operation"])

        status, detail = self._get(f"/api/conversations/{conversation_id}")
        self.assertEqual(len(detail["attachments"]), 1)
        self.assertEqual(detail["attachments"][0]["message_id"], out["message"]["id"])

        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/attachments/{att['id']}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.read(), b"attachment bytes here")

        self.assertEqual(self._get_status(f"/api/attachments/does-not-exist"), 404)

    def test_upload_rejects_an_oversized_attachment_with_400(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        huge = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
        status, err = self._post(f"/api/conversations/{conv['id']}/attachments", {"filename": "big.bin", "content_type": "application/octet-stream", "data_base64": huge})
        self.assertEqual(status, 400)

    def test_message_rejects_an_attachment_id_from_a_different_conversation(self):
        status, conv_a = self._post("/api/conversations", {"title": "A"})
        status, conv_b = self._post("/api/conversations", {"title": "B"})
        payload = base64.b64encode(b"x").decode()
        status, att = self._post(f"/api/conversations/{conv_a['id']}/attachments", {"filename": "a.txt", "content_type": "text/plain", "data_base64": payload})
        status, err = self._post(f"/api/conversations/{conv_b['id']}/messages", {"content": "hi", "attachment_ids": [att["id"]]})
        self.assertEqual(status, 400)


class AggregateEndpointsHttpTests(_LiveFalgunaServerCase):
    port = 8801

    def test_missions_board_buckets_are_present_and_empty_on_a_fresh_repo(self):
        status, board = self._get("/api/missions/board")
        self.assertEqual(status, 200)
        # Falguna V2.1: "archived" is a real bucket too now (Mission Control
        # count cleanup) -- a run explicitly dismissed from the active board,
        # never deleted. See _archive_run_from_board in web.py.
        self.assertEqual(set(board.keys()), {"running", "needs_you", "completed", "failed", "cancelled", "archived"})
        self.assertEqual(sum(len(v) for v in board.values()), 0)

    def test_live_summary_reports_zero_counts_on_a_fresh_repo(self):
        status, live = self._get("/api/live-summary")
        self.assertEqual(status, 200)
        self.assertEqual(live, {"running": 0, "needs_you": 0, "failed": 0, "unread_notifications": 0})

    def test_usage_reports_real_zero_cost_with_an_honest_subscription_note(self):
        status, usage = self._get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(usage["totals"]["chat"]["calls"], 0)
        self.assertIn("subscription", usage["note"].lower())

    def test_usage_reflects_a_real_recorded_chat_call_cost_and_tokens(self):
        # No codex executable in this environment, so drive the aggregation
        # through the real ConversationStore path instead of over HTTP.
        chat = ConversationStore(self.store)
        conversation_id = chat.create_conversation("Chat")
        pending = chat.add_pending_message(conversation_id)
        chat.complete_message(pending["id"], "hi", {"cost_usd": 0.0, "input_tokens": 42, "output_tokens": 7}, None)
        status, usage = self._get("/api/usage")
        self.assertEqual(usage["totals"]["chat"]["calls"], 1)
        self.assertEqual(usage["totals"]["chat"]["input_tokens"], 42)
        self.assertEqual(usage["totals"]["chat"]["output_tokens"], 7)

    def test_files_lists_uploaded_attachments(self):
        status, conv = self._post("/api/conversations", {"title": "Chat"})
        payload = base64.b64encode(b"file contents").decode()
        self._post(f"/api/conversations/{conv['id']}/attachments", {"filename": "doc.txt", "content_type": "text/plain", "data_base64": payload})
        status, files = self._get("/api/files")
        self.assertEqual(status, 200)
        self.assertEqual(len(files["uploads"]), 1)
        self.assertEqual(files["uploads"][0]["filename"], "doc.txt")
        self.assertEqual(files["generated"], [])

    def test_notifications_endpoints_over_http(self):
        status, out = self._get("/api/notifications")
        self.assertEqual(status, 200)
        self.assertEqual(out["notifications"], [])
        status, out = self._post("/api/notifications/read-all", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["marked"], 0)
        self.assertEqual(self._get_status("/api/notifications/does-not-exist"), 404)


class MissionControlArchiveTests(_LiveFalgunaServerCase):
    """Falguna V2.1: Mission Control count cleanup. Archiving is a pure
    board-visibility toggle -- these tests prove it never deletes or
    mutates the underlying run, never lets an actively-running mission be
    hidden from view, and correctly moves cards in/out of the headline
    counts (the "0 running / 27 needs you / 55 failed" confusion this
    feature exists to fix)."""

    port = 8805

    def _create_run(self, status="FAILED"):
        """Boots a real run through the real control plane (mirroring
        test_chat_web.py's handoff test), then force-overrides its status
        directly in the store so each test can target a specific bucket
        without needing a real worker failure/success to occur."""
        policy = RunPolicy()
        ids = self.control.create_mission("fix", "set value to 2", self.repo, policy)
        run_id = self.control.start(
            ids["task_id"], ScriptedWorker(lambda *_: WorkerResult(True, "ok", 0)),
            "scripted", "none", policy, force_stop_after="WORKTREE_READY",
        )
        if status is not None:
            self.store.update("runs", run_id, status=status)
        return run_id

    def test_archiving_a_failed_run_moves_it_out_of_failed_and_into_archived(self):
        run_id = self._create_run(status="FAILED")
        status, board = self._get("/api/missions/board")
        self.assertEqual({c["run_id"] for c in board["failed"]}, {run_id})
        self.assertEqual(board["archived"], [])

        status, out = self._post(f"/api/runs/{run_id}/board-archive", {})
        self.assertEqual(status, 200)
        self.assertEqual(out, {"run_id": run_id, "archived": True})

        status, board = self._get("/api/missions/board")
        self.assertEqual(board["failed"], [])
        self.assertEqual({c["run_id"] for c in board["archived"]}, {run_id})
        self.assertTrue(board["archived"][0]["archived"])

    def test_unarchiving_restores_a_run_to_its_real_status_bucket(self):
        run_id = self._create_run(status="FAILED")
        self._post(f"/api/runs/{run_id}/board-archive", {})
        status, out = self._post(f"/api/runs/{run_id}/board-unarchive", {})
        self.assertEqual(status, 200)
        self.assertEqual(out, {"run_id": run_id, "archived": False})

        status, board = self._get("/api/missions/board")
        self.assertEqual({c["run_id"] for c in board["failed"]}, {run_id})
        self.assertEqual(board["archived"], [])

    def test_archiving_never_deletes_or_mutates_the_underlying_run(self):
        run_id = self._create_run(status="CANCELLED")
        status, before = self._get(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)

        status, _ = self._post(f"/api/runs/{run_id}/board-archive", {})
        self.assertEqual(status, 200)

        # Still fully reachable by id, with the same real status -- archiving
        # touched only the board-visibility flag, nothing else.
        status, after = self._get(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["status"], "CANCELLED")

    def test_an_actively_running_mission_cannot_be_archived(self):
        run_id = self._create_run(status="WORKING")
        status, out = self._post(f"/api/runs/{run_id}/board-archive", {})
        self.assertEqual(status, 400)
        self.assertIn("actively running", out["error"].lower())

        # And it must stay fully visible in the running bucket, never
        # silently dropped from the board.
        status, board = self._get("/api/missions/board")
        self.assertEqual({c["run_id"] for c in board["running"]}, {run_id})
        self.assertEqual(board["archived"], [])

    def test_live_summary_excludes_archived_runs_from_its_counts(self):
        needs_you_run = self._create_run(status="PAUSED")
        failed_run = self._create_run(status="FAILED")

        status, live = self._get("/api/live-summary")
        self.assertEqual(live["needs_you"], 1)
        self.assertEqual(live["failed"], 1)

        self._post(f"/api/runs/{needs_you_run}/board-archive", {})
        self._post(f"/api/runs/{failed_run}/board-archive", {})

        status, live = self._get("/api/live-summary")
        self.assertEqual(live["needs_you"], 0)
        self.assertEqual(live["failed"], 0)


if __name__ == "__main__":
    unittest.main()
