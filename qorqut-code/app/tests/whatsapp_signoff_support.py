"""A synthetic host for the owner-approved personal WhatsApp send: the real runtime, Signoff engine and background service
with a scripted stand-in for the connector. No socket, secret, WhatsApp account, model or network is used."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from bazarlyk.background.integration import attach_background
from bazarlyk.bootstrap import whatsapp as installation
from bazarlyk.service.whatsapp_send import UnknownOutcome, payload_fingerprint
from host_foundation_support import build_instance, create_registered_global, owner_global_context, register_sources
from qorqut_operations.contracts import InvocationOptions, Principal, new_id

ACCOUNT = "a" * 64
OTHER_ACCOUNT = "b" * 64
CHAT = "friend@s.whatsapp.net"
TEXT = "Meet at 5 <pm> & bring the   notes"
MARKER = "WA-PRIVATE-MARKER-9b1e"


@dataclass
class FakeConnector:
    """Mirrors the managed connector's contract: reserve-before-send by request ID, receipt-only lookup, fixed errors."""

    account_ref: str = ACCOUNT
    connected: bool = True
    chats: dict = field(default_factory=lambda: {CHAT: "Synthetic Friend"})
    receipts: dict = field(default_factory=dict)
    sends: list = field(default_factory=list)
    lookups: list = field(default_factory=list)
    mode: str = "accept"            # accept | lose_reply | never_arrives | unknown_state | down
    echo_state: str | None = None   # a later own-device echo turns an unknown receipt into this state

    def account(self):
        if self.mode == "down":
            from qorqut_operations.contracts import DomainError
            raise DomainError("operation_unavailable", "down")
        return {"account_ref": self.account_ref, "connected": self.connected}

    def chat(self, chat_id):
        return {"known": chat_id in self.chats, "name": self.chats.get(chat_id, "")}

    def _receipt(self, request_id, chat_id, text, state):
        return {"request_id": request_id, "chat_id": chat_id, "message_id": "msg-" + request_id[-8:], "state": state,
                "sent_at": 200 if state == "accepted" else 0, "indexed": state == "accepted",
                "account_ref": self.account_ref, "fingerprint": payload_fingerprint(chat_id, text)}

    def send(self, request_id, chat_id, text, account_ref):
        self.sends.append((request_id, chat_id, text, account_ref))
        if account_ref != self.account_ref:
            from qorqut_operations.contracts import DomainError
            raise DomainError("forbidden", "mismatch")
        if request_id in self.receipts:
            return self.receipts[request_id]          # a recorded request is never sent again
        if self.mode == "never_arrives":
            raise UnknownOutcome()
        state = "unknown" if self.mode in {"lose_reply", "unknown_state"} else "accepted"
        self.receipts[request_id] = self._receipt(request_id, chat_id, text, state)
        if self.mode == "lose_reply":
            raise UnknownOutcome()
        return self.receipts[request_id]

    def receipt(self, request_id, account_ref):
        self.lookups.append(request_id)
        if account_ref != self.account_ref:
            from qorqut_operations.contracts import DomainError
            raise DomainError("forbidden", "mismatch")
        found = self.receipts.get(request_id)
        if found and found["state"] == "unknown" and self.echo_state:
            found = self.receipts[request_id] = {**found, "state": self.echo_state, "sent_at": 201}
        return found


