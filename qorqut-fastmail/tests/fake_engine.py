"""An in-process stand-in for the email engine's ``/fastmail`` account API and ``/integrations/routes``, over httpx.MockTransport.

It keeps the engine's ledger rules that matter to the provider: the receipt digest is the hash of the exact JSON posted, a
request id is applied once, a sender identity must belong to the account, and a response can be lost *after* the action was
recorded. Nothing here opens a socket."""

import hashlib
import json

import httpx

TOKEN = "t" * 40
ACTIONS = ["message_update", "message_delete", "folder_create", "folder_update", "folder_delete", "create_draft", "update_draft",
           "send", "vacation_set", "masked_email_set"]


class FakeEngine:
    def __init__(self, account_id="acct-test", token=TOKEN):
        self.account_id, self.token = account_id, token
        self.identities = [{"id": "id-1", "name": "Owner", "email": "owner@example.test", "replyTo": None, "bcc": None},
                           {"id": "id-2", "name": "Billing alias", "email": "billing@example.test", "replyTo": None, "bcc": None},
                           {"id": "id-3", "name": "Forwarding", "email": "bcc@example.test", "replyTo": None, "bcc": "x@example.test"}]
        self.actions = list(ACTIONS)
        self.messages = {"m-1": {"id": "m-1", "thread_id": "t-1", "subject": "Hello", "expected_state": "s1", "bodyTruncated": False, "body": {"content": "Original body"}, "attachments": [], "from": {"emailAddress": {"address": "friend@example.test"}}}}
        self.attachment = bytes(range(256)) * 10
        self.posts, self.requests, self.receipts, self.routes = [], [], {}, []
        self.mode = None            # None | "lost_after_record" | "lost_before_record" | "prepared" | "uncertain" | "failed" | "server_error"
        self.receipt_available = True

    # -- transport ------------------------------------------------------------------------------------------------
    def client_factory(self, **options):
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url=options.get("base_url", "http://127.0.0.1:1"),
                            headers=options.get("headers"), timeout=options.get("timeout"))

    def respond(self, status, body=None):
        return httpx.Response(status, json=body if body is not None else {})

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        if request.headers.get("authorization") != "Bearer " + self.token:
            return self.respond(401, {"error": "unauthorized"})
        path = request.url.path
        if path.startswith("/integrations/routes/"):
            return self.route(request, path.rsplit("/", 1)[1])
        if path == "/integrations/routes":
            return self.respond(200, {"routes": self.routes})
        if not path.startswith("/fastmail"):
            return self.respond(404, {"error": "not_found"})
        bits = [bit for bit in path[len("/fastmail"):].split("/") if bit]
        method = request.method
        if method == "GET" and not bits:
            return self.respond(200, {"account_id": self.account_id, "capabilities": ["mail"], "folders": [],
                                      "identities": self.identities, "actions": self.actions})
        if method == "GET" and bits == ["folders"]:
            return self.respond(200, {"state": "f1", "folders": [{"id": "inbox", "name": "Inbox"}]})
        if method == "GET" and bits == ["identities"]:
            return self.respond(200, {"identities": self.identities})
        if method == "GET" and bits == ["messages"]:
            return self.respond(200, {"ids": list(self.messages), "total": len(self.messages),
                                      "query": dict(request.url.params)})
        if method == "GET" and bits[0] == "messages" and len(bits) == 2:
            message = self.messages.get(bits[1])
            return self.respond(200, {"message": message, "expected_state": message["expected_state"]}) if message else self.respond(404, {"error": "not_found"})
        if method == "GET" and bits[0] == "threads":
            return self.respond(200, {"thread_id": bits[1], "messages": list(self.messages.values())})
        if method == "GET" and bits[0] == "attachments":
            offset, limit = int(request.url.params.get("offset", 0)), int(request.url.params.get("limit", 5 * 1024 * 1024))
            content = self.attachment[offset:offset + limit]
            return httpx.Response(200, content=content, headers={"X-Attachment-Total": str(len(self.attachment)),
                                                                 "X-Attachment-Offset": str(offset)})
        if method == "GET" and bits[0] == "actions":
            receipt = self.receipts.get(bits[1]) if self.receipt_available else None
            return self.respond(200, receipt) if receipt else self.respond(404, {"error": "operation_not_found"})
        if method == "POST" and bits == ["actions"]:
            return self.post(request)
        return self.respond(404, {"error": "not_found"})

    def post(self, request):
        parsed = json.loads(request.content)
        self.posts.append(parsed)
        digest = hashlib.sha256(json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if parsed["account_id"] != self.account_id:
            return self.respond(409, {"error": "account_identity_mismatch"})
        if parsed["action"] == "send":
            identity = next((item for item in self.identities if item["id"] == parsed.get("identity_id")), None)
            if (identity is None or identity["email"].lower() != str(parsed.get("from_address")).lower()
                    or identity["bcc"] or identity["replyTo"]):
                return self.respond(403, {"error": "account_sender_identity_denied"})
        if self.mode == "server_error":
            return self.respond(502, {"error": "bad_gateway"})
        if self.mode == "lost_before_record":
            raise httpx.ReadTimeout("lost before the engine recorded the request", request=request)
        state = {"prepared": "prepared", "uncertain": "uncertain", "failed": "failed"}.get(self.mode, "completed")
        receipt = {"request_id": parsed["request_id"], "account_id": parsed["account_id"], "digest": digest,
                   "action": parsed["action"], "state": state, "updated_at": "2026-10-25T00:00:00Z", "result": {}}
        self.receipts.setdefault(parsed["request_id"], receipt)
        if self.mode == "lost_after_record":
            raise httpx.ReadTimeout("response lost after the engine recorded the request", request=request)
        return self.respond(202 if state == "prepared" else 200, self.receipts[parsed["request_id"]])

    def route(self, request, action):
        body = json.loads(request.content)
        if body.get("refuse"):
            return self.respond(409, {"error": "route_conflict"})
        view = {"route_id": "r1", "revision": 1, "generation": 1, "client_id": body.get("client_id", "c"), "business_id": None,
                "mailbox_ref": "m", "purpose": "outreach_archive", "sender_ref": "s", "active": action == "ensure", "state": "ready",
                "ready": True, "blocker": None}
        return self.respond(200, view)

    def complete(self, request_id):
        """Mark a prepared/uncertain request completed, as the engine's own reconciliation would."""
        self.receipts[request_id] = {**self.receipts[request_id], "state": "completed"}
