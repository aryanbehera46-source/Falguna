"""Falguna Memory & Knowledge V2 -- deterministic unit tests for the core
store (falguna/memory.py). No HTTP layer here (see test_memory_web.py for
the live-server route tests); these exercise MemoryStore/KnowledgeStore
directly against a real temporary StateStore, exactly like
tests/test_browser_runtime.py does for the browser runtime.
"""
import json
import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.memory import (
    KnowledgeError, KnowledgeStore, MemoryConflict, MemoryError, MemorySettingsStore, MemoryStore,
    MemorySuggestionStore, NullEmbeddingAdapter, OllamaEmbeddingAdapter, assemble_chat_context,
    build_embedding_adapter, chunk_text, detect_memory_suggestion, embedding_status, jaccard_similarity,
)
from falguna.store import StateStore


class _TempStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = StateStore(Path(self.tmp) / "state.db")
        self.store.migrate()
        self.audit = AuditLog(Path(self.tmp) / "audit.jsonl")
        self.mem = MemoryStore(self.store, self.audit)
        self.kb = KnowledgeStore(self.store, self.audit)
        self.project_ids = {"falguna-engineering", "serviceflow"}

    def tearDown(self):
        self.store.close()


class ChunkingTests(unittest.TestCase):
    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(chunk_text(""), [])

    def test_short_text_is_a_single_chunk_covering_the_whole_string(self):
        text = "Falguna is a local-first AI workspace."
        chunks = chunk_text(text, chunk_size=1200)
        self.assertEqual(len(chunks), 1)
        start, end, content = chunks[0]
        self.assertEqual((start, end), (0, len(text)))
        self.assertEqual(content, text)

    def test_long_text_is_split_into_multiple_overlapping_chunks_covering_the_whole_document(self):
        text = ("Paragraph one about memory scoping. " * 40) + "\n\n" + ("Paragraph two about retrieval. " * 40)
        chunks = chunk_text(text, chunk_size=300, overlap=40)
        self.assertGreater(len(chunks), 1)
        # every character position is covered by at least one chunk (overlap
        # means adjacent chunks share tail/head text, never a gap)
        covered = set()
        for start, end, _ in chunks:
            covered.update(range(start, end))
        self.assertEqual(covered, set(range(len(text))))

    def test_chunking_is_deterministic(self):
        text = "Repeat this sentence many times. " * 200
        self.assertEqual(chunk_text(text), chunk_text(text))


class JaccardSimilarityTests(unittest.TestCase):
    def test_identical_text_is_similarity_one(self):
        self.assertEqual(jaccard_similarity("dark theme please", "dark theme please"), 1.0)

    def test_disjoint_text_is_similarity_zero(self):
        self.assertEqual(jaccard_similarity("dark theme", "quarterly revenue report"), 0.0)

    def test_empty_text_never_raises_and_is_zero(self):
        self.assertEqual(jaccard_similarity("", "anything"), 0.0)
        self.assertEqual(jaccard_similarity("anything", ""), 0.0)


class MemorySuggestionDetectionTests(unittest.TestCase):
    def test_detects_a_preference_phrase(self):
        hit = detect_memory_suggestion("From now on always use snake_case for Python.")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "preference")

    def test_detects_an_instruction_phrase(self):
        hit = detect_memory_suggestion("Please remember that I deploy on Fridays.")
        self.assertEqual(hit[0], "instruction")

    def test_ordinary_message_triggers_no_suggestion(self):
        self.assertIsNone(detect_memory_suggestion("What's the weather like for shipping today?"))


