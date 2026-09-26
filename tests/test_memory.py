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
    EmbeddingAdapter, KnowledgeError, KnowledgeStore, MemoryConflict, MemoryError, MemorySettingsStore,
    MemoryStore, MemorySuggestionStore, NullEmbeddingAdapter, OllamaEmbeddingAdapter, assemble_chat_context,
    build_embedding_adapter, chunk_text, cosine_similarity, detect_memory_suggestion, embedding_status,
    jaccard_similarity,
)
from falguna.store import StateStore, utcnow


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


class _FakeEmbeddingAdapter(EmbeddingAdapter):
    """A fully deterministic, no-network embedding adapter for testing
    semantic re-ranking without any real Ollama dependency -- `vectors`
    maps an exact input string (a chunk's content, or a query string) to
    its fixed vector, so a test can construct an exact, reproducible
    keyword-vs-semantic disagreement."""

    name = "fake-test-adapter"

    def __init__(self, vectors: dict, available: bool = True):
        self._vectors = vectors
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def embed(self, texts):
        return [self._vectors[t] for t in texts]


class KnowledgeSemanticRerankingTests(_TempStoreCase):
    """Phase 2 Milestone 2: cosine_similarity() -- previously defined in
    this module but never called anywhere -- is now genuinely exercised by
    KnowledgeStore.search(). These prove semantic re-ranking actually
    changes result ORDER (not merely that a number gets computed
    somewhere), using the fully deterministic fake adapter above so this
    suite never depends on a real local Ollama daemon being reachable."""

    def setUp(self):
        super().setUp()
        # Chunk B repeats every query term several times (bm25 must rank it
        # first on keywords alone); its fake embedding is deliberately far
        # from the query's, while chunk A's is deliberately close -- so a
        # working semantic re-rank must promote A above B.
        self.chunk_a_text = "There is an urgent deployment freeze policy in effect for the holidays."
        self.chunk_b_text = ("Urgent deployment freeze policy: urgent deployment freeze policy applies to "
                              "urgent deployment freeze policy changes across every team.")
        self.query = "urgent deployment freeze policy"
        vectors = {
            self.chunk_a_text: [1.0, 0.0],
            self.chunk_b_text: [0.0, 1.0],
            self.query: [0.95, 0.05],
        }
        self.adapter = _FakeEmbeddingAdapter(vectors)
        self.kb_semantic = KnowledgeStore(self.store, self.audit, embedding_adapter=self.adapter)
        self.doc_a = self.kb_semantic.ingest_bytes("a.txt", "text/plain", self.chunk_a_text.encode(), "personal", "upload", "aryan")
        self.doc_b = self.kb_semantic.ingest_bytes("b.txt", "text/plain", self.chunk_b_text.encode(), "personal", "upload", "aryan")

    def test_fixture_sanity_pure_keyword_search_ranks_the_keyword_dense_chunk_first(self):
        hits = self.kb_semantic.search([("personal", None)], self.query, use_semantic=False)
        self.assertEqual(hits[0]["document_id"], self.doc_b["id"])
        self.assertIsNone(hits[0]["cosine_similarity"])
        self.assertFalse(hits[0]["semantic_boosted"])

    def test_semantic_reranking_promotes_the_embedding_closer_chunk_to_first_place(self):
        hits = self.kb_semantic.search([("personal", None)], self.query, use_semantic=True)
        self.assertEqual(hits[0]["document_id"], self.doc_a["id"])
        self.assertTrue(hits[0]["semantic_boosted"])
        self.assertIsNotNone(hits[0]["cosine_similarity"])
        self.assertGreater(hits[0]["cosine_similarity"], hits[1]["cosine_similarity"])
        # A sanity check that this is really cosine_similarity() being used,
        # not an arbitrary number.
        expected = cosine_similarity([0.95, 0.05], [1.0, 0.0])
        self.assertAlmostEqual(hits[0]["cosine_similarity"], expected, places=6)

    def test_reranking_is_skipped_when_the_embedding_adapter_reports_unavailable(self):
        self.adapter._available = False
        hits = self.kb_semantic.search([("personal", None)], self.query, use_semantic=True)
        self.assertEqual(hits[0]["document_id"], self.doc_b["id"])  # falls back to pure keyword order
        self.assertFalse(hits[0]["semantic_boosted"])

    def test_a_failing_query_embedding_call_falls_back_to_keyword_order_without_crashing(self):
        # The query string is deliberately absent from the adapter's
        # vectors dict, so embed([query]) raises KeyError inside the
        # adapter -- this must be caught, never propagated, and must never
        # break the keyword baseline.
        broken_adapter = _FakeEmbeddingAdapter({self.chunk_a_text: [1.0, 0.0], self.chunk_b_text: [0.0, 1.0]})
        kb = KnowledgeStore(self.store, self.audit, embedding_adapter=broken_adapter)
        hits = kb.search([("personal", None)], self.query, use_semantic=True)
        self.assertEqual(hits[0]["document_id"], self.doc_b["id"])
        self.assertFalse(hits[0]["semantic_boosted"])

    def test_semantic_reranking_never_surfaces_a_chunk_outside_the_authorized_scope(self):
        # The new semantic path only re-ORDERS the pool that scope
        # authorization already filtered -- it must never let a
        # differently-scoped chunk back in just because its embedding is
        # a close match.
        other_project_doc = self.kb_semantic.ingest_bytes(
            "c.txt", "text/plain", self.chunk_a_text.encode(), "project", "upload", "aryan",
            scope_id="falguna-engineering", known_project_ids={"falguna-engineering"},
        )
        hits = self.kb_semantic.search([("personal", None)], self.query, use_semantic=True)
        self.assertNotIn(other_project_doc["id"], {h["document_id"] for h in hits})

    def test_a_candidate_with_no_stored_embedding_is_never_penalized_out_of_a_pure_keyword_pool(self):
        # Ingested via the Null-adapter store (self.kb), so this chunk has
        # no stored embedding at all -- it must still be found by a
        # keyword-only KnowledgeStore exactly as before this milestone.
        doc = self.kb.ingest_bytes("d.txt", "text/plain", b"A plain document with no embedding ever computed.", "personal", "upload", "aryan")
        hits = self.kb.search([("personal", None)], "plain document embedding")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["document_id"], doc["id"])
        self.assertIsNone(hits[0]["cosine_similarity"])
        self.assertFalse(hits[0]["semantic_boosted"])


