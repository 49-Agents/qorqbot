"""Python helper for registered local programs: post an Urgent Notification through the local gateway socket.

    from bazarlyk.telegram.client import notify
    notify("/path/to/notify.sock", "/path/to/producer-secret", event_key="db-down", title="Database unreachable",
           text="Writes fail. Fail over.", reason="Data loss risk")

The secret is read from its own private file. Nothing here logs or prints it.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket

from .private_files import read_private_file

MAX_RESPONSE_BYTES = 64 * 1024


class GatewayError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _call(socket_path, secret_file, action, fields, timeout):
    secret = read_private_file(Path(secret_file), max_bytes=512).decode("utf-8").strip()
    payload = json.dumps({"version": 1, "action": action, "secret": secret, **fields}, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(socket_path))
            connection.sendall(payload)
            data = b""
            while b"\n" not in data and len(data) <= MAX_RESPONSE_BYTES:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError as exc:
            raise GatewayError("unavailable", "The Telegram gateway is unreachable", retryable=True) from exc
    try:
        response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as exc:
        raise GatewayError("invalid_response", "The gateway returned an invalid response", retryable=True) from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = (response or {}).get("error") or {}
        raise GatewayError(error.get("code", "error"), error.get("message", "The gateway refused the request"),
                           bool(error.get("retryable")))
    return response["result"]


def notify(socket_path, secret_file, *, event_key, title, text, reason, link=None, state="firing", timeout=10.0):
    fields = {"event_key": event_key, "title": title, "text": text, "reason": reason, "state": state}
    if link is not None:
        fields["link"] = link
    return _call(socket_path, secret_file, "notify", fields, timeout)


def notification_status(socket_path, secret_file, *, event_key, timeout=10.0):
    return _call(socket_path, secret_file, "status", {"event_key": event_key}, timeout)