class Setup:
    def __init__(self, tmp_path, *, connector=None, workers=True):
        self.host, ids = build_instance(tmp_path)
        self.business = ids["first"]
        register_sources(self.host, ids["first"], "first")
        self.runtime = self.host.runtime
        self.agent = create_registered_global(self.host, "ordubashy")
        self.connector = connector or FakeConnector()
        self.operations = self.runtime.application.whatsapp_send
        self.operations.client_factory = lambda settings: self.connector
        attach_background(self.runtime, self.runtime.programs)
        self.runtime.freeze_registry()
        if workers:      # the explicit deployment step; without it the program has no trusted worker
            with self.host.database.transaction() as uow:
                self.runtime.authority.register_workers(uow, [ids["first"]])
        self.bind(self.agent)
        self.grant("signoff")

    # -- installation and policy ------------------------------------------------------------------------------------

    def bind(self, agent, account_ref=ACCOUNT, *, version=2):
        instance = self.host.paths.instance_root
        binary = instance / "runtime/connectors" / installation.NAME / ("c" * 40) / installation.NAME
        binding = {"version": version, "agent_id": agent, "binary": str(binary), "sha256": "d" * 64,
                   "socket": str(installation.socket_path(instance))}
        if version == 2:
            binding["managed_send"] = {"socket": str(installation.send_socket_path(instance)),
                                       "secret_file": str(installation.send_secret_path(instance)), "account_ref": account_ref}
        target = instance / "config" / (installation.NAME + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(binding))
        os.chmod(target, 0o600)

    def grant(self, mode, agent=None, *, tool="whatsapp_send"):
        agent = agent or self.agent
        with self.host.database.transaction() as uow:
            row = uow.repository("agent_profiles", business_id=None).get(agent, required=True)
            profile = row["profile_json"]
            overrides = profile.setdefault("tool_policy", {}).setdefault("overrides", {})
            if mode is None:
                overrides.pop(tool, None)
            else:
                overrides[tool] = mode
            profile["profile_revision"] += 1
            uow.repository("agent_profiles", business_id=None).update(agent, {"profile_json": profile}, expected_revision=row["revision"])

    def capability(self, name, agent=None):
        agent = agent or self.agent
        with self.host.database.transaction() as uow:
            row = uow.repository("agent_profiles", business_id=None).get(agent, required=True)
            profile = row["profile_json"]
            held = set(profile["scope"].get("global_capabilities", ()))
            profile["scope"]["global_capabilities"] = sorted(held | {name})
            profile["profile_revision"] += 1
            uow.repository("agent_profiles", business_id=None).update(agent, {"profile_json": profile}, expected_revision=row["revision"])

    # -- callers --------------------------------------------------------------------------------------------------------

    def agent_context(self, agent=None):
        return self.runtime.resolver.invocation_context(Principal(agent or self.agent, "agent")).select_business(None)

    def owner(self):
        return owner_global_context(self.host)

    def propose(self, *, chat=CHAT, text=TEXT, context=None, business_id=None):
        return self.runtime.application.invoke(context or self.agent_context(), "whatsapp_send", {"chat_id": chat, "text": text},
                                               InvocationOptions(idempotency_key=new_id(), business_id=business_id))

    def detail(self, signoff_id, context=None):
        return self.runtime.signoffs.get(context or self.owner(), signoff_id).data["signoff"]

    def approve(self, signoff_id):
        detail = self.detail(signoff_id)
        return self.runtime.signoffs.review(self.owner(), signoff_id, decision="approve",
            proposal_revision=detail["proposal_revision"], payload_hash=detail["payload_hash"],
            expected_status_revision=detail["status_revision"], idempotency_key=new_id())

    def reject(self, signoff_id, feedback="No."):
        detail = self.detail(signoff_id)
        return self.runtime.signoffs.review(self.owner(), signoff_id, decision="reject", feedback=feedback,
            proposal_revision=detail["proposal_revision"], payload_hash=detail["payload_hash"],
            expected_status_revision=detail["status_revision"], idempotency_key=new_id())

    def approved_job(self, **kwargs):
        """Propose, approve and apply; the queued job's ID (nothing is sent yet)."""
        staged = self.propose(**kwargs)
        assert staged.status == "signoff_required", staged.to_dict()
        assert self.approve(staged.signoff["id"]).status == "accepted"
        self.runtime.signoffs.run_one(staged.signoff["id"])
        return staged.signoff["id"], self.detail(staged.signoff["id"])["apply_result"]["data"]["job_id"]

    def job(self, job_id):
        with self.host.database.read() as uow:
            return uow.repository("jobs", business_id=None).get(job_id, required=True)

    def forge_job(self, arguments, *, marker=False):
        """A queued whatsapp.send job written without any approved Signoff (what a compromised caller would try)."""
        extra = {"whatsapp_send": {"operation": "whatsapp_send", "agent_id": self.agent}} if marker else None
        with self.host.database.transaction() as uow:
            values = self.runtime.programs.job_values(self.owner(), "whatsapp.send", arguments, uow, definition_extra=extra)
            uow.repository("jobs", business_id=None).insert({**values, "initiated_by_principal_id": self.agent})
        return values["id"]

    def other_agent(self, name):
        from host_foundation_support import create_worker
        return create_worker(self.host, name)

    def job_count(self):
        with self.host.database.read() as uow:
            return uow.connection.execute("SELECT count(*) FROM jobs WHERE kind='whatsapp.send'").fetchone()[0]

    def close(self):
        self.runtime.release_instance()
        self.host.database.close()