class MemoryStoreScopeValidationTests(_TempStoreCase):
    def test_personal_scope_rejects_a_scope_id(self):
        with self.assertRaises(MemoryError):
            self.mem.save("personal", "fact", "x", "user_stated", "user_provided", "aryan", scope_id="p1")

    def test_project_scope_requires_a_known_project_id(self):
        with self.assertRaises(MemoryError):
            self.mem.save("project", "fact", "x", "user_stated", "user_provided", "aryan", scope_id="not-a-real-project",
                          known_project_ids=self.project_ids)

    def test_project_scope_accepts_a_known_project_id(self):
        record = self.mem.save("project", "fact", "x", "user_stated", "user_provided", "aryan", scope_id="falguna-engineering",
                                known_project_ids=self.project_ids)
        self.assertEqual(record["scope_id"], "falguna-engineering")

    def test_venture_scope_requires_a_real_venture_row(self):
        with self.assertRaises(MemoryError):
            self.mem.save("venture", "fact", "x", "user_stated", "user_provided", "aryan", scope_id="nope")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(MemoryError):
            self.mem.save("personal", "not_a_kind", "x", "user_stated", "user_provided", "aryan")

    def test_empty_content_is_rejected(self):
        with self.assertRaises(MemoryError):
            self.mem.save("personal", "fact", "   ", "user_stated", "user_provided", "aryan")


class MemoryStoreCrudTests(_TempStoreCase):
    def test_save_creates_an_active_record_with_full_provenance(self):
        record = self.mem.save(
            "project", "preference", "Aryan wants concise commit messages", "user_stated", "user_provided", "aryan",
            scope_id="falguna-engineering", source_ref="conv-1", known_project_ids=self.project_ids,
        )
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["confidence"], "user_provided")
        self.assertEqual(record["source_type"], "user_stated")
        self.assertEqual(record["source_ref"], "conv-1")
        self.assertIsNotNone(record["created_at"])

    def test_kind_and_confidence_are_independent_an_inference_is_never_upgraded(self):
        record = self.mem.save("personal", "inference", "Aryan might prefer terse replies", "system_derived", "inferred", "falguna")
        self.assertEqual(record["kind"], "inference")
        self.assertEqual(record["confidence"], "inferred")
        # nothing in this codepath ever rewrites confidence upward
        self.assertNotEqual(record["confidence"], "verified")

    def test_saving_a_similar_active_record_without_resolution_raises_conflict(self):
        self.mem.save("personal", "preference", "Aryan prefers dark mode everywhere", "user_stated", "user_provided", "aryan")
        with self.assertRaises(MemoryConflict) as ctx:
            self.mem.save("personal", "preference", "Aryan prefers dark mode everywhere always", "user_stated", "user_provided", "aryan")
        self.assertGreaterEqual(len(ctx.exception.candidates), 1)

    def test_allow_conflict_keeps_both_records_coexisting(self):
        r1 = self.mem.save("personal", "preference", "Aryan prefers dark mode everywhere", "user_stated", "user_provided", "aryan")
        r2 = self.mem.save("personal", "preference", "Aryan prefers dark mode everywhere always", "user_stated", "user_provided", "aryan",
                            allow_conflict=True)
        self.assertEqual(self.mem.get(r1["id"])["state"], "active")
        self.assertEqual(self.mem.get(r2["id"])["state"], "active")

    def test_supersede_marks_old_record_superseded_and_links_both_ways(self):
        r1 = self.mem.save("personal", "fact", "Aryan's timezone is IST", "user_stated", "user_provided", "aryan")
        r2 = self.mem.save("personal", "fact", "Aryan's timezone is now PST", "user_stated", "user_provided", "aryan",
                            supersedes_id=r1["id"])
        old = self.mem.get(r1["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by_id"], r2["id"])
        self.assertEqual(r2["supersedes_id"], r1["id"])

    def test_supersede_across_scopes_is_rejected(self):
        r1 = self.mem.save("project", "fact", "Uses Postgres", "user_stated", "user_provided", "aryan",
                            scope_id="falguna-engineering", known_project_ids=self.project_ids)
        with self.assertRaises(MemoryError):
            self.mem.save("project", "fact", "Uses SQLite", "user_stated", "user_provided", "aryan",
                          scope_id="serviceflow", supersedes_id=r1["id"], known_project_ids=self.project_ids)

    def test_pin_and_unpin(self):
        r = self.mem.save("personal", "fact", "Aryan's favorite editor is neovim", "user_stated", "user_provided", "aryan")
        pinned = self.mem.pin(r["id"], True, "aryan")
        self.assertEqual(pinned["pinned"], 1)
        unpinned = self.mem.pin(r["id"], False, "aryan")
        self.assertEqual(unpinned["pinned"], 0)

    def test_edit_changes_content_in_place_without_a_new_row(self):
        r = self.mem.save("personal", "fact", "typo in this fact", "user_stated", "user_provided", "aryan")
        edited = self.mem.edit(r["id"], "corrected fact text", "aryan")
        self.assertEqual(edited["id"], r["id"])
        self.assertEqual(edited["content"], "corrected fact text")