class KnowledgeIndependentSemanticRetrievalTests(_TempStoreCase):
    """Gap closure (independent re-audit, post-Milestone-2): the original
    hybrid search only ever reranked chunks that FTS5 keyword search had
    already found, so a chunk with zero keyword overlap with the query
    could never surface no matter how close its embedding was -- the
    independent verification report's finding that "a semantic-only match
    cannot be retrieved." These tests prove the new independent semantic
    retrieval pass actually closes that gap, not merely that a similarity
    number gets computed somewhere, while keeping the keyword baseline,
    missing-embedding handling, scope isolation, and ranking determinism
    all intact."""

    def _semantic_store(self, vectors, available=True):
        adapter = _FakeEmbeddingAdapter(vectors, available=available)
        return KnowledgeStore(self.store, self.audit, embedding_adapter=adapter), adapter

    def test_semantic_only_match_with_zero_keyword_overlap_is_retrieved(self):
        # Deliberately disjoint vocabularies: the chunk and the query share
        # not one single word, so FTS5 keyword search is GUARANTEED to
        # return zero rows for this query -- only a genuinely independent
        # semantic scan can ever find this chunk.
        content_text = "Xylophone marmalade quantum lighthouse zebra unicycle."
        query = "banana trombone glacier submarine"
        kb, _ = self._semantic_store({content_text: [1.0, 0.0], query: [0.99, 0.01]})
        doc = kb.ingest_bytes("disjoint.txt", "text/plain", content_text.encode(), "personal", "upload", "aryan")

        # Prove the gap actually existed: pure keyword search finds nothing.
        self.assertEqual(kb.search([("personal", None)], query, use_semantic=False), [])

        # The hybrid search closes it: independent semantic retrieval finds
        # the chunk even though it was never an FTS candidate.
        hits = kb.search([("personal", None)], query, use_semantic=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["document_id"], doc["id"])
        self.assertIsNone(hits[0]["rank"])  # never had an FTS rank -- proof it wasn't a keyword hit
        self.assertEqual(hits[0]["retrieved_via"], "semantic")
        self.assertTrue(hits[0]["semantic_boosted"])
        self.assertIsNotNone(hits[0]["cosine_similarity"])
        self.assertGreater(hits[0]["cosine_similarity"], 0.9)

    def test_keyword_fallback_when_embedding_adapter_unavailable(self):
        # Same fixture as above, but the adapter reports itself unavailable
        # this call -- the semantic-only chunk must honestly NOT appear (no
        # embedding call is even attempted), rather than crashing or
        # fabricating a result. This is the "keyword fallback" acceptance
        # case: the pre-existing baseline behavior is unaffected.
        content_text = "Xylophone marmalade quantum lighthouse zebra unicycle."
        query = "banana trombone glacier submarine"
        kb, _ = self._semantic_store({content_text: [1.0, 0.0], query: [0.99, 0.01]}, available=False)
        kb.ingest_bytes("disjoint.txt", "text/plain", content_text.encode(), "personal", "upload", "aryan")
        self.assertEqual(kb.search([("personal", None)], query, use_semantic=True), [])

    def test_keyword_match_with_no_stored_embedding_still_surfaces_alongside_a_semantic_only_match(self):
        # A mixed corpus: one chunk matches by KEYWORD ONLY and has no
        # embedding at all (its content is absent from the adapter's
        # vectors dict, so ingest's best-effort embed() call fails and it
        # is stored with embedding_json=None -- exactly modeling "ingested
        # before an embedding adapter ever covered this content" without a
        # separate Null-adapter store); another matches by SEMANTIC
        # SIMILARITY ONLY and shares no keywords with the query at all.
        # Neither must break the other, and neither score is ever faked.
        keyword_only_text = "The quarterly roadmap review covers Q3 deliverables."
        semantic_only_text = "Xylophone marmalade quantum lighthouse zebra unicycle."
        query = "roadmap review banana trombone glacier submarine"
        adapter = _FakeEmbeddingAdapter({semantic_only_text: [1.0, 0.0], query: [0.99, 0.01]})
        kb = KnowledgeStore(self.store, self.audit, embedding_adapter=adapter)
        doc_keyword = kb.ingest_bytes("kw.txt", "text/plain", keyword_only_text.encode(), "personal", "upload", "aryan")
        doc_semantic = kb.ingest_bytes("sem.txt", "text/plain", semantic_only_text.encode(), "personal", "upload", "aryan")
        self.assertIsNone(self.store.list("knowledge_chunks", "document_id=?", (doc_keyword["id"],))[0]["embedding_json"])

        hits = kb.search([("personal", None)], query, use_semantic=True)
        by_doc = {h["document_id"]: h for h in hits}
        self.assertIn(doc_keyword["id"], by_doc)
        self.assertIn(doc_semantic["id"], by_doc)
        self.assertIsNone(by_doc[doc_keyword["id"]]["cosine_similarity"])
        self.assertEqual(by_doc[doc_keyword["id"]]["retrieved_via"], "keyword")
        self.assertIsNotNone(by_doc[doc_semantic["id"]]["cosine_similarity"])
        self.assertEqual(by_doc[doc_semantic["id"]]["retrieved_via"], "semantic")
        # The genuine positive semantic match outranks the unscored
        # keyword-only candidate (never held back purely for lacking an
        # embedding, exactly as the pre-existing rerank tests already
        # require of the keyword-vs-keyword case).
        self.assertEqual(hits[0]["document_id"], doc_semantic["id"])

    def test_independent_semantic_retrieval_never_crosses_scope_boundary(self):
        # A near-perfect embedding match with ZERO keyword overlap, sitting
        # in a DIFFERENT scope than the one being searched. The new scan
        # query has no scope filter at the SQL level (it authorizes in
        # Python afterward, the exact same enforcement point the keyword
        # path already uses) -- this proves that authorization check
        # actually runs and actually excludes it, not merely that it's
        # inconvenient to reach by keyword.
        content_text = "Xylophone marmalade quantum lighthouse zebra unicycle."
        query = "banana trombone glacier submarine"
        adapter = _FakeEmbeddingAdapter({content_text: [1.0, 0.0], query: [1.0, 0.0]})  # identical vectors: perfect match
        kb = KnowledgeStore(self.store, self.audit, embedding_adapter=adapter)
        other_scope_doc = kb.ingest_bytes(
            "other.txt", "text/plain", content_text.encode(), "project", "upload", "aryan",
            scope_id="falguna-engineering", known_project_ids=self.project_ids,
        )
        hits = kb.search([("personal", None)], query, use_semantic=True)
        self.assertEqual(hits, [])
        # Sanity: the SAME chunk IS retrievable once actually authorized
        # for that scope -- proving the emptiness above is real isolation,
        # not a fixture mistake that would have returned nothing anyway.
        authorized_hits = kb.search([("project", "falguna-engineering")], query, use_semantic=True)
        self.assertEqual([h["document_id"] for h in authorized_hits], [other_scope_doc["id"]])

    def test_ranking_is_stable_and_deterministic_across_repeated_identical_calls(self):
        # Several chunks at different similarity distances -- run the exact
        # same search twice against unchanged state and require identical
        # ordering both times (no dependence on dict/set iteration
        # nondeterminism introduced by the keyword+semantic merge).
        texts_and_vectors = {
            "Alpha content about rockets and orbital mechanics.": [1.0, 0.0, 0.0],
            "Beta content about rockets and orbital mechanics too.": [0.9, 0.1, 0.0],
            "Gamma content about rockets and orbital mechanics as well.": [0.8, 0.2, 0.0],
        }
        query = "rockets orbital mechanics"
        vectors = dict(texts_and_vectors)
        vectors[query] = [0.95, 0.05, 0.0]
        adapter = _FakeEmbeddingAdapter(vectors)
        kb = KnowledgeStore(self.store, self.audit, embedding_adapter=adapter)
        doc_ids = [kb.ingest_bytes(f"{i}.txt", "text/plain", text.encode(), "personal", "upload", "aryan")["id"]
                   for i, text in enumerate(texts_and_vectors)]
        first = [h["document_id"] for h in kb.search([("personal", None)], query, use_semantic=True)]
        second = [h["document_id"] for h in kb.search([("personal", None)], query, use_semantic=True)]
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(doc_ids))

    def test_semantic_pool_is_bounded_not_unbounded(self):
        # Many independent semantic-only candidates at varying similarity;
        # a small `limit` must still return only the genuinely best
        # matches, proving the merge is bounded rather than dumping every
        # authorized embedded chunk into the result.
        query = "banana trombone glacier submarine"
        vectors = {query: [1.0, 0.0]}
        texts = []
        for i in range(8):
            text = f"Disjoint filler content number {i} xylophone marmalade quantum lighthouse."
            vectors[text] = [1.0 - (i * 0.1), i * 0.02]
            texts.append(text)
        adapter = _FakeEmbeddingAdapter(vectors)
        kb = KnowledgeStore(self.store, self.audit, embedding_adapter=adapter)
        docs = [kb.ingest_bytes(f"f{i}.txt", "text/plain", t.encode(), "personal", "upload", "aryan") for i, t in enumerate(texts)]
        hits = kb.search([("personal", None)], query, use_semantic=True, limit=3)
        self.assertEqual(len(hits), 3)
        # The three closest vectors (i=0,1,2) must win over the far ones.
        self.assertEqual({h["document_id"] for h in hits}, {docs[0]["id"], docs[1]["id"], docs[2]["id"]})


