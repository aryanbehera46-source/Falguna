"""Chat: persistent conversations with a Chat -> Work handoff.

Chat is additive to Falguna Engineering. It reuses the same StateStore,
the same replaceable ModelGateway/transport routing Work missions already
use (falguna.gateway / falguna.codex_transport), and the same
ControlPlane.create_mission/start entrypoint for handoff -- it never
creates a worktree, edits a repository file, runs verification, or
decides a merge itself. Chat has no tools and cannot touch a repository;
it can only produce a proposed objective that a human hands off to Work,
which then goes through discovery, policy, isolation, verification,
independent review, and the merge-approval gate exactly as any other
mission does.
"""
import json
from typing import Callable, List, Optional

from .providers import ErrorCategory, FalgunaModelError
from .store import StateStore, utcnow


CHAT_SYSTEM_PROMPT = (
    "You are Falguna, a local engineering assistant embedded in a bounded "
    "self-building control plane. This is Chat: a scoping and discussion "
    "surface with no tools. You cannot read or write any repository file, "
    "run a command, or make a change from this conversation. When the "
    "person describes a concrete, bounded engineering change they want "
    "made to one of their approved projects, propose one short objective "
    "sentence they could hand off to Work (Falguna's isolated-worktree "
    "mission runner) and put it in suggested_objective; otherwise leave "
    "suggested_objective null. Never claim to have made, started, or "
    "verified a change yourself -- only Work does that, and only after "
    "the person hands the objective off and later approves the merge."
)

CHAT_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "suggested_objective": {"type": ["string", "null"]},
    },
    "required": ["reply", "suggested_objective"],
    "additionalProperties": False,
}


class ChatError(RuntimeError):
    """Raised for a user-facing chat failure (bad input or model/transport
    failure). `message` (this exception's str()) must always be safe to
    show verbatim -- never raw provider stdout/stderr. `category` is one
    of falguna.providers.ErrorCategory (defaults to TRANSPORT_FAILURE for
    a caller that didn't specify one, e.g. a plain validation error keeps
    working exactly as before this field existed). `technical_detail` may
    carry raw diagnostic text and is surfaced only in an expandable
    "technical details" panel, never in the primary bubble text."""

    def __init__(self, message: str, category: Optional[str] = None, technical_detail: str = ""):
        super().__init__(message)
        self.category = category or ErrorCategory.TRANSPORT_FAILURE
        self.technical_detail = technical_detail or ""


