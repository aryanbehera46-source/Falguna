"""Falguna Local AI Independence V1.1: incremental extraction of the
growing "reply" string value out of a JSON-schema-constrained model
response while it is still streaming in.

Chat's response format (falguna.chat.CHAT_REPLY_SCHEMA) is a strict JSON
object -- {"reply": str, "suggested_objective": str|null} -- so a local
model's raw token-by-token stream is itself a stream of JSON syntax, not
prose. Showing those raw deltas to a person would render broken fragments
(`{"`, `repl`, `y":`, ` "`, `Hello`, `!"`, ...) rather than a readable,
growing answer.

IncrementalReplyExtractor solves this by scanning the accumulated raw
text character-by-character, tracking whether the scan position is
inside the "reply" string value, and only ever revealing text that is
unambiguously decoded. It never assumes a chunk boundary means anything
-- verified against a real local Ollama daemon
(`ollama pull qwen2.5:1.5b-instruct` + a live streamed /api/chat call
with this exact schema), chunk boundaries routinely fuse a closing quote
with the next literal character, split a JSON key across two chunks, or
land mid-escape-sequence. An escape sequence split across two feed()
calls (including a \\uXXXX unicode escape needing 4 more hex digits than
have arrived yet) is buffered, never guessed at.
"""
import re
from typing import Optional

_REPLY_KEY_RE = re.compile(r'"reply"\s*:\s*"')

_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
    '"': '"', "\\": "\\", "/": "/",
}


class IncrementalReplyExtractor:
    """Feed raw streamed text deltas in order; get back only the newly-
    revealed, fully-decoded characters of the "reply" value so far (may
    be an empty string for a feed() call that revealed nothing new yet,
    e.g. still inside the JSON preamble or a not-yet-complete escape).
    """

    def __init__(self):
        self._raw = ""
        self._value_start: Optional[int] = None
        self._scan_pos: Optional[int] = None
        self._pending_escape = False
        self.done = False

    def feed(self, delta: str) -> str:
        if not delta or self.done:
            return ""
        self._raw += delta
        if self._value_start is None:
            match = _REPLY_KEY_RE.search(self._raw)
            if not match:
                return ""
            self._value_start = match.end()
            self._scan_pos = self._value_start

        raw = self._raw
        n = len(raw)
        i = self._scan_pos
        out = []
        while i < n:
            ch = raw[i]
            if self._pending_escape:
                if ch == "u":
                    if i + 5 > n:
                        break  # \uXXXX not fully arrived yet -- wait for more
                    hex_digits = raw[i + 1:i + 5]
                    try:
                        out.append(chr(int(hex_digits, 16)))
                    except ValueError:
                        out.append("u" + hex_digits)  # malformed -- surface literally, never crash
                    i += 5
                    self._pending_escape = False
                    continue
                out.append(_SIMPLE_ESCAPES.get(ch, ch))
                self._pending_escape = False
                i += 1
                continue
            if ch == "\\":
                self._pending_escape = True
                i += 1
                continue
            if ch == '"':
                self.done = True
                i += 1
                break
            out.append(ch)
            i += 1
        self._scan_pos = i
        return "".join(out)