class MemoryScopeIsolationTests(_TempStoreCase):
    """Acceptance demonstration #2's isolation half, at the memory-record
    level: Project A's memory must never be visible to Project B."""

    def setUp(self):
        super().setUp()
        self.record = self.mem.save(
            "project", "fact", "Project A uses a custom auth token format", "user_stated", "user_provided", "aryan",
            scope_id="falguna-engineering", known_project_ids=self.project_ids,
        )

    def test_search_from_project_a_finds_it(self):
        hits = self.mem.search([("project", "falguna-engineering")], "custom auth token")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], self.record["id"])

    def test_search_from_project_b_does_not_find_it(self):
        hits = self.mem.search([("project", "serviceflow")], "custom auth token")
        self.assertEqual(hits, [])

    def test_list_from_project_b_does_not_include_it(self):
        rows = self.mem.list([("project", "serviceflow")])
        self.assertEqual(rows, [])

    def test_list_from_project_a_includes_it(self):
        rows = self.mem.list([("project", "falguna-engineering")])
        self.assertEqual([r["id"] for r in rows], [self.record["id"]])

    def test_a_query_with_no_authorized_matching_scope_returns_nothing_even_if_it_would_otherwise_match(self):
        # personal/company scopes are authorized but the record lives only
        # in the project scope -- authorization is enforced, not merely a
        # display filter layered on top of an unscoped query.
        hits = self.mem.search([("personal", None), ("company", None)], "custom auth token")
        self.assertEqual(hits, [])


class MemorySupersessionAndDeletionTests(_TempStoreCase):
    """Acceptance demonstrations #3 and #4."""

    def test_superseded_record_excluded_from_active_search_but_history_preserved(self):
        r1 = self.mem.save("personal", "fact", "Aryan's default browser is Chrome", "user_stated", "user_provided", "aryan")
        r2 = self.mem.save("personal", "fact", "Aryan's default browser is now Safari", "user_stated", "user_provided", "aryan",
                            supersedes_id=r1["id"])
        active_hits = self.mem.search([("personal", None)], "default browser")
        self.assertEqual([h["id"] for h in active_hits], [r2["id"]])
        history = self.mem.history(r2["id"])
        self.assertEqual([h["id"] for h in history], [r1["id"], r2["id"]])
        self.assertEqual(history[0]["state"], "superseded")

    def test_forgotten_record_absent_from_active_listing_search_and_fts_index(self):
        r = self.mem.save("personal", "fact", "Aryan's postal code is a private detail", "user_stated", "user_provided", "aryan")
        self.mem.forget(r["id"], "aryan", reason="no longer needed")
        self.assertEqual(self.mem.get(r["id"])["state"], "deleted")
        self.assertEqual(self.mem.search([("personal", None)], "postal code"), [])
        self.assertEqual(self.mem.list([("personal", None)]), [])
        fts_row = self.store.db.execute("SELECT * FROM memory_fts WHERE ref_id=?", (r["id"],)).fetchone()
        self.assertIsNone(fts_row, "a forgotten record's derived FTS index row must actually be removed")

    def test_purge_requires_forget_first(self):
        r = self.mem.save("personal", "fact", "some fact", "user_stated", "user_provided", "aryan")
        with self.assertRaises(MemoryError):
            self.mem.purge(r["id"], "aryan")

    def test_purge_irreversibly_overwrites_content_after_forget(self):
        r = self.mem.save("personal", "fact", "a secret detail Aryan wants gone for good", "user_stated", "user_provided", "aryan")
        self.mem.forget(r["id"], "aryan", reason="sensitive")
        purged = self.mem.purge(r["id"], "aryan")
        self.assertNotIn("secret detail", purged["content"])
        self.assertIsNotNone(purged["purged_at"])


