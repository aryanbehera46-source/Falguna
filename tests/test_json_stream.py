"""Tests for falguna.json_stream.IncrementalReplyExtractor -- Local AI
Independence V1.1's real token-streaming pass depends on this correctly
recovering the growing "reply" string out of a JSON-schema-constrained
stream no matter how the underlying provider chunks its output."""
import json
import unittest

from falguna.json_stream import IncrementalReplyExtractor


class IncrementalReplyExtractorTests(unittest.TestCase):
    def _drain(self, chunks):
        extractor = IncrementalReplyExtractor()
        revealed = []
        for chunk in chunks:
            revealed.append(extractor.feed(chunk))
        return "".join(revealed), extractor

    def test_reveals_nothing_before_the_reply_key_is_seen(self):
        extractor = IncrementalReplyExtractor()
        self.assertEqual(extractor.feed('{"suggested'), "")
        self.assertFalse(extractor.done)

    def test_exact_real_ollama_chunk_sequence_reassembles_the_full_reply(self):
        # This is the literal chunk sequence observed from a real local
        # Ollama daemon (qwen2.5:1.5b-instruct) streaming a response
        # constrained to CHAT_REPLY_SCHEMA -- captured directly from
        # /api/chat with stream:true during this phase's own verification,
        # not invented. Chunk boundaries fuse a closing quote with the
        # next literal character ('!"'), split words across chunks
        # ('s' + 'uggested' + '_' + 'objective'), and split JSON syntax
        # tokens ('"' + 'reply' + '":' + ' "') -- exactly the cases this
        # extractor exists to handle correctly.
        chunks = [
            '{"', 'reply', '":', ' "', 'Hello', '!"', ',', ' "',
            's', 'uggested', '_', 'objective', '":', ' "',
            'Initiate', ' a', ' conversation', ' or', ' interaction', '.', '"', '}',
        ]
        revealed, extractor = self._drain(chunks)
        self.assertEqual(revealed, "Hello!")
        self.assertTrue(extractor.done)

    def test_never_reveals_text_from_the_suggested_objective_field(self):
        chunks = ['{"reply": "Hi', ' there', '"', ', "suggested_objective": "Do the thing"}']
        revealed, extractor = self._drain(chunks)
        self.assertEqual(revealed, "Hi there")
        self.assertTrue(extractor.done)

    def test_handles_simple_escape_sequences_split_across_chunks(self):
        # A literal backslash arrives in one chunk, its escaped character
        # in the next -- must not be revealed as a bare backslash.
        chunks = ['{"reply": "Line one\\', 'nLine two"}']
        revealed, _ = self._drain(chunks)
        self.assertEqual(revealed, "Line one\nLine two")

    def test_handles_a_unicode_escape_split_across_multiple_chunks(self):
        # – is an en dash (–). Split the escape itself across
        # three separate feed() calls -- nothing must be revealed until
        # all four hex digits have actually arrived.
        chunks = ['{"reply": "a', '\\u', '20', '13', 'b"}']
        revealed, _ = self._drain(chunks)
        self.assertEqual(revealed, "a–b")

    def test_matches_a_real_json_parse_of_the_fully_assembled_content(self):
        # End-to-end sanity: whatever this extractor revealed
        # incrementally must equal what a plain json.loads() of the full,
        # completed content says the "reply" field actually is -- the
        # extractor is a progressive UI convenience, never a second
        # source of truth that could disagree with the real parse.
        full_json = json.dumps({"reply": "A short, multi–word reply with a \"quote\" inside.", "suggested_objective": None})
        # Feed it back in small, arbitrary, non-token-aligned chunks.
        chunks = [full_json[i:i + 3] for i in range(0, len(full_json), 3)]
        revealed, extractor = self._drain(chunks)
        parsed = json.loads(full_json)
        self.assertEqual(revealed, parsed["reply"])
        self.assertTrue(extractor.done)

    def test_feed_after_done_is_a_harmless_noop(self):
        extractor = IncrementalReplyExtractor()
        extractor.feed('{"reply": "hi"')
        self.assertTrue(extractor.done)
        self.assertEqual(extractor.feed(', "suggested_objective": null}'), "")

    def test_empty_reply_value_reveals_nothing_and_still_marks_done(self):
        extractor = IncrementalReplyExtractor()
        self.assertEqual(extractor.feed('{"reply": ""'), "")
        self.assertTrue(extractor.done)


if __name__ == "__main__":
    unittest.main()
