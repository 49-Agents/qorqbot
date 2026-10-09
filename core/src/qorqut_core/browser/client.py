"""Bounded protocol-1 calls to a configured loopback origin, outside SQL."""

import hashlib
import http.client
import json
import time
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..contracts import CoreError
from ..storage.database import assert_external_io_allowed
from .models import Observation, Receipt

TIMEOUT = 8.0  # Browser's five-second gate wait plus bounded transport overhead.
OBSERVATION_TIMEOUT = 2.0
MAX_REPLY = 65536


def payload_hash(command):
    # Browser protocol 1 uses ASCII-escaped canonical JSON.
    body = json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()


class BrowserClient:
    def call(self, url, actor_id, path, model, payload=None):
        assert_external_io_allowed()
        destination = urlsplit(url)
        timeout = TIMEOUT if payload is not None else OBSERVATION_TIMEOUT
        connection = http.client.HTTPConnection(destination.hostname, destination.port, timeout=timeout)
        deadline = time.monotonic() + timeout
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(left)
        try:
            connection.connect()
            remaining()
            connection.request("GET" if payload is None else "POST", path,
                body=payload.model_dump_json().encode() if payload is not None else None,
                headers={"X-Qorqut-Actor": str(actor_id), "Content-Type": "application/json", "Accept": "application/json"})
            remaining()
            response = connection.getresponse()
            body = bytearray()
            while True:
                remaining()
                chunk = response.read1(min(8192, MAX_REPLY + 1 - len(body)))
                body.extend(chunk)
                if len(body) > MAX_REPLY:
                    raise ValueError
                if not chunk:
                    break
            if (response.length not in (None, 0)
                    or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"
                    or response.getheader("Content-Encoding", "identity") != "identity"):
                raise ValueError
            if response.status != 200:
                envelope = json.loads(body)
                error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
                if not isinstance(error, dict):
                    error = {}
                code = error.get("code")
                if code not in {"not_found", "target_changed", "not_ready", "uncertain_effect", "invalid_argument", "idempotency_conflict"}:
                    code = "unavailable"
                raise CoreError(code, "Browser service did not confirm the request")
            return model.model_validate_json(body)
        except (OSError, http.client.HTTPException, ValueError, ValidationError):
            raise CoreError("unavailable", "Browser service observation or response is unavailable") from None
        finally:
            connection.close()

    def observe(self, url, actor_id):
        return self.call(url, actor_id, "/v1/identity", Observation)

    def receipt(self, url, actor_id, command_id):
        try:
            return self.call(url, actor_id, f"/v1/browser/commands/{command_id}", Receipt)
        except CoreError as exc:
            if exc.code == "not_found":
                return None
            raise

    def reconcile(self, url, actor_id, command):
        return self.call(url, actor_id, "/v1/browser/control", Receipt, command)