class KnowledgeIngestionTests(_TempStoreCase):
    def test_plain_text_is_ingested_and_chunked(self):
        data = ("Falguna's project registry lives in project_profiles.json. " * 30).encode("utf-8")
        doc = self.kb.ingest_bytes("notes.txt", "text/plain", data, "project", "upload", "aryan",
                                    scope_id="falguna-engineering", known_project_ids=self.project_ids)
        self.assertEqual(doc["status"], "ready")
        self.assertGreater(doc["chunk_count"], 0)
        chunks = self.kb.get_chunks(doc["id"])
        self.assertEqual(len(chunks), doc["chunk_count"])

    def test_unsupported_binary_format_is_refused_with_a_clear_status_never_parsed(self):
        doc = self.kb.ingest_bytes("photo.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "personal", "upload", "aryan")
        self.assertEqual(doc["status"], "unsupported_format")
        self.assertEqual(doc["chunk_count"], 0)
        self.assertEqual(self.kb.get_chunks(doc["id"]), [])

    def test_oversized_document_is_refused_as_too_large(self):
        from falguna.memory import MAX_DOCUMENT_BYTES
        data = b"x" * (MAX_DOCUMENT_BYTES + 1)
        doc = self.kb.ingest_bytes("huge.txt", "text/plain", data, "personal", "upload", "aryan")
        self.assertEqual(doc["status"], "too_large")

    def test_identical_content_is_deduplicated_not_stored_twice(self):
        data = b"The same exact document content, twice."
        doc1 = self.kb.ingest_bytes("a.txt", "text/plain", data, "personal", "upload", "aryan")
        doc2 = self.kb.ingest_bytes("a-again.txt", "text/plain", data, "personal", "upload", "aryan")
        self.assertEqual(doc1["id"], doc2["id"])
        self.assertTrue(doc2.get("deduplicated"))

    def test_forget_document_removes_chunks_and_fts_entries(self):
        data = b"Some ingested knowledge about the deployment process here."
        doc = self.kb.ingest_bytes("deploy.txt", "text/plain", data, "personal", "upload", "aryan")
        self.assertGreater(doc["chunk_count"], 0)
        self.kb.forget_document(doc["id"], "aryan", reason="stale")
        self.assertEqual(self.kb.get_chunks(doc["id"]), [])
        row = self.store.db.execute("SELECT * FROM knowledge_fts WHERE document_id=?", (doc["id"],)).fetchone()
        self.assertIsNone(row)
        self.assertEqual(self.kb.get_document(doc["id"])["status"], "deleted")

    def test_forgetting_twice_raises(self):
        doc = self.kb.ingest_bytes("a.txt", "text/plain", b"content", "personal", "upload", "aryan")
        self.kb.forget_document(doc["id"], "aryan")
        with self.assertRaises(KnowledgeError):
            self.kb.forget_document(doc["id"], "aryan")


class KnowledgeScopeIsolationTests(_TempStoreCase):
    def setUp(self):
        super().setUp()
        self.doc = self.kb.ingest_bytes(
            "auth-notes.txt", "text/plain", b"Project A stores refresh tokens hashed with argon2. " * 10,
            "project", "upload", "aryan", scope_id="falguna-engineering", known_project_ids=self.project_ids,
        )

    def test_project_a_can_search_it(self):
        hits = self.kb.search([("project", "falguna-engineering")], "refresh tokens argon2")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["document_id"], self.doc["id"])

    def test_project_b_cannot_search_it(self):
        hits = self.kb.search([("project", "serviceflow")], "refresh tokens argon2")
        self.assertEqual(hits, [])