class ConversationStore:
    """Persistence for conversations, messages, and Work handoffs.

    Thin wrapper over the existing StateStore -- no new database, no new
    process, no new write path into mission state. A handoff row only ever
    records a run_id that ControlPlane.create_mission/start already
    produced; it cannot be used to create or mutate a run directly.
    """

    def __init__(self, store: StateStore):
        self.store = store

    def create_conversation(self, title: str = "", project_id: Optional[str] = None) -> str:
        title = (title or "New chat").strip()[:120] or "New chat"
        now = utcnow()
        return self.store.create("conversations", {
            "title": title,
            "project_id": project_id,
            "status": "ACTIVE",
            "created_at": now,
            "updated_at": now,
        })

    def get_conversation(self, conversation_id: str) -> Optional[dict]:
        return self.store.get("conversations", conversation_id)

    def list_conversations(self, limit: int = 50) -> List[dict]:
        """Active conversations only, most-recently-updated first. Archived
        conversations are intentionally excluded from this recent list but
        remain reachable (and reopenable) through search.

        Each row additionally carries a read-only ``last_message_preview``
        (last chat_messages.content for that conversation, truncated) so the
        sidebar can show a one-line preview without a second round trip.
        This is purely additive presentation data -- it does not change
        conversation identity or ordering, and nothing else reads it."""
        rows = [r for r in self.store.list("conversations") if r["status"] == "ACTIVE"]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        rows = rows[:limit]
        for row in rows:
            row["last_message_preview"] = self._last_message_preview(row["id"])
        return rows

    def _last_message_preview(self, conversation_id: str, max_len: int = 140) -> str:
        cur = self.store.db.execute(
            "SELECT content FROM chat_messages WHERE conversation_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        row = cur.fetchone()
        if not row:
            return ""
        content = (row["content"] or "").strip().replace("\n", " ")
        if len(content) > max_len:
            content = content[:max_len].rstrip() + "…"
        return content

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        title = (title or "").strip()[:120]
        if not title:
            raise ChatError("Title cannot be empty")
        if not self.get_conversation(conversation_id):
            raise ChatError("conversation not found")
        self.store.update("conversations", conversation_id, title=title)

    def archive_conversation(self, conversation_id: str) -> None:
        if not self.get_conversation(conversation_id):
            raise ChatError("conversation not found")
        self.store.update("conversations", conversation_id, status="ARCHIVED")

    def list_messages(self, conversation_id: str) -> List[dict]:
        """Every message ever created before the `superseded` column existed
        has a NULL value there, which this filter treats as "not superseded"
        -- so this stays exactly backward compatible with every existing
        caller/test that never superseded anything."""
        rows = self.store.list("chat_messages", "conversation_id=?", (conversation_id,))
        return [r for r in rows if not r.get("superseded")]

    def add_message(self, conversation_id: str, role: str, content: str,
                     model_call: Optional[dict] = None, error: Optional[str] = None,
                     status: Optional[str] = None) -> dict:
        if role not in {"user", "assistant"}:
            raise ChatError("invalid message role")
        content = (content or "").strip()
        if role == "user" and not content:
            raise ChatError("Message cannot be empty")
        record = {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "model_call_json": json.dumps(model_call, sort_keys=True) if model_call else None,
            "error": error,
            "created_at": utcnow(),
            "status": status or ("FAILED" if error else "COMPLETED"),
            "superseded": 0,
            "edited_at": None,
        }
        message_id = self.store.create("chat_messages", record)
        self.store.update("conversations", conversation_id, updated_at=utcnow())
        return {"id": message_id, **record}

    # ---------------------------------------------------- message state (V2)
    # Explicit, truthful message states (Section 4): PENDING (queued, worker
    # not yet started) -> GENERATING (transport call in flight) ->
    # COMPLETED / FAILED / CANCELLED. Every transition below only ever
    # writes a state Falguna actually reached -- there is no fabricated
    # progress percentage or fake intermediate step.

    def add_pending_message(self, conversation_id: str) -> dict:
        """Creates the placeholder assistant row a background worker will
        fill in. Exists from the first HTTP response onward so a page
        refresh mid-generation shows a truthful PENDING/GENERATING bubble
        instead of nothing."""
        record = {
            "conversation_id": conversation_id, "role": "assistant", "content": "",
            "model_call_json": None, "error": None, "created_at": utcnow(),
            "status": "PENDING", "superseded": 0, "edited_at": None,
        }
        message_id = self.store.create("chat_messages", record)
        return {"id": message_id, **record}

    def mark_generating(self, message_id: str) -> None:
        row = self.store.get("chat_messages", message_id)
        if row and row.get("status") == "PENDING":
            self.store.update("chat_messages", message_id, status="GENERATING")

    def complete_message(self, message_id: str, content: str, model_call: Optional[dict],
                          suggested_objective: Optional[str] = None) -> bool:
        """Only applies the result if the message has not already been
        cancelled (Stop generation) -- returns False when a cancel raced it,
        in which case the caller must not surface the (already-discarded)
        reply as if it were delivered."""
        row = self.store.get("chat_messages", message_id)
        if not row or row.get("status") == "CANCELLED":
            return False
        self.store.update(
            "chat_messages", message_id, content=content, error=None, status="COMPLETED",
            model_call_json=json.dumps(model_call, sort_keys=True) if model_call else None,
            suggested_objective=suggested_objective,
        )
        self.store.update("conversations", row["conversation_id"], updated_at=utcnow())
        return True

    def fail_message(self, message_id: str, error: str, category: Optional[str] = None,
                      detail: Optional[str] = None) -> bool:
        row = self.store.get("chat_messages", message_id)
        if not row or row.get("status") == "CANCELLED":
            return False
        self.store.update(
            "chat_messages", message_id, content="", error=str(error), status="FAILED",
            error_category=category, error_detail=detail,
        )
        return True

    def cancel_message(self, message_id: str) -> dict:
        """Stop generation. Marks the message CANCELLED so a still-running
        background call cannot later overwrite it with a delivered reply --
        Falguna cannot kill an in-flight Codex CLI subprocess call, so this
        is an honest "discard the result, never apply it" cancel rather than
        a claim that the underlying call was interrupted."""
        row = self.store.get("chat_messages", message_id)
        if not row:
            raise ChatError("message not found")
        if row["role"] != "assistant":
            raise ChatError("only an assistant message can be cancelled")
        if row.get("status") not in {"PENDING", "GENERATING"}:
            raise ChatError("message is not currently generating")
        self.store.update("chat_messages", message_id, status="CANCELLED", content="", error="Cancelled by user")
        return self.store.get("chat_messages", message_id)

    def edit_user_message(self, conversation_id: str, message_id: str, content: str) -> dict:
        """Edit-and-resubmit: updates a user message's text in place and
        supersedes (soft-deletes from the visible thread) every message that
        came after it, so a fresh reply can be generated against the edited
        history. Superseded rows are kept, never deleted, for audit."""
        content = (content or "").strip()
        if not content:
            raise ChatError("Message cannot be empty")
        row = self.store.get("chat_messages", message_id)
        if not row or row["conversation_id"] != conversation_id:
            raise ChatError("message not found")
        if row["role"] != "user":
            raise ChatError("only a user message can be edited")
        self.store.update("chat_messages", message_id, content=content, edited_at=utcnow())
        for later in self.store.list("chat_messages", "conversation_id=? AND created_at>?", (conversation_id, row["created_at"])):
            self.store.update("chat_messages", later["id"], superseded=1)
        return self.store.get("chat_messages", message_id)

    def prepare_regenerate(self, conversation_id: str, message_id: str) -> dict:
        """Regenerate / Retry: supersedes the given assistant message (which
        must be the latest visible message) so a fresh PENDING reply can
        replace it. Works identically for a COMPLETED reply the person wants
        reworded and a FAILED one they want retried."""
        visible = self.list_messages(conversation_id)
        if not visible or visible[-1]["id"] != message_id:
            raise ChatError("only the most recent assistant message can be regenerated")
        row = visible[-1]
        if row["role"] != "assistant":
            raise ChatError("only an assistant message can be regenerated")
        if row.get("status") == "GENERATING":
            raise ChatError("message is still generating")
        self.store.update("chat_messages", message_id, superseded=1)
        return row

    def delete_conversation(self, conversation_id: str) -> None:
        if not self.get_conversation(conversation_id):
            raise ChatError("conversation not found")
        self.store.update("conversations", conversation_id, status="DELETED")

    def record_handoff(self, conversation_id: str, run_id: str, objective: str) -> str:
        return self.store.create("conversation_handoffs", {
            "conversation_id": conversation_id,
            "run_id": run_id,
            "objective": objective,
            "created_at": utcnow(),
        })

    def list_handoffs(self, conversation_id: str) -> List[dict]:
        return self.store.list("conversation_handoffs", "conversation_id=?", (conversation_id,))

    def handoffs_for_run(self, run_id: str) -> List[dict]:
        return self.store.list("conversation_handoffs", "run_id=?", (run_id,))

    def search_conversations(self, query: str, limit: int = 25) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []
        like = f"%{query}%"
        rows = self.store.db.execute(
            "SELECT DISTINCT c.id, c.title, c.updated_at FROM conversations c "
            "LEFT JOIN chat_messages m ON m.conversation_id = c.id "
            "WHERE c.status != 'DELETED' AND (c.title LIKE ? OR m.content LIKE ?) "
            "ORDER BY c.updated_at DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [{"type": "conversation", "id": r["id"], "title": r["title"], "updated_at": r["updated_at"]} for r in rows]


def search_missions(store: StateStore, query: str, limit: int = 25) -> List[dict]:
    """Search mission title + objective text. Read-only; mirrors the same
    mission/task/requirement join web.py already does for /api/runs."""
    query = (query or "").strip().lower()
    if not query:
        return []
    hits = []
    for run in reversed(store.list("runs")):
        task = store.get("tasks", run["task_id"])
        requirement = store.get("requirements", task["requirement_id"]) if task else None
        mission = store.get("missions", requirement["mission_id"]) if requirement else None
        if not mission:
            continue
        haystack = f"{mission['title']} {requirement['body']}".lower()
        if query in haystack:
            hits.append({
                "type": "mission", "run_id": run["id"], "title": mission["title"],
                "status": run["status"], "updated_at": run["updated_at"], "repository": task["repository"],
            })
        if len(hits) >= limit:
            break
    return hits


def _model_call(decoded: dict, config: dict, purpose: str = "chat") -> dict:
    """Same cost/usage accounting as StructuredEditWorker._model_call.
    `purpose` records which surface made the call (chat/research/...) for
    audit and cost-reporting; it does not change how the cost is computed."""
    usage = decoded.get("usage", {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    calculated_cost = max(0, prompt_tokens - cached_tokens) * 0.75e-6 + cached_tokens * 0.075e-6 + completion_tokens * 4.50e-6
    cost = float(decoded.get("_falguna_cost_usd", calculated_cost))
    metadata = decoded.get("_falguna_metadata", {})
    return {
        "provider": decoded.get("_falguna_provider", "openai-compatible"),
        "model": metadata.get("routed_model", config["model"]),
        "purpose": purpose,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "cost_usd": cost,
        "metadata": {"adapter": purpose, **metadata},
    }


class ChatResponder:
    """Generates assistant replies through the same replaceable model-gateway
    transport Work missions use. Never given worktree/editable-file context,
    so it cannot read or propose edits to real files -- only converse and,
    optionally, suggest an objective for a human-initiated Work handoff.
    """

    def __init__(self, gateway, transport: Callable, model: str, timeout_seconds: int = 60):
        self.gateway = gateway
        self.transport = transport
        self.model = model
        self.timeout_seconds = timeout_seconds

    def reply(self, history: List[dict]) -> dict:
        """history: [{"role": "user"|"assistant", "content": str}, ...] oldest first.
        Returns {"reply", "suggested_objective", "model_call"} or raises ChatError
        with a message safe to show the person -- never a fabricated reply."""
        config = dict(self.gateway.configuration())
        config["model"] = self.model
        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-20:]]
        payload = {
            "model": config["model"],
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "chat_reply", "strict": True, "schema": CHAT_REPLY_SCHEMA},
            },
        }
        try:
            decoded = self.transport(config, payload, self.timeout_seconds)
        except FalgunaModelError as exc:
            # Already a sanitized, user-safe message/category from the
            # provider or router -- pass it through unchanged rather than
            # re-wrapping it in a generic string (that re-wrap is exactly
            # how raw transport text used to leak into the chat bubble).
            raise ChatError(exc.message, category=exc.category, technical_detail=exc.technical_detail) from exc
        except Exception as exc:
            # An unexpected failure from a transport that isn't provider-
            # aware yet (e.g. a bare Callable in a test) -- still never
            # dumps the raw exception into the user-facing message.
            raise ChatError(
                "Falguna couldn't reach the model provider for this reply.",
                category=ErrorCategory.TRANSPORT_FAILURE, technical_detail=str(exc),
            ) from exc
        message = decoded["choices"][0]["message"]
        if message.get("refusal"):
            raise ChatError("The model declined to respond to this message.", category=ErrorCategory.TRANSPORT_FAILURE)
        try:
            parsed = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise ChatError(
                "The model returned a response Falguna could not parse.",
                category=ErrorCategory.TRANSPORT_FAILURE, technical_detail=str(message.get("content", ""))[:2000],
            ) from exc
        return {
            "reply": parsed["reply"],
            "suggested_objective": parsed.get("suggested_objective"),
            "model_call": _model_call(decoded, config),
        }