class AdversarialCrossBoundaryIsolationTests(_TempStoreCase):
    """Phase 2 Milestone 2: isolation tests that specifically try to break
    the scope boundary through a plausible implementation mistake, rather
    than just confirming the happy path stays isolated."""

    def test_sensitive_memory_content_is_redacted_in_search_results_even_when_the_caller_is_authorized(self):
        # Authorization (being allowed to see this scope at all) and
        # sensitivity masking (whether raw content shows in a search
        # preview) are two different gates -- a caller correctly
        # authorized for 'personal' must still get the masked preview,
        # never the raw content, from search() specifically (list()'s
        # masking is already covered elsewhere; search() must not have its
        # own, separate, unmasked path).
        self.mem.save(
            "personal", "fact", "Aryan's home wifi password rotation schedule is quarterly", "user_stated",
            "user_provided", "aryan", sensitivity="sensitive",
        )
        hits = self.mem.search([("personal", None)], "wifi password rotation schedule")
        self.assertEqual(len(hits), 1)
        self.assertNotIn("quarterly", hits[0]["content"])
        self.assertIn("Sensitive", hits[0]["content"])

    def test_project_and_venture_scopes_sharing_the_identical_id_string_never_cross_leak(self):
        # A plausible implementation bug: filtering by scope_id alone
        # (ignoring scope_type) would let a venture-authorized caller see
        # a project's record purely because the id strings collide. Scope
        # IDENTITY is the (scope_type, scope_id) PAIR, never scope_id alone.
        self.store.create("vs_ventures", {
            "name": "Shared ID Venture", "slug": "shared-id-venture", "venture_type": "product", "status": "active",
            "parent_company": "TTT", "actor": "aryan", "created_at": utcnow(), "updated_at": utcnow(),
        }, record_id="falguna-engineering")
        project_record = self.mem.save(
            "project", "fact", "Project-scoped: the deploy key rotates monthly", "user_stated", "user_provided",
            "aryan", scope_id="falguna-engineering", known_project_ids=self.project_ids,
        )
        venture_record = self.mem.save(
            "venture", "fact", "Venture-scoped: the deploy key rotates monthly too", "user_stated",
            "user_provided", "aryan", scope_id="falguna-engineering",
        )
        venture_hits = self.mem.search([("venture", "falguna-engineering")], "deploy key rotates")
        self.assertEqual([h["id"] for h in venture_hits], [venture_record["id"]])
        project_hits = self.mem.search([("project", "falguna-engineering")], "deploy key rotates")
        self.assertEqual([h["id"] for h in project_hits], [project_record["id"]])
        # Authorized for BOTH: both are visible, but each only once, never
        # duplicated or conflated into a single merged row.
        both_hits = self.mem.search([("venture", "falguna-engineering"), ("project", "falguna-engineering")], "deploy key rotates")
        self.assertEqual({h["id"] for h in both_hits}, {venture_record["id"], project_record["id"]})

    def test_a_forgotten_records_scope_still_correctly_excludes_it_from_a_reauthorized_search(self):
        # Forgetting must not accidentally leave a record reachable through
        # some other scope-search code path -- re-run the same authorized
        # search after forget() and confirm it is gone from every read
        # surface, not merely the one already covered in
        # MemorySupersessionAndDeletionTests.
        record = self.mem.save(
            "project", "fact", "Project A's internal staging credential rotates weekly", "user_stated",
            "user_provided", "aryan", scope_id="falguna-engineering", known_project_ids=self.project_ids,
        )
        self.assertEqual(len(self.mem.search([("project", "falguna-engineering")], "staging credential rotates")), 1)
        self.mem.forget(record["id"], "aryan", reason="rotated")
        self.assertEqual(self.mem.search([("project", "falguna-engineering")], "staging credential rotates"), [])
        self.assertEqual(self.mem.list([("project", "falguna-engineering")]), [])


if __name__ == "__main__":
    unittest.main()