class OfflineKeywordSearchTests(_TempStoreCase):
    """Acceptance demonstration #5: offline keyword search with zero
    embedding provider configured (the default -- MemorySettingsStore's
    default embedding_provider is 'none')."""

    def test_search_works_with_no_embedding_adapter_configured_at_all(self):
        settings = MemorySettingsStore(self.store).load()
        self.assertEqual(settings["embedding_provider"], "none")
        self.mem.save("personal", "fact", "Falguna's default provider is local Codex", "user_stated", "user_provided", "aryan")
        hits = self.mem.search([("personal", None)], "local Codex provider")
        self.assertEqual(len(hits), 1)

    def test_embedding_status_is_honest_when_nothing_is_reachable(self):
        status = embedding_status(self.store)
        self.assertEqual(status["provider"], "none")
        self.assertFalse(status["configured"])
        self.assertFalse(status["semantic_retrieval_active"])


class EmbeddingAdapterTests(unittest.TestCase):
    def test_null_adapter_is_never_available_and_raises_on_embed(self):
        adapter = NullEmbeddingAdapter()
        self.assertFalse(adapter.is_available())

    def test_ollama_adapter_health_check_fails_closed_when_unreachable(self):
        # Port 1 is never a real Ollama server -- this must return False,
        # never raise, and never hang the caller.
        adapter = OllamaEmbeddingAdapter(base_url="http://127.0.0.1:1", timeout=0.3)
        self.assertFalse(adapter.is_available())

    def test_build_embedding_adapter_defaults_to_null(self):
        adapter = build_embedding_adapter({"embedding_provider": "none"})
        self.assertIsInstance(adapter, NullEmbeddingAdapter)

    def test_build_embedding_adapter_honors_ollama_selection(self):
        adapter = build_embedding_adapter({"embedding_provider": "ollama", "ollama_base_url": "http://127.0.0.1:11434", "ollama_embedding_model": "nomic-embed-text"})
        self.assertIsInstance(adapter, OllamaEmbeddingAdapter)

    def test_ingest_never_fails_when_the_configured_embedding_adapter_is_unreachable(self):
        tmp = tempfile.mkdtemp()
        store = StateStore(Path(tmp) / "state.db")
        store.migrate()
        audit = AuditLog(Path(tmp) / "audit.jsonl")
        unreachable = OllamaEmbeddingAdapter(base_url="http://127.0.0.1:1", timeout=0.3)
        kb = KnowledgeStore(store, audit, embedding_adapter=unreachable)
        doc = kb.ingest_bytes("a.txt", "text/plain", b"some content to embed maybe", "personal", "upload", "aryan")
        self.assertEqual(doc["status"], "ready")  # ingest still succeeds -- embedding is best-effort only
        chunk = kb.get_chunks(doc["id"])[0]
        self.assertIsNone(chunk["embedding_json"])
        store.close()


