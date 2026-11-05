"""Synthetic schema-91 installation with local role trios for single-agent conversion tests."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from bazarlyk.catalog.packages import content_hash, files_digest
from qorqut_operations.contracts import canonical_bytes
from qorqut_operations.documents.serialization import document_bytes
from bazarlyk.domains.working_memory import memory_id
from bazarlyk.owner_sessions import METADATA_VERSION, PURPOSE
from bazarlyk.storage import Database

T = "2026-09-20T10:00:00.000000Z"
ROLE_VERSIONS = {"onbashy": "2.26.1", "strategist": "2.7.2", "marketing": "2.23.1", "coder": "1.0.0",
                 "founder": "1.10.0", "ordubashy": "1.32.0", "learning-applier": "1.0.0",
                 "outreach-specialist": "1.4.0", "business-agent": "1.0.0", "global-marketing": "1.0.0"}
EMAIL = {"id": "email-management", "version": "2.0.0", "digest": "sha256:" + "e" * 64, "config": {"mailboxes": []}}
TARGET_ROLES = ("business-agent", "global-marketing")


def uid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "single-agent-fixture:" + label))


def J(value) -> str:
    return canonical_bytes(value).decode()


class Installation:
    """Businesses alpha (full trio + coder), beta (no Marketing) and archived gamma, plus Global agents."""

    def __init__(self, tmp_path: Path, *, variants=()):
        self.tmp, self.variants = tmp_path, set(variants)
        self.root = tmp_path / "businesses"
        self.state = tmp_path / "state"
        self.root.mkdir()
        (self.root / ".bazarlyk").mkdir(mode=0o700)
        self.db = self.root / ".bazarlyk" / "state.sqlite3"
        Database(self.db).initialize(through=92)
        self.conn = sqlite3.connect(self.db)
        self.conn.create_function("inbox_actor", 0, lambda: None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.agents, self.pins, self.docs = {}, {}, {}
        self.build()
        self.conn.execute("DELETE FROM inbox_changes")
        self.conn.commit()
        self.conn.close()

    # -- helpers -----------------------------------------------------------------------------------
    def insert(self, table, **values):
        self.conn.execute(f'INSERT INTO "{table}" ({",".join(values)}) VALUES ({",".join("?" * len(values))})',
                          tuple(values.values()))

    def write(self, path: Path, data: bytes, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def hq(self, slug) -> Path:
        return self.root / slug / "hq"

    def publish(self, kind, identity, version):
        stored = Path(".bazarlyk/artifacts") / kind / identity / version
        files = {f"{kind}.yaml": f"id: {identity}\nversion: {version}\n".encode()}
        for name, data in files.items():
            self.write(self.root / stored / name, data)
        digest = files_digest(files)
        self.insert("artifacts", kind=kind, artifact_id=identity, version=version, digest=digest,
                    stored_path=stored.as_posix(), created_at=T)
        return {"id": identity, "version": version, "digest": digest}

    def principal(self, label, kind, display):
        identity = uid(label)
        self.insert("principals", id=identity, kind=kind, display_name=display, created_at=T, updated_at=T)
        return identity

    def agent(self, label, role, *, business=None, name=None, display=None, status="active", scope=None,
              registered=True, plugins=None, runtime="claude-code"):
        name = name or role
        identity = self.principal(label, "agent", display or name.title())
        base = self.hq(business) if business else self.root
        workspace = base / "agents" / name
        data = {"schema_version": 2, "agent_id": identity, "profile_revision": 3, "name": name,
                "display_name": display or name.title(), "status": "inactive" if status != "active" else "active",
                "runtime": runtime, "role": self.pins[role],
                "scope": scope or {"kind": "business", "business_id": self.business_ids[business]},
                "assignment_brief": f"{role} work", "plugins": plugins if plugins is not None else [EMAIL],
                "tool_policy": {"overrides": {}}, "presentation": {}, "created_at": T}
        namespace = self.business_ids[business] if business else "ecosystem"
        self.insert("agent_profiles", agent_id=identity, principal_id=identity,
                    home_business_id=self.business_ids[business] if business else None, namespace=namespace, name=name,
                    workspace=str(workspace), status=status, profile_json=J(data), revision=3, created_at=T, updated_at=T)
        if business:
            self.insert("business_memberships", business_id=self.business_ids[business], principal_id=identity,
                        created_at=T, updated_at=T)
        workspace.mkdir(parents=True, exist_ok=True)
        self.write(workspace / "work" / "scratch.txt", f"scratch for {label}\n".encode())
        if registered:
            files = {"CLAUDE.md": f"# {name}\nRole {role}\n".encode(), ".mcp.json": b'{"mcpServers":{}}\n',
                     ".bazarlyk/registration.json": J({"agent_id": identity, "capture_transcripts": True}).encode()}
            for relative, data_ in files.items():
                self.write(workspace / relative, data_, 0o600 if relative.startswith(".bazarlyk") else 0o644)
            token = "token-" + hashlib.sha256(label.encode()).hexdigest()
            self.write(workspace / ".bazarlyk" / "credential", (token + "\n").encode(), 0o600)
            credential, registration = uid(label + ":credential"), uid(label + ":registration")
            self.insert("credentials", id=credential, principal_id=identity,
                        token_hash=hashlib.sha256(token.encode()).hexdigest(),
                        metadata_json=J({"registration_id": registration}), created_at=T, updated_at=T)
            self.insert("machine_registrations", id=registration, principal_id=identity, workspace=str(workspace),
                        service_url="http://127.0.0.1:1071", credential_id=credential, profile_revision=3,
                        projection_json=J({"files": {k: content_hash(v) for k, v in files.items()}}),
                        created_at=T, updated_at=T)
        self.agents[label] = identity
        return identity

    def document(self, key, *, business, owner, kind, path, body, metadata=None, archived=0):
        identity = uid("doc:" + key)
        data = document_bytes(metadata or {"title": key}, body)
        base = self.hq(business) if business else self.root
        self.write(base / path, data)
        bid = self.business_ids[business] if business else None
        self.insert("documents", id=identity, business_id=bid, namespace=bid or "global", owning_agent_id=owner,
                    kind=kind, path=path, content_hash=hashlib.sha256(data).hexdigest(),
                    metadata_json=J(metadata or {"title": key}), archived=archived, created_at=T, updated_at=T)
        self.docs[key] = identity
        return identity, data

    def memory(self, key, *, business, agent, body, versions=1):
        identity = memory_id(agent, self.business_ids[business])
        path = f"context/agent-memory/{agent}.md"
        metadata = {"title": "Working memory", "schema_version": 1}
        payloads = [document_bytes(metadata, f"{body} (v{version})") for version in range(1, versions + 1)]
        self.write(self.hq(business) / path, payloads[-1])
        self.insert("documents", id=identity, business_id=self.business_ids[business], namespace=self.business_ids[business],
                    owning_agent_id=agent, kind="working_memory", path=path,
                    content_hash=hashlib.sha256(payloads[-1]).hexdigest(), metadata_json=J(metadata), revision=versions,
                    created_at=T, updated_at=T)
        for version, data in enumerate(payloads, 1):
            self.insert("working_memory_versions", document_id=identity, version=version,
                        business_id=self.business_ids[business], file_bytes=base64.b64encode(data).decode(),
                        content_hash=hashlib.sha256(data).hexdigest(), source_refs_json="[]", author_id=agent,
                        created_at=T)
        self.docs[key] = identity
        return identity

    def event(self, number, *, business, source_type, source_id, visibility=None):
        self.insert("inbox_events", id=number, business_id=self.business_ids.get(business), source_type=source_type,
                    source_id=source_id, event_type="created", summary=f"event {number}",
                    visibility_json=J(visibility or {}), created_at=T)
        return number

    def deliver(self, agent, event, *, acknowledged=False):
        self.insert("inbox_deliveries", agent_id=self.agents[agent], event_id=event,
                    acknowledged_at=T if acknowledged else None, outcome="done" if acknowledged else None)

    def message(self, key, event, sender, recipient, *, reply_to=None, thread=None, business="alpha"):
        identity = uid("message:" + key)
        self.insert("agent_messages", id=identity, event_id=event, business_id=self.business_ids[business],
                    sender_id=self.agents[sender], recipient_id=self.agents[recipient],
                    reply_to=uid("message:" + reply_to) if reply_to else None,
                    thread_id=uid("message:" + thread) if thread else identity, body=f"body {key}", created_at=T)
        return identity

    def task(self, key, business, status, *, assignees=()):
        identity = uid("task:" + key)
        self.insert("tasks", business_id=self.business_ids[business], id=identity, title=f"Task {key}", status=status,
                    created_by=self.owner, created_at=T, updated_at=T)
        for index, (agent, kind) in enumerate(assignees):
            self.edge(f"{key}:{index}", business, "task", identity, "assigned_to", kind,
                      self.owner if agent == "owner" else self.agents[agent])
        return identity

    def edge(self, key, business, from_type, from_id, role, to_type, to_id):
        self.insert("relationships", business_id=self.business_ids[business], id=uid("edge:" + key), from_type=from_type,
                    from_id=from_id, to_type=to_type, to_id=to_id, role=role, created_at=T, updated_at=T,
                    created_by=self.owner)

    # -- installation --------------------------------------------------------------------------------
    def build(self):
        for role, version in ROLE_VERSIONS.items():
            if "unpublished_targets" in self.variants and role in TARGET_ROLES:
                continue
            self.pins[role] = self.publish("role", role, version)
        self.owner = self.principal("owner", "human", "Owner")
        self.system = self.principal("worker", "system", "Worker")
        self.business_ids = {}
        for slug in ("alpha", "beta", "gamma"):
            identity = uid("business:" + slug)
            self.business_ids[slug] = identity
            hq = self.hq(slug)
            (hq / "context").mkdir(parents=True)
            self.write(hq / "business.yaml", f"name: {slug}\n".encode())
            config = {}
            if slug == "alpha":
                config = {"mailbox_grant_delegations": [{"coordinator_roles": ["onbashy"],
                          "recipient_roles": ["marketing", "strategist"],
                          "plugin": {**EMAIL, "config": {"mailboxes": ["hello@alpha.test"]}}, "expires_at": "2027-01-01T00:00:00Z"}]}
            self.insert("businesses", id=identity, slug=slug, workspace=str(self.root / slug), hq_path=str(hq), name=slug.title(),
                        config_json=J(config), created_at=T, updated_at=T,
                        archived_at=T if slug == "gamma" else None, archived_by=self.owner if slug == "gamma" else None)
            self.insert("business_memberships", business_id=identity, principal_id=self.owner, is_owner=1, created_at=T, updated_at=T)
        alpha, beta = self.business_ids["alpha"], self.business_ids["beta"]
        eco = lambda ids, caps=(): {"kind": "ecosystem", "allowed_business_ids": sorted(ids), "global_capabilities": list(caps)}
        self.agent("founder", "founder", display="Founder", scope=eco([alpha]), plugins=[])
        self.agent("ordubashy", "ordubashy", display="Ordubashy", scope=eco([alpha, beta]), plugins=[], runtime="codex")
        self.agent("learning", "learning-applier", display="Learning Applier", scope=eco([]), plugins=[])
        partial = "specialist_partial" in self.variants
        self.agent("outreach", "outreach-specialist", display="Outreach", scope=eco([alpha] if partial else [alpha, beta]), plugins=[])
        for label in ("founder", "ordubashy", "outreach"):
            self.insert("business_memberships", business_id=alpha, principal_id=self.agents[label], created_at=T, updated_at=T)
        for label in ("ordubashy",) if partial else ("ordubashy", "outreach"):
            self.insert("business_memberships", business_id=beta, principal_id=self.agents[label], created_at=T, updated_at=T)
        self.insert("installation", id="service", installation_id=uid("installation"), owner_principal_id=self.owner,
                    management_coordinator_id=self.agents["ordubashy"], created_at=T, updated_at=T)
        self.insert("maintenance_state", id="service", state="open", created_at=T, updated_at=T)
        integration = self.principal("outreach-integration", "integration", "Outreach")
        self.insert("integration_principals", id=uid("integration"), slug="outreach", display_name="Outreach",
                    principal_id=integration, designated_specialist_id=self.agents["outreach"], created_at=T, updated_at=T)
        # Local trios: alpha full plus a legacy coder, beta without Marketing, archived gamma.
        marketing_plugins = [EMAIL, {"id": "cold-outbound-marketing", "version": "1.2.0", "digest": "sha256:" + "c" * 64, "config": {}}]
        self.agent("a.onbashy", "onbashy", business="alpha", display="Onbashy")
        self.agent("a.strategist", "strategist", business="alpha", display="Strategist")
        self.agent("a.marketing", "marketing", business="alpha", name="marketer", display="Marketing", plugins=marketing_plugins)
        self.agent("a.coder", "coder", business="alpha", status="inactive", registered=False, display="Coder")
        self.agent("b.onbashy", "onbashy", business="beta", display="Onbashy",
                   registered="unprepared_onbashy" not in self.variants)
        self.agent("b.strategist", "strategist", business="beta", display="Strategist")
        if "beta_missing_onbashy" in self.variants:
            self.conn.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (self.agents["b.onbashy"],))
        self.agent("g.onbashy", "onbashy", business="gamma", display="Onbashy")
        self.agent("g.strategist", "strategist", business="gamma", display="Strategist")
        self.agent("g.marketing", "marketing", business="gamma", display="Marketing", registered=False)
        a = self.agents
        # Owner-session credential of the human owner must survive.
        self.insert("credentials", id=uid("owner-session"), principal_id=self.owner,
                    token_hash=hashlib.sha256(b"owner-session-token").hexdigest(),
                    metadata_json=J({"purpose": PURPOSE, "version": METADATA_VERSION, "business_id": alpha, "label": "code"}),
                    created_at=T, updated_at=T)
        # Documents: duplicate leaf names, promotions, kept private notes and a Global note tagged with alpha.
        self.document("onbashy-plan", business="alpha", owner=a["a.onbashy"], kind="agent_note",
                      path="agents/onbashy/notes/2026-01-01-weekly-plan.md", body="Onbashy weekly plan\n")
        self.document("strategist-plan", business="alpha", owner=a["a.strategist"], kind="agent_note",
                      path="agents/strategist/notes/2026-01-01-weekly-plan.md", body="Strategist weekly plan\n")
        self.document("positioning", business="alpha", owner=a["a.strategist"], kind="agent_note",
                      path="agents/strategist/notes/2026-02-01-positioning.md", body="Positioning decision draft\n",
                      metadata={"title": "Positioning", "created": "2026-02-01T00:00:00Z"})
        self.document("campaign", business="alpha", owner=a["a.marketing"], kind="agent_note",
                      path="agents/marketer/notes/2026-02-02-campaign-ideas.md", body="Campaign ideas\n",
                      metadata={"title": "Campaign ideas", "date": "2026-02-02"})
        self.document("private", business="alpha", owner=a["a.marketing"], kind="agent_note",
                      path="agents/marketer/notes/2026-02-03-private.md", body="Private marketing note\n")
        self.document("beta-note", business="beta", owner=a["b.strategist"], kind="agent_note",
                      path="agents/strategist/notes/2026-03-01-beta.md", body="Beta strategy\n")
        self.document("global-note", business=None, owner=a["founder"], kind="agent_note",
                      path="agents/founder/notes/2026-03-02-alpha-thoughts.md", body="Founder thoughts on alpha\n",
                      metadata={"title": "Alpha thoughts", "related_business_id": alpha})
        self.memory("wm-onbashy", business="alpha", agent=a["a.onbashy"], body="Onbashy handoff", versions=2)
        self.memory("wm-strategist", business="alpha", agent=a["a.strategist"], body="Strategist handoff differs")
        self.memory("wm-marketing", business="alpha", agent=a["a.marketing"], body="Marketing handoff differs")
        # Work: open strategy, marketing and coder items, a closed item, a campaign Story.
        self.task("t1", "alpha", "open", assignees=[("a.strategist", "agent")])
        self.task("t2", "alpha", "doing", assignees=[("a.marketing", "agent"), ("a.onbashy", "agent")])
        self.task("t3", "alpha", "done", assignees=[("a.strategist", "agent")])
        self.task("t4", "alpha", "open", assignees=[("a.coder", "agent")])
        story = uid("story:s1")
        self.insert("stories", business_id=alpha, id=story, title="Campaign", purpose="Launch", state="working",
                    created_by=self.owner, created_at=T, updated_at=T)
        self.edge("s1:responsible", "alpha", "story", story, "campaign_responsible", "agent", a["a.marketing"])
        self.edge("s1:participant", "alpha", "agent", a["a.strategist"], "participates_in", "story", story)
        # Conversations: a reply chain with Ordubashy and one local-to-local exchange.
        self.event(1, business="alpha", source_type="agent_messages", source_id=uid("message:m1"))
        self.event(2, business="alpha", source_type="agent_messages", source_id=uid("message:m2"))
        self.event(3, business="alpha", source_type="agent_messages", source_id=uid("message:m3"))
        self.message("m1", 1, "ordubashy", "a.strategist")
        self.message("m2", 2, "a.strategist", "ordubashy", reply_to="m1", thread="m1")
        self.message("m3", 3, "a.onbashy", "a.marketing")
        self.deliver("a.strategist", 1)
        self.deliver("ordubashy", 2, acknowledged=True)
        self.deliver("a.marketing", 3)
        # The same task event unread by all three local agents, plus private and shared note events.
        self.event(4, business="alpha", source_type="tasks", source_id=uid("task:t1"))
        for label in ("a.onbashy", "a.strategist", "a.marketing"):
            self.deliver(label, 4)
        self.event(5, business="alpha", source_type="documents", source_id=self.docs["positioning"],
                   visibility={"kind": "agent_note", "owning_agent_id": a["a.strategist"]})
        self.deliver("a.strategist", 5)
        self.event(6, business="alpha", source_type="documents", source_id=self.docs["private"],
                   visibility={"kind": "agent_note", "owning_agent_id": a["a.marketing"]})
        self.deliver("a.marketing", 6)
        self.event(7, business="alpha", source_type="tasks", source_id=uid("task:t3"))
        self.deliver("a.strategist", 7, acknowledged=True)
        # Subscriptions overlapping on one target.
        for label, related in (("a.strategist", 1), ("a.onbashy", 0)):
            self.insert("inbox_subscriptions", id=uid("sub:" + label), agent_id=a[label], business_id=alpha,
                        source_type="tasks", source_id=uid("task:t1"), include_related=related, created_at=T)
        self.insert("inbox_subscriptions", id=uid("sub:marketing"), agent_id=a["a.marketing"], business_id=alpha,
                    source_type="stories", source_id=story, created_at=T)
        # Scheduled broadcast addressed to the whole trio.
        self.insert("broadcasts", id=uid("broadcast"), business_id=alpha, sender_id=a["ordubashy"], name="Daily",
                    created_by=self.owner, delivery="inbox", message="Daily check",
                    recipients_json=J([{"agent_id": a[x], "business_id": alpha} for x in ("a.onbashy", "a.strategist", "a.marketing")]),
                    timing_json=J({"kind": "daily", "at": "09:00", "timezone": "UTC"}), status="active",
                    occurrence_count=3, next_due_at="2026-09-25T09:00:00Z", created_at=T, updated_at=T)
        self.insert("broadcast_deliveries", broadcast_id=uid("broadcast"), occurrence=1, recipient_id=a["a.strategist"],
                    business_id=alpha, state="delivered", payload_json="{}", created_at=T, finished_at=T)
        # Receipts: the same idempotency key under two trio principals, an applied receipt.
        for label in ("a.strategist", "a.onbashy"):
            self.insert("idempotency_receipts", principal_id=a[label], target_namespace="business:" + alpha,
                        operation_name="tasks_update", idempotency_key="k1", input_hash="h" + label, result_json="{}",
                        created_at=T)
        self.insert("apply_receipts", id=uid("apply"), principal_id=a["a.strategist"], target_namespace="business:" + alpha,
                    operation_name="tasks_update", payload_hash="p", change_set=b"{}", state="applied",
                    created_at=T, updated_at=T)
        # A failed job whose provider effect succeeded: blocks until reconciled.
        self.insert("jobs", id=uid("job:failed"), business_id=alpha, target_namespace="business:" + alpha,
                    kind="platform.email_send", principal_id=self.system, initiated_by_principal_id=a["a.marketing"],
                    arguments_json="{}", dedupe_key="d1", state="failed",
                    result_json=J({"external_receipt": {"provider_id": "prov-1"}}), created_at=T, updated_at=T)
        if "approved_signoff" in self.variants:
            self.insert("signoffs", id=uid("signoff"), review_business_id=alpha, proposer_principal_id=a["a.strategist"],
                        proposer_agent_id=a["a.strategist"], operation_name="tasks_update", operation_version=1,
                        proposal_revision=1, payload_hash="x", change_set=b"{}", preview_json="{}",
                        expected_targets_json="[]", policy_binding_json="{}", affected_scope_json="{}", state="approved",
                        updated_sequence=1, created_at=T, updated_at=T)
        if "delayed_callback" in self.variants:
            self.insert("jobs", id=uid("job:waiting"), business_id=alpha, target_namespace="business:" + alpha,
                        kind="platform.reddit_post", principal_id=self.system, initiated_by_principal_id=a["a.onbashy"],
                        arguments_json="{}", dedupe_key="d2", state="running", lease_until="2026-09-30T00:00:00Z",
                        result_json=J({"dispatch_intent": {"at": T}}), created_at=T, updated_at=T)
        if "archival_profile" in self.variants:
            # A legacy record archived by an earlier migration: status retired and no role.
            legacy = self.principal("a.launcher", "agent", "launcher")
            self.conn.execute("UPDATE principals SET active=0 WHERE id=?", (legacy,))
            self.insert("agent_profiles", agent_id=legacy, principal_id=legacy, home_business_id=alpha, namespace=alpha,
                        name="launcher", workspace=str(self.hq("alpha") / ".bazarlyk/legacy/agents/launcher"), status="retired",
                        profile_json=J({"schema_version": 2, "archival_record": True, "name": "launcher",
                                        "display_name": "launcher", "status": "retired", "reason": "owner-requested"}),
                        revision=2, created_at=T, updated_at=T)
        if "external_worker" in self.variants:
            # Delegated worker scratch registration outside the businesses root, as XDG state creates it.
            worker = self.tmp / "xdg-state" / "bazarlyk" / "delegated-workers" / a["ordubashy"] / uid("assignment")
            self.write(worker / "AGENTS.md", b"# worker\n")
            self.insert("machine_registrations", id=uid("worker:registration"), principal_id=a["ordubashy"],
                        workspace=str(worker), service_url="http://127.0.0.1:1071", credential_id=None, profile_revision=3,
                        projection_json=J({"files": {"AGENTS.md": content_hash(b"# worker\n")}}), created_at=T, updated_at=T)
        if "embedded_ids" in self.variants:
            # Caller-chosen keys and handled-delivery outcomes that mention a retired agent.
            self.insert("idempotency_receipts", principal_id=self.owner, target_namespace="business:" + alpha,
                        operation_name="agents_update", idempotency_key="release:role:marketing:" + a["a.marketing"],
                        input_hash="h", result_json="{}", created_at=T)
            self.insert("inbox_deliveries", agent_id=a["ordubashy"], event_id=4, acknowledged_at=T,
                        outcome="Signoff approved and applied; agent_id " + a["a.marketing"])
        if "reviewed_duplicate" in self.variants:
            # Both t2 assignment edges were reviewed; they collapse onto one agent.
            for index in (0, 1):
                edge, review = uid(f"edge:t2:{index}"), uid(f"review:t2:{index}")
                self.insert("context_reviews", business_id=alpha, id=review, relationship_id=edge, item_type="task",
                            item_id=uid("task:t2"), source_type="agent", source_id=a["a.marketing"], source_token="t",
                            relationship_revision=1, result="no_change", explanation="Reviewed", actor_id=self.owner,
                            prepared_at=T, created_at=T)
                self.insert("context_review_states", business_id=alpha, relationship_id=edge, item_type="task",
                            item_id=uid("task:t2"), review_id=review, created_at=T, updated_at=T)
        if "unclassified" in self.variants:
            self.insert("system_incident_fault_runs", fingerprint="f", agent_key="k", session_id="s", business_id=alpha,
                        operation="op", component="component " + a["a.strategist"], call_site="c", first_seen=T, last_seen=T)
        # Live session state for the trio.
        for label in ("a.onbashy", "a.strategist"):
            self.insert("inbox_watchers", agent_id=a[label], consumer_id=uid("consumer:" + label), last_check_at=T,
                        lease_until="2026-09-30T00:00:00Z")
            self.insert("agent_focus", agent_id=a[label], status="working", updated_at=T)
            self.insert("agent_activity", agent_id=a[label], session_id=uid("session:" + label),
                        registration_id=uid(label + ":registration"), runtime="claude-code", state="idle", last_seen_at=T)


def mutate(install: Installation, sql: str, parameters=()):
    """A raw writer standing in for a concurrent service write."""
    connection = sqlite3.connect(install.db)
    connection.create_function("inbox_actor", 0, lambda: None)
    connection.execute(sql, parameters)
    connection.execute("DELETE FROM inbox_changes")
    connection.commit()
    connection.close()


def decisions(install: Installation, **overrides) -> dict:
    ids = install.business_ids
    value = {
        "target_roles": {"business-agent": "1.0.0", "global-marketing": "1.0.0"},
        "responsibility_defaults": {"strategist": "founder", "marketing": "canonical", "coder": "owner"},
        "document_defaults": {"strategist": "transfer", "marketing": "keep", "coder": "keep"},
        "documents": {install.docs["positioning"]: {"action": "promote", "category": "strategy-notes"},
                      install.docs["campaign"]: {"action": "promote", "category": "marketing-notes"}},
        "working_memory": {ids["alpha"]: {"body": "Objective: launch alpha.\nNext: finish T2.\n", "sources": []},
                           ids["beta"]: {"body": "Objective: beta research.\n", "sources": []}},
        "reconciled_effects": {"jobs:" + uid("job:failed"): {"outcome": "delivered", "evidence": "provider shows prov-1 sent"}},
        "collapsed_pending_messages": "acknowledge",
        "release": {"core": "test", "roles": "test"},
    }
    value.update(overrides)
    return value


def write_decisions(install: Installation, value: dict) -> Path:
    path = install.tmp / "decisions.json"
    path.write_text(json.dumps(value))
    return path


def release_workspace(install: Installation, *, versions=None) -> Path:
    """An engine workspace whose package source map holds minimal target role packages, one with a skill."""
    workspace, sources = install.tmp / "release", {}
    for role, scope in (("business-agent", "business"), ("global-marketing", "ecosystem")):
        base = workspace / "agent-roles"
        files = {f"roles/{role}/BRIEF.md": f"# {role}\nWork across the {scope}.\n",
                 f"roles/{role}/role.yaml": (
                     f"schema_version: 1\nid: {role}\nversion: {(versions or ROLE_VERSIONS)[role]}\n"
                     f"scope_kinds:\n- {scope}\nbrief: roles/{role}/BRIEF.md\n"
                     f"scope_brief: {'business-agent' if scope == 'business' else 'ecosystem-agent'}\n"
                     f"skills:\n- id: {role}-triage\n  path: skills/{role}-triage/SKILL.md\n"
                     "default_tool_policy:\n  tasks_list: allow\n"),
                 f"skills/{role}-triage/SKILL.md": (f"---\nname: bazarlyk-role-{role}-triage\n"
                                                    "description: Triage the queue.\n---\nRead the inbox first.\n")}
        for relative, text in files.items():
            install.write(base / relative, text.encode())
        sources[role] = {"kind": "role", "path": "agent-roles", "manifest": f"roles/{role}/role.yaml"}
    install.write(workspace / "biz-core" / "package-sources.json", json.dumps(sources).encode())
    return workspace


def barrier(install: Installation, manifest_digest: str) -> Path:
    path = install.tmp / "barrier.json"
    path.write_text(json.dumps({"schema_version": 1, "manifest_digest": manifest_digest, "attested_by": "owner",
                                "stopped_units": [{"name": "qorqut-dashboard.service", "scope": "system"}]}))
    return path


def stopped(unit):
    return {"active_state": "inactive", "main_pid": 0}
