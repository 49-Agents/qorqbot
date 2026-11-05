"""Synthetic provider records for outgoing agent messages: the two native representations of one ``agents_message_send`` result.

Pure builders over plain dicts (no host, database or archive), shaped like the records the Claude and Codex clients write. The
envelope is the MCP success result the operation returns; every ID and name here is invented."""

import json

TIME = "2026-10-16T10:00:00.000Z"


def send_envelope(message_id, recipient_id="recipient-0001", *, event_id=1, full=False, status="ok", **message):
    """The JSON text of one send result: the compact receipt, or the full-detail message with its other columns."""
    receipt = {"id": message_id, "event_id": event_id, "thread_id": message_id, "created_at": "2026-10-16T10:00:00.000000+00:00",
               "recipient_id": recipient_id, "recipient_name": "Synthetic"}
    if full:
        receipt.update(business_id=None, sender_id="sender-0001", sender_name="Sender", reply_to=None, body="Synthetic body",
                       summary=None, current_sender_id="sender-0001", current_recipient_id=recipient_id, acknowledged_at=None)
    receipt.update(message)
    return json.dumps({"status": status, "data": {"message": receipt}, "response_detail": "full" if full else "compact"},
                      separators=(",", ":"))


def text_content(*texts):
    return [{"type": "text", "text": text} for text in texts]


def claude_result(content, *, tool_id="toolu_synthetic", is_error=None):
    """One ``tool_result`` block; ``content`` is the client's text-block list (or a plain string)."""
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return block


def claude_results(session, *blocks, uuid_="tool-result-1", **extra):
    """A Claude user record that carries only tool results, as the client writes one after a tool call."""
    return {"type": "user", "uuid": uuid_, "sessionId": session, "isSidechain": False, "timestamp": TIME,
            "message": {"role": "user", "content": list(blocks)}, **extra}


def codex_send_call(*texts, thread=None, status="completed", is_error=False, server="bazarlyk", tool="agents_message_send",
                    item_id="call_synthetic", **item):
    """A completed-item event for one MCP tool call; ``texts`` are the result's text blocks."""
    payload = {"type": "item_completed", "turn_id": "turn-1",
               "item": {"type": "McpToolCall", "id": item_id, "server": server, "tool": tool, "arguments": {}, "status": status,
                        "result": {"content": text_content(*texts), "isError": is_error}, **item}}
    if thread is not None:
        payload["thread_id"] = thread
    return {"type": "event_msg", "timestamp": TIME, "payload": payload}


def codex_tool_output(*texts, call_id="call_synthetic"):
    """The duplicate envelope Codex also writes for the same call; it is not a second message."""
    return {"type": "response_item", "timestamp": TIME,
            "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": text_content(*texts)}}
