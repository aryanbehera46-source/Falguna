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
    """Raised for a user-facing chat failure (bad input or model/transport failure)."""


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
        return self.store.list("chat_messages", "conversation_id=?", (conversation_id,))

    def add_message(self, conversation_id: str, role: str, content: str,
                     model_call: Optional[dict] = None, error: Optional[str] = None) -> dict:
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
        }
        message_id = self.store.create("chat_messages", record)
        self.store.update("conversations", conversation_id, updated_at=utcnow())
        return {"id": message_id, **record}

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


def _model_call(decoded: dict, config: dict) -> dict:
    """Same cost/usage accounting as StructuredEditWorker._model_call, purpose='chat'."""
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
        "purpose": "chat",
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "cost_usd": cost,
        "metadata": {"adapter": "chat", **metadata},
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
        except Exception as exc:
            raise ChatError(f"MODEL_UNAVAILABLE: {exc}") from exc
        message = decoded["choices"][0]["message"]
        if message.get("refusal"):
            raise ChatError("The model declined to respond to this message.")
        try:
            parsed = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise ChatError("The model returned a response Falguna could not parse.") from exc
        return {
            "reply": parsed["reply"],
            "suggested_objective": parsed.get("suggested_objective"),
            "model_call": _model_call(decoded, config),
        }
