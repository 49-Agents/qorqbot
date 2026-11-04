"""Archive reads of an agent's captured conversations for consumers other than Chat: the agent pane's session list and
historical session view, and the learning programs' transcript source.

The archive is the one authority for a transcript. A conversation is offered only as the complete records of a committed,
hash-verified archive prefix, so a growing session is never read half-written. Which conversations exist comes from the
agent's own capture records; the archive root is selected by the registered identity and home scope, never by a stored path
or a directory listing.
"""

from __future__ import annotations

import hashlib
import re

from qorqut_operations.contracts import DomainError

from bazarlyk.native_archive import CaptureError

from . import store

MAX_SESSION_BYTES = 8 * 1024 * 1024
SESSION_NAME = re.compile(r"^(codex-)?([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\.jsonl$")
CODEX_PREFIX = "codex-"


def session_name(provider: str, conversation_id: str) -> str:
    """The stable name the agent pane lists a conversation under."""
    return f"{CODEX_PREFIX if provider == 'codex' else ''}{conversation_id}.jsonl"


class ArchiveTranscripts:
    def __init__(self, runtime, indexer):
        self.runtime, self.indexer = runtime, indexer

    def listing(self, agent_id: str) -> list[dict]:
        """The agent's captured conversations, newest first. Conversations with nothing committed yet are not offered."""
        with self.runtime.database.read() as uow:
            captures = [row for row in store.agent_captures(uow.connection, agent_id) if row["receipt_id"] and row["captured_length"]]
        captures.sort(key=lambda row: (row["updated_at"], row["conversation_id"]), reverse=True)
        return [{**row, "name": session_name(row["provider"], row["conversation_id"])} for row in captures]

    def read(self, agent_id: str, name: str) -> bytes:
        """The complete committed records of one conversation, bounded."""
        if not isinstance(name, str) or not SESSION_NAME.fullmatch(name):
            raise DomainError("validation_error", "Invalid registered session filename")
        capture = next((row for row in self.listing(agent_id) if row["name"] == name), None)
        if capture is None:
            raise DomainError("not_found", "Registered historical session was not found")
        return self.content(agent_id, capture)

    def content(self, agent_id: str, capture: dict) -> bytes:
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths, any_status=True)
        if profile is None:
            raise DomainError("not_found", "Registered historical session was not found")
        archive = self.indexer.archive(profile)
        try:
            receipt = archive.load_receipt(capture["receipt_id"])
            if receipt.record_boundary > MAX_SESSION_BYTES:
                raise DomainError("payload_too_large", "Historical session exceeds the bounded transcript size")
            return archive.read_raw(receipt, offset=0, limit=receipt.record_boundary).data if receipt.record_boundary else b""
        except (CaptureError, ValueError) as error:
            raise DomainError("operation_unavailable", f"Historical session archive is unavailable: {getattr(error, 'code', 'invalid')}") from None

    def identity(self, agent: dict, capture: dict) -> dict:
        """The stable registered identity of one conversation, whether or not its content could be read."""
        return {"session_id": capture["conversation_id"],
                "source_id": f"native-archive/{agent['agent_id']}/{capture['provider']}/{capture['conversation_id']}",
                "agent_id": agent["agent_id"], "agent_kind": agent["profile_json"].get("role", {}).get("id", "")}

    def source(self, agent: dict, capture: dict, content: bytes) -> dict:
        """One conversation in the learning programs' source shape."""
        return {**self.identity(agent, capture),
                "content": content.decode("utf-8"), "transcript_hash": hashlib.sha256(content).hexdigest()}


__all__ = ["ArchiveTranscripts", "MAX_SESSION_BYTES", "session_name"]