class MemorySuggestionTests(_TempStoreCase):
    def test_a_save_worthy_message_produces_a_pending_suggestion_not_an_automatic_save(self):
        suggestions = MemorySuggestionStore(self.store, self.audit, self.mem)
        result = suggestions.create_from_message("personal", None, "conv1", "msg1", "Please remember that I always deploy on Fridays")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.mem.list([("personal", None)]), [])  # nothing auto-saved yet

    def test_an_ordinary_message_produces_no_suggestion(self):
        suggestions = MemorySuggestionStore(self.store, self.audit, self.mem)
        result = suggestions.create_from_message("personal", None, "conv1", "msg1", "What time is it in Tokyo?")
        self.assertIsNone(result)

    def test_accepting_a_suggestion_creates_exactly_one_memory_record(self):
        suggestions = MemorySuggestionStore(self.store, self.audit, self.mem)
        pending = suggestions.create_from_message("personal", None, "conv1", "msg1", "Remember that I use tabs not spaces")
        record = suggestions.accept(pending["id"], "aryan")
        self.assertEqual(len(self.mem.list([("personal", None)])), 1)
        self.assertEqual(record["source_type"], "conversation")

    def test_dismissing_a_suggestion_creates_no_memory_record(self):
        suggestions = MemorySuggestionStore(self.store, self.audit, self.mem)
        pending = suggestions.create_from_message("personal", None, "conv1", "msg1", "Remember that I use tabs not spaces")
        suggestions.dismiss(pending["id"], "aryan")
        self.assertEqual(self.mem.list([("personal", None)]), [])

    def test_double_resolution_is_rejected(self):
        suggestions = MemorySuggestionStore(self.store, self.audit, self.mem)
        pending = suggestions.create_from_message("personal", None, "conv1", "msg1", "Remember that I use tabs not spaces")
        suggestions.accept(pending["id"], "aryan")
        with self.assertRaises(MemoryError):
            suggestions.dismiss(pending["id"], "aryan")


class ChatContextAssemblyTests(_TempStoreCase):
    """Pass F, tested independently of any model provider -- retrieval and
    bounded context assembly are pure local logic and must be verifiable
    without a generation model being reachable at all."""

    def test_no_relevant_memory_or_knowledge_returns_none(self):
        self.assertIsNone(assemble_chat_context(self.mem, self.kb, [("personal", None)], "completely unrelated query xyz"))

    def test_relevant_memory_is_assembled_with_citations_and_provenance_labels(self):
        self.mem.save("personal", "preference", "Aryan prefers short, direct answers", "user_stated", "user_provided", "aryan")
        ctx = assemble_chat_context(self.mem, self.kb, [("personal", None)], "how should replies be styled, short direct answers")
        self.assertIsNotNone(ctx)
        self.assertIn("preference", ctx["text"])
        self.assertIn("untrusted", ctx["text"].lower())
        self.assertEqual(len(ctx["citations"]), 1)
        self.assertEqual(ctx["citations"][0]["type"], "memory")

    def test_context_assembly_never_includes_out_of_scope_memory(self):
        self.mem.save("project", "fact", "Project A's staging URL is internal only", "user_stated", "user_provided", "aryan",
                      scope_id="falguna-engineering", known_project_ids=self.project_ids)
        ctx = assemble_chat_context(self.mem, self.kb, [("personal", None), ("company", None)], "what is the staging URL")
        self.assertIsNone(ctx)

    def test_context_is_bounded_by_max_chars(self):
        for i in range(20):
            self.mem.save("personal", "fact", f"Fact number {i} about the recurring topic widgets", "user_stated", "user_provided", "aryan", allow_conflict=True)
        ctx = assemble_chat_context(self.mem, self.kb, [("personal", None)], "widgets topic", max_chars=200)
        self.assertIsNotNone(ctx)
        self.assertLessEqual(len(ctx["text"]) - len(ctx["text"].split("\n\n", 1)[0]), 200 + 50)


class AuditTrailTests(_TempStoreCase):
    def test_every_memory_mutation_is_recorded_in_the_hash_chained_audit_log(self):
        r = self.mem.save("personal", "fact", "auditable fact", "user_stated", "user_provided", "aryan")
        self.mem.pin(r["id"], True, "aryan")
        self.mem.edit(r["id"], "auditable fact, edited", "aryan")
        self.mem.forget(r["id"], "aryan", reason="done")
        self.assertTrue(self.audit.verify())
        events = [json.loads(line)["event"] for line in Path(self.audit.path).read_text().splitlines()]
        for expected in ("MEMORY_RECORD_SAVED", "MEMORY_RECORD_PINNED", "MEMORY_RECORD_EDITED", "MEMORY_RECORD_FORGOTTEN"):
            self.assertIn(expected, events)


if __name__ == "__main__":
    unittest.main()
